"""Adjudicate physical captures and independent image quality checks."""

from .storage import Rejected, encode, fingerprint, insert, latest, natural, require, rows, text


SCAN_FIELDS = {
    "session": text, "frame": text, "minute": natural, "fingerprint": fingerprint,
    "image": fingerprint, "grade": natural,
    "ticket": lambda x: x is None or text(x),
}


def register(db, kind, event_id, data, batch):
    if kind == "scan" and _has_joint_shape(data):
        return register_joint(db, "joint-scan", event_id, data, batch)
    if kind == "session":
        require(data["start"] < data["end"], "session must have a positive time window")
    if kind == "scan":
        require(isinstance(data, dict), "invalid scan data")
        require(set(data) == set(SCAN_FIELDS), f"{event_id}: invalid scan fields")
        for key, validator in SCAN_FIELDS.items():
            require(validator(data[key]), f"{event_id}: invalid {key}")
        require(data["grade"] <= 100, "scan grade outside 0..100")
    table = {"session": "sessions", "scan": "scans", "scan-check": "scan_checks"}[kind]
    insert(db, table, id=event_id, **data)


RULES = {
    "session": ({"operator": text, "scanner": text, "start": natural, "end": natural}, register),
    "scan": (None, register),
    "scan-check": ({"scan": text, "minute": natural, "verdict": lambda x: x in ("CLEAR", "REVOKE")}, register),
}

JOINT_KINDS = (
    "joint", "joint-scan", "joint_scan", "joint-capture", "joint_capture",
    "double", "double-scan", "double_scan", "double-capture", "double_capture",
    "double-page", "double_page", "double-page-scan", "double_page_scan",
    "double-page-capture", "double_page_capture", "dual-page-scan", "dual_page_scan",
    "spread", "spread-scan", "spread_scan", "two-page-scan", "two_page_scan",
    "two-page", "paired-scan", "pair-scan", "two-up", "two-up-scan", "facing-pages",
    "capture",
)
JOINT_CHECK_KINDS = (
    "joint-check", "joint_check", "double-check", "double_check", "artifact-check",
    "quality-check", "quality_check", "scan-qc", "qc-check", "inspection",
)

_MISSING = object()


def _first_key(data, keys, label, required=True):
    for key in keys:
        if key in data:
            return data[key]
    if required:
        raise RejectedJoint(f"joint capture requires {label}")
    return None


class RejectedJoint(Rejected):
    """A flexible joint-capture receipt is malformed."""


def _has_joint_shape(data):
    if not isinstance(data, dict):
        return False
    if data.get("joint") is True or data.get("double_page") is True or data.get("two_page") is True:
        return True
    capture_kind = data.get("capture_kind") or data.get("artifact_kind") or data.get("type")
    if isinstance(capture_kind, str) and capture_kind in ("joint", "joint-scan", "double", "double-scan", "spread"):
        return True
    if any(key in data for key in ("sides", "page_sides", "physical_sides", "left", "right", "side_a", "side_b")):
        return True
    frames = data.get("frames") or data.get("frame_codes")
    if isinstance(frames, list) and len(frames) == 2:
        return True
    return ("left_frame" in data and "right_frame" in data) or (
        "frame_left" in data and "frame_right" in data)


def _side_from_mapping(raw, position, fallback_fingerprint=None, fallback_page=None):
    require(isinstance(raw, dict), "each joint side must be an object or frame code")
    frame = _first_key(raw, ("frame", "frame_code", "code", "frame_id", "canonical_frame",
                             "frameCode", "frame_code_id"), "side frame")
    content_fingerprint = _first_key(
        raw, ("fingerprint", "content_fingerprint", "frame_fingerprint", "content_hash",
              "checksum", "sha256", "digest", "hash", "fp"),
        "side fingerprint", required=False)
    if content_fingerprint is None:
        content_fingerprint = fallback_fingerprint
    page = _first_key(raw, ("page", "logical_page", "page_number", "page_no", "number"),
                      "side page", required=False)
    if page is None:
        page = fallback_page
    side = _first_key(raw, ("side", "physical_side", "label", "position_name"), "side label", required=False)
    require(text(frame), "joint side frame must be a frame code")
    require(fingerprint(content_fingerprint), f"joint side {frame} has invalid content fingerprint")
    require(page is None or (type(page) is int and not isinstance(page, bool) and page > 0),
            f"joint side {frame} has invalid page")
    require(side is None or text(side), f"joint side {frame} has invalid side label")
    return {"position": position, "side": side, "page": page, "frame": frame,
            "fingerprint": content_fingerprint}


