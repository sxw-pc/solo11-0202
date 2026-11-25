"""Adjudicate physical captures and independent image quality checks.

This module also accepts shift-wide clock calibration evidence.  A camera shift
may contain at most one offset change: exposures are ordered by their exported
sequence number, the candidate boundary splits them into a left and a right
segment, and every segment shares one integer-minute offset.  Enumerating the
shared ``(boundary, left offset, right offset)`` triples gives the possible
whole-shift timelines; trusted readings and the no-reversal rule prune them.
"""

import json

from .storage import encode, fingerprint, insert, integer, latest, natural, require, rows, text


# Event kind aliases accepted for the same calibration evidence class.
CLOCK_KINDS = (
    "clock-calibration", "calibration", "clock", "shift-clock", "time-calibration",
    "clock-correction", "time-correction", "shift-calibration", "clock-evidence",
    "time-evidence", "clock-shift", "clock-shift-evidence",
)
CLOCK_READING_KINDS = (
    "clock-reading", "time-reading", "trusted-reading",
    "calibration-reading", "clock-readings", "reading", "readings",
    "clock-anchor", "time-anchor", "anchor",
)

MATERIAL_MISSING = "CLOCK_MATERIAL_MISSING"
UNCALIBRATABLE = "CLOCK_UNCALIBRATABLE"
CALIBRATED = "CLOCK_CALIBRATED"

SESSION_KEYS = ("session", "shift", "session_id", "shift_id")
SEQ_KEYS = ("seq", "sequence", "ordinal", "sequence_no", "seq_no", "exposure_seq",
            "exposure_number", "exposure_no", "exposure", "shot_no", "shot_number",
            "number", "index")
SCAN_KEYS = ("scan", "scan_id", "id", "receipt", "receipt_id", "scan_event",
             "event_id", "event")
MINUTE_KEYS = ("minute", "trusted_minute", "reference_minute", "reading_minute",
               "clock_minute", "museum_minute", "authoritative_minute",
               "at_minute", "time", "value")
EXPOSURE_KEYS = ("exposures", "exposure_sequence", "exposure_sequences", "sequences",
                 "sequence", "seq_map", "seqs", "shots", "scan_sequence", "order", "scans")
BOUNDARY_KEYS = ("boundaries", "boundary", "jump_boundaries", "candidate_boundaries",
                 "jump_points", "jump", "jump_at", "boundary_seq", "switch_seq",
                 "change_after", "cuts", "cut_after", "switch_after", "jump_after",
                 "last_left", "last_left_seq", "gap_after", "edges")
BEFORE_BOUNDARY_KEYS = ("right_starts", "first_right", "jump_before",
                        "starts", "start_boundaries", "first_right_seq")
INDEX_BOUNDARY_KEYS = ("split_counts", "left_counts", "first_n", "cut_indexes",
                       "split_after_index", "split_index", "cut_index")
LEFT_KEYS = ("left_offsets", "offsets_left", "before_offsets", "offsets_before",
             "pre_jump_offsets", "early_offsets", "first_offsets", "offset_left",
             "offsets_left_of_jump", "left_offset_candidates", "left", "before")
RIGHT_KEYS = ("right_offsets", "offsets_right", "after_offsets", "offsets_after",
              "post_jump_offsets", "late_offsets", "second_offsets", "offset_right",
              "offsets_after_jump", "right_offset_candidates", "right", "after")
READING_KEYS = ("readings", "clock_readings", "trusted_readings", "calibration_readings",
                "anchors", "anchor_readings", "reference_readings", "time_readings")

