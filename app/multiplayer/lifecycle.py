"""Session lifecycle: start, launch coordination, health, termination, recovery.

Launch ordering (native mechanisms): the host applies its session-scoped
configuration, exits the current game only when its user approved the
replacement, launches, waits until the emulator is really up and listening, and
only then asks each guest -- in player-slot order -- to launch. A guest never
launches on a peer's say-so: the request creates a pending offer that this
machine's own user confirms (or, only if they opted in, auto-confirms when no
other game is running). Games running locally are never closed on a remote
request.
"""

from datetime import timedelta
from typing import Any, Dict, Optional

from . import models as m
from .adapters.base import MANUAL
from .core import EVENT_SESSION
from .sessions import launch_pending, occupied, settle_live, settle_lobby
from .store import add_event

try:
    from ..integrations.streamdeck.game_runtime import same_rom
except ImportError:  # pragma: no cover - flat execution
    from integrations.streamdeck.game_runtime import same_rom  # type: ignore

LAUNCH_DEADLINE = timedelta(seconds=120)
HOST_START_TIMEOUT = 45.0
HOST_READY_TIMEOUT = 20.0
GUEST_HOST_WAIT = 30.0
EXIT_TIMEOUT = 15.0
MAX_RETRIES = 3
SILENT_AFTER = timedelta(seconds=60)
HOST_LOST_AFTER = timedelta(seconds=120)
KEEP_COMPLETED = 20
ENDED_REPLICA_TTL = timedelta(hours=1)


