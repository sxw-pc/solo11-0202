"""Assemble page provenance, gate tenders, and preserve issued manifests."""

import json

from . import capture, layout, library, seams
from .storage import digest, encode, insert, one, require, rows, text


def register(db, _kind, event_id, data, batch):
    issue = data["issue"]
    require(one(db, "SELECT id FROM issues WHERE id=?", issue), "tender needs a declared edition")
    require(not one(db, "SELECT id FROM tenders WHERE batch=? AND issue=?", batch, issue),
            "one tender per edition per batch")
    insert(db, "tenders", id=event_id, issue=issue, batch=batch)


RULES = {"tender": ({"issue": text}, register)}


def compose(db, issue, scans):
    reels, by_reel, by_page = layout.map_issue(db, issue)
    edges = seams.certify_all(db, reels, by_reel)
    notices, signoffs = library.evidence(db, issue["id"])
    grouped_scans = {}
    for scan in scans.values():
        grouped_scans.setdefault(scan["frame"], []).append(scan)
    pages, manifest = [], []
    for page, frames in by_page.items():
        routing, primary = seams.route(frames, edges)
        need, repair = library.decide(notices.get(page, []), signoffs, scans, {f["id"] for f in frames})
        status = routing
        selected = None
        if primary is not None:
            if repair is not None:
                status, selected = need["state"], repair
            elif need["state"] in ("NO_NOTICE", "NOTICE_DISMISSED"):
                status, selected = capture.original(grouped_scans.get(primary["id"], []), primary["id"])
            else:
                status = need["state"]
        pages.append({"page": page, "route": primary["id"] if primary else None, "library": need,
                      "state": status, "scan": selected["id"] if selected else None})
        if selected is not None:
            manifest.append({"page": page, "canonical_frame": primary["id"], "capture_frame": selected["frame"],
                             "scan": selected["id"], "image": selected["image"], "ticket": selected["ticket"],
                             "operator": selected["operator"], "scanner": selected["scanner"],
                             "captured_minute": selected["minute"]})
    blockers = [f"SEAM:{'/'.join(e['reels'])}:{e['state']}" for e in edges if e["state"] != "CERTIFIED"]
    blockers.extend(f"PAGE:{page['page']}:{page['state']}" for page in pages if page["scan"] is None)
    blockers.extend(f"MISSING_REEL_RANK:{rank}" for rank in layout.missing_ranks(issue, reels))
    return {"edition": issue["id"], "pages": pages, "edges": edges, "blockers": blockers}, manifest


def last_report(db, issue):
    record = one(db, "SELECT report FROM snapshots WHERE issue=? ORDER BY batch DESC LIMIT 1", issue)
    return json.loads(record["report"]) if record else None


def advance(db, issue, batch, scans):
    issue_id = issue["id"]
    report, manifest = compose(db, issue, scans)
    serialized = encode({"edition": issue_id, "pages": manifest})
    checksum = digest(serialized)
    previous = last_report(db, issue_id) or {}
    versions = previous.get("versions", [])
    changes = []
    current = next((version for version in versions if version["state"] == "CURRENT"), None)
    if current and (report["blockers"] or current["sha"] != checksum):
        current["state"] = "WITHDRAWN"
        reason = "EVIDENCE_BLOCKED" if report["blockers"] else "MANIFEST_CHANGED"
        changes.append({"version": current["seq"], "from": "CURRENT", "to": "WITHDRAWN", "reason": reason})
        current = None
    tender = one(db, "SELECT id FROM tenders WHERE batch=? AND issue=?", batch, issue_id)
    outcome = None
    if tender:
        outcome = {"id": tender["id"]}
        if report["blockers"]:
            outcome["state"] = "TENDER_DENIED"
        elif current:
            outcome.update(state="ALREADY_CURRENT", version=current["seq"])
        else:
            seq = len(versions) + 1
            insert(db, "versions", tender=tender["id"], issue=issue_id, seq=seq, snapshot=serialized, sha=checksum)
            versions.append({"seq": seq, "tender": tender["id"], "sha": checksum, "state": "CURRENT"})
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
    record = one(db, "SELECT snapshot, sha FROM versions WHERE issue=? AND seq=?", issue, selected["seq"])
    require(record and digest(record["snapshot"]) == record["sha"] == selected["sha"], "manifest checksum mismatch")
    return selected | {"manifest": json.loads(record["snapshot"])}
