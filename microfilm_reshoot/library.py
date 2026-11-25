"""Track missing-leaf authority and enforce ticket-scoped repair approvals."""

from .storage import encode, insert, latest, natural, one, positive, require, rows, text


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
        state = {"REOPEN": "RESHOOT_REOPENED", "DISMISS": "NOTICE_DISMISSED"}[choice["action"]]
        return result | {"state": state}, None
    scan = scans.get(choice["scan"])
    if scan is None:
        return result | {"state": "RESHOOT_SCAN_PENDING", "scan": choice["scan"]}, None
    scan_frames = scan.get("frame_codes")
    if scan_frames is None:
        scan_frames = [scan.get("frame")] if scan.get("frame") is not None else []
    frame_in_scope = any(frame in possible_frames for frame in scan_frames)
    if (scan["ticket"] != notice["id"] or not frame_in_scope
            or not notice["minute"] <= scan["minute"] <= choice["minute"]):
        return result | {"state": "RESHOOT_SCOPE_MISMATCH", "scan": scan["id"]}, None
    if scan["state"] != "USABLE":
        return result | {"state": "RESHOOT_SCAN_UNUSABLE", "scan": scan["id"]}, None
    return result | {"state": "RESHOOT_ACCEPTED", "scan": scan["id"]}, scan


ANCHOR_KINDS = (
    "anchor", "joint-anchor", "anchor-joint", "retention", "retain", "retention-anchor",
    "retention_anchor", "original-anchor", "original_anchor", "original-retention",
    "collection-anchor", "collection_anchor", "preservation-anchor", "preserve-original",
    "hold-original", "retain-original",
)


def _data_value(data, keys):
    for key in keys:
        if key in data:
            return data[key]
    return None


def register_anchor(db, kind, event_id, data, batch):
    issue_id = _data_value(data, ("issue", "edition", "issue_id"))
    if isinstance(issue_id, dict):
        issue_id = issue_id.get("id") or issue_id.get("issue")
    joint_id = _data_value(data, (
        "anchor", "retention", "retention_id", "joint", "joint_scan", "joint_id",
        "double", "double_scan", "double_id", "item", "item_id", "artifact",
        "artifact_id", "capture", "capture_id", "original", "original_id",
        "source", "source_id", "retain", "retained", "anchor_item",
    ))
    if isinstance(joint_id, dict):
        joint_id = (joint_id.get("joint") or joint_id.get("joint_id")
                    or joint_id.get("id") or joint_id.get("artifact") or joint_id.get("item"))
    minute = _data_value(data, ("minute", "anchored_minute", "selected_minute"))
    require(text(issue_id), "retention anchor needs an edition")
    require(text(joint_id), "retention anchor needs one joint artifact")
    require(minute is None or natural(minute), "invalid retention anchor minute")
    issue = one(db, "SELECT id FROM issues WHERE id=?", issue_id)
    require(issue, "retention anchor needs a declared edition")
    insert(db, "retention_anchors", id=event_id, issue=issue_id, joint=joint_id,
           minute=minute, batch=batch, payload=encode(data))


for kind in ANCHOR_KINDS:
    RULES[kind] = (None, register_anchor)


def current_anchor(db, issue):
    issue_id = issue if isinstance(issue, str) else issue["id"]
    anchors = rows(db, "SELECT * FROM retention_anchors WHERE issue=? ORDER BY rowid", issue_id)
    return anchors[-1] if anchors else None


def propagate_joint_constraint(joint, side_results):
    """Collapse per-page library facts into one whole-artifact decision.

    A joint capture is indivisible for delivery.  Any side that cannot prove
    the joint blocks the entire artifact; callers must not split off the other
    side and still describe the joint as delivering both pages.
    """
    blocked = [result for result in side_results if not result.get("joint_allowed")]
    independently_signed = [result["page"] for result in side_results
                            if result.get("state") == "RESHOOT_ACCEPTED"
                            and result.get("selected") not in (None, joint.get("id"))]
    state = "JOINT_AUTHORIZED" if not blocked else "JOINT_SIDE_CONSTRAINT"
    return {
        "joint": joint.get("id"), "state": state,
        "eligible": not blocked and joint.get("state") == "USABLE",
        "blocked_pages": [result["page"] for result in blocked],
        "independently_signed_pages": independently_signed,
        "side_results": side_results,
    }


joint_constraint = propagate_joint_constraint
propagate_constraint = propagate_joint_constraint