class LifecycleMixin:
    # ---------------------------------------------------------------- context
    def launch_context(self, session: dict, role: str, *, port: int = 0, secret: str = "") -> dict:
        local = session.get("local") or {}
        context = self.public_context(session, role)
        # The per-game key must name *this* machine's file: matching content may carry a different file name.
        context.update(game_path=local.get("game_path", ""), rom_name=local.get("rom_name") or session["game"]["rom_name"],
                       system=session["game"]["system"],
                       port=port or local.get("port") or 0, secret=secret or session.get("secret", ""))
        return context

    def active_conflict(self, game_path: str, replace: bool) -> Optional[dict]:
        """Another game is running and the user has not approved replacing it -> describe it.

        The host uses this even when the foreground ROM is the session game: Netplay
        settings are applied by launching again, so that still needs approval.
        """
        active = self.launcher.active_game()
        if not active:
            return None
        return None if replace else active

    def _different_game_running(self, game_path: str) -> Optional[dict]:
        """A foreground game that is not this session's ROM.

        The session ROM already running is participation, not a conflict to kill.
        """
        active = self.launcher.active_game()
        if not active:
            return None
        if game_path and same_rom(active.get("rom_path", ""), game_path):
            return None
        return active

    @staticmethod
    def _active_summary(active: dict) -> dict:
        return {"name": active.get("name", ""), "system": active.get("system", ""), "emulator": active.get("emulator", "")}

    # ------------------------------------------------------------------ start
    def start_session(self, session_id: str, replace_running_game: bool = False, actor: str = "") -> dict:
        self.require_enabled()
        replace = m.clean_bool(replace_running_game, "replace_running_game")
        with self.session_lock(session_id):
            session = self.require_host(session_id)
            if occupied(session) > session["max_players"] or any(
                    p.get("slot") and p["slot"] > session["max_players"]
                    for p in session["players"].values() if p["status"] not in (m.P_LEFT, m.P_REMOVED, m.P_DECLINED)):
                raise m.MultiplayerError("session_full", "The lobby exceeds this mechanism's player capacity. Remove players or reassign slots before starting.", 409)
            if session["state"] != m.READY:
                waiting = [p["name"] for k, p in session["players"].items() if p["status"] == m.P_JOINED and not p["ready"]]
                if session["state"] in m.PRE_START_STATES:
                    detail = f" Waiting for: {', '.join(waiting)}." if waiting else " At least one other player must join."
                    raise m.MultiplayerError("players_not_ready", "Everyone must join and be ready before the session can start." + detail, 409)
                raise m.MultiplayerError("session_started", f"The session is {session['state'].lower()}.", 409)
            adapter = self.adapter_map.get(session["emulator"]["adapter"])
            if adapter is None:
                raise m.MultiplayerError("unknown_adapter", "The session's multiplayer mechanism is not available on this Drone.", 409)
            self._require_adapter_enabled(adapter)
            self._check_host_launch_configuration(session, adapter)
            if session["mechanism"] == m.REMOTE_STREAMING and (not self.cfg.get()["streaming"]["enabled"] or
                    self.cfg.get()["privacy"]["remote_controller_authorization"] == "disabled"):
                raise m.MultiplayerError("streaming_disabled", "Remote streaming or remote controllers are disabled in Multiplayer settings.", 409)
            problems = adapter.validate_configuration(session, m.HOST_ROLE)
            if problems:
                raise m.MultiplayerError("cannot_start", problems[0], 409, {"problems": problems})
            path = session["local"].get("game_path", "")
            if not path or not self._exists(path):
                raise m.MultiplayerError("game_file_missing", "The game's file is no longer on this machine.", 409)
            if session["mechanism"] in (m.RETROARCH_NETPLAY, m.DOLPHIN_NETPLAY):
                game = self.library.get(session["game"]["system"], session["game"]["unique_id"])
                if not game or self.library.content_hash(game) != session["game"].get("sha256"):
                    raise m.MultiplayerError("content_changed", "The selected ROM changed. Create a new session before launching.", 409)
            if adapter.effective_automation() != MANUAL:
                conflict = self.active_conflict(path, replace)
                if conflict:
                    raise m.MultiplayerError("game_running", "Another game is running on this machine. Approve replacing it to continue.", 409,
                                             {"running": self._active_summary(conflict)})

            def begin(s: dict) -> None:
                for key, player in s["players"].items():
                    if player["status"] == m.P_INVITED:
                        player.update(status=m.P_REMOVED, error="The invitation was withdrawn because the session started.")
                s["state"] = m.STARTING
                s["error"] = ""
                add_event(s, "starting", "The session is starting.", self.me)
            session = self.store.mutate(session_id, begin)
            local = session.get("local") or {}
            self.store.set_local(
                session_id, replace_running_game=replace,
                launch_deadline=(m.now() + LAUNCH_DEADLINE).isoformat(),
                launch_wave=int(local.get("launch_wave") or 0) + 1,
                offered_waves={},
                retries=dict(local.get("retries") or {}))
        self.publish(session_id)
        self._log(session, "starting", "session starting", actor)
        self._spawn(lambda: self.run_start(session_id), f"multiplayer-start-{session_id[:8]}")
        return self.session_view(self.store.require(session_id))

    @staticmethod
    def _check_launch_fingerprint(session: dict, fingerprint: str) -> None:
        reviewed = (session.get("local") or {}).get("tested_config_fingerprint")
        if reviewed and fingerprint != reviewed:
            raise m.MultiplayerError(
                "configuration_changed", "The inspected game or emulator settings changed since the lobby was reviewed. "
                "Create a new session before launching.", 409)

    def _check_host_launch_configuration(self, session: dict, adapter: Any) -> None:
        # A lobby can outlive the detection cache. Reinspect before any temporary
        # edits, including when the worker was queued after Start was approved.
        adapter.refresh()
        game = self.library.get(session["game"]["system"], session["game"]["unique_id"])
        if not game:
            raise m.MultiplayerError("game_file_missing", "The game's file is no longer on this machine.", 409)
        assessments = self.assessor.assess(
            game, game_path=self.library.absolute_path(game),
            core_override=(session.get("local") or {}).get("core_override", ""))
        fingerprint = next((a["config_fingerprint"] for a in assessments
                            if a["mechanism"] == session["mechanism"]), "")
        self._check_launch_fingerprint(session, fingerprint)

    @staticmethod
    def _exists(path: str) -> bool:
        import os
        return os.path.exists(path)

    def run_start(self, session_id: str) -> None:
        try:
            with self.session_lock(session_id):
                session = self.store.require(session_id)
                if session["state"] != m.STARTING:
                    return  # cancelled before this worker acquired the session
                adapter = self.adapter_map[session["emulator"]["adapter"]]
                # Settings may have changed while this launch worker was queued.
                self.require_enabled()
                self._require_adapter_enabled(adapter)
                self._check_host_launch_configuration(session, adapter)
                local = session["local"]
                path = local["game_path"]
                if adapter.effective_automation() == MANUAL:
                    self._finish_without_launch(session_id)
                    return
                port = adapter.session_port(self, session)
                context = self.launch_context(session, m.HOST_ROLE, port=port)
                edits = adapter.configure_networking(session, m.HOST_ROLE, context) + adapter.configure_controllers(session, m.HOST_ROLE, context)
                if edits:
                    self.apply_edits(session_id, edits)
                active = self.launcher.active_game()
                if active:
                    if not local.get("replace_running_game"):
                        raise m.MultiplayerError("game_running", "Another game started on this machine before the session could launch.", 409)
                    self.launcher.exit_game()
                    if not self.launcher.wait_for_exit(EXIT_TIMEOUT):
                        raise m.MultiplayerError("game_not_exited", "The running game did not exit in time.", 409)
                # Recorded before the launch call: a launch that starts the game and then fails must still be cleaned up.
                self.store.set_local(session_id, port=port, host_launched=True)
                adapter.launch(session, m.HOST_ROLE, context, self.launcher)
                started = self.launcher.wait_for_start(path, HOST_START_TIMEOUT)
                if not started:
                    raise m.MultiplayerError("host_launch_timeout", "The game did not start on this machine.", 409)
                self.store.set_local(session_id, launched_pid=started.get("pid"))
                self._await_health(adapter, session_id)
                if not adapter.guest_launches:
                    self._finish_without_launch(session_id, host_running=True)
                    return
                self.store.mutate(session_id, lambda s: s["players"][self.me].update(status=m.P_RUNNING))
            self._send_launches(session_id)
            self.evaluate_launch(session_id)
        except Exception as error:  # noqa: BLE001 - any failure must end in a clean FAILED state, not a stuck STARTING one
            self.fail_session(session_id, getattr(error, "message", None) or f"{type(error).__name__}: {error}")

    def _await_health(self, adapter: Any, session_id: str) -> None:
        waited = 0.0
        while waited < HOST_READY_TIMEOUT:
            session = self.store.require(session_id)
            health = adapter.check_session_health(session, {"role": m.HOST_ROLE, "port": session["local"].get("port") or 0})
            if health.get("ok"):
                return
            self._sleep(1.0)
            waited += 1.0
        detail = health.get("detail") or "The host's multiplayer service did not become ready."
        raise m.MultiplayerError("host_not_ready", detail, 409)

    def _send_launches(self, session_id: str) -> None:
        session = self.store.require(session_id)
        guests = sorted((p for k, p in session["players"].items() if k != self.me and p["status"] == m.P_JOINED),
                        key=lambda p: p.get("slot") or 99)
        for player in guests:
            self._send_launch(session_id, player["device_id"])

    def _send_launch(self, session_id: str, peer_id: str) -> bool:
        session = self.store.require(session_id)
        if session["state"] not in (m.STARTING, m.DEGRADED):
            return False
        wave = int((session.get("local") or {}).get("launch_wave") or 0)
        error = ""
        undelivered = False
        try:
            self.network.post_checked(self.paired_peer(peer_id), "/launch", {
                "message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": session_id,
                "port": session["local"].get("port") or 0, "secret": session.get("secret", ""),
                "wave": wave}, timeout=8.0)
        except m.MultiplayerError as failure:
            error = f"Could not reach this player to start the game: {failure.message}"
            undelivered = failure.code == "peer_unreachable"

        def mark(s: dict) -> None:
            player = s["players"].get(peer_id)
            if player is None:
                return
            if undelivered:
                # Transport miss: JOINED/LAUNCHING stay expected until the deadline;
                # a retry of a failed player stays failed so a later retry can still target them.
                player["error"] = error
                return
            if error:
                player.update(status=m.P_FAILED, error=error)
                add_event(s, "launch_failed", f"{player['name']}: {error}", self.me)
                return
            if player["status"] in (m.P_JOINED, m.P_FAILED, m.P_UNREACHABLE):
                player.update(status=m.P_LAUNCHING, error="")
        self.store.mutate(session_id, mark)
        if not undelivered and not error:
            offered = dict((self.store.require(session_id).get("local") or {}).get("offered_waves") or {})
            offered[peer_id] = wave
            self.store.set_local(session_id, offered_waves=offered)
        return not undelivered and not error

    def _finish_without_launch(self, session_id: str, host_running: bool = False) -> None:
        """Mechanisms with no Drone launch (manual steps, streaming guests): everyone follows the listed steps."""
        def done(s: dict) -> None:
            for key, player in s["players"].items():
                if player["status"] in (m.P_JOINED, m.P_LAUNCHING):
                    player["status"] = m.P_RUNNING
            s["state"] = m.RUNNING
            add_event(s, "running", "Session is running. Follow the listed steps on each machine.", self.me)
        self.store.mutate(session_id, done)
        self.store.set_local(session_id, reached_running=True)
        self.publish(session_id)

    # ------------------------------------------------------- launch evaluation
    def _confirmed_launching(self, session: dict) -> set:
        """Launching guests whose own status shows they really run the session game (housekeeping probe)."""
        confirmed = set()
        if not self.guest_launches_game(session):
            return confirmed
        for pid, player in session["players"].items():
            if pid == self.me or player["status"] not in (m.P_JOINED, m.P_LAUNCHING):
                continue
            try:
                body = self.network.get(self.paired_peer(pid), f"/status?session={session['id']}", timeout=4.0)
            except Exception:  # noqa: BLE001 - unverifiable now; the next sweep asks again
                continue
            remote = body.get("session") if isinstance(body, dict) else None
            # Foreground ROM alone is not a Drone launch. Only a guest that actually
            # confirmed and launched this wave can be promoted from a housekeeping probe.
            if (isinstance(remote, dict) and remote.get("known")
                    and body.get("session_game_running") is True
                    and remote.get("launch_status") == "running"):
                confirmed.add(pid)
        return confirmed

    def evaluate_launch(self, session_id: str, probe: bool = False) -> None:
        confirmed: set = set()
        if probe:
            current = self.store.get(session_id)
            if current and self.local_role(current) == m.HOST_ROLE and current["state"] in (m.STARTING, m.DEGRADED):
                confirmed = self._confirmed_launching(current)
        with self.session_lock(session_id):
            session = self.store.get(session_id)
            if not session or self.local_role(session) != m.HOST_ROLE or session["state"] not in (m.STARTING, m.DEGRADED):
                return
            deadline = m.parse_time(session["local"].get("launch_deadline"))
            overdue = bool(deadline and deadline < m.now())

            def evaluate(s: dict) -> None:
                for pid in confirmed:
                    if s["players"].get(pid, {}).get("status") in (m.P_JOINED, m.P_LAUNCHING):
                        s["players"][pid].update(status=m.P_RUNNING, error="")
                if overdue:
                    closed = False
                    for player in list(s["players"].values()):
                        if player["device_id"] == s["host_id"] or not launch_pending(s, player):
                            continue
                        player.update(status=m.P_FAILED, error="Did not confirm the launch in time.")
                        add_event(s, "launch_failed", f"{player['name']} did not confirm the launch in time.", self.me)
                        closed = True
                    if closed:
                        s.setdefault("local", {})["launch_wave"] = int((s.get("local") or {}).get("launch_wave") or 0) + 1
                settle_live(s)
            updated = self.store.mutate(session_id, evaluate)
            if updated["state"] == m.RUNNING or updated["state"] == m.DEGRADED:
                self.store.set_local(session_id, reached_running=True)
        updated = self.store.require(session_id)
        if updated["state"] == m.FAILED:
            self.fail_session(session_id, updated.get("error") or "The session failed to start.")
        else:
            self.publish(session_id)

    def fail_session(self, session_id: str, message: str) -> None:
        """Mark the session FAILED, roll back configuration, stop what we started, tell the guests."""
        with self.session_lock(session_id):
            session = self.store.get(session_id)
            if session is None:
                return
            # Even if the session was ended while the start was still in flight, nothing it set up may survive.
            self.cleanup_host_resources(session, stop_game=True)
            if session["state"] in (m.COMPLETED, m.STOPPING):
                return

            def apply(s: dict) -> None:
                s["state"] = m.FAILED
                s["error"] = m.clean_text(message, 400)
                s.pop("secret", None)
                add_event(s, "failed", s["error"], self.me)
            if session["state"] != m.FAILED or not session.get("error"):
                self.store.mutate(session_id, apply)
            else:
                self.store.mutate(session_id, lambda s: s.pop("secret", None))
        failed = self.store.require(session_id)
        self._record_failure(failed, message)
        self.publish(session_id)
        self._log(failed, "failed", f"session failed: {message}")

    def _record_failure(self, session: dict, message: str) -> None:
        try:
            emulator = session["emulator"]
            self.compat.record_session_result(
                system=session["game"]["system"], title_key=m.normalize_title(session["game"]["rom_name"]), title=session["game"]["title"],
                mechanism=session["mechanism"], config_fp=(session.get("local") or {}).get("tested_config_fingerprint") or "unknown",
                emulator_version=emulator.get("version", ""), success=False, failure=message, gameplay_evidence=False)
        except Exception:  # noqa: BLE001 - diagnostics must not mask the real failure
            pass

    def _owns_host_game(self, session: dict) -> bool:
        local = session.get("local") or {}
        active = self.launcher.active_game()
        if not active or not same_rom(active.get("rom_path", ""), local.get("game_path", "")):
            return False
        pid = local.get("launched_pid")
        return pid is None or active.get("pid") == pid

    def cleanup_host_resources(self, session: dict, *, stop_game: bool) -> dict:
        outcome: Dict[str, Any] = {"errors": []}
        def attempt(label, operation):
            try:
                return operation()
            except Exception as error:  # noqa: BLE001 - each resource must get its own cleanup attempt
                outcome["errors"].append(f"{label}: {getattr(error, 'message', None) or error}")
                return None

        adapter = self.adapter_map.get(session.get("emulator", {}).get("adapter", ""))
        local = session.get("local") or {}
        context = {"role": m.HOST_ROLE, "port": local.get("port") or 0}
        if adapter is not None:
            for label, hook in (("Adapter shutdown", adapter.stop_session), ("Adapter cleanup", adapter.cleanup)):
                attempt(label, lambda hook=hook: hook(session, context))
        def stop_owned_game():
            if stop_game and local.get("host_launched") and self._owns_host_game(session):
                self.launcher.exit_game()
                outcome["game_stopped"] = True
        attempt("Game shutdown (close the game locally if it remains running)", stop_owned_game)
        attempt("Relay cleanup", lambda: self.network.relay.close_session(session["id"]))
        outcome["config"] = attempt("Configuration restoration", lambda: self.restore_if_enabled(session["id"])) or {}
        if outcome["errors"]:
            self.store.set_local(session["id"], cleanup_errors=outcome["errors"])
            self.store.mutate(session["id"], lambda s: add_event(
                s, "cleanup_failed", "Cleanup needs attention: " + "; ".join(outcome["errors"]), self.me))
        return outcome

    def _require_adapter_enabled(self, adapter: Any) -> None:
        if adapter.id in self.cfg.get()["emulators"]["disabled_adapters"]:
            raise m.MultiplayerError("adapter_disabled", f"{adapter.name} is disabled in Multiplayer settings.", 409)

    # ------------------------------------------------------------ guest launch
    def _require_live_launch_host(self, session: dict, plan: dict) -> None:
        """A persisted offer is consent to join a live host, not offline launch authority.

        Recheck through the pinned peer channel both at confirmation and in the
        queued worker. An unavailable host leaves an unconsumed offer retryable;
        a closed/lobby session or an expired player start attempt voids it.
        Never infer launch permission from an open game port or a live session alone.
        """
        try:
            body = self.network.get(self.host_peer(session), f"/status?session={session['id']}", timeout=4.0)
        except Exception as error:
            raise m.MultiplayerError("host_unreachable", "Could not confirm that the host is still starting this session. Try again when it is reachable.", 409) from error
        remote = body.get("session") if isinstance(body, dict) else None
        if (not isinstance(remote, dict) or remote.get("known") is not True or
                remote.get("state") not in m.LIVE_STATES or
                remote.get("player_status") not in (m.P_JOINED, m.P_LAUNCHING) or
                remote.get("launch_allowed") is not True or
                not isinstance(remote.get("version"), int) or remote["version"] < plan.get("version", 0)):
            self.store.set_local(session["id"], pending_launch=None, launch_status="expired")
            raise m.MultiplayerError("launch_expired", "The host is no longer starting this session for you. Ask it for a new launch offer.", 409)

    def confirm_launch(self, session_id: str, replace_running_game: bool = False) -> dict:
        self.require_enabled()
        replace = m.clean_bool(replace_running_game, "replace_running_game")
        with self.session_lock(session_id):
            session = self.store.require(session_id)
            if self.local_role(session) != m.GUEST_ROLE:
                raise m.MultiplayerError("not_a_guest", "The host launches from Start Session.", 409)
            plan = (session.get("local") or {}).get("pending_launch")
            if not plan or plan.get("consumed"):
                raise m.MultiplayerError("nothing_to_launch", "There is no launch waiting for you.", 409)
            expiry = m.parse_time(plan.get("expires_at"))
            if expiry and expiry < m.now():
                self.store.set_local(session_id, pending_launch=None, launch_status="expired")
                raise m.MultiplayerError("launch_expired", "The launch offer expired. Ask the host to retry.", 409)
            self._require_live_launch_host(session, plan)
            adapter = self.adapter_map.get(session["emulator"]["adapter"])
            if adapter is None:
                raise m.MultiplayerError("unknown_adapter", "This Drone cannot take part in that mechanism.", 409)
            self._require_adapter_enabled(adapter)
            path = session["local"].get("game_path", "")
            automated = adapter.effective_automation() != MANUAL and adapter.guest_launches
            if automated:
                if not path or not self._exists(path):
                    raise m.MultiplayerError("game_file_missing", "The game's file is missing on this machine.", 409)
                conflict = None if replace else self._different_game_running(path)
                if conflict:
                    raise m.MultiplayerError("game_running", "A game is running here. Approve replacing it to continue.", 409,
                                             {"running": self._active_summary(conflict)})
            # The queued worker is bound to this exact confirmation: a replacement offer, a retry or a
            # reset creates a plan without this token, so an old worker can never execute it.
            confirmation = m.new_message_id()
            self.store.set_local(session_id, pending_launch={**plan, "consumed": True, "confirmation": confirmation},
                                 launch_status="launching", replace_running_game=replace)
        self._spawn(lambda: self.run_guest_launch(session_id, confirmation), f"multiplayer-launch-{session_id[:8]}")
        return {"status": "launching"}

    def run_guest_launch(self, session_id: str, confirmation: str = "") -> None:
        launched = False
        try:
            with self.session_lock(session_id):
                session = self.store.require(session_id)
                local = session["local"]
                plan = local.get("pending_launch")
                if local.get("ended") or session["state"] not in (m.STARTING, m.RUNNING, m.DEGRADED, m.READY) or not plan:
                    return  # removed, ended, or cancelled while the worker was queued
                if not confirmation or plan.get("consumed") is not True or plan.get("confirmation") != confirmation:
                    return  # the offer this worker was confirmed for was voided or replaced: no consent for the current one
                self._require_live_launch_host(session, plan)
                adapter = self.adapter_map[session["emulator"]["adapter"]]
                adapter.refresh()
                verdict = self.validate_for_join(session)
                problems = verdict["problems"]
                if problems:
                    raise m.MultiplayerError("cannot_launch", problems[0], 409)
                self._check_launch_fingerprint(session, verdict["tested_config_fingerprint"])
                path = local.get("game_path", "")
                if adapter.effective_automation() != MANUAL and adapter.guest_launches:
                    context = self.launch_context(session, m.GUEST_ROLE, port=plan.get("port") or 0, secret=plan.get("secret", ""))
                    if not context["host_address"]:
                        raise m.MultiplayerError("host_address_unknown", "Could not work out an address for the host from your swarm connection.", 409)
                    if context["port"] and adapter.awaits_host_port:
                        self._wait_for_host_port(context["host_address"], context["port"])
                    edits = adapter.configure_networking(session, m.GUEST_ROLE, context) + adapter.configure_controllers(session, m.GUEST_ROLE, context)
                    if edits:
                        self.apply_edits(session_id, edits)
                    active = self.launcher.active_game()
                    already = bool(active and path and same_rom(active.get("rom_path", ""), path))
                    if active and not already:
                        if not local.get("replace_running_game"):
                            raise m.MultiplayerError("game_running", "A game is running here and replacing it was not approved.", 409)
                        self.launcher.exit_game()
                        if not self.launcher.wait_for_exit(EXIT_TIMEOUT):
                            raise m.MultiplayerError("game_not_exited", "The running game did not exit in time.", 409)
                    if not already:
                        launched = True  # even a failing launch call may already have started the game
                        adapter.launch(session, m.GUEST_ROLE, context, self.launcher)
                        started = self.launcher.wait_for_start(path, HOST_START_TIMEOUT)
                        if not started:
                            raise m.MultiplayerError("launch_timeout", "The game did not start on this machine.", 409)
                        self.store.set_local(session_id, launched_pid=started.get("pid"))
                wave = (plan or {}).get("wave")
                self.store.set_local(session_id, launch_status="running", game_launched=True, pending_launch=None)
            self._guest_message(self.store.require(session_id), "launch_result", ok=True, wave=wave)
        except Exception as error:  # noqa: BLE001
            message = getattr(error, "message", None) or f"{type(error).__name__}: {error}"
            self._fail_guest_launch(session_id, message, stop_game=launched)

    def _fail_guest_launch(self, session_id: str, message: str, *, stop_game: bool) -> None:
        with self.session_lock(session_id):
            # Persist the original failure first. A restoration backend outage
            # must leave a failed launch eligible for journal recovery.
            plan = (self.store.require(session_id).get("local") or {}).get("pending_launch") or {}
            self.store.set_local(session_id, launch_status="failed", launch_error=m.clean_text(message, 300),
                                 pending_launch=None,
                                 recovery_response={"action": "launch_result", "ok": False,
                                                    "error": m.clean_text(message, 300), "wave": plan.get("wave")})
            errors = []
            if stop_game:
                try:
                    session = self.store.require(session_id)
                    if self._owns_host_game(session):
                        self.launcher.exit_game()
                except Exception as error:  # noqa: BLE001 - restoration is independent of process shutdown
                    errors.append(f"Game shutdown (close the game locally if it remains running): {error}")
            try:
                result = self.restore_if_enabled(session_id)
                errors.extend((result or {}).get("errors", []))
            except Exception as error:  # noqa: BLE001 - retain journal and original launch failure for recovery
                errors.append(f"Configuration restoration: {error}")
            if errors:
                self.store.set_local(session_id, cleanup_errors=errors)
        self._flush_guest_recovery_response(session_id)

    def _flush_guest_recovery_response(self, session_id: str) -> None:
        """Retry a saved recovery decision over the usual authenticated guest channel.

        Cleanup stands locally even if the host is offline. Keep the decision in
        the session so housekeeping (including after another restart) can deliver it.
        """
        session = self.store.get(session_id)
        response = (session.get("local") or {}).get("recovery_response") if session else None
        if not response or self.local_role(session) != m.GUEST_ROLE:
            return
        try:
            self._guest_message(session, **response)
        except Exception:  # noqa: BLE001 - peer delivery must never prevent local cleanup
            return
        with self.session_lock(session_id):
            current = self.store.get(session_id)
            if current and (current.get("local") or {}).get("recovery_response") == response:
                self.store.set_local(session_id, recovery_response=None)

    def _wait_for_host_port(self, address: str, port: int) -> None:
        waited = 0.0
        while waited < GUEST_HOST_WAIT:
            if self.probe.tcp_open(address, port, 1.5):
                return
            self._sleep(1.0)
            waited += 1.0
        raise m.MultiplayerError("host_unreachable", f"The host's game server at {address}:{port} did not answer. "
                                 "Check that both machines share a LAN or tailnet.", 409)

    # --------------------------------------------------------------- retry/end
    def retry_connection(self, session_id: str, peer_id: str = "") -> dict:
        self.require_enabled()
        peer_id = m.clean_peer_id(peer_id) if peer_id else ""
        with self.session_lock(session_id):
            session = self.require_host(session_id)
            state = session["state"]
            retries = dict((session.get("local") or {}).get("retries") or {})
            key = peer_id or "session"
            if retries.get(key, 0) >= MAX_RETRIES:
                raise m.MultiplayerError("retry_limit", f"Gave up after {MAX_RETRIES} retries. End the session and start a new one.", 409)
            if state in m.PRE_START_STATES:
                retries[key] = retries.get(key, 0) + 1
                self.store.set_local(session_id, retries=retries)
                for pid, player in session["players"].items():
                    if player["status"] == m.P_INVITED and (not peer_id or pid == peer_id):
                        self._deliver_invitation(session_id, self.paired_peer(pid))
            elif state in (m.STARTING, m.DEGRADED):
                wanted = [pid for pid, p in session["players"].items() if pid != self.me and p["status"] in (m.P_FAILED, m.P_UNREACHABLE)
                          and (not peer_id or pid == peer_id)]
                if not wanted:
                    raise m.MultiplayerError("nothing_to_retry", "No player needs a retry.", 409)
                local = session.get("local") or {}
                wave = int(local.get("launch_wave") or 0) + 1
                offered = dict(local.get("offered_waves") or {})
                for pid in wanted:
                    offered.pop(pid, None)
                self.store.set_local(session_id, launch_wave=wave, offered_waves=offered,
                                     launch_deadline=(m.now() + LAUNCH_DEADLINE).isoformat())
                delivered = False
                for pid in wanted:
                    delivered = self._send_launch(session_id, pid) or delivered
                if delivered:
                    retries[key] = retries.get(key, 0) + 1
                    self.store.set_local(session_id, retries=retries)
                self.evaluate_launch(session_id)
            elif state == m.FAILED:
                def reset(s: dict) -> None:
                    s["state"] = m.CONFIGURING
                    s["error"] = ""
                    s["secret"] = m.new_secret()
                    for player in s["players"].values():
                        if player["status"] in (m.P_FAILED, m.P_UNREACHABLE, m.P_RUNNING, m.P_LAUNCHING):
                            player.update(status=m.P_JOINED, error="")
                        if player["status"] == m.P_JOINED:
                            player["ready"] = False  # everyone confirms again before the next attempt
                    add_event(s, "retry", "The host reset the session for another attempt.", self.me)
                retries[key] = retries.get(key, 0) + 1
                self.store.set_local(session_id, retries=retries)
                self.store.mutate(session_id, reset)
                self.store.mutate(session_id, settle_lobby)  # FAILED may only move to CONFIGURING first
                self.store.set_local(session_id, host_launched=False)
            else:
                raise m.MultiplayerError("nothing_to_retry", "There is nothing to retry in this state.", 409)
        self.publish(session_id)
        return self.session_view(self.store.require(session_id))

    def end_session(self, session_id: str, actor: str = "", reason: str = "", stop_game: bool = True) -> dict:
        with self.session_lock(session_id):
            session = self.require_host(session_id)
            if session["state"] == m.COMPLETED:
                return self.session_view(session)
            if session["state"] != m.STOPPING:
                session = self.store.mutate(session_id, lambda s: (s.update(state=m.STOPPING),
                                                                   add_event(s, "stopping", reason or "The host ended the session.", self.me)))
            self.publish(session_id)
            outcome = self.cleanup_host_resources(session, stop_game=stop_game)
            errors = (outcome.get("config") or {}).get("errors") or []

            def finish(s: dict) -> None:
                s["state"] = m.COMPLETED
                s.pop("secret", None)
                if errors:
                    add_event(s, "restore_failed", "Some settings could not be restored automatically: " + "; ".join(errors[:3]), self.me)
                add_event(s, "completed", reason or "Session ended.", self.me)
            session = self.store.mutate(session_id, finish)
            self.store.archive(session, "completed" if session["local"].get("reached_running") else "ended-early")
            self._prune_completed()
        self.publish(session_id)
        self._log(session, "completed", reason or "session ended", actor)
        return self.session_view(session)

    def _prune_completed(self) -> None:
        done = [s for s in self.store.all() if s["state"] == m.COMPLETED]
        for old in done[KEEP_COMPLETED:]:
            self.store.delete(old["id"])

    def guest_cleanup(self, session_id: str, reason: str, *, reopenable: bool = False, departed: bool = False) -> None:
        """A guest leaves/ends locally: settings restored, offers dropped. The running game is NOT closed.

        ``reopenable`` marks a host-side FAILED: if the host retries, the still-joined guest's lobby comes back.
        ``departed`` is this machine's own decision to leave; it revokes reopening for good (until a fresh
        invitation is accepted), and a later host snapshot can never turn an ended replica reopenable.
        """
        with self.session_lock(session_id):
            session = self.store.get(session_id)
            if session is None or self.local_role(session) != m.GUEST_ROLE:
                return
            launch_status = "failed" if (session.get("local") or {}).get("launch_status") == "failed" else ""
            local = session.get("local") or {}
            # Only a membership that was still locally authorized may be recovered by a host retry.
            reopen = bool(reopenable) and not departed and not local.get("departed") and not local.get("ended")
            if local.get("ended") and local.get("reopenable") and not departed:
                reopen = bool(reopenable)  # already awaiting a retry: keep it so
            self.store.set_local(session_id, pending_launch=None, launch_status=launch_status, ended=True,
                                 ended_at=m.now_iso(), ended_reason=m.clean_text(reason, 200),
                                 reopenable=reopen, departed=bool(departed or local.get("departed")))
            try:
                restored = self.restore_if_enabled(session_id)
            except Exception as error:  # noqa: BLE001 - retain journal for housekeeping without reopening participation
                restored = {"errors": [f"Configuration restoration: {error}"]}
            if restored.get("errors"):
                self.store.set_local(session_id, cleanup_errors=restored["errors"])
            if not (session.get("local") or {}).get("archived"):
                self.store.archive(session, "left" if "left" in reason.lower() else "ended")
                self.store.set_local(session_id, archived=True)
            self.audit(EVENT_SESSION, "Multiplayer session ended", reason,
                       {"session_id": session_id, "restore_errors": restored.get("errors", [])}, bell=True)

    # ------------------------------------------------------------- tick/recover
    def tick(self) -> None:
        """Periodic housekeeping (called by the worker): expiry, liveness, health. Never raises."""
        try:
            self._expire_invitations()
            self.guard.sweep_watched()
            self._retry_abandoned_configuration()
            for session in list(self.store.all()):
                try:
                    self._tick_session(session)
                except Exception:  # noqa: BLE001 - one bad session must not stop the sweep
                    continue
        except Exception:  # noqa: BLE001
            return

    def _retry_abandoned_configuration(self) -> None:
        """Recover retained journals independently of whether their session is still active."""
        if not self.cfg.get()["general"]["auto_restore_configuration"]:
            return
        for session_id in self.guard.pending():
            try:
                with self.session_lock(session_id):
                    session = self.store.get(session_id)
                    local = (session or {}).get("local") or {}
                    if session and session["state"] not in (m.COMPLETED, m.FAILED) and not local.get("ended") and local.get("launch_status") != "failed":
                        continue
                    result = self.guard.retry_restore(session_id)
                    if result is not None and session:
                        errors = result.get("errors") or []
                        message = ("Configuration restore retry failed: " + "; ".join(errors) if errors else
                                   "Temporary emulator settings restored after cleanup retry.")
                        self.store.mutate(session_id, lambda s: add_event(s, "restore_retry", message, self.me))
            except Exception:  # noqa: BLE001 - one damaged journal cannot block the others
                continue

    def _expire_invitations(self) -> None:
        for invitation in self.store.invitations():
            expiry = m.parse_time(invitation["expires_at"])
            if invitation["status"] == "pending" and expiry and expiry < m.now():
                self.store.update_invitation(invitation["session_id"], status="expired")

    def _tick_session(self, session: dict) -> None:
        local = session.get("local") or {}
        session_id = session["id"]
        now = m.now()
        if self.local_role(session) == m.GUEST_ROLE and local.get("recovery_response"):
            self._flush_guest_recovery_response(session_id)
            session = self.store.require(session_id)
            local = session.get("local") or {}
        if session["state"] == m.COMPLETED or local.get("ended"):
            ended = m.parse_time(local.get("ended_at"))
            if local.get("ended") and ended and now - ended > ENDED_REPLICA_TTL:
                self.store.delete(session_id)
            return
        expiry = m.parse_time(session.get("expires_at"))
        if self.local_role(session) == m.HOST_ROLE:
            if expiry and expiry < now:
                self.end_session(session_id, reason="The session timed out.")
                return
            self._tick_host(session)
        else:
            if expiry and expiry < now:
                self.guest_cleanup(session_id, "The session timed out.")
                return
            self._tick_guest(session)

    def _tick_host(self, session: dict) -> None:
        session_id, local = session["id"], session.get("local") or {}
        deadline = m.parse_time(local.get("launch_deadline"))
        wave = local.get("launch_wave")
        offered = dict(local.get("offered_waves") or {})
        if session["state"] in (m.STARTING, m.DEGRADED) and deadline and m.now() <= deadline:
            for pid, player in list(session["players"].items()):
                if pid == self.me or player["status"] not in (m.P_JOINED, m.P_LAUNCHING) or offered.get(pid) == wave:
                    continue
                self._send_launch(session_id, pid)
                session = self.store.require(session_id)
        if session["state"] in (m.STARTING, m.DEGRADED):
            self.evaluate_launch(session_id, probe=True)
            session = self.store.require(session_id)
            local = session.get("local") or {}
        if local.get("publish_failed"):
            self.publish(session_id)
        if session["state"] in m.PRE_START_STATES:
            self._expire_unanswered_invitations(session)
        silent = [pid for pid, p in session["players"].items() if pid != self.me and p["status"] in (m.P_JOINED, m.P_LAUNCHING, m.P_RUNNING, m.P_UNREACHABLE)
                  and (m.parse_time((local.get("last_seen") or {}).get(pid)) or m.parse_time(p.get("joined_at")) or m.now()) < m.now() - SILENT_AFTER]
        for pid in silent:
            self._probe_player(session_id, pid)
        if session["state"] in (m.RUNNING, m.DEGRADED) and local.get("host_launched"):
            if self._owns_host_game(session):
                self.store.set_local(session_id, host_game_misses=0)
            else:
                misses = int(local.get("host_game_misses") or 0) + 1
                self.store.set_local(session_id, host_game_misses=misses)
                if misses >= 2:
                    self.end_session(session_id, reason="The host's game exited.", stop_game=False)

    def _expire_unanswered_invitations(self, session: dict) -> None:
        """Invitees who never answered within the invitation lifetime leave the lobby (they can be invited again)."""
        lapsed = [pid for pid, p in session["players"].items() if p["status"] == m.P_INVITED
                  and (self.invitation_expiry(session, pid) or m.now()) < m.now()]
        if not lapsed:
            return

        def expire(s: dict) -> None:
            for pid in lapsed:
                player = s["players"][pid]
                if player["status"] == m.P_INVITED:
                    player.update(status=m.P_REMOVED, ready=False, slot=None, error="The invitation expired before it was answered.")
                    add_event(s, "invitation_expired", f"{player['name']}'s invitation expired.", self.me)
            settle_lobby(s)
        self.store.mutate(session["id"], expire)
        self.publish(session["id"])

    def _probe_player(self, session_id: str, peer_id: str) -> None:
        misses_key = "misses"
        reason = "Lost contact with this player."
        try:
            body = self.network.get(self.paired_peer(peer_id), f"/status?session={session_id}", timeout=4.0)
            reason = self._participation_gap(self.store.require(session_id), peer_id, body)
            alive = not reason
            reason = reason or "Lost contact with this player."
        except Exception:  # noqa: BLE001
            alive = False
        session = self.store.require(session_id)
        local = session.get("local") or {}
        misses = dict(local.get(misses_key) or {})
        if alive:
            misses.pop(peer_id, None)
            self.store.set_local(session_id, **{misses_key: misses}, **self._seen_patch(session, peer_id))

            def recover(s: dict) -> None:
                player = s["players"][peer_id]
                if player["status"] == m.P_UNREACHABLE:
                    previous = s.setdefault("local", {}).setdefault("unreachable_from", {}).pop(peer_id, None)
                    player["status"] = previous or (m.P_RUNNING if s["state"] in m.LIVE_STATES else m.P_JOINED)
                    player["error"] = ""
                    add_event(s, "reconnected", f"{player['name']} is reachable again.", self.me)
                    settle_live(s)
            self.store.mutate(session_id, recover)
            return
        misses[peer_id] = misses.get(peer_id, 0) + 1
        self.store.set_local(session_id, **{misses_key: misses})
        if misses[peer_id] >= 2:
            def lost(s: dict) -> None:
                player = s["players"][peer_id]
                if player["status"] in (m.P_JOINED, m.P_RUNNING, m.P_LAUNCHING):
                    s.setdefault("local", {}).setdefault("unreachable_from", {})[peer_id] = player["status"]
                    player.update(status=m.P_UNREACHABLE, error=reason)
                    add_event(s, "unreachable", f"Lost contact with {player['name']}.", self.me)
                    if s["state"] == m.RUNNING:
                        s["state"] = m.DEGRADED
                    elif s["state"] in m.PRE_START_STATES:
                        player["ready"] = False
                        settle_lobby(s)
            self.store.mutate(session_id, lost)
            self.publish(session_id)

    def _participation_gap(self, session: dict, peer_id: str, body: Any) -> str:
        """Why a responding Drone is not (any longer) a participant; "" when its status backs the session.

        Answering the coordination API only proves the Drone is up: it must still hold the session and,
        once the game is live, have its native emulator running (when this mechanism launches it for guests).
        """
        body = body if isinstance(body, dict) else {}
        remote = body.get("session") or {}
        if not remote.get("known") or remote.get("state") in (m.STOPPING, m.COMPLETED, m.FAILED):
            return "This Drone is online but no longer has the session."
        player = session["players"].get(peer_id) or {}
        adapter = self.adapter_map.get((session.get("emulator") or {}).get("adapter", ""))
        launches = adapter is not None and adapter.guest_launches and adapter.effective_automation() != MANUAL
        in_session = body.get("session_game_running") is True  # an omitted flag is not confirmation
        previous = ((session.get("local") or {}).get("unreachable_from") or {}).get(peer_id, m.P_RUNNING)
        needs_game = player.get("status") == m.P_RUNNING or (player.get("status") == m.P_UNREACHABLE and previous == m.P_RUNNING)
        if (launches and session["state"] in m.LIVE_STATES and needs_game
                and not in_session):
            return "This Drone is online but is not running this session's game."
        return ""

    def _tick_guest(self, session: dict) -> None:
        session_id, local = session["id"], session.get("local") or {}
        pending = local.get("pending_launch")
        expiry = m.parse_time((pending or {}).get("expires_at"))
        if pending and not pending.get("consumed") and expiry and expiry < m.now():
            self.store.set_local(session_id, pending_launch=None, launch_status="expired")
        try:
            status = self.network.get(self.host_peer(session), f"/status?session={session_id}", timeout=4.0)
            remote = status.get("session") or {}
            if not remote.get("known") or remote.get("state") in (m.STOPPING, m.COMPLETED, m.FAILED):
                self.guest_cleanup(session_id, "The host ended or no longer knows this session.",
                                   reopenable=bool(remote.get("known")) and remote.get("state") == m.FAILED)
                return
            if local.get("host_unreachable_since"):
                self.store.set_local(session_id, host_unreachable_since="")
        except Exception:  # noqa: BLE001
            since = m.parse_time(local.get("host_unreachable_since"))
            if since is None:
                self.store.set_local(session_id, host_unreachable_since=m.now_iso())
            elif m.now() - since > HOST_LOST_AFTER:
                self.guest_cleanup(session_id, "The host became unavailable; this emulator session cannot continue without it.")
                return
        mine = session["players"].get(self.me) or {}
        if mine.get("status") == m.P_RUNNING and local.get("game_launched") and self.guest_launches_game(session):
            if not self.session_game_running(session):
                misses = int(local.get("game_misses") or 0) + 1
                self.store.set_local(session_id, game_misses=misses)
                if misses >= 2:
                    try:
                        self._guest_message(session, "leave")
                    except m.MultiplayerError:
                        pass
                    self.guest_cleanup(session_id, "You left the session because its game is no longer running here.", departed=True)
            else:
                self.store.set_local(session_id, game_misses=0)

    def recover(self) -> dict:
        """After a Drone restart: restore orphaned settings, fail half-started sessions, resume live ones."""
        summary = {"restored": 0, "failed": 0, "resumed": 0, "ended": 0}
        try:
            pending = list(self.guard.pending())
        except Exception:  # noqa: BLE001 - session bookkeeping can recover even when the journal backend is unavailable
            pending = []
        for session_id in pending:
            try:
                session = self.store.get(session_id)
                if session is None or session["state"] in (m.COMPLETED, m.FAILED) or (session.get("local") or {}).get("ended"):
                    self.guard.restore(session_id)
                    summary["restored"] += 1
            except Exception:  # noqa: BLE001 - another session must still be recovered
                pass
        for session in self.store.active():
            try:
                local, session_id = session.get("local") or {}, session["id"]
                if local.get("ended"):
                    continue
                if self.local_role(session) == m.HOST_ROLE:
                    if session["state"] == m.STARTING:
                        self.fail_session(session_id, "The Drone restarted while the session was starting.")
                        summary["failed"] += 1
                    elif session["state"] in (m.RUNNING, m.DEGRADED):
                        if local.get("host_launched") and not self._owns_host_game(session):
                            self.end_session(session_id, reason="The game was not running after the Drone restarted.", stop_game=False)
                            summary["ended"] += 1
                        else:
                            summary["resumed"] += 1
                    elif session["state"] == m.STOPPING:
                        self.end_session(session_id, reason="Finished stopping after a restart.")
                        summary["ended"] += 1
                else:
                    if local.get("launch_status") == "launching":
                        self._fail_guest_launch(session_id, "The Drone restarted while launching.", stop_game=True)
                        summary["failed"] += 1
                    elif local.get("launch_status") == "running" and self.guest_launches_game(session):
                        if self.session_game_running(session):
                            summary["resumed"] += 1
                        else:
                            # Reconcile immediately on restart, rather than wait
                            # for the normal two-miss game-exit debounce.
                            self.store.set_local(session_id, recovery_response={"action": "leave"})
                            self.guest_cleanup(session_id, "The session game was not running after the Drone restarted.", departed=True)
                            self._flush_guest_recovery_response(session_id)
                            summary["ended"] += 1
            except Exception:  # noqa: BLE001 - isolate each session
                pass
        return summary

    # ---------------------------------------------------- results, diagnostics
    def record_result(self, session_id: str, success: Any, note: str = "") -> dict:
        """The player confirms (or denies) that the session really worked; recorded per tested configuration."""
        success = m.clean_bool(success, "success")
        session = self.store.require(session_id)
        local = session.get("local") or {}
        ran = local.get("reached_running") if self.local_role(session) == m.HOST_ROLE else (local.get("game_launched") or local.get("launch_status") == "running")
        if not ran:
            raise m.MultiplayerError("never_ran", "The session never ran on this machine, so there is no result to record.", 409)
        if local.get("result_recorded"):
            raise m.MultiplayerError("already_recorded", "You already recorded a result for this session.", 409)
        emulator = session["emulator"]
        fingerprint = local.get("tested_config_fingerprint")
        if not fingerprint:
            raise m.MultiplayerError("configuration_unverified", "This session has no inspected local configuration to verify.", 409)
        record = self.compat.record_session_result(
            system=session["game"]["system"], title_key=m.normalize_title(session["game"]["rom_name"]), title=session["game"]["title"],
            mechanism=session["mechanism"], config_fp=fingerprint, emulator_version=emulator.get("version", ""), success=success,
            max_players=len([p for p in session["players"].values() if p["status"] in (m.P_RUNNING, m.P_LEFT)]) or None,
            failure=note if not success else "")
        self.store.set_local(session_id, result_recorded=True)
        self._log(session, "result", f"result recorded: {'worked' if success else 'did not work'}")
        return {"recorded": True, "config_fingerprint": fingerprint, "record": {key: record.get(key) for key in ("system", "gameId", "title", "lastValidated")}}

    def restore_configuration(self, session_id: str) -> dict:
        """Manual 'restore my settings' (also what to use when automatic restore is switched off)."""
        session_id = m.clean_session_id(session_id)
        session = self.store.get(session_id)
        if session and session["state"] in m.LIVE_STATES:
            raise m.MultiplayerError("session_live", "End the session before restoring its settings.", 409)
        return self.guard.restore(session_id)

    def diagnostics(self, session_id: str) -> dict:
        session = self.store.require(session_id)
        adapter = self.adapter_map.get(session.get("emulator", {}).get("adapter", ""))
        peers = []
        for pid, player in session["players"].items():
            if pid == self.me:
                continue
            try:
                result = self.network.test_peer(self.paired_peer(pid), samples=1)
            except m.MultiplayerError as error:
                result = {"peer_id": pid, "reachable": False, "error": error.message, "scope": "unknown", "latency_ms": None, "advice": []}
            peers.append({**result, "name": player["name"], "status": player["status"]})
        journal = self.guard.pending(session_id).get(session_id)
        local = session.get("local") or {}
        return {
            "session": self.session_view(session),
            "adapter": adapter.capabilities() if adapter else None,
            "problems": adapter.validate_configuration(session, self.local_role(session)) if adapter else ["unknown mechanism"],
            "connectivity": peers,
            "configuration": {"pending_restore": bool(journal),
                              "files": [{"file": path.rsplit("/", 1)[-1], "keys": [e["key"] for e in spec["edits"]]}
                                        for path, spec in (journal or {}).get("files", {}).items()]},
            "active_game": self._active_summary(self.launcher.active_game() or {}) if self.launcher.active_game() else None,
            "local": {key: local.get(key) for key in ("role", "port", "host_launched", "launch_status", "launch_error", "reached_running",
                                                      "host_unreachable_since", "retries", "publish_failed", "result_recorded", "cleanup_errors")},
            "events": session.get("events", [])[-50:],
        }