def normalize_joint(data):
    require(isinstance(data, dict), "joint capture data must be an object")
    session = _first_key(data, ("session", "shift", "session_id"), "session")
    minute = _first_key(data, ("minute", "captured_minute", "capture_minute", "time"), "minute")
    image = _first_key(data, ("image", "image_digest", "real_digest", "content_digest",
                              "checksum", "digest", "sha256", "hash"), "image digest")
    grade = _first_key(data, ("grade", "quality", "score"), "grade")
    ticket = _first_key(data, ("ticket", "ticket_id", "notice"), "ticket", required=False)
    require(text(session), "joint capture requires a session")
    require(natural(minute), "joint capture minute must be a natural number")
    require(fingerprint(image), "joint image must be a SHA-256 digest")
    require(natural(grade) and grade <= 100, "joint grade outside 0..100")
    require(ticket is None or text(ticket), "joint ticket must be a ticket id or null")

    raw_sides = None
    for key in ("sides", "page_sides", "physical_sides"):
        if key in data:
            raw_sides = data[key]
            break
    if isinstance(raw_sides, dict):
        raw_sides = [raw_sides.get("left", raw_sides.get("a", raw_sides.get(0))),
                     raw_sides.get("right", raw_sides.get("b", raw_sides.get(1)))]
    if raw_sides is None and ("left" in data or "right" in data):
        raw_sides = [data.get("left"), data.get("right")]
    if raw_sides is None and ("side_a" in data or "side_b" in data):
        raw_sides = [data.get("side_a"), data.get("side_b")]
    if raw_sides is None:
        frames = _first_key(data, ("frames", "frame_codes", "frame"), "side frames", required=False)
        if isinstance(frames, str):
            frames = [frames]
        if isinstance(frames, dict):
            frames = [frames.get("left", frames.get("a", frames.get(0))),
                      frames.get("right", frames.get("b", frames.get(1)))]
        if frames is not None:
            require(isinstance(frames, list) and len(frames) == 2, "joint capture must name two side frames")
            fingerprints = _first_key(data, ("fingerprints", "content_fingerprints"), "fingerprints", required=False)
            pages = _first_key(data, ("pages",), "pages", required=False)
            if not isinstance(fingerprints, list):
                fingerprints = [None, None]
            if not isinstance(pages, list):
                pages = [None, None]
            require(len(fingerprints) == 2 and len(pages) == 2, "joint capture requires two side details")
            raw_sides = []
            for i, frame in enumerate(frames):
                if isinstance(frame, dict):
                    raw_sides.append(frame)
                else:
                    raw_sides.append({"frame": frame, "fingerprint": fingerprints[i], "page": pages[i]})
    if raw_sides is None:
        left_frame = _first_key(data, ("left_frame", "frame_left", "left_frame_code", "left_code"),
                                "left frame", required=False)
        right_frame = _first_key(data, ("right_frame", "frame_right", "right_frame_code", "right_code"),
                                 "right frame", required=False)
        if left_frame is not None and right_frame is not None:
            raw_sides = [
                {"side": "left", "frame": left_frame,
                 "fingerprint": data.get("left_fingerprint") or data.get("left_content_fingerprint"),
                 "page": data.get("left_page")},
                {"side": "right", "frame": right_frame,
                 "fingerprint": data.get("right_fingerprint") or data.get("right_content_fingerprint"),
                 "page": data.get("right_page")},
            ]
    require(isinstance(raw_sides, list) and len(raw_sides) == 2,
            "joint capture must contain exactly two physical page sides")
    sides = []
    parallel_fingerprints = data.get("fingerprints") if isinstance(data.get("fingerprints"), list) else []
    parallel_pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    prefixes = ("left", "right")
    for position, raw_side in enumerate(raw_sides):
        prefix = prefixes[position] if position < 2 else f"side_{position}"
        frame_list = data.get("frames") if isinstance(data.get("frames"), list) else (
            data.get("frame_codes") if isinstance(data.get("frame_codes"), list) else [])
        if isinstance(raw_side, int) and not isinstance(raw_side, bool):
            raw_side = {
                "page": raw_side,
                "frame": frame_list[position] if position < len(frame_list) else None,
            }
        fallback_fingerprint = (
            data.get(f"{prefix}_fingerprint") or data.get(f"{prefix}_content_fingerprint")
            or data.get(f"{prefix}_content_hash") or data.get(f"{prefix}_digest")
            or data.get(f"fingerprint_{prefix}")
            or (parallel_fingerprints[position] if position < len(parallel_fingerprints) else None)
        )
        fallback_page = data.get(f"{prefix}_page") or (
            parallel_pages[position] if position < len(parallel_pages) else None)
        if isinstance(raw_side, str):
            raw_side = {"frame": raw_side}
        sides.append(_side_from_mapping(raw_side, position, fallback_fingerprint, fallback_page))
    return {"session": session, "minute": minute, "image": image, "grade": grade,
            "ticket": ticket, "sides": sides}


def register_joint(db, kind, event_id, data, _batch):
    try:
        normalized = normalize_joint(data)
    except RejectedJoint as exc:
        raise Rejected(str(exc)) from exc
    insert(db, "joint_captures", id=event_id, kind=kind, session=normalized["session"],
           minute=normalized["minute"], image=normalized["image"], grade=normalized["grade"],
           ticket=normalized["ticket"], payload=encode(data))
    for side in normalized["sides"]:
        insert(db, "joint_sides", joint=event_id, **side)


