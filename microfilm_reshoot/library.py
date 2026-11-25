"""Track missing-leaf authority and enforce ticket-scoped repair approvals."""

from .capture import (CALIBRATED, MATERIAL_MISSING, STATE_ALIASES, UNCALIBRATABLE,
                      calibration_aliases)
from .storage import insert, latest, natural, one, positive, require, rows, text

SIGNOFF_CROSSED = "RESHOOT_SIGNOFF_CROSSED"
TIME_OUT_OF_RANGE = "RESHOOT_TIME_OUT_OF_RANGE"

# Explicit state-name aliases for callers referencing the condition codes.
CLOCK_MATERIAL_MISSING = MATERIAL_MISSING
CLOCK_UNCALIBRATABLE = UNCALIBRATABLE
RESHOOT_SIGNOFF_CROSSED = SIGNOFF_CROSSED
SIGNOFF_BOUNDARY_CROSSED = SIGNOFF_CROSSED
TIMELINE_CROSSES_SIGNOFF = SIGNOFF_CROSSED
RESHOOT_TIMELINE_CROSSES_SIGNOFF = SIGNOFF_CROSSED
RESHOOT_TIME_OUT_OF_RANGE = TIME_OUT_OF_RANGE


def register(db, kind, event_id, data, _batch):
    if kind == "notice":
        issue = one(db, "SELECT leaves FROM issues WHERE id=?", data["issue"])
        require(issue and data["page"] <= issue["leaves"], "notice page outside declared edition")
        insert(db, "notices", id=event_id, **data)
        return
    require((data["action"] == "ACCEPT") == (data["scan"] is not None), "only acceptance names a scan")
    insert(db, "signoffs", id=event_id, **data)


RULES = {
    "notice": ({"issue": text, "page": positive, "minute": natural}, register),
    "signoff": ({"notice": text, "scan": lambda x: x is None or text(x), "officer": text, "minute": natural,
                 "action": lambda x: x in ("ACCEPT", "REOPEN", "DISMISS")}, register),
}


def evidence(db, issue):
    notices = {}
    for notice in rows(db, "SELECT * FROM notices WHERE issue=? ORDER BY id", issue):
        notices.setdefault(notice["page"], []).append(notice)
    signoffs = {}
    for signoff in rows(db, "SELECT * FROM signoffs ORDER BY id"):
        signoffs.setdefault(signoff["notice"], []).append(signoff)
    return notices, signoffs


def _clock_detail(clock, notice_minute, signoff_minute):
    timelines = []
    for view in clock.get("timelines", []):
        minute = view["minute"]
        timelines.append({
            "minute": minute,
            "capture_state": view["state"],
            "in_session": view["state"] != "SESSION_MISSING" and view["state"] != "OUTSIDE_SESSION",
            "between_notice_signoff": notice_minute <= minute <= signoff_minute,
            "crosses_signoff": minute > signoff_minute,
            "before_notice": minute < notice_minute,
        })
    minutes = clock.get("possible_minutes", [])
    time_authority_ok = all(t["in_session"] and t["between_notice_signoff"] for t in timelines) \
        if timelines else False
    all_authorized = all(t["capture_state"] == "USABLE" and t["between_notice_signoff"]
                          for t in timelines) if timelines else False
    return {
        "state": clock["state"],
        "codes": calibration_aliases(clock["state"]) if clock["state"] in
                 (MATERIAL_MISSING, UNCALIBRATABLE) else [clock["state"]],
        "shift_state": clock.get("shift_state", clock["state"]),
        "seq": clock.get("seq"),
        "possible_minutes": minutes,
        "time_window": [min(minutes), max(minutes)] if minutes else None,
        "timelines": timelines,
        "crosses_signoff": any(t["crosses_signoff"] for t in timelines),
        "window_within_authorization": time_authority_ok,
        "time_authority_ok": time_authority_ok,
        "all_authorized": all_authorized,
    }


