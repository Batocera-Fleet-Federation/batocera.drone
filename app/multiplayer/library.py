"""Read-side API: dashboard, game browser, peers, adapters, sessions list, settings.

Nothing here changes a session. Game rows reuse the Drone's ROM cache (no new
library) and its cached fingerprints; peer availability is fetched only for the
page being shown and cached briefly, so browsing never hashes or scans a whole
library.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from . import models as m
from .matching import MAX_MATCH_QUERIES, browser_status, level_satisfies

PAGE_MAX = 100
PEER_FILTER_SCAN = 200
MATCH_CACHE_TTL = 45.0
STATUSES = ("ready", "configuration", "untested", "unsupported")


class LibraryMixin:
    # ---------------------------------------------------------------- adapters
    def list_adapters(self) -> List[dict]:
        disabled = set(self.cfg.get()["emulators"]["disabled_adapters"])
        result = []
        for adapter in self.adapters:
            capabilities = adapter.capabilities()
            capabilities["enabled"] = adapter.id not in disabled
            result.append(capabilities)
        return result

    def adapter_detail(self, adapter_id: str) -> dict:
        adapter = self.adapter_map.get(str(adapter_id))
        if adapter is None:
            raise m.MultiplayerError("adapter_not_found", "No such emulator adapter.", 404)
        capabilities = adapter.capabilities()
        capabilities["enabled"] = adapter.id not in self.cfg.get()["emulators"]["disabled_adapters"]
        return capabilities

    def refresh_detection(self) -> List[dict]:
        for adapter in self.adapters:
            adapter.refresh()
        self.library.invalidate()
        return self.list_adapters()

    # ------------------------------------------------------------------- peers
    def list_peers(self) -> List[dict]:
        status = self.peer_health()
        transport = self.preferred_transport()
        peers = []
        for peer in self.paired_peers():
            peer_id = str(peer.get("drone_id") or peer.get("device_id") or "")
            if not peer_id:
                continue
            check = status.get(peer_id, {})
            peers.append({"device_id": peer_id, "name": self.peer_name(peer), "online": check.get("status") == "pass",
                          "latency_ms": check.get("latency_ms"), "last_checked": check.get("checked_at"),
                          "scope": self.network.peer_scope(peer, transport), "failure": check.get("failure_reason") if check.get("status") != "pass" else None})
        return sorted(peers, key=lambda item: (not item["online"], item["name"].lower()))

    def test_connectivity(self, peer_id: str) -> dict:
        return self.network.test_peer(self.paired_peer(peer_id))

    # -------------------------------------------------------------------- games
    def list_game_systems(self) -> List[dict]:
        rows = []
        for system in self.library.systems():
            usable = [a.id for a in self.adapters if a.supports_system(system) and a.id != "streaming" and a.detect_installed()]
            rows.append({"system": system, "adapters": usable, "native": bool(usable)})
        return rows

    def list_games(self, params: Dict[str, Any]) -> dict:
        self.require_enabled()
        system = m.clean_system(params.get("system"))
        query = m.clean_text(params.get("q"), 80).lower()
        mechanism = m.clean_mechanism(params.get("mechanism"), allow_empty=True)
        status = str(params.get("status") or "").lower()
        if status and status not in STATUSES:
            raise m.MultiplayerError("invalid_filter", f"status must be one of: {', '.join(STATUSES)}")
        min_players = m.clean_int(params.get("players"), "players", 2, m.MAX_PLAYERS_LIMIT) if params.get("players") else 0
        peer_filter = m.clean_peer_id(params["peer"]) if params.get("peer") else ""
        limit = m.clean_int(params.get("limit", 50), "limit", 1, PAGE_MAX)
        offset = m.clean_int(params.get("offset", 0), "offset", 0, 10 ** 7)
        include_peers = str(params.get("include_peers") or "").lower() in ("1", "true", "yes") or bool(peer_filter)

        rows = []
        for game in self.library.games(system):
            if query and query not in game["title"].lower() and query not in game["rom_name"].lower():
                continue
            if min_players and (game["players"] or 0) < min_players:
                continue
            assessments = self.assessor.assess(game)
            if mechanism and not any(a["mechanism"] == mechanism and a["classification"] not in (m.UNSUPPORTED, m.INCOMPATIBLE) for a in assessments):
                continue
            bucket = browser_status(assessments)
            if status and bucket != status:
                continue
            rows.append((game, assessments, bucket))

        truncated = False
        if peer_filter:
            truncated = len(rows) > PEER_FILTER_SCAN
            rows = rows[:PEER_FILTER_SCAN]
            matches = self._peer_matches([row[0] for row in rows], [self.paired_peer(peer_filter)])
            rows = [row for row in rows if self._peer_has_copy(matches.get(row[0]["id"], {}).get(peer_filter), row[1])]
        total = len(rows)
        page = rows[offset:offset + limit]
        peers_by_game: Dict[str, Dict[str, dict]] = {}
        if include_peers and page:
            online = [self.paired_peer(item["device_id"]) for item in self.list_peers() if item["online"]]
            peers_by_game = self._peer_matches([row[0] for row in page], online)
        games = [self._game_row(game, assessments, bucket, peers_by_game.get(game["id"], {})) for game, assessments, bucket in page]
        return {"system": system, "total": total, "offset": offset, "limit": limit, "truncated": truncated, "games": games,
                "filters": {"statuses": list(STATUSES), "mechanisms": list(m.MECHANISMS)}}

    @staticmethod
    def _peer_has_copy(report: Optional[dict], assessments: List[dict]) -> bool:
        if not report or not report.get("reachable"):
            return False
        usable = [a for a in assessments if a["classification"] not in (m.UNSUPPORTED, m.INCOMPATIBLE)]
        return any(level_satisfies(report.get("level", "none"), a["content_match"]) and a["content_match"] != "host_only" for a in usable)

    def _game_row(self, game: dict, assessments: List[dict], bucket: str, peers: Dict[str, dict]) -> dict:
        usable = [a for a in assessments if a["classification"] not in (m.UNSUPPORTED, m.INCOMPATIBLE)]
        best = usable[0] if usable else (assessments[0] if assessments else None)
        return {
            "id": game["id"], "system": game["system"], "title": game["title"], "rom_name": game["rom_name"],
            "fingerprint": game["fingerprint"], "players": game["players"], "image": game["image"], "status": bucket,
            "best_mechanism": best["mechanism"] if best else "", "classification": best["classification"] if best else m.UNSUPPORTED,
            "mechanisms": [{"mechanism": a["mechanism"], "label": m.MECHANISM_LABELS[a["mechanism"]], "classification": a["classification"],
                            "ready": a["ready"], "readiness": a["readiness"], "tier": a["tier"], "reasons": a["reasons"][:2],
                            "emulator": a["emulator"]} for a in assessments],
            "peers": [{"device_id": pid, "name": report.get("name", pid), "level": report.get("level", "none"),
                       "reachable": report.get("reachable", False)} for pid, report in peers.items() if report.get("reachable")],
        }

    def _peer_matches(self, games: List[dict], peers: List[dict]) -> Dict[str, Dict[str, dict]]:
        """``{game_id: {peer_id: report}}`` for these games, cached ``MATCH_CACHE_TTL`` seconds."""
        result: Dict[str, Dict[str, dict]] = {game["id"]: {} for game in games}
        if not games or not peers:
            return result
        now = time.monotonic()
        tasks = []
        for peer in peers:
            peer_id = str(peer.get("drone_id") or peer.get("device_id"))
            missing = []
            for game in games:
                hit = self._match_cache.get((peer_id, game["system"], game["rom_name"], game["fingerprint"]))
                if hit and now - hit[0] < MATCH_CACHE_TTL:
                    result[game["id"]][peer_id] = hit[1]
                else:
                    missing.append(game)
            for start in range(0, len(missing), MAX_MATCH_QUERIES):
                tasks.append((peer, peer_id, missing[start:start + MAX_MATCH_QUERIES]))

        def ask(task: tuple) -> tuple:
            peer, peer_id, chunk = task
            queries = [{key: self.game_query(game)[key] for key in ("system", "rom_name", "title_key", "fingerprint", "size")} for game in chunk]
            try:
                body = self.network.post(peer, "/match", {"message_id": m.new_message_id(), "issued_at": m.now_iso(), "queries": queries}, timeout=5.0)
                reports = body.get("results") or []
            except Exception:  # noqa: BLE001 - an unreachable peer simply has no data
                reports = []
            return peer, peer_id, chunk, reports

        if tasks:
            with ThreadPoolExecutor(max_workers=min(4, len(tasks))) as pool:
                for peer, peer_id, chunk, reports in pool.map(ask, tasks):
                    for index, game in enumerate(chunk):
                        raw = reports[index] if index < len(reports) and isinstance(reports[index], dict) else None
                        report = {"reachable": raw is not None, "name": self.peer_name(peer), "device_id": peer_id,
                                  "level": raw.get("level") if raw and raw.get("level") in ("exact", "same_file", "title", "none") else "none"}
                        result[game["id"]][peer_id] = report
                        if raw is not None:
                            self._match_cache[(peer_id, game["system"], game["rom_name"], game["fingerprint"])] = (now, report)
            if len(self._match_cache) > 5000:
                self._match_cache.clear()
        return result

    def game_detail(self, system: str, game_id: str) -> dict:
        self.require_enabled()
        game = self.library.get(system, game_id)
        if game is None:
            raise m.MultiplayerError("game_not_found", "That game is not in this Drone's library.", 404)
        path = self.library.absolute_path(game)
        assessments = self.assessor.assess(game, game_path=path)
        online = [self.paired_peer(item["device_id"]) for item in self.list_peers() if item["online"]]
        reports = self.peer_reports(online, game)
        from .mechanism import choose, evaluate_candidates
        config = self.cfg.get()
        candidates = evaluate_candidates(assessments, list(reports.values()), streaming_enabled=config["streaming"]["enabled"] and config["privacy"]["remote_controller_authorization"] != "disabled")
        decision = choose(candidates, preferred=config["general"]["preferred_mechanism"])
        row = self._game_row(game, assessments, browser_status(assessments), {})
        return {"game": row, "assessments": assessments,
                "peers": [{"device_id": pid, **{k: v for k, v in report.items() if k != "assessments"},
                           "mechanisms": {name: item["classification"] for name, item in report["assessments"].items()}}
                          for pid, report in reports.items()],
                "recommendation": {"mechanism": decision["mechanism"], "reason": decision["reason"], "candidates": decision["candidates"]},
                "image": game["image"], "size": game["size"]}

    def shared_games(self, limit: int = 20) -> List[dict]:
        """Multiplayer-capable local games that online peers also own (bounded scan for the dashboard)."""
        self.require_enabled()
        online = [self.paired_peer(item["device_id"]) for item in self.list_peers() if item["online"]]
        if not online:
            return []
        candidates = []
        for system in self.library.systems():
            if system == "":
                continue
            for game in self.library.games(system):
                if (game["players"] or 0) >= 2:
                    candidates.append(game)
                if len(candidates) >= 120:
                    break
            if len(candidates) >= 120:
                break
        matches = self._peer_matches(candidates, online)
        found = []
        for game in candidates:
            owners = [report for report in matches.get(game["id"], {}).values() if report.get("reachable") and report["level"] in ("exact", "same_file")]
            if owners:
                found.append({"id": game["id"], "system": game["system"], "title": game["title"], "players": game["players"],
                              "image": game["image"], "peers": [{"device_id": o["device_id"], "name": o["name"], "level": o["level"]} for o in owners]})
            if len(found) >= limit:
                break
        return found

    # ---------------------------------------------------------------- sessions
    def list_sessions(self, include_ended: bool = False) -> List[dict]:
        sessions = self.store.all() if include_ended else self.store.active()
        return [self.session_view(s) for s in sessions if include_ended or not (s.get("local") or {}).get("ended")]

    def get_session(self, session_id: str) -> dict:
        return self.session_view(self.store.require(session_id))

    def list_invitations(self, include_closed: bool = False) -> List[dict]:
        self._expire_invitations()
        items = self.store.invitations()
        if not include_closed:
            items = [item for item in items if item["status"] == "pending"]
        return [{key: value for key, value in item.items() if key != "session"} for item in items]

    def history(self, limit: int = 50) -> List[dict]:
        return self.store.history(limit)

    # ---------------------------------------------------------------- dashboard
    def overview(self) -> dict:
        config = self.cfg.get()
        peers = self.list_peers()
        adapters = [{"id": a["id"], "name": a["name"], "tier": a["tier"], "automation": a["automation"], "installed": a["installed"],
                     "version": a["version"], "readiness": a["readiness"], "mechanisms": a["mechanisms"], "enabled": a["enabled"]}
                    for a in self.list_adapters()]
        mechanisms = []
        for mechanism in m.MECHANISMS:
            via = [a["id"] for a in adapters if mechanism in a["mechanisms"] and a["readiness"]["status"] in ("ready", "manual_steps_required") and a["enabled"]]
            mechanisms.append({"mechanism": mechanism, "label": m.MECHANISM_LABELS[mechanism], "available": bool(via), "adapters": via})
        scopes = {}
        for peer in peers:
            scopes[peer["scope"]] = scopes.get(peer["scope"], 0) + 1
        return {
            "enabled": config["general"]["enabled"], "me": {"device_id": self.me, "name": self.my_name},
            "peers": peers, "online_peers": sum(1 for p in peers if p["online"]),
            "sessions": self.list_sessions(), "invitations": self.list_invitations(), "recent": self.history(8),
            "adapters": adapters, "mechanisms": mechanisms,
            "network": {"transport": config["networking"]["transport"], "scopes": scopes, "relay": self.network.relay.id,
                        "controllers": self.controller_count()},
            "settings": config,
        }

    # ---------------------------------------------------------------- settings
    def get_settings(self) -> dict:
        return self.cfg.get()

    def update_settings(self, patch: Any) -> dict:
        updated = self.cfg.update(patch)
        self.library.invalidate()
        return updated

    # ------------------------------------------------------------ compatibility
    def list_compatibility(self) -> List[dict]:
        return self.compat.all()

    def import_compatibility_from_peer(self, peer_id: str) -> dict:
        self.require_enabled()
        peer = self.paired_peer(peer_id)
        body = self.network.get(peer, "/compatibility", timeout=8.0)
        return self.compat.import_shared(body.get("records"), str(peer.get("drone_id") or peer_id), self.peer_name(peer))
