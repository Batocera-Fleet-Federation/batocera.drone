"""Drone-to-Drone coordination (the ``/peer/multiplayer/*`` side) and the guest's local actions.

Every inbound message arrives on the mTLS listener, already tied to a paired
peer's certificate by the HTTP layer; ``caller_id`` here is that certificate's
identity, never a value from the body. Messages carry a random ``message_id``
and ``issued_at`` (replay window), and session mutations are additionally
guarded by session membership and the monotonically increasing version.
Peer-supplied data is rebuilt field by field before it is stored.
"""

import re
from datetime import timedelta
from typing import Any, List, Optional

from . import models as m
from .adapters.base import CONTENT_HOST_ONLY
from .core import EVENT_INVITATION, EVENT_SESSION
from .matching import MAX_MATCH_QUERIES, level_note, level_satisfies
from .sessions import launch_pending, next_free_slot, settle_live, settle_lobby
from .store import add_event, replica_view, sanitize_session, require_same_identity

try:
    from ..integrations.streamdeck.game_runtime import same_rom
except ImportError:  # pragma: no cover - flat execution
    from integrations.streamdeck.game_runtime import same_rom  # type: ignore

ACTIONS = ("accept", "decline", "leave", "ready", "not_ready", "launch_result")
MAX_PENDING_INVITATIONS = 20
LAUNCH_CONFIRM_WINDOW = timedelta(minutes=5)
PEER_FIELDS = ("mechanism", "adapter_name", "classification", "readiness", "readiness_message", "reasons", "emulator", "guest_ready")


_CORE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