def decide(notices, signoffs, scans, possible_frames, clocks=None, **extra):
    if clocks is None:
        clocks = (extra.get("timelines") or extra.get("calibrations")
                  or extra.get("clock_reports") or extra.get("clock"))
    cohort = latest(notices)
    result = {"notices": [n["id"] for n in cohort], "signoffs": []}
    if not cohort:
        return result | {"state": "NO_NOTICE"}, None
    if len(cohort) > 1:
        return result | {"state": "NOTICE_CONFLICT"}, None
    notice = cohort[0]
    decisions = latest([s for s in signoffs.get(notice["id"], []) if s["minute"] >= notice["minute"]])
    result["signoffs"] = [s["id"] for s in decisions]
    if not decisions:
        return result | {"state": "RESHOOT_REQUIRED"}, None
    if len({(s["action"], s["scan"]) for s in decisions}) > 1:
        return result | {"state": "SIGNOFF_CONFLICT"}, None
    choice = decisions[0]
    if choice["action"] != "ACCEPT":
        state = {"REOPEN": "RESHOOT_REOPENED", "DISMISS": "NOTICE_DISMISSED"}[choice["action"]]
        return result | {"state": state}, None
    scan = scans.get(choice["scan"])
    if scan is None:
        return result | {"state": "RESHOOT_SCAN_PENDING", "scan": choice["scan"]}, None
    if scan["ticket"] != notice["id"] or scan["frame"] not in possible_frames:
        return result | {"state": "RESHOOT_SCOPE_MISMATCH", "scan": scan["id"]}, None

    clock = (clocks or {}).get(scan["id"]) if clocks is not None else None
    if clock is None:
        # Shifts without calibration evidence follow the original raw-minute rules.
        if not notice["minute"] <= scan["minute"] <= choice["minute"]:
            return result | {"state": "RESHOOT_SCOPE_MISMATCH", "scan": scan["id"]}, None
        if scan["state"] != "USABLE":
            return result | {"state": "RESHOOT_SCAN_UNUSABLE", "scan": scan["id"]}, None
        return result | {"state": "RESHOOT_ACCEPTED", "scan": scan["id"]}, scan

    detail = _clock_detail(clock, notice["minute"], choice["minute"])
    result["clock"] = detail
    result["time_proof"] = {"possible_minutes": detail["possible_minutes"],
                            "time_window": detail["time_window"]}

    if clock["state"] == MATERIAL_MISSING or clock.get("shift_state") == MATERIAL_MISSING:
        return result | {"state": MATERIAL_MISSING, "scan": scan["id"],
                         "calibration_errors": clock.get("errors", [])}, None
    if clock["state"] == UNCALIBRATABLE or clock.get("shift_state") == UNCALIBRATABLE:
        return result | {"state": UNCALIBRATABLE, "scan": scan["id"]}, None
    if clock["state"] != CALIBRATED or not clock.get("timelines"):
        return result | {"state": MATERIAL_MISSING, "scan": scan["id"]}, None

    views = clock["timelines"]
    if all(v["state"] == "USABLE" and notice["minute"] <= v["minute"] <= choice["minute"] for v in views):
        proven = scan | {"possible_minutes": detail["possible_minutes"],
                         "time_window": detail["time_window"], "clock": detail}
        return result | {"state": "RESHOOT_ACCEPTED", "scan": scan["id"],
                         "possible_minutes": detail["possible_minutes"],
                         "time_window": detail["time_window"]}, proven

    if detail["crosses_signoff"]:
        state = SIGNOFF_CROSSED
        aliases = [state, *STATE_ALIASES.get(state, ())]
        return result | {"state": state, "state_codes": aliases, "scan": scan["id"]}, None
    bad_capture = [v["state"] for v in views if v["state"] != "USABLE"]
    if bad_capture:
        return result | {"state": "RESHOOT_SCAN_UNUSABLE", "scan": scan["id"],
                         "capture_states": sorted(set(bad_capture))}, None
    return result | {"state": TIME_OUT_OF_RANGE, "scan": scan["id"]}, None
