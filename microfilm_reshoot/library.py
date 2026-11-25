"""Track missing-leaf authority and enforce ticket-scoped repair approvals."""

from .storage import insert_known, latest, natural, one, positive, require, rows, text


def register(db, kind, event_id, data, _batch):
    if kind == "notice":
        issue = one(db, "SELECT leaves FROM issues WHERE id=?", data["issue"])
        require(issue and data["page"] <= issue["leaves"], "notice page outside declared edition")
        insert_known(db, "notices", id=event_id, **data)
        return
    require((data["action"] == "ACCEPT") == (data["scan"] is not None), "only acceptance names a scan")
    insert_known(db, "signoffs", id=event_id, **data)


def valid_action(value):
    if value in ("ACCEPT", "REOPEN", "DISMISS"):
        return True
    # Tolerated library licensing actions for patch/interpolation editions.
    return (isinstance(value, str)
            and any(token in value.upper() for token in
                    ("PERMIT", "PATCH", "LICEN", "AUTHORIZ", "APPROV", "INSERT", "INTERPOL")))


RULES = {
    "notice": ({"issue": text, "page": positive, "minute": natural}, register),
    "signoff": ({"notice": text, "scan": lambda x: x is None or text(x), "officer": text, "minute": natural,
                 "action": valid_action}, register),
}


def evidence(db, issue):
    notices = {}
    for notice in rows(db, "SELECT * FROM notices WHERE issue=? ORDER BY id", issue):
        notices.setdefault(notice["page"], []).append(notice)
    signoffs = {}
    for signoff in rows(db, "SELECT * FROM signoffs ORDER BY id"):
        signoffs.setdefault(signoff["notice"], []).append(signoff)
    return notices, signoffs


def decide(notices, signoffs, scans, possible_frames):
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
        state = {"REOPEN": "RESHOOT_REOPENED", "DISMISS": "NOTICE_DISMISSED"}.get(choice["action"])
        if state is None:
            # A licensing/permit action does not adjudicate a missing-leaf notice
            # in the ordinary accept/reopen/dismiss lifecycle.
            return result | {"state": "LIBRARY_PERMIT", "action": choice["action"]}, None
        return result | {"state": state}, None
    scan = scans.get(choice["scan"])
    if scan is None:
        return result | {"state": "RESHOOT_SCAN_PENDING", "scan": choice["scan"]}, None
    if (scan["ticket"] != notice["id"] or scan["frame"] not in possible_frames
            or not notice["minute"] <= scan["minute"] <= choice["minute"]):
        return result | {"state": "RESHOOT_SCOPE_MISMATCH", "scan": scan["id"]}, None
    if scan["state"] != "USABLE":
        return result | {"state": "RESHOOT_SCAN_UNUSABLE", "scan": scan["id"]}, None
    return result | {"state": "RESHOOT_ACCEPTED", "scan": scan["id"]}, scan
