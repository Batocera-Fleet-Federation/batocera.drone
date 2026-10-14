"""Host-side session operations: create, invite, lobby management, and the shared session view.

The host is the only authority. Every mutation here goes through
``SessionStore.mutate`` (state machine + version bump) and is then published to
joined guests, which only ever move forward (see ``store.apply_replica``).
"""

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from typing import Any, Dict, List, Optional

from . import models as m
from .adapters.streaming import build_stream_profile
from .core import EVENT_SESSION
from .mechanism import choose, evaluate_candidates
from .store import add_event, public_view, replica_view

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
MAX_INVITEES = m.MAX_PLAYERS_LIMIT - 1


def settle_lobby(session: dict) -> None:
    """Move a lobby-phase session to the state its players imply (no-op once started)."""
    if session["state"] not in m.PRE_START_STATES:
        return
    others = [player for key, player in session["players"].items() if key != session["host_id"]]
    joined = [player for player in others if player["status"] == m.P_JOINED]
    invited = [player for player in others if player["status"] == m.P_INVITED]
    host = session["players"][session["host_id"]]
    if not others and session["state"] == m.CREATED:
        return
    if joined and host["ready"] and all(player["ready"] for player in joined):
        target = m.READY
    elif joined:
        target = m.WAITING_FOR_PLAYERS
    elif invited:
        target = m.INVITING
    else:
        target = m.WAITING_FOR_PLAYERS
    if session["state"] != target:
        session["state"] = target


def next_free_slot(session: dict) -> int:
    taken = {player["slot"] for player in session["players"].values()
             if player.get("slot") and player["status"] not in (m.P_DECLINED, m.P_LEFT, m.P_REMOVED)}
    return next(slot for slot in range(1, m.MAX_PLAYERS_LIMIT + 1) if slot not in taken)


def occupied(session: dict) -> int:
    return sum(1 for player in session["players"].values()
               if player["status"] not in (m.P_DECLINED, m.P_LEFT, m.P_REMOVED))


