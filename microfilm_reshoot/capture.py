"""Adjudicate physical captures and independent image quality checks."""

from .storage import fingerprint, insert_known, latest, natural, require, rows, text


def register(db, kind, event_id, data, _batch):
    if kind == "session":
        require(data["start"] < data["end"], "session must have a positive time window")
    if kind == "scan":
        require(data["grade"] <= 100, "scan grade outside 0..100")
    table = {"session": "sessions", "scan": "scans", "scan-check": "scan_checks"}[kind]
    insert_known(db, table, id=event_id, **data)


RULES = {
    "session": ({"operator": text, "scanner": text, "start": natural, "end": natural}, register),
    "scan": ({"session": text, "frame": text, "minute": natural, "fingerprint": fingerprint,
              "image": fingerprint, "grade": natural, "ticket": lambda x: x is None or text(x)}, register),
    "scan-check": ({"scan": text, "minute": natural, "verdict": lambda x: x in ("CLEAR", "REVOKE")}, register),
}


def assess(db):
    sessions = {s["id"]: s for s in rows(db, "SELECT * FROM sessions")}
    frames = {f["id"]: f for f in rows(db, "SELECT * FROM frames")}
    checks = {}
    for check in rows(db, "SELECT * FROM scan_checks ORDER BY id"):
        checks.setdefault(check["scan"], []).append(check)
    report = {}
    for scan in rows(db, "SELECT * FROM scans ORDER BY id"):
        session, frame = sessions.get(scan["session"]), frames.get(scan["frame"])
        cohort = latest([c for c in checks.get(scan["id"], []) if c["minute"] >= scan["minute"]])
        verdicts = {c["verdict"] for c in cohort}
        if session is None:
            state = "SESSION_MISSING"
        elif not session["start"] <= scan["minute"] < session["end"]:
            state = "OUTSIDE_SESSION"
        elif frame is None:
            state = "FRAME_UNKNOWN"
        elif frame["fingerprint"] != scan["fingerprint"]:
            state = "CONTENT_MISMATCH"
        elif scan["grade"] < 70:
            state = "GRADE_TOO_LOW"
        elif len(verdicts) > 1:
            state = "QUALITY_CONFLICT"
        elif verdicts == {"REVOKE"}:
            state = "SCAN_REVOKED"
        else:
            state = "USABLE"
        report[scan["id"]] = scan | {"state": state, "checks": [c["id"] for c in cohort],
                                     "operator": session["operator"] if session else None,
                                     "scanner": session["scanner"] if session else None}
    return report


def original(scans, frame_code):
    candidates = [s for s in scans if s["frame"] == frame_code and s["state"] == "USABLE" and s["ticket"] is None]
    if not candidates:
        return "ORIGINAL_SCAN_MISSING", None
    best_grade = max(s["grade"] for s in candidates)
    best = [s for s in candidates if s["grade"] == best_grade]
    if len({s["image"] for s in best}) > 1:
        return "ORIGINAL_SCAN_CONFLICT", None
    return "ORIGINAL_READY", min(best, key=lambda s: (s["minute"], s["id"]))
