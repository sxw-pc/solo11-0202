"""Apply evidence batches and replay the same ledger into a clean SQLite database."""

from contextlib import closing
import json
import sqlite3

from . import capture, delivery, layout, library, seams
from .storage import TABLES, Rejected, connect, digest, encode, insert, one, require, rows, text

DOMAINS = {"layout": layout.RULES, "seam": seams.RULES, "capture": capture.RULES,
           "joint": capture.RULES, "double": capture.RULES,
           "library": library.RULES, "retention": library.RULES, "anchor": library.RULES,
           "delivery": delivery.RULES, "tender": delivery.RULES}
RULES = {kind: rule for domain in DOMAINS.values() for kind, rule in domain.items()}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def decode(raw, rules):
    try:
        payload = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError) as exc:
        raise Rejected(f"invalid JSON batch: {exc}") from exc
    require(isinstance(payload, list) and bool(payload), "batch must be a nonempty JSON array")
    parsed = []
    for event in payload:
        require(isinstance(event, dict) and set(event) == {"id", "kind", "data"}, "invalid event envelope")
        event_id, kind, data = event["id"], event["kind"], event["data"]
        require(text(event_id) and text(kind) and kind in rules, "invalid or out-of-scope event kind or id")
        rule = rules[kind]
        validators, handler = rule[0], rule[1]
        optional = rule[2] if len(rule) > 2 else frozenset()
        require(isinstance(data, dict), f"{event_id}: invalid event data")
        if validators is not None:
            fields = set(validators)
            require(set(data) <= fields and fields - optional <= set(data),
                     f"{event_id}: invalid {kind} fields")
            for key in fields & set(data):
                validator = validators[key]
                require(validator(data[key]), f"{event_id}: invalid {key}")
        parsed.append((event_id, kind, data, handler))
    return parsed


def apply(db, raw, domain=None):
    parsed = decode(raw, DOMAINS[domain] if domain else RULES)
    checksum = digest(raw)
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            existing = one(db, "SELECT seq FROM batches WHERE sha=?", checksum)
            if existing:
                return {"batch": existing["seq"], "state": "ALREADY_IMPORTED", "sha": checksum}
            batch = db.execute("INSERT INTO batches(raw, sha) VALUES (?, ?)", (raw, checksum)).lastrowid
            accepted = 0
            for event_id, kind, data, handler in parsed:
                body = encode(data)
                prev = one(db, "SELECT kind, body FROM events WHERE id=?", event_id)
                if prev:
                    require(prev == {"kind": kind, "body": body}, f"immutable event id conflict: {event_id}")
                    continue
                insert(db, "events", id=event_id, kind=kind, body=body, batch=batch)
                handler(db, kind, event_id, data, batch)
                accepted += 1
            scans = capture.assess(db)
            for issue in rows(db, "SELECT * FROM issues ORDER BY id"):
                delivery.advance(db, issue, batch, scans)
            return {"batch": batch, "state": "IMPORTED", "events": accepted, "sha": checksum}
    except (sqlite3.IntegrityError, UnicodeError, OverflowError) as exc:
        raise Rejected(f"invalid or conflicting evidence: {exc}") from exc


def history(db, issue):
    records = rows(db, "SELECT batch, report FROM snapshots WHERE issue=? ORDER BY batch", issue)
    require(records, "unknown edition")
    return [{"batch": row["batch"]} | json.loads(row["report"]) for row in records]


def audit(db):
    with db, closing(connect(":memory:")) as clean:
        db.execute("BEGIN")
        receipts = rows(db, "SELECT seq, raw, sha FROM batches ORDER BY seq")
        for receipt in receipts:
            try:
                require(digest(receipt["raw"]) == receipt["sha"], "raw SHA mismatch")
                record = apply(clean, receipt["raw"])
                require(record["batch"] == receipt["seq"] and record["state"] == "IMPORTED", "batch sequence mismatch")
            except (Rejected, TypeError, ValueError) as exc:
                return {"audit": "MISMATCH", "batch": receipt["seq"], "reason": str(exc)}
        differences = [table for table in TABLES
                       if rows(db, f"SELECT * FROM {table} ORDER BY 1, 2") != rows(clean, f"SELECT * FROM {table} ORDER BY 1, 2")]
        return {"audit": "MISMATCH" if differences else "MATCH", "batches": len(receipts),
                "events": one(clean, "SELECT count(*) AS n FROM events")["n"],
                "manifests": one(clean, "SELECT count(*) AS n FROM versions")["n"], "differences": differences}