class SessionMixin:
    # ------------------------------------------------------------------ helpers
    def local_role(self, session: dict) -> str:
        return (session.get("local") or {}).get("role") or (m.HOST_ROLE if session.get("host_id") == self.me else m.GUEST_ROLE)

    def require_host(self, session_id: str) -> dict:
        session = self.store.require(session_id)
        if self.local_role(session) != m.HOST_ROLE:
            raise m.MultiplayerError("not_session_host", "Only the session host can do that.", 403)
        return session

    def publish(self, session_id: str) -> None:
        """Push the current snapshot to every guest that has a replica (best effort, retried by ``tick``)."""
        def push() -> None:
            session = self.store.get(session_id)
            if not session or self.local_role(session) != m.HOST_ROLE:
                return
            failed = {}
            notified = set((session.get("local") or {}).get("notified_removed") or [])
            for peer_id, player in session["players"].items():
                if peer_id == self.me or player["status"] == m.P_DECLINED:
                    continue
                if player["status"] == m.P_REMOVED:
                    if peer_id in notified:
                        continue  # told once already
                elif not player.get("joined_at"):
                    continue  # still only invited: no replica to update
                try:
                    peer = self.paired_peer(peer_id)
                    self.network.post(peer, "/update", {"message_id": m.new_message_id(), "issued_at": m.now_iso(),
                                                        "session": replica_view(session)}, timeout=4.0)
                    if player["status"] == m.P_REMOVED:
                        notified.add(peer_id)
                except Exception as error:  # noqa: BLE001 - peers drop out; tick() retries and tracks health
                    failed[peer_id] = str(error)[:120]
            self.store.set_local(session_id, publish_failed=failed, notified_removed=sorted(notified))
        self._spawn(push, f"multiplayer-publish-{session_id[:8]}")

    def _log(self, session: dict, kind: str, message: str, actor: str = "") -> None:
        self.audit(EVENT_SESSION, f"Multiplayer: {message}", message,
                   {"session_id": session["id"], "kind": kind, "game": session.get("game", {}).get("title"),
                    "mechanism": session.get("mechanism"), "actor": actor or self.me})

    def session_view(self, session: dict) -> dict:
        view = public_view(session, viewer_id=self.me)
        role = self.local_role(session)
        view["mechanism_label"] = m.MECHANISM_LABELS.get(session.get("mechanism", ""), "")
        view["my_player"] = session["players"].get(self.me)
        view["my_steps"] = self.my_steps(session, role)
        view["actions"] = self.allowed_actions(session, role)
        view["local"] = {"game_available": bool(session.get("local", {}).get("game_path")) or role == m.HOST_ROLE,
                         "launch_status": (session.get("local") or {}).get("launch_status", ""),
                         "host_unreachable": bool((session.get("local") or {}).get("host_unreachable_since")),
                         "ended": bool((session.get("local") or {}).get("ended")),
                         "ended_reason": (session.get("local") or {}).get("ended_reason", ""),
                         "launch_error": (session.get("local") or {}).get("launch_error", "")}
        return view

    def my_steps(self, session: dict, role: str) -> List[str]:
        adapter = self.adapter_map.get((session.get("emulator") or {}).get("adapter", ""))
        if adapter is None:
            return []
        try:
            return list(adapter.prepare_session(session, role, self.public_context(session, role)).get("manual_steps", []))
        except Exception:  # noqa: BLE001 - instructions are advisory
            return []

    def public_context(self, session: dict, role: str) -> dict:
        context = {"role": role, "host_address": "", "port": (session.get("local") or {}).get("port") or 0,
                   "nickname": self.my_name, "conf_path": str(self.settings.batocera_conf_file)}
        if role == m.GUEST_ROLE:
            try:
                context["host_address"] = self.network.host_address(self.paired_peer(session["host_id"]),
                                                                    self.preferred_transport())
            except m.MultiplayerError:
                pass
        return context

    def allowed_actions(self, session: dict, role: str) -> List[str]:
        state = session["state"]
        actions: List[str] = []
        if role == m.HOST_ROLE:
            if state in m.PRE_START_STATES:
                actions += ["invite", "assign_slot", "remove_player", "set_ready"]
                if state == m.READY:
                    actions.append("start")
            if state in (m.STARTING, m.DEGRADED, m.FAILED) or any(p["status"] in (m.P_FAILED, m.P_UNREACHABLE) for p in session["players"].values()):
                actions.append("retry")
            if state not in m.TERMINAL_STATES and state != m.STOPPING:
                actions.append("end")
        else:
            if state in m.PRE_START_STATES:
                actions.append("set_ready")
            if (session.get("local") or {}).get("pending_launch") and not session["local"]["pending_launch"].get("consumed"):
                actions.append("launch")
            if state not in m.TERMINAL_STATES:
                actions.append("leave")
        return actions

    # ------------------------------------------------------------ peer reports
    @staticmethod
    def host_core(assessments: List[dict]) -> str:
        """The libretro core the host will run (peers are asked whether they can run the same one)."""
        return next((a["emulator"].get("core", "") for a in assessments if a["mechanism"] == m.RETROARCH_NETPLAY and a["emulator"].get("core")), "")

    def peer_reports(self, peers: List[dict], game: dict, core: str = "") -> Dict[str, dict]:
        """Ask each peer (in parallel, bounded) whether and how well it can play ``game``."""
        if not peers:
            return {}
        query = {key: self.game_query(game)[key] for key in ("system", "rom_name", "title_key", "fingerprint", "size")}
        if core:
            query["core"] = core
        transport = self.preferred_transport()

        def ask(peer: dict) -> tuple:
            peer_id = str(peer.get("drone_id") or peer.get("device_id"))
            base = {"device_id": peer_id, "name": self.peer_name(peer), "reachable": False, "level": "none",
                    "assessments": {}, "controllers": None, "scope": self.network.peer_scope(peer, transport), "error": ""}
            try:
                body = self.network.post(peer, "/match", {"message_id": m.new_message_id(), "issued_at": m.now_iso(),
                                                          "queries": [query]}, timeout=5.0)
                result = (body.get("results") or [{}])[0]
                assessments = {item["mechanism"]: item for item in result.get("assessments") or [] if isinstance(item, dict)
                               and item.get("mechanism") in m.MECHANISMS}
                for mechanism_name, item in list(assessments.items()):
                    assessments[mechanism_name] = self._trust_peer_assessment(item)
                base.update(reachable=True, level=result.get("level") if result.get("level") in ("exact", "same_file", "title", "none") else "none",
                            assessments=assessments, controllers=body.get("controllers"))
            except Exception as error:  # noqa: BLE001 - recorded, shown, never raised
                base["error"] = str(error)[:200] or type(error).__name__
            return peer_id, base

        with ThreadPoolExecutor(max_workers=min(4, len(peers))) as pool:
            return dict(pool.map(ask, peers))

    @staticmethod
    def _trust_peer_assessment(item: dict) -> dict:
        """Reduce a peer's self-reported assessment to the fields we use, bounded and typed."""
        classification = item.get("classification")
        return {
            "mechanism": item["mechanism"],
            "adapter_name": m.clean_text(item.get("adapter_name"), 60),
            "classification": classification if classification in m.COMPATIBILITY_CLASSES else m.UNKNOWN,
            "readiness": m.clean_text(item.get("readiness"), 40),
            "readiness_message": m.clean_text(item.get("readiness_message"), 200),
            "reasons": [m.clean_text(reason, 200) for reason in (item.get("reasons") or [])[:4]] if isinstance(item.get("reasons"), list) else [],
            "emulator": {key: m.clean_text((item.get("emulator") or {}).get(key), 60) for key in ("name", "core", "version", "core_version")},
            "guest_ready": item.get("guest_ready") if isinstance(item.get("guest_ready"), bool) else None,
        }

    # ------------------------------------------------------------------ create
    def create_session(self, body: Any, actor: str = "") -> dict:
        with self._creation_lock:
            return self._create_session(body, actor)

    def _create_session(self, body: Any, actor: str = "") -> dict:
        config = self.require_enabled()
        body = m.require_dict(body)
        system = m.clean_system(body.get("system"))
        game_id = m.clean_unique_id(body.get("game_id"))
        request_id = str(body.get("client_request_id") or "")
        if request_id and not _REQUEST_ID.fullmatch(request_id):
            raise m.MultiplayerError("invalid_request_id", "client_request_id must be 1-64 letters, digits, _ or -")
        active = self.store.active()
        if request_id:
            for existing in active:
                if (existing.get("local") or {}).get("client_request_id") == request_id:
                    return self.session_view(existing)  # idempotent retry of the same request
        if len(active) >= config["general"]["max_simultaneous_sessions"]:
            raise m.MultiplayerError("too_many_sessions", "You already have the maximum number of multiplayer sessions open. End one first.", 409)
        forced = m.clean_mechanism(body.get("mechanism"), allow_empty=True)
        override = body.get("emulator_override") or {}
        core_override = ""
        if override:
            override = m.require_dict(override, "emulator_override")
            core_override = str(override.get("core") or "").strip().lower()
            if core_override and not _TOKEN.fullmatch(core_override):
                raise m.MultiplayerError("invalid_core", "emulator_override.core is not a valid core name")
        invitees = []
        for raw in (body.get("peers") or []) if isinstance(body.get("peers"), list) else []:
            peer_id = m.clean_peer_id(raw)
            if peer_id == self.me:
                raise m.MultiplayerError("invalid_peer_id", "You cannot invite yourself.")
            if peer_id not in invitees:
                invitees.append(peer_id)
        if len(invitees) > MAX_INVITEES:
            raise m.MultiplayerError("too_many_players", f"A session holds at most {m.MAX_PLAYERS_LIMIT} players.")
        game = self.library.get(system, game_id)
        if game is None:
            raise m.MultiplayerError("game_not_found", "That game is not in this Drone's library.", 404)
        path = self.library.absolute_path(game)
        if path is None:
            raise m.MultiplayerError("game_file_missing", "The game's file is missing on this machine.", 409)
        peers = [self.paired_peer(peer_id) for peer_id in invitees]

        requested = body.get("max_players")
        if requested not in (None, ""):
            requested = m.clean_int(requested, "max_players", 2, m.MAX_PLAYERS_LIMIT)
        session_id = m.new_session_id()
        created = m.now()
        session = {
            "id": session_id, "version": 1, "state": m.CREATED, "host_id": self.me, "host_name": self.my_name,
            "created_at": created.isoformat(), "updated_at": created.isoformat(),
            "expires_at": (created + timedelta(minutes=config["general"]["session_timeout_minutes"])).isoformat(),
            "game": {"system": system, "title": game["title"], "rom_name": game["rom_name"], "fingerprint": game["fingerprint"],
                     "size": game["size"], "unique_id": game["id"], "game_id": "", "players": game["players"]},
            "mechanism": "", "mechanism_reason": "", "emulator": {"adapter": "", "name": "", "core": "", "version": "", "core_version": ""},
            "max_players": 2,
            "players": {self.me: self._host_player()},
            "events": [], "error": "", "manual_steps": [], "stream_profile": {},
            "secret": m.new_secret(),
            "local": {"role": m.HOST_ROLE, "client_request_id": request_id, "game_path": str(path), "retries": {}, "core_override": core_override},
        }
        add_event(session, "created", f"{self.my_name} created the session for {game['title']}.", self.me)
        self.store.create(session)
        try:
            self.store.mutate(session_id, lambda s: s.update(state=m.DISCOVERING))
            host_assessments = self.assessor.assess(game, game_path=path, core_override=core_override)
            reports = self.peer_reports(peers, game, self.host_core(host_assessments))
            plan = self.plan_mechanism(session, host_assessments, reports, forced=forced, config=config)
        except Exception:
            self.store.delete(session_id)
            raise

        # Native deterministic netplay needs full content equality, not a sampled library hash.
        if plan["mechanism"] in (m.RETROARCH_NETPLAY, m.DOLPHIN_NETPLAY):
            try:
                digest = self.library.content_hash(game)
                self.store.mutate(session_id, lambda s: s["game"].update(sha256=digest))
                # Evidence is bound to this exact content: refresh the tested-configuration fingerprint now that it is hashed.
                refreshed = self.assessor.assess(game, game_path=path, core_override=core_override)
                plan["config_fingerprint"] = next((a["config_fingerprint"] for a in refreshed if a["mechanism"] == plan["mechanism"]),
                                                  plan["config_fingerprint"])
            except Exception:
                self.store.delete(session_id)
                raise
        cap = plan["max_players"]
        max_players = cap if requested in (None, "") else m.clean_int(requested, "max_players", 2, m.MAX_PLAYERS_LIMIT)
        max_players = max(2, min(max_players, cap, m.MAX_PLAYERS_LIMIT))
        if len(invitees) + 1 > max_players:
            self.store.delete(session_id)
            raise m.MultiplayerError("too_many_players", f"{plan['label']} supports at most {max_players} players for this game.", 409)

        def configure(s: dict) -> None:
            s["mechanism"], s["mechanism_reason"], s["emulator"] = plan["mechanism"], plan["reason"], plan["emulator"]
            s["max_players"] = max_players
            s["game"]["game_id"] = plan.get("game_id", "")
            s["stream_profile"] = plan["stream_profile"]
            s["state"] = m.CONFIGURING
            for peer in peers:
                peer_id = str(peer.get("drone_id") or peer.get("device_id"))
                s["players"][peer_id] = self._invited_player(peer, reports.get(peer_id, {}))
            add_event(s, "mechanism", plan["reason"], self.me)
            settle_lobby(s)
        session = self.store.mutate(session_id, configure)
        self.store.set_local(session_id, mechanism_forced=bool(forced), tested_config_fingerprint=plan["config_fingerprint"])
        self._log(session, "created", f"session for {game['title']} created ({plan['label']})", actor)
        for peer in peers:
            self._deliver_invitation(session_id, peer)
        self.publish(session_id)
        return self.session_view(self.store.require(session_id))

    def _host_player(self) -> dict:
        return {"device_id": self.me, "name": self.my_name, "role": m.HOST_ROLE, "status": m.P_JOINED, "ready": False, "slot": 1,
                "latency_ms": None, "route": "local", "controllers": self.controller_count(),
                "compat": {"classification": m.UNKNOWN, "reasons": []}, "invited_at": "", "joined_at": m.now_iso(), "error": ""}

    def _invited_player(self, peer: dict, report: dict) -> dict:
        return {"device_id": str(peer.get("drone_id") or peer.get("device_id")), "name": self.peer_name(peer), "role": m.GUEST_ROLE,
                "status": m.P_INVITED, "ready": False, "slot": None, "latency_ms": None, "route": report.get("scope", ""),
                "controllers": report.get("controllers") if isinstance(report.get("controllers"), int) else None,
                "compat": {"classification": m.UNKNOWN, "reasons": []}, "invited_at": m.now_iso(), "joined_at": "", "error": ""}

    def plan_mechanism(self, session: dict, host_assessments: List[dict], reports: Dict[str, dict], *, forced: str = "",
                       config: Optional[dict] = None) -> dict:
        """Choose the mechanism for ``session`` given what the host and each peer reported."""
        config = config or self.cfg.get()
        peers = list(reports.values())
        candidates = evaluate_candidates(host_assessments, peers, streaming_enabled=config["streaming"]["enabled"] and config["privacy"]["remote_controller_authorization"] != "disabled")
        decision = choose(candidates, preferred=config["general"]["preferred_mechanism"], forced=forced)
        if not decision["mechanism"]:
            raise m.MultiplayerError("no_mechanism", decision["reason"], 409, {"candidates": decision["candidates"]})
        chosen = next(item for item in candidates if item["mechanism"] == decision["mechanism"])
        assessment = chosen["assessment"]
        profile = {}
        if decision["mechanism"] == m.REMOTE_STREAMING:
            profile = build_stream_profile(config["streaming"], self.probe.hardware())
        return {"mechanism": decision["mechanism"], "reason": decision["reason"], "emulator": assessment["emulator"],
                "max_players": min(assessment.get("max_players") or 2, m.MAX_PLAYERS_LIMIT), "label": chosen["label"],
                "stream_profile": profile, "game_id": assessment.get("game_id", ""), "config_fingerprint": assessment["config_fingerprint"],
                "candidates": decision["candidates"]}

    # ------------------------------------------------------------------ invite
    def invite_peer(self, session_id: str, peer_id: str, actor: str = "") -> dict:
        self.require_enabled()
        with self.session_lock(session_id):
            session = self.require_host(session_id)
            if session["state"] not in m.PRE_START_STATES:
                raise m.MultiplayerError("session_started", "Players can only be invited before the session starts.", 409)
            peer = self.paired_peer(peer_id)
            peer_id = str(peer.get("drone_id") or peer.get("device_id"))
            existing = session["players"].get(peer_id)
            if existing and existing["status"] in (m.P_INVITED, m.P_JOINED):
                if existing["status"] == m.P_INVITED:
                    self._deliver_invitation(session_id, peer)  # idempotent: re-send
                return self.session_view(self.store.require(session_id))
            if occupied(session) >= session["max_players"]:
                raise m.MultiplayerError("session_full", f"This session already has its {session['max_players']} players.", 409)
            game = self.library.get(session["game"]["system"], session["game"]["unique_id"])
            if game is None:
                raise m.MultiplayerError("game_not_found", "The game is no longer in this Drone's library.", 409)
            self.store.mutate(session_id, lambda s: s.update(state=m.CONFIGURING))
            current = self.store.require(session_id)
            path = self.library.absolute_path(game)
            host_assessments = self.assessor.assess(game, game_path=path, core_override=(current.get("local") or {}).get("core_override", ""))
            reports = self.peer_reports([self.paired_peer(pid) for pid in current["players"] if pid != self.me
                                         and current["players"][pid]["status"] in (m.P_INVITED, m.P_JOINED)] + [peer], game,
                                        self.host_core(host_assessments))
            reports[peer_id] = reports.get(peer_id, {})
            forced = current["mechanism"] if (current.get("local") or {}).get("mechanism_forced") else ""
            try:
                plan = self.plan_mechanism(current, host_assessments, reports, forced=forced)
                joined = [p for k, p in current["players"].items() if k != self.me and p["status"] == m.P_JOINED]
                if joined and plan["mechanism"] != current["mechanism"]:
                    raise m.MultiplayerError(
                        "mechanism_change", f"Adding {self.peer_name(peer)} would change the session from "
                        f"{m.MECHANISM_LABELS[current['mechanism']]} to {m.MECHANISM_LABELS[plan['mechanism']]}, but players have already joined. "
                        "Start a new session for this group instead.", 409)
            except m.MultiplayerError:
                self.store.mutate(session_id, settle_lobby)
                raise

            def add(s: dict) -> None:
                if plan["mechanism"] != s["mechanism"]:
                    s["mechanism"], s["emulator"], s["stream_profile"] = plan["mechanism"], plan["emulator"], plan["stream_profile"]
                    s["max_players"] = min(s["max_players"], max(2, plan["max_players"]))
                    add_event(s, "mechanism", plan["reason"], self.me)
                s["mechanism_reason"] = plan["reason"]
                s["players"][peer_id] = self._invited_player(peer, reports.get(peer_id, {}))
                add_event(s, "invited", f"{self.peer_name(peer)} was invited.", self.me)
                settle_lobby(s)
            self.store.mutate(session_id, add)
            self._deliver_invitation(session_id, peer)
            self.publish(session_id)
            self._log(self.store.require(session_id), "invited", f"invited {self.peer_name(peer)}", actor)
            return self.session_view(self.store.require(session_id))

    def _deliver_invitation(self, session_id: str, peer: dict) -> None:
        peer_id = str(peer.get("drone_id") or peer.get("device_id"))
        session = self.store.require(session_id)
        ttl = self.cfg.get()["general"]["invitation_ttl_minutes"]
        expires = min(m.parse_time(session["expires_at"]) or m.now(), m.now() + timedelta(minutes=ttl))
        error = ""
        try:
            self.network.post(peer, "/invite", {"message_id": m.new_message_id(), "issued_at": m.now_iso(),
                                                "invite_expires_at": expires.isoformat(), "session": replica_view(session)}, timeout=6.0)
        except Exception as failure:  # noqa: BLE001
            error = f"Could not deliver the invitation: {str(failure)[:160] or type(failure).__name__}"

        def mark(s: dict) -> None:
            player = s["players"].get(peer_id)
            if player and player["status"] == m.P_INVITED:
                player["error"] = error
                if error:
                    add_event(s, "invite_failed", f"{player['name']}: {error}", self.me)
        self.store.mutate(session_id, mark)

    # ----------------------------------------------------------- lobby control
    def assign_slot(self, session_id: str, peer_id: str, slot: Any) -> dict:
        self.require_enabled()
        slot = m.clean_int(slot, "slot", 1, m.MAX_PLAYERS_LIMIT)
        peer_id = m.clean_peer_id(peer_id)
        with self.session_lock(session_id):
            session = self.require_host(session_id)
            if session["state"] not in m.PRE_START_STATES:
                raise m.MultiplayerError("session_started", "Player slots are fixed once the session has started.", 409)
            if slot > session["max_players"]:
                raise m.MultiplayerError("invalid_slot", f"This session has {session['max_players']} player slots.", 409)

            def apply(s: dict) -> None:
                player = s["players"].get(peer_id)
                if player is None or player["status"] in (m.P_DECLINED, m.P_LEFT, m.P_REMOVED):
                    raise m.MultiplayerError("not_a_player", "That Drone is not a player in this session.", 404)
                holder = next((p for p in s["players"].values() if p.get("slot") == slot and p["device_id"] != peer_id
                               and p["status"] not in (m.P_DECLINED, m.P_LEFT, m.P_REMOVED)), None)
                if holder:  # swap rather than refuse
                    holder["slot"] = player.get("slot")
                player["slot"] = slot
                add_event(s, "slot", f"{player['name']} is now player {slot}.", self.me)
            self.store.mutate(session_id, apply)
            self.publish(session_id)
            return self.session_view(self.store.require(session_id))

    def remove_player(self, session_id: str, peer_id: str, actor: str = "") -> dict:
        self.require_enabled()
        peer_id = m.clean_peer_id(peer_id)
        with self.session_lock(session_id):
            session = self.require_host(session_id)
            if peer_id == session["host_id"]:
                raise m.MultiplayerError("cannot_remove_host", "The host cannot be removed; end the session instead.", 409)
            if session["state"] in (m.STOPPING, m.COMPLETED):
                raise m.MultiplayerError("session_ended", "The session has ended.", 409)

            def apply(s: dict) -> None:
                player = s["players"].get(peer_id)
                if player is None or player["status"] in (m.P_REMOVED, m.P_DECLINED):
                    raise m.MultiplayerError("not_a_player", "That Drone is not a player in this session.", 404)
                player.update(status=m.P_REMOVED, ready=False, slot=None)
                add_event(s, "removed", f"{player['name']} was removed by the host.", self.me)
                if s["state"] in m.LIVE_STATES:
                    s["state"] = m.DEGRADED if any(p["status"] == m.P_RUNNING for k, p in s["players"].items() if k != s["host_id"]) else s["state"]
                else:
                    settle_lobby(s)
            session = self.store.mutate(session_id, apply)
            self.publish(session_id)
            self._log(session, "removed", f"removed {peer_id}", actor)
            return self.session_view(session)

    def set_ready(self, session_id: str, ready: Any) -> dict:
        self.require_enabled()
        ready = m.clean_bool(ready, "ready")
        with self.session_lock(session_id):
            session = self.store.require(session_id)
            if session["state"] not in m.PRE_START_STATES:
                raise m.MultiplayerError("session_started", "Readiness can only change before the session starts.", 409)
            if self.local_role(session) == m.HOST_ROLE:
                def apply(s: dict) -> None:
                    s["players"][self.me]["ready"] = ready
                    settle_lobby(s)
                session = self.store.mutate(session_id, apply)
                self.publish(session_id)
                return self.session_view(session)
            return self._guest_message(session, "ready" if ready else "not_ready")
