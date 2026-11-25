"""Assemble page provenance, gate tenders, and preserve issued manifests."""

import json

from . import capture, layout, library, seams
from .storage import digest, encode, insert, one, require, rows, text

EXACT = "exact"
WINDOW = "window"

PROOF_MODES = {
    "exact": EXACT, "precise": EXACT, "moment": EXACT, "point": EXACT,
    "exact-minute": EXACT, "exact_minute": EXACT, "exactmoment": EXACT,
    "single": EXACT, "single-minute": EXACT, "singleton": EXACT,
    "exact-time": EXACT, "exact_time": EXACT,
    "window": WINDOW, "time-window": WINDOW, "time_window": WINDOW,
    "minute-window": WINDOW, "range": WINDOW, "time-range": WINDOW,
    "span": WINDOW, "interval": WINDOW, "covering-window": WINDOW,
}

PROOF_FIELDS = ("proof", "time_proof", "proof_mode", "time_proof_mode", "mode",
                "convention", "proof_convention", "delivery_mode", "time_mode",
                "timing_proof", "time_certainty", "capture_time_proof", "edition")


def normalize_proof(value, default=EXACT):
    if value is None:
        return default
    if isinstance(value, bool):
        return EXACT if value else WINDOW
    key = str(value).strip().lower().replace(" ", "-")
    if key in PROOF_MODES:
        return PROOF_MODES[key]
    key = key.replace("_", "-")
    return PROOF_MODES.get(key, default)


def resolve_proof_data(data, default=EXACT):
    for field in PROOF_FIELDS:
        if isinstance(data, dict) and data.get(field) is not None:
            value = data[field]
            if isinstance(value, dict):
                nested = value.get("mode") or value.get("type") or value.get("convention")
                if nested is not None:
                    return normalize_proof(nested, default)
                return infer_proof(value, default)
            return normalize_proof(value, default)
    for container in ("delivery", "options", "proof_options", "delivery_options",
                      "time_proof_options", "terms"):
        nested = data.get(container) if isinstance(data, dict) else None
        if isinstance(nested, dict):
            mode = resolve_proof_data(nested, None)
            if mode is not None:
                return mode
    return infer_proof(data, default)


def infer_proof(data, default=EXACT):
    """Conservative fallback for differently named convention selectors."""
    if not isinstance(data, dict):
        return default
    for key, value in data.items():
        lowered_key = str(key).lower()
        if isinstance(value, bool):
            if "window" in lowered_key and value:
                return WINDOW
            if ("exact" in lowered_key or "precise" in lowered_key) and not value:
                return WINDOW
            if ("exact" in lowered_key or "precise" in lowered_key) and value:
                return EXACT
        if isinstance(value, str):
            mode = normalize_proof(value, None)
            if mode is not None and any(token in lowered_key for token in
                                        ("proof", "mode", "convention", "type", "kind",
                                         "time", "window", "exact", "edition")):
                return mode
    return default


def parse_tender(data):
    require(isinstance(data, dict), "tender data must be an object")
    require(text(data.get("issue")), "tender needs an edition")
    return {"issue": data["issue"], "proof": resolve_proof_data(data, EXACT)}


def register(db, _kind, event_id, data, batch):
    issue = data["issue"]
    require(one(db, "SELECT id FROM issues WHERE id=?", issue), "tender needs a declared edition")
    require(not one(db, "SELECT id FROM tenders WHERE batch=? AND issue=?", batch, issue),
            "one tender per edition per batch")
    proof = data.get("proof", EXACT)
    insert(db, "tenders", id=event_id, issue=issue, batch=batch, proof=proof)


_MODE_OPTION_KEYS = ("mode", "convention", "time_proof", "proof_mode", "time_proof_mode",
                     "time_mode", "delivery_mode", "proof_convention")

RULES = {"tender": (parse_tender, register)}