def _hex(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if len(text) <= 64 and all(c in "0123456789abcdef" for c in text) else ""


class ProtocolMixin:
    # -------------------------------------------------------------- inbound gate
    def check_message(self, caller_id: str, payload: Any, *, allow_duplicate: bool = False) -> str:
        """Authorise an inbound peer message. Returns ``"ok"`` or ``"duplicate"`` (replays never reapply)."""
        payload = m.require_dict(payload)
        caller_id = m.clean_peer_id(caller_id, "caller")
        self.paired_peer(caller_id)
        claimed = str(payload.get("source_drone_id") or caller_id)
        if claimed != caller_id:
            raise m.MultiplayerError("identity_mismatch", "source_drone_id does not match the paired client certificate", 403)
        status = self.store.check_replay(caller_id, m.clean_message_id(payload.get("message_id")), payload.get("issued_at"))
        if status == "duplicate" and not allow_duplicate:
            return "duplicate"
        return status

    # ------------------------------------------------------------- match (any)
    def peer_match(self, caller_id: str, payload: Any) -> dict:
        self.require_enabled()
        self.check_message(caller_id, payload, allow_duplicate=True)  # read-only: duplicates are harmless
        raw = payload.get("queries")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_MATCH_QUERIES:
            raise m.MultiplayerError("invalid_body", f"queries must hold 1-{MAX_MATCH_QUERIES} entries")
        results = []
        for item in raw:
            item = m.require_dict(item, "query")
            rom_name = m.clean_text(item.get("rom_name"), 255)
            size = item.get("size")
            query = {"system": m.clean_system(item.get("system")), "rom_name": rom_name, "title_key": m.normalize_title(rom_name),
                     "fingerprint": _hex(item.get("fingerprint")),
                     "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0}
            core = str(item.get("core") or "").strip().lower()
            core = core if _CORE.fullmatch(core) else ""
            found = self.library.find_copy(query)
            if found:
                game = found["game"]
                assessments = self.assessor.assess(game, game_path=self.library.absolute_path(game), core_override=core)
            else:
                synthetic = {"system": query["system"], "rom_name": rom_name, "title_key": query["title_key"], "title": rom_name,
                             "players": None, "fingerprint": "", "size": 0, "id": ""}
                assessments = [a for a in self.assessor.assess(synthetic) if a["mechanism"] == m.REMOTE_STREAMING]
            results.append({"found": bool(found), "level": found["level"] if found else "none",
                            "assessments": [{key: assessment.get(key) for key in PEER_FIELDS} for assessment in assessments[:8]]})
        return {"enabled": True, "device_id": self.me, "controllers": self.controller_count(), "results": results}

    def peer_status(self, caller_id: str, session_id: str = "") -> dict:
        self.check_message_light(caller_id)
        body = {"device_id": self.me, "enabled": self.cfg.get()["general"]["enabled"], "session": None,
                "game_running": self.launcher.active_game() is not None}
        if session_id:
            session = self.store.get(m.clean_session_id(session_id))
            if session and caller_id in session["players"] and not (session.get("local") or {}).get("ended"):
                deadline = m.parse_time((session.get("local") or {}).get("launch_deadline"))
                # Use the host's clock: a live session may continue after one
                # guest's start attempt has expired, even before the next sweep.
                launch_allowed = (self.local_role(session) == m.HOST_ROLE and
                                  session["state"] in m.LIVE_STATES and
                                  session["players"][caller_id]["status"] in (m.P_JOINED, m.P_LAUNCHING) and
                                  bool(deadline and m.now() <= deadline))
                body["session"] = {"known": True, "state": session["state"], "version": session["version"],
                                   "player_status": session["players"][caller_id]["status"],
                                   "launch_allowed": launch_allowed,
                                   "launch_status": (session.get("local") or {}).get("launch_status") or ""}
                body["session_game_running"] = self.session_game_running(session)
                self.store.set_local(session["id"], **self._seen_patch(session, caller_id))
        return body

    def session_game_running(self, session: dict) -> bool:
        """The foreground game on this machine is this session's game (not merely *some* game)."""
        path = (session.get("local") or {}).get("game_path", "")
        active = self.launcher.active_game()
        return bool(path and active and same_rom(active.get("rom_path", ""), path))

    def guest_launches_game(self, session: dict) -> bool:
        adapter = self.adapter_map.get((session.get("emulator") or {}).get("adapter", ""))
        return adapter is not None and adapter.guest_launches and adapter.effective_automation() != "manual"

    def _verified_launch_payload(self, session: dict, caller_id: str, payload: dict) -> Optional[dict]:
        """A launch success report is untrusted until the reporter really runs the session game.

        Returns the payload to apply (downgraded to a failure when the guest says it is not running the game),
        or ``None`` when that could not be checked right now (the report is then not applied).
        """
        if payload.get("ok") is not True or not self.guest_launches_game(session):
            return payload
        try:
            body = self.network.get(self.paired_peer(caller_id), f"/status?session={session['id']}", timeout=4.0)
        except Exception:  # noqa: BLE001
            return None
        remote = body.get("session") if isinstance(body, dict) else None
        if (isinstance(remote, dict) and remote.get("known")
                and body.get("session_game_running") is True
                and remote.get("launch_status") == "running"):
            return payload
        return {**payload, "ok": False, "error": "Reported a successful launch but is not running this session's game."}

    def check_message_light(self, caller_id: str) -> None:
        self.paired_peer(m.clean_peer_id(caller_id, "caller"))

    def _seen_patch(self, session: dict, peer_id: str) -> dict:
        seen = dict((session.get("local") or {}).get("last_seen") or {})
        seen[peer_id] = m.now_iso()
        return {"last_seen": seen}

    def peer_compat_export(self, caller_id: str) -> dict:
        self.require_enabled()
        self.check_message_light(caller_id)
        return {"version": 1, "device_id": self.me, "records": self.compat.export_shareable()}

    # -------------------------------------------------------------- host: respond
    def peer_respond(self, caller_id: str, payload: Any) -> dict:
        self.require_enabled()
        replay = self.check_message(caller_id, payload)
        payload = m.require_dict(payload)
        session_id = m.clean_session_id(payload.get("session_id"))
        action = str(payload.get("action") or "")
        if action not in ACTIONS:
            raise m.MultiplayerError("invalid_action", f"action must be one of: {', '.join(ACTIONS)}")
        if action == "launch_result" and replay != "duplicate":
            # Ask the reporter about itself outside the session lock (a bounded network call).
            known = self.store.get(session_id)
            if known is not None and self.local_role(known) == m.HOST_ROLE and caller_id in known["players"]:
                payload = self._verified_launch_payload(known, caller_id, payload)
                if payload is None:
                    return {"session": replica_view(known), "applied": False, "reason": "unverified"}
        with self.session_lock(session_id):
            session = self.store.get(session_id)
            if session is None or self.local_role(session) != m.HOST_ROLE:
                raise m.MultiplayerError("session_not_found", "This Drone does not host that session.", 404)
            player = session["players"].get(caller_id)
            if player is None:
                raise m.MultiplayerError("not_a_member", "You are not a member of that session.", 403)
            if replay == "duplicate":
                return {"session": replica_view(session), "duplicate": True}
            if action == "accept" and player["status"] == m.P_INVITED:
                expires = self.invitation_expiry(session, caller_id)
                if expires and expires < m.now():
                    raise m.MultiplayerError("invitation_expired", "This invitation has expired. Ask the host to invite you again.", 409)
            handler = getattr(self, f"_respond_{action}")
            updated = self.store.mutate(session_id, lambda s: handler(s, caller_id, payload))
            self.store.set_local(session_id, **self._seen_patch(updated, caller_id))
            updated = self.store.require(session_id)
        self.after_respond(updated, caller_id, action)
        self.publish(session_id)
        return {"session": replica_view(updated)}

    def after_respond(self, session: dict, caller_id: str, action: str) -> None:
        player = session["players"].get(caller_id, {})
        self._log(session, action, f"{player.get('name', caller_id)}: {action}", caller_id)
        if action == "launch_result":
            self.evaluate_launch(session["id"])

    def _respond_accept(self, s: dict, caller_id: str, payload: dict) -> None:
        if s["state"] not in m.PRE_START_STATES:
            raise m.MultiplayerError("session_started", "The session has already started.", 409)
        expiry = m.parse_time(s.get("expires_at"))
        if expiry and expiry < m.now():
            raise m.MultiplayerError("session_expired", "The session has expired.", 409)
        player = s["players"][caller_id]
        if player["status"] == m.P_JOINED:
            return
        if player["status"] != m.P_INVITED:
            raise m.MultiplayerError("not_invited", "You must be invited again to join this session.", 409)
        # Check consent on the authoritative host before changing membership.
        # Checking only the response on the guest is too late: a stale invitation
        # would leave a joined host player with no guest replica.
        reviewed = payload.get("reviewed_session")
        if reviewed is None:
            raise m.MultiplayerError("invitation_review_required", "Review the current invitation before accepting it.", 409)
        require_same_identity(sanitize_session(reviewed), s)
        compat = payload.get("compat") if isinstance(payload.get("compat"), dict) else {}
        classification = compat.get("classification")
        controllers = payload.get("controllers")
        player.update(status=m.P_JOINED, joined_at=m.now_iso(), slot=player.get("slot") or next_free_slot(s), error="", ready=False,
                      controllers=controllers if isinstance(controllers, int) and not isinstance(controllers, bool) and 0 <= controllers <= 64 else None,
                      compat={"classification": classification if classification in m.COMPATIBILITY_CLASSES else m.UNKNOWN,
                              "reasons": [m.clean_text(reason, 300) for reason in (compat.get("reasons") or [])[:6]]
                              if isinstance(compat.get("reasons"), list) else []})
        add_event(s, "joined", f"{player['name']} joined.", caller_id)
        settle_lobby(s)

    def _respond_decline(self, s: dict, caller_id: str, payload: dict) -> None:
        player = s["players"][caller_id]
        if player["status"] in (m.P_INVITED, m.P_JOINED) and s["state"] in m.PRE_START_STATES:
            player.update(status=m.P_DECLINED, ready=False, slot=None)
            add_event(s, "declined", f"{player['name']} declined.", caller_id)
            settle_lobby(s)

    def _respond_leave(self, s: dict, caller_id: str, payload: dict) -> None:
        player = s["players"][caller_id]
        if player["status"] in (m.P_DECLINED, m.P_LEFT, m.P_REMOVED):
            return
        player.update(status=m.P_LEFT, ready=False, slot=None)
        add_event(s, "left", f"{player['name']} left.", caller_id)
        if s["state"] in m.PRE_START_STATES:
            settle_lobby(s)
        elif s["state"] in m.LIVE_STATES:
            settle_live(s)

    def _respond_ready(self, s: dict, caller_id: str, payload: dict, ready: bool = True) -> None:
        if s["state"] not in m.PRE_START_STATES:
            raise m.MultiplayerError("session_started", "Readiness can only change before the session starts.", 409)
        player = s["players"][caller_id]
        if player["status"] != m.P_JOINED:
            raise m.MultiplayerError("not_joined", "Join the session before setting readiness.", 409)
        player["ready"] = ready
        settle_lobby(s)

    def _respond_not_ready(self, s: dict, caller_id: str, payload: dict) -> None:
        self._respond_ready(s, caller_id, payload, ready=False)

    def _respond_launch_result(self, s: dict, caller_id: str, payload: dict) -> None:
        player = s["players"][caller_id]
        if s["state"] not in (m.STARTING, m.RUNNING, m.DEGRADED):
            return
        if payload.get("ok") is True:
            if not self._launch_result_current(s, caller_id, payload):
                return
            player.update(status=m.P_RUNNING, error="")
            return
        if player["status"] not in (m.P_JOINED, m.P_LAUNCHING, m.P_UNREACHABLE):
            return
        player.update(status=m.P_FAILED, error=m.clean_text(payload.get("error") or "The launch failed.", 300))
        add_event(s, "launch_failed", f"{player['name']}: {player['error']}", caller_id)

    @staticmethod
    def _launch_result_current(session: dict, caller_id: str, payload: dict) -> bool:
        """A success report is consent for this launch wave only, while the host still expects it."""
        player = session["players"][caller_id]
        if not launch_pending(session, player):
            return False
        local = session.get("local") or {}
        wave = payload.get("wave")
        if not isinstance(wave, int) or isinstance(wave, bool) or wave != local.get("launch_wave"):
            return False
        deadline = m.parse_time(local.get("launch_deadline"))
        return not (deadline and m.now() > deadline)

    # --------------------------------------------------------------- guest: invite
    def peer_invite(self, caller_id: str, payload: Any) -> dict:
        config = self.require_enabled()
        replay = self.check_message(caller_id, payload)
        payload = m.require_dict(payload)
        if not config["general"]["invitations_enabled"]:
            raise m.MultiplayerError("invitations_disabled", "This Drone is not accepting multiplayer invitations.", 403)
        privacy = config["privacy"]
        if privacy["invite_permission"] == "none" or (privacy["invite_permission"] == "allowlist"
                                                       and caller_id not in privacy["allowed_inviters"]):
            raise m.MultiplayerError("invite_not_permitted", "This Drone does not accept invitations from you.", 403)
        snapshot = sanitize_session(payload.get("session"))
        if snapshot["host_id"] != caller_id:
            raise m.MultiplayerError("not_session_host", "Only the session host can send its invitations.", 403)
        snapshot["events"] = []  # the invitation table is bounded; the session log arrives with the replica
        mine = snapshot["players"].get(self.me)
        if mine is None or mine["status"] != m.P_INVITED or snapshot["state"] in m.TERMINAL_STATES or snapshot["state"] not in m.PRE_START_STATES:
            raise m.MultiplayerError("invalid_invitation", "That invitation does not apply to this Drone.", 400)
        owned = self.store.get(snapshot["id"])
        if owned and (owned.get("host_id") != caller_id or self.local_role(owned) == m.HOST_ROLE):
            raise m.MultiplayerError("session_exists", "That session id already belongs to another session.", 409)
        existing = self.store.invitation(snapshot["id"])
        if existing and existing["host_id"] != caller_id:
            raise m.MultiplayerError("session_exists", "That invitation id belongs to another host.", 409)
        if existing and snapshot["version"] < existing["session"]["version"]:
            return {"status": existing["status"], "reason": "stale"}
        # A new host message (not a replay) may ask again after the guest left, declined,
        # or let the invitation lapse. An invitation the guest accepted and is still in is left alone.
        renewing = bool(existing) and replay != "duplicate" and self._invitation_reopenable(existing, snapshot["id"])
        if replay == "duplicate" or (existing and existing["status"] != "pending" and not renewing):
            return {"status": (existing or {}).get("status", "pending"), "duplicate": True}
        pending = [item for item in self.store.invitations() if item["status"] == "pending"]
        if (not existing or renewing) and len(pending) >= MAX_PENDING_INVITATIONS:
            raise m.MultiplayerError("too_many_invitations", "Too many pending invitations.", 429)
        ttl = timedelta(minutes=config["general"]["invitation_ttl_minutes"])
        asked = m.parse_time(payload.get("invite_expires_at")) or (m.now() + ttl)
        expires = min(asked, m.now() + ttl, m.parse_time(snapshot["expires_at"]) or (m.now() + ttl))
        invitation = {
            "session_id": snapshot["id"], "host_id": caller_id, "host_name": snapshot["host_name"], "status": "pending",
            "received_at": m.now_iso(), "expires_at": expires.isoformat(), "game": snapshot["game"], "mechanism": snapshot["mechanism"],
            "mechanism_label": m.MECHANISM_LABELS.get(snapshot["mechanism"], ""), "emulator": snapshot["emulator"],
            "max_players": snapshot["max_players"], "mechanism_reason": snapshot["mechanism_reason"], "session": snapshot,
        }
        self.store.put_invitation(invitation)
        self.audit(EVENT_INVITATION, f"Multiplayer invitation from {snapshot['host_name']}",
                   f"{snapshot['host_name']} invited you to play {snapshot['game']['title']}.",
                   {"session_id": snapshot["id"], "host_id": caller_id, "game": snapshot["game"]["title"]}, bell=True)
        status = "pending"
        if config["general"]["auto_accept_invitations"]:
            # Joining the lobby is all auto-accept does; launching always needs this machine's own confirmation.
            self._spawn(lambda: self._auto_accept(snapshot["id"]), f"multiplayer-auto-accept-{snapshot['id'][:8]}")
            status = "auto_accept_pending"
        return {"status": status}

    def _invitation_reopenable(self, existing: dict, session_id: str) -> bool:
        """Whether a fresh invite should become pending again for this machine.

        ``expired`` and ``cancelled`` lapsed without a decision still standing.
        ``declined`` was an answer to the previous ask; Invite Peer is a new ask.
        ``accepted`` reopens only after this machine has left that membership.
        Accepting the old invitation, with no new invite, does not rejoin.
        """
        status = existing.get("status")
        if status in ("expired", "cancelled", "declined"):
            return True
        if status != "accepted":
            return False
        owned = self.store.get(session_id)
        if owned is None or (owned.get("local") or {}).get("ended"):
            return True
        mine = (owned.get("players") or {}).get(self.me) or {}
        return mine.get("status") in (m.P_LEFT, m.P_REMOVED, m.P_DECLINED)

    def _auto_accept(self, session_id: str) -> None:
        try:
            self.accept_invitation(session_id)
        except m.MultiplayerError:
            pass  # it stays pending for the person to review

    # ---------------------------------------------------------------- guest: update
    def peer_update(self, caller_id: str, payload: Any) -> dict:
        self.require_enabled()
        replay = self.check_message(caller_id, payload)
        payload = m.require_dict(payload)
        if replay == "duplicate":
            return {"applied": False, "reason": "duplicate"}
        try:
            updated = self.store.apply_replica(payload.get("session"), caller_id)
        except m.MultiplayerError as error:
            if error.code == "session_not_found":
                self._maybe_cancel_invitation(payload.get("session"), caller_id)
                return {"applied": False, "reason": "not_joined"}
            raise
        if updated is None:
            return {"applied": False, "reason": "stale"}
        self.after_replica_update(updated)
        return {"applied": True, "version": updated["version"]}

    def _maybe_cancel_invitation(self, raw: Any, caller_id: str) -> None:
        """A host withdrew (or ended) a session we were only invited to: close the pending invitation."""
        try:
            snapshot = sanitize_session(raw)
        except m.MultiplayerError:
            return
        invitation = self.store.invitation(snapshot["id"])
        if not invitation or invitation["status"] != "pending" or invitation["host_id"] != caller_id:
            return
        if snapshot["host_id"] != caller_id or snapshot["version"] < invitation["session"]["version"]:
            return
        mine = snapshot["players"].get(self.me)
        if mine is None or mine["status"] != m.P_INVITED or snapshot["state"] not in m.PRE_START_STATES:
            self.store.update_invitation(snapshot["id"], status="cancelled")

    def after_replica_update(self, session: dict) -> None:
        mine = session["players"].get(self.me)
        if session["state"] == m.RUNNING and mine is not None and mine["status"] == m.P_RUNNING:
            adapter = self.adapter_map.get((session.get("emulator") or {}).get("adapter", ""))
            if adapter is not None and (adapter.effective_automation() == "manual" or not adapter.guest_launches):
                self.store.set_local(session["id"], launch_status="running", game_launched=True)  # followed the listed steps
        removed = mine is not None and mine["status"] in (m.P_REMOVED, m.P_DECLINED)
        if session["state"] in (m.COMPLETED, m.FAILED, m.STOPPING) or removed or mine is None:
            reason = ("You were removed from the session by the host." if removed else
                      "The host ended the session." if session["state"] in (m.COMPLETED, m.STOPPING) else
                      session.get("error") or "The session failed.")
            self.guest_cleanup(session["id"], reason, reopenable=session["state"] == m.FAILED and not removed and mine is not None)
        elif session["state"] in m.PRE_START_STATES:
            # Back to the lobby (e.g. a retry): any earlier launch offer is void.
            self.store.set_local(session["id"], pending_launch=None, launch_status="", recovery_response=None)
            local = session.get("local") or {}
            if local.get("ended") and local.get("reopenable") and mine is not None and mine["status"] == m.P_JOINED:
                # The host retried a failed session this guest had joined: reopen its lobby (launch still needs confirmation).
                self.store.set_local(session["id"], ended=False, ended_at="", ended_reason="", reopenable=False, archived=False,
                                     launch_error="", game_launched=False, game_misses=0, host_unreachable_since="")

    # ---------------------------------------------------------------- guest: launch
    def peer_launch(self, caller_id: str, payload: Any) -> dict:
        config = self.require_enabled()
        replay = self.check_message(caller_id, payload)
        payload = m.require_dict(payload)
        session_id = m.clean_session_id(payload.get("session_id"))
        with self.session_lock(session_id):
            session = self.store.get(session_id)
            if session is None or self.local_role(session) != m.GUEST_ROLE or session["host_id"] != caller_id:
                raise m.MultiplayerError("session_not_found", "This Drone is not a guest in that session.", 404)
            mine = session["players"].get(self.me)
            if mine is None or mine["status"] in (m.P_DECLINED, m.P_LEFT, m.P_REMOVED):
                raise m.MultiplayerError("not_a_member", "You are not an active player in that session.", 403)
            if replay == "duplicate":
                return {"status": session.get("local", {}).get("launch_status") or "awaiting_confirmation", "duplicate": True}
            if (session.get("local") or {}).get("ended"):
                raise m.MultiplayerError("session_closed", "You already left this session.", 409)
            if session["state"] not in (m.READY, m.STARTING, m.RUNNING, m.DEGRADED):
                raise m.MultiplayerError("session_not_starting", "That session is not starting.", 409)
            port = payload.get("port")
            wave = payload.get("wave")
            plan = {
                "port": m.clean_int(port, "port", 1, 65535) if port not in (None, "") else 0,
                "secret": str(payload.get("secret") or "")[:64] if str(payload.get("secret") or "").isalnum() else "",
                "received_at": m.now_iso(), "expires_at": (m.now() + LAUNCH_CONFIRM_WINDOW).isoformat(), "consumed": False,
                "version": session["version"],
                "wave": wave if isinstance(wave, int) and not isinstance(wave, bool) and 0 < wave < 2 ** 31 else 0,
            }
            self.store.set_local(session_id, pending_launch=plan, launch_status="awaiting_confirmation", recovery_response=None)
        title = session["game"]["title"]
        self.audit(EVENT_INVITATION, "Multiplayer game ready to start", f"{session['host_name']} started {title}. Confirm to launch it here.",
                   {"session_id": session_id, "game": title}, bell=True)
        if config["privacy"]["local_launch_authorization"] == "auto" and self.launcher.active_game() is None:
            self._spawn(lambda: self._auto_confirm(session_id), f"multiplayer-auto-launch-{session_id[:8]}")
            return {"status": "launching"}
        return {"status": "awaiting_confirmation"}

    def _auto_confirm(self, session_id: str) -> None:
        try:
            self.confirm_launch(session_id, replace_running_game=False)
        except m.MultiplayerError:
            pass

    # ------------------------------------------------------- guest: local actions
    def host_peer(self, session: dict) -> dict:
        return self.paired_peer(session["host_id"])

    def validate_for_join(self, snapshot: dict) -> dict:
        """What would stop this machine from taking part? Returns problems, a compat summary and the local copy."""
        problems: List[str] = []
        reasons: List[str] = []
        adapter = self.adapter_map.get((snapshot.get("emulator") or {}).get("adapter", ""))
        classification = m.UNKNOWN
        tested_config_fp = ""
        local = None
        if adapter is None:
            problems.append("This Drone does not know the multiplayer mechanism the host chose.")
        elif snapshot["mechanism"] not in adapter.mechanisms:
            problems.append("The requested mechanism does not belong to the session's emulator adapter.")
        elif adapter.id in self.cfg.get()["emulators"]["disabled_adapters"]:
            problems.append(f"{adapter.name} is disabled in your Multiplayer settings.")
        else:
            requirement = adapter.content_match
            local = self.local_copy(snapshot["game"]) if requirement != CONTENT_HOST_ONLY else None
            if requirement != CONTENT_HOST_ONLY:
                level = local["level"] if local else "none"
                if not level_satisfies(level, requirement):
                    problems.append(f"You need the same game: {level_note(level, requirement)}")
                else:
                    reasons.append(level_note(level, requirement))
            if local and snapshot["mechanism"] in (m.RETROARCH_NETPLAY, m.DOLPHIN_NETPLAY):
                wanted = snapshot["game"].get("sha256", "")
                try:
                    actual = self.library.content_hash(local["game"])
                    if len(wanted) != 64 or wanted != actual:
                        problems.append("The host and this machine need identical ROM content (full SHA-256 differs or is missing).")
                    else:
                        reasons.append("Selected ROM content verified by full SHA-256.")
                except m.MultiplayerError as error:
                    problems.append(error.message)
            problems.extend(adapter.validate_configuration(snapshot, m.GUEST_ROLE))
            if snapshot["mechanism"] == m.REMOTE_STREAMING and (not self.cfg.get()["streaming"]["enabled"] or
                    self.cfg.get()["privacy"]["remote_controller_authorization"] == "disabled"):
                problems.append("Remote streaming is turned off in your Multiplayer settings.")
            if local:
                probe_game = dict(local["game"])
                for assessment in self.assessor.assess(probe_game, game_path=self.library.absolute_path(probe_game),
                                                       core_override=(snapshot.get("emulator") or {}).get("core", "")):
                    if assessment["mechanism"] == snapshot["mechanism"]:
                        classification = assessment["classification"]
                        tested_config_fp = assessment["config_fingerprint"]
                        reasons.extend(assessment["reasons"][:3])
                        if classification in (m.UNSUPPORTED, m.INCOMPATIBLE):
                            problems.append(assessment["reasons"][0] if assessment["reasons"] else "This game is not supported here.")
                        elif classification == m.REQUIRES_CONFIGURATION:
                            problems.append(assessment["reasons"][-1] if assessment["reasons"] else "This game needs configuration first.")
        return {"problems": problems, "compat": {"classification": classification, "reasons": reasons[:6]}, "local": local, "tested_config_fingerprint": tested_config_fp}

    def accept_invitation(self, session_id: str, actor: str = "") -> dict:
        with self._creation_lock:
            return self._accept_invitation(session_id, actor)

    def _accept_invitation(self, session_id: str, actor: str = "") -> dict:
        self.require_enabled()
        session_id = m.clean_session_id(session_id)
        invitation = self.store.invitation(session_id)
        if invitation is None:
            raise m.MultiplayerError("invitation_not_found", "No such invitation.", 404)
        if invitation["status"] == "accepted" and self.store.get(session_id):
            return self.session_view(self.store.get(session_id))
        if invitation["status"] != "pending":
            raise m.MultiplayerError("invitation_closed", f"This invitation was {invitation['status']}.", 409)
        expiry = m.parse_time(invitation["expires_at"])
        if expiry and expiry < m.now():
            self.store.update_invitation(session_id, status="expired")
            raise m.MultiplayerError("invitation_expired", "This invitation has expired.", 409)
        if not self.store.get(session_id) and len(self.store.active()) >= self.cfg.get()["general"]["max_simultaneous_sessions"]:
            raise m.MultiplayerError("too_many_sessions", "End an existing multiplayer session before joining another.", 409)
        snapshot = invitation["session"]
        verdict = self.validate_for_join(snapshot)
        if verdict["problems"]:
            raise m.MultiplayerError("cannot_join", verdict["problems"][0], 409, {"problems": verdict["problems"]})
        host = self.paired_peer(invitation["host_id"])
        body = self.network.post_checked(host, "/respond", {
            "message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": session_id, "action": "accept",
            "controllers": self.controller_count(), "compat": verdict["compat"],
            "reviewed_session": replica_view(snapshot),
            "level": verdict["local"]["level"] if verdict["local"] else "none"})
        response = sanitize_session(body.get("session"))
        require_same_identity(snapshot, response)
        mine = response["players"].get(self.me)
        if response["state"] not in m.PRE_START_STATES or not mine or mine["status"] != m.P_JOINED or mine["role"] != m.GUEST_ROLE:
            raise m.MultiplayerError("invalid_membership", "The host did not confirm your membership in the lobby.", 409)
        local_game = verdict["local"]["game"] if verdict["local"] else None
        path = self.library.absolute_path(local_game) if local_game else None
        replica = self.store.adopt_replica(response, invitation["host_id"],
                                           {"role": m.GUEST_ROLE, "game_path": str(path) if path else "",
                                            "rom_name": local_game["rom_name"] if local_game else "", "pending_launch": None,
                                            "tested_config_fingerprint": verdict["tested_config_fingerprint"],
                                            # A renewed invite after leave must not keep the departed replica closed.
                                            "ended": False, "ended_at": "", "ended_reason": "", "reopenable": False,
                                            "archived": False, "launch_status": "", "launch_error": "",
                                            "game_launched": False, "game_misses": 0, "host_unreachable_since": ""})
        self.store.update_invitation(session_id, status="accepted")
        self.audit(EVENT_SESSION, "Joined a multiplayer session", f"Joined {replica['game']['title']} hosted by {replica['host_name']}.",
                   {"session_id": session_id, "host_id": invitation["host_id"]})
        return self.session_view(replica)

    def decline_invitation(self, session_id: str, actor: str = "") -> dict:
        session_id = m.clean_session_id(session_id)
        invitation = self.store.invitation(session_id)
        if invitation is None:
            raise m.MultiplayerError("invitation_not_found", "No such invitation.", 404)
        if invitation["status"] != "pending":
            return {"status": invitation["status"]}
        self.store.update_invitation(session_id, status="declined")
        try:  # best effort: the decision stands locally even if the host is unreachable
            self.network.post(self.paired_peer(invitation["host_id"]), "/respond",
                              {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": session_id, "action": "decline"}, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass
        return {"status": "declined"}

    def _guest_message(self, session: dict, action: str, **extra: Any) -> dict:
        body = self.network.post_checked(self.host_peer(session), "/respond", {
            "message_id": m.new_message_id(), "issued_at": m.now_iso(), "session_id": session["id"], "action": action, **extra})
        try:
            self.store.apply_replica(body.get("session"), session["host_id"])
        except m.MultiplayerError:
            pass
        return self.session_view(self.store.require(session["id"]))

    def leave_session(self, session_id: str, actor: str = "") -> dict:
        self.require_enabled()
        with self.session_lock(session_id):
            session = self.store.require(session_id)
            if self.local_role(session) == m.HOST_ROLE:
                return self.end_session(session_id, actor=actor)
            try:
                self._guest_message(session, "leave")
            except m.MultiplayerError:
                pass  # leaving must work even when the host is gone
            self.guest_cleanup(session_id, "You left the session.")
            return self.session_view(self.store.require(session_id))
