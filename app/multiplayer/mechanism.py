"""Pick (and explain) the multiplayer mechanism for a game and its players.

Order of preference: the user's forced choice (must be usable, never silently
substituted), then the saved preference if usable, then the best *native*
mechanism, and remote streaming only when no native one works. Every candidate
carries the blockers and warnings that decided its fate so the UI can show why.
"""

from typing import Dict, List

from . import models as m
from .adapters.base import CONTENT_HOST_ONLY
from .matching import level_note, level_satisfies

CLASS_WEIGHT = {m.VERIFIED_COMPATIBLE: 5, m.LIKELY_COMPATIBLE: 4, m.EXPERIMENTAL: 2, m.UNKNOWN: 2,
                m.REQUIRES_CONFIGURATION: 1, m.INCOMPATIBLE: 0, m.UNSUPPORTED: 0}
MAX_LATENCY_WARN_MS = 120.0


def version_conflicts(mechanism: str, host: dict, peer: dict, label: str) -> List[str]:
    """Emulator/core version differences that make the peer's join-time validation refuse this mechanism.

    Mirrors the adapters' own guest checks (RetroArch major.minor + core version, Dolphin exact version) so the
    mismatch is known before a session is offered. Unreported versions are not treated as conflicts.
    """
    mine, theirs = host.get("emulator") or {}, (peer or {}).get("emulator") or {}
    issues: List[str] = []
    if mechanism == m.RETROARCH_NETPLAY:
        a, b = mine.get("version", ""), theirs.get("version", "")
        if a and b and a.split(".")[:2] != b.split(".")[:2]:
            issues.append(f"{label}: RetroArch versions are incompatible (host {a}, {label} {b}).")
        a, b = mine.get("core_version", ""), theirs.get("core_version", "")
        if a and b and a != b:
            issues.append(f"{label}: core versions differ (host {a}, {label} {b}).")
    elif mechanism == m.DOLPHIN_NETPLAY:
        a, b = mine.get("version", ""), theirs.get("version", "")
        if a and b and a != b:
            issues.append(f"{label}: the host and client are running incompatible Dolphin versions (host {a}, {label} {b}).")
    return issues


def evaluate_candidates(host_assessments: List[dict], peers: List[dict], *, streaming_enabled: bool = True) -> List[dict]:
    candidates = []
    for assessment in host_assessments:
        mechanism = assessment["mechanism"]
        blockers: List[str] = []
        warnings: List[str] = []
        version_issues: List[str] = []
        if assessment["classification"] in (m.UNSUPPORTED, m.INCOMPATIBLE):
            blockers.extend(assessment["reasons"][:2] or [f"{assessment['adapter_name']} cannot run this game."])
        elif assessment["classification"] == m.REQUIRES_CONFIGURATION:
            blockers.extend(assessment["reasons"][-1:] or ["This mechanism needs configuration first."])
        elif assessment["readiness"] == "not_installed":
            blockers.append(assessment["readiness_message"])
        elif assessment["readiness"] == "unsupported_build":
            blockers.append(assessment["readiness_message"])
        # Host-only gaps (a build that can join but cannot host) are not a guest problem.
        # ``host_block`` is set by the adapter on the hosting machine's own assessment.
        if assessment.get("host_capable") is False and assessment.get("host_block"):
            blockers.append(str(assessment["host_block"]))
        if mechanism == m.REMOTE_STREAMING and not streaming_enabled:
            blockers.append("Remote streaming is turned off in Multiplayer settings.")
        for peer in peers:
            label = peer.get("name") or peer.get("device_id")
            peer_assessment = (peer.get("assessments") or {}).get(mechanism)
            if peer.get("reachable") is False:
                warnings.append(f"{label} could not be reached to confirm compatibility.")
                continue
            if peer_assessment is not None and mechanism != m.REMOTE_STREAMING:
                version_issues.extend(version_conflicts(mechanism, assessment, peer_assessment, str(label)))
            if mechanism == m.REMOTE_STREAMING:
                if peer_assessment is None or peer_assessment.get("guest_ready") is False:
                    blockers.append(f"{label} does not have Moonlight installed.")
            else:
                if peer_assessment is None:
                    blockers.append(f"{label} has no {assessment['adapter_name']} support for this game.")
                elif peer_assessment["classification"] in (m.UNSUPPORTED, m.INCOMPATIBLE) or peer_assessment["readiness"] in (
                        "not_installed", "unsupported_build"):
                    detail = (peer_assessment.get("reasons") or [peer_assessment.get("readiness_message", "")])[0]
                    blockers.append(f"{label}: {detail}")
                elif peer_assessment["classification"] == m.REQUIRES_CONFIGURATION:
                    blockers.append(f"{label} needs configuration first: {(peer_assessment.get('reasons') or [''])[-1]}")
            requirement = assessment["content_match"]
            if requirement != CONTENT_HOST_ONLY:
                level = peer.get("level", "none")
                if not level_satisfies(level, requirement):
                    blockers.append(f"{label}: {level_note(level, requirement)}")
                elif level == "same_file":
                    warnings.append(f"{label}: {level_note(level, requirement)}")
            if assessment["needs_same_lan"] and peer.get("scope") not in ("lan", None):
                blockers.append(f"{label} is reachable only over {peer.get('scope')}; this mechanism relies on LAN broadcast discovery, "
                                "which Tailscale and routed links do not carry.")
            latency = peer.get("latency_ms")
            if isinstance(latency, (int, float)) and latency > MAX_LATENCY_WARN_MS:
                warnings.append(f"{label} has {latency:.0f} ms latency; expect lag.")
        if assessment["classification"] in (m.UNKNOWN, m.EXPERIMENTAL):
            warnings.append("Not yet verified for this game and configuration.")
        candidates.append({
            "mechanism": mechanism, "label": m.MECHANISM_LABELS[mechanism], "adapter": assessment["adapter"],
            "adapter_name": assessment["adapter_name"], "classification": assessment["classification"],
            "automation": assessment["automation"], "usable": not blockers, "blockers": blockers[:6], "warnings": warnings[:6],
            "max_players": assessment.get("max_players"), "assessment": assessment, "version_issues": version_issues[:4],
        })
    # A version mismatch rules a native mechanism out only when another mechanism (streaming) can serve the group;
    # otherwise it stays a warning and the guest is refused with the specific reason when it tries to join.
    fallback = any(item["usable"] and not item["version_issues"] for item in candidates)
    for item in candidates:
        issues = item.pop("version_issues")
        if not issues:
            continue
        if fallback:
            item["blockers"] = (item["blockers"] + issues)[:6]
            item["usable"] = False
        else:
            item["warnings"] = (item["warnings"] + issues)[:6]
    return candidates