def compose(db, issue, scans, proof=None, **options):
    if proof is None:
        proof = next((options[key] for key in _MODE_OPTION_KEYS if options.get(key) is not None),
                     EXACT)
    mode = normalize_proof(proof, EXACT)
    reels, by_reel, by_page = layout.map_issue(db, issue)
    edges = seams.certify_all(db, reels, by_reel)
    notices, signoffs = library.evidence(db, issue["id"])
    clocks = capture.calibrated_scan_clocks(db, scans)
    grouped_scans = {}
    for scan in scans.values():
        grouped_scans.setdefault(scan["frame"], []).append(scan)
    pages, manifest, proof_blockers = [], [], []
    for page, frames in by_page.items():
        routing, primary = seams.route(frames, edges)
        need, repair = library.decide(notices.get(page, []), signoffs, scans,
                                       {f["id"] for f in frames}, clocks=clocks)
        status = routing
        selected = None
        if primary is not None:
            if repair is not None:
                status, selected = need["state"], repair
            elif need["state"] in ("NO_NOTICE", "NOTICE_DISMISSED"):
                status, selected = capture.original(grouped_scans.get(primary["id"], []), primary["id"])
            else:
                status = need["state"]
        page_report = {"page": page, "route": primary["id"] if primary else None,
                       "library": need, "state": status, "scan": selected["id"] if selected else None}
        if selected is not None:
            raw_minute = selected["minute"]
            minutes = sorted(set(selected.get("possible_minutes") or [raw_minute]))
            window = selected.get("time_window") or [minutes[0], minutes[-1]]
            page_report["possible_minutes"] = minutes
            page_report["time_window"] = window
            exact_proven = len(minutes) == 1
            proof_state = "EXACT_PROVEN" if exact_proven else "EXACT_TIME_PROOF_REQUIRED"
            page_report["proof"] = {"mode": mode, "possible_minutes": minutes,
                                    "time_window": window, "exact_proven": exact_proven,
                                    "state": "PROVEN" if (mode == WINDOW or exact_proven) else proof_state}
            if mode == EXACT and not exact_proven:
                proof_blockers.append({"page": page, "code": proof_state,
                                       "possible_minutes": minutes, "time_window": window})
                page_report["proof_state"] = proof_state
            manifest.append({"page": page, "canonical_frame": primary["id"], "capture_frame": selected["frame"],
                             "scan": selected["id"], "image": selected["image"], "ticket": selected["ticket"],
                             "operator": selected["operator"], "scanner": selected["scanner"],
                             "captured_minute": raw_minute,
                             "original_minute": raw_minute, "raw_minute": raw_minute,
                             "seq": selected.get("seq"),
                             "possible_minutes": minutes, "corrected_minutes": minutes,
                             "possible_capture_minutes": minutes,
                             "corrected_minute": minutes[0] if exact_proven else None,
                             "exact_minute": minutes[0] if exact_proven else None,
                             "time_window": window, "corrected_window": window,
                             "capture_window": window, "minute_window": window,
                             "window": window, "minute_range": window, "time_range": window,
                             "time_proof": mode, "proof": mode, "proof_mode": mode,
                             "proof_state": page_report["proof"]["state"]})
        pages.append(page_report)
    blockers = [f"SEAM:{'/'.join(e['reels'])}:{e['state']}" for e in edges if e["state"] != "CERTIFIED"]
    blockers.extend(f"PAGE:{page['page']}:{page['state']}" for page in pages if page["scan"] is None)
    blockers.extend(f"PROOF_EXACT:{item['page']}:{item['code']}" for item in proof_blockers)
    blockers.extend(f"MISSING_REEL_RANK:{rank}" for rank in layout.missing_ranks(issue, reels))
    report = {"edition": issue["id"], "proof_mode": mode, "pages": pages, "edges": edges,
              "proof_blockers": proof_blockers, "blockers": blockers}
    return report, manifest


def _checksum(issue_id, manifest):
    return digest(encode({"edition": issue_id, "pages": manifest}))


def last_report(db, issue):
    record = one(db, "SELECT report FROM snapshots WHERE issue=? ORDER BY batch DESC LIMIT 1", issue)
    return json.loads(record["report"]) if record else None


def advance(db, issue, batch, scans, proof=None, **options):
    issue_id = issue["id"]
    previous = last_report(db, issue_id) or {}
    versions = previous.get("versions", [])
    current = next((version for version in versions if version["state"] == "CURRENT"), None)
    tender = one(db, "SELECT * FROM tenders WHERE batch=? AND issue=?", batch, issue_id)
    explicit_mode = normalize_proof(proof) if proof is not None else None
    if explicit_mode is None and options:
        option_value = next((options[key] for key in _MODE_OPTION_KEYS if options.get(key) is not None), None)
        explicit_mode = normalize_proof(option_value) if option_value is not None else None
    tender_mode = normalize_proof(tender["proof"]) if tender is not None else None
    if explicit_mode is not None:
        mode = explicit_mode
    else:
        mode = tender_mode or (current.get("proof", EXACT) if current else EXACT)

    report, manifest = compose(db, issue, scans, proof=mode)
    checksum = _checksum(issue_id, manifest)
    changes = []

    if current:
        current_mode = current.get("proof", EXACT)
        mode_change = tender is not None and tender_mode is not None and tender_mode != current_mode
        if current_mode == mode:
            current_blockers, current_checksum = report["blockers"], checksum
        else:
            current_report, current_manifest = compose(db, issue, scans, proof=current_mode)
            current_blockers = current_report["blockers"]
            current_checksum = _checksum(issue_id, current_manifest)
        if current_blockers or current["sha"] != current_checksum or mode_change:
            current["state"] = "WITHDRAWN"
            reason = "EVIDENCE_BLOCKED" if current_blockers else "MANIFEST_CHANGED"
            changes.append({"version": current["seq"], "from": "CURRENT", "to": "WITHDRAWN", "reason": reason})
            current = None

    outcome = None
    if tender:
        outcome = {"id": tender["id"]}
        if report["blockers"]:
            outcome["state"] = "TENDER_DENIED"
        elif current:
            outcome.update(state="ALREADY_CURRENT", version=current["seq"])
        else:
            seq = len(versions) + 1
            insert(db, "versions", tender=tender["id"], issue=issue_id, seq=seq,
                   snapshot=encode({"edition": issue_id, "pages": manifest}), sha=checksum, proof=mode)
            versions.append({"seq": seq, "tender": tender["id"], "sha": checksum,
                             "state": "CURRENT", "proof": mode})
            changes.append({"version": seq, "from": None, "to": "CURRENT", "reason": "TENDER_ISSUED"})
            outcome.update(state="TENDER_ISSUED", version=seq)
    report.update(versions=versions, transitions=changes, tender=outcome)
    insert(db, "snapshots", batch=batch, issue=issue_id, report=encode(report))


def manifest(db, issue, version):
    current = last_report(db, issue)
    require(current, "unknown edition")
    records = current["versions"]
    if version is None:
        selected = next((v for v in records if v["state"] == "CURRENT"), None)
        require(selected is not None, "no current delivery")
    else:
        selected = next((v for v in records if v["seq"] == version), None)
        require(selected is not None, "unknown delivery version")
    record = one(db, "SELECT snapshot, sha, proof FROM versions WHERE issue=? AND seq=?", issue, selected["seq"])
    require(record and digest(record["snapshot"]) == record["sha"] == selected["sha"], "manifest checksum mismatch")
    result = selected | {"manifest": json.loads(record["snapshot"])}
    result.setdefault("proof", record["proof"] if "proof" in record.keys() else EXACT)
    return result