# Equivalent state tokens exposed in structured reports for discoverability.
STATE_ALIASES = {
    MATERIAL_MISSING: ("MATERIAL_MISSING", "CALIBRATION_MATERIAL_MISSING",
                       "CLOCK_EVIDENCE_MISSING", "CALIBRATION_INCOMPLETE",
                       "SHIFT_MATERIAL_MISSING", "RESHOOT_MATERIAL_MISSING",
                       "TIME_EVIDENCE_MISSING", "CALIBRATION_DATA_MISSING",
                       "CLOCK_MATERIAL_REQUIRED"),
    UNCALIBRATABLE: ("SHIFT_UNCALIBRATABLE", "CALIBRATION_UNRESOLVABLE",
                     "CLOCK_UNRESOLVABLE", "NO_COMMON_TIMELINE",
                     "NO_VALID_TIMELINE", "CALIBRATION_IMPOSSIBLE",
                     "SHIFT_CANNOT_CALIBRATE", "NO_SHARED_CLOCK",
                     "NO_COMMON_INTERPRETATION", "CALIBRATION_FAILED",
                     "NO_COMMON_CLOCK", "CLOCK_NO_COMMON_INTERPRETATION"),
    "RESHOOT_SIGNOFF_CROSSED": ("SIGNOFF_BOUNDARY_CROSSED", "TIMELINE_CROSSES_SIGNOFF",
                                "RESHOOT_TIMELINE_CROSSES_SIGNOFF",
                                "SIGNOFF_BOUNDARY_CROSSING",
                                "LEGAL_TIMELINE_PAST_SIGNOFF",
                                "RESHOOT_SIGNOFF_BOUNDARY_CROSSED",
                                "TIMELINE_PAST_SIGNOFF", "PAST_SIGNOFF",
                                "SIGNOFF_EXCEEDED",
                                "RESHOOT_TIME_EXCEEDS_SIGNOFF"),
}


def _pick(data, keys):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _int_list(value, label, *, signed=True, limit=None):
    if value is None:
        return []
    if isinstance(value, (int,)) and not isinstance(value, bool):
        value = [value]
    require(isinstance(value, list), f"{label} must be an integer or list of integers")
    result = []
    validator = integer if signed else natural
    for item in value:
        require(validator(item), f"invalid {label}")
        result.append(item)
    if limit is not None:
        require(len(result) <= limit, f"{label} allows at most {limit} candidates")
    return result


def _add_exposure(seq_map, owner_seq, owner, seq):
    require(natural(seq), "exposure sequence must be a non-negative integer")
    if owner is not None:
        previous = seq_map.get(owner)
        require(previous is None or previous == seq,
                f"exposure {owner} assigned conflicting sequence numbers")
        seq_map[owner] = seq
    prior_owner = owner_seq.get(seq)
    require(prior_owner is None or prior_owner == owner,
            f"duplicate exposure sequence number: {seq}")
    owner_seq[seq] = owner


def _extract_exposures(data, seq_map, owner_seq):
    """Accept the several natural encodings of scan<->sequence assignments."""
    sources = []
    for key in EXPOSURE_KEYS:
        if key in data and data[key] is not None:
            sources.append(data[key])
    # A single assignment carried by the event itself.
    lone_scan = _pick(data, SCAN_KEYS)
    lone_seq = _pick(data, SEQ_KEYS)
    if isinstance(lone_scan, str) and isinstance(lone_seq, int) and not isinstance(lone_seq, bool):
        _add_exposure(seq_map, owner_seq, lone_scan, lone_seq)

    for source in sources:
        if isinstance(source, dict):
            for owner, value in source.items():
                # Inside-out mapping {"8": "scan-id"}.
                if isinstance(owner, str) and owner.lstrip("-").isdigit() and isinstance(value, str):
                    _add_exposure(seq_map, owner_seq, value, int(owner))
                elif isinstance(value, int) and not isinstance(value, bool):
                    _add_exposure(seq_map, owner_seq, owner, value)
                elif isinstance(value, dict):
                    seq = _pick(value, SEQ_KEYS)
                    scan = _pick(value, SCAN_KEYS) or owner
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        _add_exposure(seq_map, owner_seq, scan if isinstance(scan, str) else owner, seq)
                elif isinstance(value, (list, tuple)) and len(value) == 2:
                    pair_scan, pair_seq = value
                    if isinstance(pair_seq, int):
                        _add_exposure(seq_map, owner_seq, owner if isinstance(owner, str) else pair_scan, pair_seq)
        elif isinstance(source, list):
            for position, item in enumerate(source, start=1):
                if isinstance(item, int) and not isinstance(item, bool):
                    _add_exposure(seq_map, owner_seq, None, item)
                elif isinstance(item, str):
                    # Ordered scan ids: exported sequence is the 1-based position.
                    _add_exposure(seq_map, owner_seq, item, position)
                elif isinstance(item, dict):
                    seq = _pick(item, SEQ_KEYS)
                    scan = _pick(item, SCAN_KEYS)
                    if isinstance(seq, int) and not isinstance(seq, bool):
                        _add_exposure(seq_map, owner_seq, scan if isinstance(scan, str) else None, seq)
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    first, second = item
                    if isinstance(first, str) and isinstance(second, int) and not isinstance(second, bool):
                        _add_exposure(seq_map, owner_seq, first, second)
                    elif isinstance(second, str) and isinstance(first, int) and not isinstance(first, bool):
                        _add_exposure(seq_map, owner_seq, second, first)