def choose(candidates: List[dict], *, preferred: str = "", forced: str = "") -> dict:
    """Return ``{"mechanism", "reason", "candidates", "forced"}``; ``mechanism`` is ``""`` when nothing is usable."""
    by_name: Dict[str, dict] = {item["mechanism"]: item for item in candidates}
    public = [{key: value for key, value in item.items() if key != "assessment"} for item in candidates]
    if forced:
        item = by_name.get(forced)
        if item is None:
            raise m.MultiplayerError("mechanism_unavailable", f"{m.MECHANISM_LABELS.get(forced, forced)} does not apply to this game.", 409,
                                     {"candidates": public})
        if not item["usable"]:
            raise m.MultiplayerError("mechanism_unusable",
                                     f"{item['label']} cannot be used: {item['blockers'][0]}", 409, {"candidates": public})
        return {"mechanism": forced, "forced": True, "candidates": public,
                "reason": f"{item['label']} was chosen manually (advanced option). Review its compatibility warnings before playing."}
    # Unknown game capability is browseable/available in Advanced, not an automatic recommendation.
    usable = [item for item in candidates if item["usable"] and item["classification"] != m.UNKNOWN]
    if not usable:
        first = next((item for item in candidates if item["blockers"]), None)
        why = f" {first['label']}: {first['blockers'][0]}" if first else ""
        return {"mechanism": "", "forced": False, "candidates": public,
                "reason": "No multiplayer mechanism is usable for this game right now." + why}
    note = ""
    if preferred:
        wanted = by_name.get(preferred)
        if wanted and wanted in usable:
            return {"mechanism": preferred, "forced": False, "candidates": public,
                    "reason": f"{wanted['label']} is your preferred mechanism and passed all checks."}
        note = (f" Your preferred {m.MECHANISM_LABELS.get(preferred, preferred)} "
                f"{'does not apply to this game' if wanted is None else ('has an unknown capability for this game; choose it under Advanced' if wanted['usable'] else 'is not usable: ' + wanted['blockers'][0])}.")
    native = [item for item in usable if item["mechanism"] != m.REMOTE_STREAMING]
    pool = native or usable
    order = {name: index for index, name in enumerate(m.MECHANISM_PRIORITY)}
    best = sorted(pool, key=lambda item: (-CLASS_WEIGHT.get(item["classification"], 0), order.get(item["mechanism"], 99)))[0]
    if best["mechanism"] == m.REMOTE_STREAMING:
        reason = ("Remote streaming was selected because no native mechanism is usable for this game; "
                  "the host runs the game and the guest plays over Sunshine/Moonlight.")
        skipped = [f"{item['label']}: {item['blockers'][0]}" for item in candidates if item["blockers"] and item["mechanism"] != m.REMOTE_STREAMING]
        if skipped:
            reason += " Native options ruled out - " + "; ".join(skipped[:3]) + "."
    else:
        reason = f"{best['label']} was selected: a native mechanism rated {best['classification'].replace('_', ' ').lower()}"
        if any(item["mechanism"] == m.REMOTE_STREAMING and item["usable"] for item in usable):
            reason += "; remote streaming was not needed."
        else:
            reason += "."
    return {"mechanism": best["mechanism"], "forced": False, "candidates": public, "reason": reason + note}