def register_artifact_check(db, kind, event_id, data, _batch):
    target = _first_key(data, ("scan", "joint", "joint_scan", "double_scan", "artifact",
                               "artifact_id", "item", "item_id", "target", "capture"),
                        "checked artifact")
    minute = _first_key(data, ("minute", "checked_minute", "verdict_minute"), "check minute")
    verdict = _first_key(data, ("verdict", "decision", "result"), "verdict")
    aliases = {"PASS": "CLEAR", "PASSED": "CLEAR", "CLEARED": "CLEAR", "APPROVE": "CLEAR",
               "REVOKED": "REVOKE", "FAIL": "REVOKE", "FAILED": "REVOKE", "BLOCK": "REVOKE"}
    verdict = aliases.get(verdict, verdict) if isinstance(verdict, str) else verdict
    require(text(target), "artifact check must name its capture")
    require(natural(minute), "check minute must be a natural number")
    require(verdict in ("CLEAR", "REVOKE"), "invalid quality verdict")
    insert(db, "scan_checks", id=event_id, scan=target, minute=minute, verdict=verdict)


for _kind in JOINT_KINDS:
    RULES[_kind] = (None, register_joint)
for _kind in JOINT_CHECK_KINDS:
    RULES[_kind] = (None, register_artifact_check)


def assess_joints(db, sessions=None, frames=None, checks=None):
    sessions = sessions if sessions is not None else {s["id"]: s for s in rows(db, "SELECT * FROM sessions")}
    frames = frames if frames is not None else {f["id"]: f for f in rows(db, "SELECT * FROM frames")}
    if checks is None:
        checks = {}
        for check in rows(db, "SELECT * FROM scan_checks ORDER BY id"):
            checks.setdefault(check["scan"], []).append(check)
    report = {}
    joints = rows(db, "SELECT * FROM joint_captures ORDER BY id")
    side_rows = rows(db, "SELECT * FROM joint_sides ORDER BY joint, position")
    sides_by_joint = {}
    for side in side_rows:
        sides_by_joint.setdefault(side["joint"], []).append(side)
    for joint in joints:
        session = sessions.get(joint["session"])
        assessed_sides = []
        side_state = "USABLE"
        pages = []
        frame_codes = []
        for raw_side in sides_by_joint.get(joint["id"], []):
            frame = frames.get(raw_side["frame"])
            page = raw_side["page"] if raw_side["page"] is not None else (frame["page"] if frame else None)
            state = "USABLE"
            if frame is None:
                state = "FRAME_UNKNOWN"
            elif raw_side["page"] is not None and raw_side["page"] != frame["page"]:
                state = "PAGE_MISMATCH"
            elif raw_side["fingerprint"] != frame["fingerprint"]:
                state = "CONTENT_MISMATCH"
            if state != "USABLE" and side_state == "USABLE":
                side_state = state
            if page is not None:
                pages.append(page)
            frame_codes.append(raw_side["frame"])
            assessed_sides.append(raw_side | {"page": page, "state": state,
                                              "canonical_frame": frame["id"] if frame else None})
        cohort = latest([c for c in checks.get(joint["id"], []) if c["minute"] >= joint["minute"]])
        verdicts = {c["verdict"] for c in cohort}
        if session is None:
            state = "SESSION_MISSING"
        elif not session["start"] <= joint["minute"] < session["end"]:
            state = "OUTSIDE_SESSION"
        elif side_state == "FRAME_UNKNOWN":
            state = "FRAME_UNKNOWN"
        elif side_state == "PAGE_MISMATCH":
            state = "PAGE_MISMATCH"
        elif side_state == "CONTENT_MISMATCH":
            state = "CONTENT_MISMATCH"
        elif joint["grade"] < 70:
            state = "GRADE_TOO_LOW"
        elif len(verdicts) > 1:
            state = "QUALITY_CONFLICT"
        elif verdicts == {"REVOKE"}:
            state = "SCAN_REVOKED"
        else:
            state = "USABLE"
        result = {key: value for key, value in joint.items() if key != "payload"}
        result.update({
            "artifact": joint["id"], "joint": joint["id"], "source_kind": "joint-scan",
            "is_joint": True, "kind": joint["kind"], "state": state,
            "checks": [c["id"] for c in cohort],
            "operator": session["operator"] if session else None,
            "scanner": session["scanner"] if session else None,
            "sides": assessed_sides, "pages": pages, "page_pair": tuple(pages) if len(pages) == 2 else None,
            "frame_codes": frame_codes, "frames": frame_codes,
            # Compatibility for callers that expect scan-like fields.  Library
            # decisions use frame_codes to validate the relevant page side.
            "frame": frame_codes[0] if frame_codes else None,
            "fingerprint": assessed_sides[0]["fingerprint"] if assessed_sides else None,
        })
        report[joint["id"]] = result
    return report


def assess_joint(db, joint_id=None, **kwargs):
    report = assess_joints(db, **kwargs)
    return report.get(joint_id) if joint_id is not None else report


joint_assessments = assess_joints
joint_captures = assess_joints
joint_capture = assess_joint
double_scan = assess_joint
joint_report = assess_joints
joint_reports = assess_joints
double_scan_report = assess_joints
double_scans = assess_joints
assess_double_scans = assess_joints
double_scan_assessments = assess_joints



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
    report.update(assess_joints(db, sessions, frames, checks))
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