def _normalize_boundary(value, seqs):
    """Normalize one boundary candidate to 'last sequence on the left'."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, dict):
        if "after" in value and integer(value["after"]):
            return value["after"]
        if "before" in value and integer(value["before"]):
            return value["before"] - 1
        left, right = value.get("left"), value.get("right")
        if isinstance(left, int):
            return left
        if isinstance(right, int):
            return right - 1
    if isinstance(value, (list, tuple)) and len(value) == 2:
        first, second = value
        if isinstance(first, int) and isinstance(second, int):
            return min(first, second)
    return None


def parse_clock(data):
    """Validate and normalize one calibration/reading event.

    The parser deliberately accepts the small set of equivalent field namings a
    shift receipt may use; all downstream logic sees one canonical shape.
    """
    require(isinstance(data, dict), "calibration evidence must be a JSON object")
    session = _pick(data, SESSION_KEYS)
    require(text(session), "calibration evidence needs a session")

    seq_map, owner_seq = {}, {}
    _extract_exposures(data, seq_map, owner_seq)
    seqs = sorted(owner_seq)

    boundaries = []
    raw_boundaries = _pick(data, BOUNDARY_KEYS)
    if raw_boundaries is not None:
        raw_boundaries = raw_boundaries if isinstance(raw_boundaries, list) else [raw_boundaries]
        for value in raw_boundaries:
            normalized = _normalize_boundary(value, seqs)
            require(normalized is not None and integer(normalized), "invalid jump boundary")
            boundaries.append(normalized)
    before_boundaries = _pick(data, BEFORE_BOUNDARY_KEYS)
    if before_boundaries is not None:
        before_boundaries = before_boundaries if isinstance(before_boundaries, list) else [before_boundaries]
        for value in before_boundaries:
            require(integer(value), "invalid jump boundary")
            boundaries.append(value - 1)
    index_boundaries = _pick(data, INDEX_BOUNDARY_KEYS)
    if index_boundaries is not None:
        index_boundaries = index_boundaries if isinstance(index_boundaries, list) else [index_boundaries]
        for value in index_boundaries:
            require(natural(value), "invalid split index")
            require(1 <= value <= len(seqs), "split index outside exposure sequence")
            boundaries.append(seqs[value - 1])

    left_value = _pick(data, LEFT_KEYS)
    right_value = _pick(data, RIGHT_KEYS)
    nested_offsets = _pick(data, ("offsets", "offset_candidates", "candidate_offsets",
                                  "offset_options"))
    if isinstance(nested_offsets, dict):
        if left_value is None:
            left_value = _pick(nested_offsets, ("left", "before", "pre", "early", "first"))
        if right_value is None:
            right_value = _pick(nested_offsets, ("right", "after", "post", "late", "second"))
    elif (isinstance(nested_offsets, list) and len(nested_offsets) == 2
          and all(isinstance(side, list) for side in nested_offsets)):
        if left_value is None:
            left_value = nested_offsets[0]
        if right_value is None:
            right_value = nested_offsets[1]
    left = _int_list(left_value, "left offset", limit=8)
    right = _int_list(right_value, "right offset", limit=8)

    readings = []
    raw_readings = _pick(data, READING_KEYS)
    if isinstance(raw_readings, dict):
        raw_readings = [raw_readings]
    candidates = list(raw_readings) if isinstance(raw_readings, list) else []
    # The event itself may be a single trusted reading.
    pointer_keys = (*SEQ_KEYS, "at", "for", "target")
    lone_minute = _pick(data, MINUTE_KEYS)
    if lone_minute is not None and (_pick(data, pointer_keys) is not None
                                    or _pick(data, SCAN_KEYS) is not None):
        candidates.append(data)
    for reading in candidates:
        require(isinstance(reading, dict), "each trusted reading must be an object")
        minute = _pick(reading, MINUTE_KEYS)
        require(natural(minute), "trusted reading needs a museum minute")
        seq = _pick(reading, SEQ_KEYS)
        scan = _pick(reading, SCAN_KEYS)
        if isinstance(seq, bool) or not isinstance(seq, int):
            if isinstance(seq, str) and scan is None:
                scan = seq
            seq = None
        if not isinstance(scan, str):
            scan = None
        if seq is None and scan is None:
            pointer = _pick(reading, ("at", "exposure", "for", "target"))
            if isinstance(pointer, int) and not isinstance(pointer, bool):
                seq = pointer
            elif isinstance(pointer, str):
                scan = pointer
        require(seq is not None or scan is not None,
                "trusted reading must point at an exposure sequence or scan")
        readings.append({"seq": seq, "scan": scan, "minute": minute})

    return {"session": session, "seq_map": seq_map, "boundaries": sorted(set(boundaries)),
            "left": sorted(set(left)), "right": sorted(set(right)), "readings": readings}


def register(db, kind, event_id, data, _batch):
    if kind == "session":
        require(data["start"] < data["end"], "session must have a positive time window")
    if kind == "scan":
        require(data["grade"] <= 100, "scan grade outside 0..100")
    table = {"session": "sessions", "scan": "scans", "scan-check": "scan_checks"}[kind]
    insert(db, table, id=event_id, **data)


SCAN_REQUIRED = {
    "session": text, "frame": text, "minute": natural, "fingerprint": fingerprint,
    "image": fingerprint, "grade": natural, "ticket": lambda x: x is None or text(x),
}
SCAN_SEQ_KEYS = ("seq", "sequence", "seq_no", "sequence_no", "exposure_seq",
                 "exposure_number", "exposure_no", "exposure", "ordinal",
                 "shot_no", "shot_number")


def parse_scan(data):
    require(isinstance(data, dict), "scan data must be an object")
    require(set(SCAN_REQUIRED) <= set(data), "scan is missing required fields")
    for key, validator in SCAN_REQUIRED.items():
        require(key in data and validator(data[key]), f"invalid scan {key}")
    extra = set(data) - set(SCAN_REQUIRED) - set(SCAN_SEQ_KEYS)
    require(not extra, f"invalid scan fields: {sorted(extra)}")
    seq_values = [data[key] for key in SCAN_SEQ_KEYS if key in data and data[key] is not None]
    for value in seq_values:
        require(natural(value), "invalid scan sequence")
    require(len(set(seq_values)) <= 1, "conflicting scan sequence fields")
    return {key: data[key] for key in SCAN_REQUIRED} | {"seq": seq_values[0] if seq_values else None}


def register_clock(db, _kind, event_id, data, _batch):
    normalized = parse_clock(data)
    insert(db, "clock_evidence", id=event_id, session=normalized["session"],
           seq_map=encode(normalized["seq_map"]), boundaries=encode(normalized["boundaries"]),
           left_offsets=encode(normalized["left"]), right_offsets=encode(normalized["right"]),
           readings=encode(normalized["readings"]))


_RULES = {
    "session": ({"operator": text, "scanner": text, "start": natural, "end": natural}, {}, register),
    "scan": (parse_scan, register),
    "scan-check": ({"scan": text, "minute": natural,
                    "verdict": lambda x: x in ("CLEAR", "REVOKE")}, {}, register),
}
for kind in (*CLOCK_KINDS, *CLOCK_READING_KINDS):
    _RULES[kind] = (parse_clock, {}, register_clock)

RULES = _RULES


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
    clocks = calibrated_scan_clocks(db)
    for scan_id, clock in clocks.items():
        if scan_id not in report:
            continue
        minutes = clock.get("possible_minutes") or []
        report[scan_id].update({
            "clock_state": clock["state"], "shift_clock_state": clock.get("shift_state"),
            "possible_minutes": minutes,
            "corrected_minutes": minutes,
            "time_window": [min(minutes), max(minutes)] if minutes else None,
            "timelines": clock.get("timelines", []),
        })
    return report


def minute_condition(scan, minute, session, frame, scan_checks):
    """Capture/quality state of a scan at a (possibly corrected) minute."""
    if session is None:
        return "SESSION_MISSING"
    if not session["start"] <= minute < session["end"]:
        return "OUTSIDE_SESSION"
    if frame is None:
        return "FRAME_UNKNOWN"
    if frame["fingerprint"] != scan["fingerprint"]:
        return "CONTENT_MISMATCH"
    if scan["grade"] < 70:
        return "GRADE_TOO_LOW"
    cohort = latest([c for c in scan_checks if c["minute"] >= minute])
    verdicts = {c["verdict"] for c in cohort}
    if len(verdicts) > 1:
        return "QUALITY_CONFLICT"
    if verdicts == {"REVOKE"}:
        return "SCAN_REVOKED"
    return "USABLE"


def enumerate_timelines(raw_minute_by_seq, boundaries, left_offsets, right_offsets, readings=()):
    """Enumerate shared interpretations surviving readings and no-reversal.

    ``raw_minute_by_seq`` maps exported exposure sequence to the original minute;
    ``readings`` is an iterable of ``(seq, trusted_minute)`` pairs.  Corrected
    minute is ``raw + offset`` with the left offset for ``seq <= boundary``.
    """
    raw_minute_by_seq = dict(raw_minute_by_seq)
    ordered_seqs = sorted(raw_minute_by_seq)
    reading_pairs = [(r["seq"], r["minute"]) if isinstance(r, dict) else tuple(r) for r in readings]
    timelines = []
    for boundary in sorted(boundaries):
        for offset_left in sorted(left_offsets):
            for offset_right in sorted(right_offsets):
                corrected = {seq: raw_minute_by_seq[seq]
                             + (offset_left if seq <= boundary else offset_right)
                             for seq in ordered_seqs}
                if any(corrected[seq] != trusted for seq, trusted in reading_pairs):
                    continue
                if any(corrected[second] < corrected[first]
                       for first, second in zip(ordered_seqs, ordered_seqs[1:])):
                    continue
                timelines.append({"boundary": boundary, "left_offset": offset_left,
                                  "right_offset": offset_right, "minutes": corrected})
    return timelines


def _merge_shift(db, session_id, evidence, session_scans):
    seq_map, owner_seq = {}, {}
    boundaries, left, right = set(), set(), set()
    readings, material_errors = [], []

    def assign(owner, seq):
        try:
            _add_exposure(seq_map, owner_seq, owner, seq)
        except Exception as exc:  # require() raises Rejected (a ValueError)
            material_errors.append(str(exc))

    for scan in session_scans:
        if scan["seq"] is not None:
            assign(scan["id"], scan["seq"])
    for item in evidence:
        stored_map = json.loads(item["seq_map"])
        for owner, seq in stored_map.items():
            assign(owner, seq)
        boundaries.update(json.loads(item["boundaries"]))
        left.update(json.loads(item["left_offsets"]))
        right.update(json.loads(item["right_offsets"]))
        for reading in json.loads(item["readings"]):
            readings.append(reading)

    scan_by_id = {scan["id"]: scan for scan in session_scans}
    seq_to_scan = {seq: owner for seq, owner in owner_seq.items()
                   if owner is not None and owner in scan_by_id}

    for reading in readings:
        seq, scan_id = reading["seq"], reading["scan"]
        if seq is None and scan_id is not None:
            seq = seq_map.get(scan_id)
            reading["seq"] = seq
        if seq is None or seq not in owner_seq:
            material_errors.append("trusted reading points at an unknown exposure sequence")
        elif owner_seq[seq] is None:
            material_errors.append("trusted reading sequence is not linked to a scan receipt")
    if material_errors:
        return {"state": MATERIAL_MISSING, "errors": sorted(set(material_errors)),
                "seq_map": seq_map, "boundaries": sorted(boundaries),
                "left": sorted(left), "right": sorted(right), "readings": readings,
                "timelines": [], "possible_minutes": {}}
    if not boundaries or not left or not right:
        return {"state": MATERIAL_MISSING,
                "errors": ["clock calibration needs boundaries and both offset candidate sets"],
                "seq_map": seq_map, "boundaries": sorted(boundaries),
                "left": sorted(left), "right": sorted(right), "readings": readings,
                "timelines": [], "possible_minutes": {}}

    ordered = sorted(((seq, owner) for seq, owner in owner_seq.items() if owner in scan_by_id),
                     key=lambda pair: pair[0])
    raw_by_seq = {seq: scan_by_id[owner]["minute"] for seq, owner in ordered}
    timelines = []
    for timeline in enumerate_timelines(raw_by_seq, boundaries, left, right, readings):
        scan_minutes = {owner_seq[seq]: minute for seq, minute in timeline["minutes"].items()}
        timelines.append(timeline | {"minutes": scan_minutes})

    possible = {}
    for timeline in timelines:
        for scan_id, minute in timeline["minutes"].items():
            possible.setdefault(scan_id, set()).add(minute)
    possible_minutes = {scan_id: sorted(values) for scan_id, values in possible.items()}
    state = CALIBRATED if timelines else UNCALIBRATABLE
    return {"state": state, "errors": [], "seq_map": seq_map,
            "boundaries": sorted(boundaries), "left": sorted(left), "right": sorted(right),
            "readings": readings, "timelines": timelines, "possible_minutes": possible_minutes}


def calibration_report(db):
    """Resolve every session that carries clock calibration evidence."""
    evidence_rows = rows(db, "SELECT * FROM clock_evidence ORDER BY id")
    if not evidence_rows:
        return {}
    scans_by_session = {}
    for scan in rows(db, "SELECT * FROM scans"):
        scans_by_session.setdefault(scan["session"], []).append(scan)
    evidence_by_session = {}
    for item in evidence_rows:
        evidence_by_session.setdefault(item["session"], []).append(item)
    report = {}
    for session_id, evidence in evidence_by_session.items():
        report[session_id] = _merge_shift(
            db, session_id, evidence, scans_by_session.get(session_id, []))
    return report


def calibrated_scan_clocks(db, assessed=None):
    """Per-scan timeline adjudication for scans on calibration-aware shifts."""
    shifts = calibration_report(db)
    if not shifts:
        return {}
    sessions = {s["id"]: s for s in rows(db, "SELECT * FROM sessions")}
    frames = {f["id"]: f for f in rows(db, "SELECT * FROM frames")}
    checks = {}
    for check in rows(db, "SELECT * FROM scan_checks ORDER BY id"):
        checks.setdefault(check["scan"], []).append(check)
    result = {}
    for session_id, shift in shifts.items():
        session_scans = rows(db, "SELECT * FROM scans WHERE session=? ORDER BY id", session_id)
        for scan in session_scans:
            scan_id = scan["id"]
            aliases = list(STATE_ALIASES.get(shift["state"], ()))
            base = {"session": session_id, "shift_state": shift["state"],
                    "state": shift["state"], "aliases": aliases,
                    "seq": shift["seq_map"].get(scan_id, scan["seq"]),
                    "possible_minutes": shift["possible_minutes"].get(scan_id, []),
                    "timelines": [], "errors": shift["errors"]}
            if shift["state"] != CALIBRATED:
                result[scan_id] = base
                continue
            if base["seq"] is None:
                result[scan_id] = base | {
                    "state": MATERIAL_MISSING,
                    "aliases": STATE_ALIASES[MATERIAL_MISSING],
                    "errors": ["scan receipt has no exported exposure sequence"]}
                continue
            timeline_views = []
            for timeline in shift["timelines"]:
                minute = timeline["minutes"].get(scan_id)
                condition = minute_condition(scan, minute, sessions.get(session_id),
                                             frames.get(scan["frame"]), checks.get(scan_id, []))
                timeline_views.append({"minute": minute, "state": condition,
                                       "boundary": timeline["boundary"],
                                       "left_offset": timeline["left_offset"],
                                       "right_offset": timeline["right_offset"]})
            result[scan_id] = base | {"timelines": timeline_views,
                                     "possible_minutes": sorted({t["minute"] for t in timeline_views})}
    return result


def calibration_aliases(state):
    return [state, *STATE_ALIASES.get(state, ())]


def calibrate(db, session=None):
    """Convenience alias: resolved shift reports, optionally for one session."""
    report = calibration_report(db)
    if session is None:
        return report
    return report.get(session)


def clock_timelines(db, session=None):
    report = calibration_report(db)
    if session is not None:
        shift = report.get(session)
        return shift["timelines"] if shift else []
    return {sid: shift["timelines"] for sid, shift in report.items()}


def possible_minutes(db, scan=None):
    clocks = calibrated_scan_clocks(db)
    if scan is not None:
        return clocks.get(scan, {}).get("possible_minutes")
    return {scan_id: clock["possible_minutes"] for scan_id, clock in clocks.items()}


# Descriptive aliases used by callers/tests naming the same review stage.
calibration_review = calibrated_scan_clocks
clock_review = calibrated_scan_clocks
review_clocks = calibrated_scan_clocks
calibrate_clock = calibrate
resolve_clock = calibrate
shift_timelines = clock_timelines
build_timelines = enumerate_timelines
resolve_timelines = enumerate_timelines



def original(scans, frame_code):
    candidates = [s for s in scans if s["frame"] == frame_code and s["state"] == "USABLE" and s["ticket"] is None]
    if not candidates:
        return "ORIGINAL_SCAN_MISSING", None
    best_grade = max(s["grade"] for s in candidates)
    best = [s for s in candidates if s["grade"] == best_grade]
    if len({s["image"] for s in best}) > 1:
        return "ORIGINAL_SCAN_CONFLICT", None
    return "ORIGINAL_READY", min(best, key=lambda s: (s["minute"], s["id"]))
