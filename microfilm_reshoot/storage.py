from hashlib import sha256
import json
from pathlib import Path
import sqlite3


class Rejected(ValueError):
    """An invalid batch or immutable evidence collision."""


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return sha256(data).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    seq INTEGER PRIMARY KEY, raw BLOB NOT NULL, sha TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL,
    batch INTEGER NOT NULL REFERENCES batches(seq)
);
CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY, leaves INTEGER NOT NULL, rolls INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reels (
    id TEXT PRIMARY KEY, issue TEXT NOT NULL REFERENCES issues(id), rank INTEGER NOT NULL, direction TEXT NOT NULL,
    UNIQUE(issue, rank)
);
CREATE TABLE IF NOT EXISTS frames (
    id TEXT PRIMARY KEY, reel TEXT NOT NULL REFERENCES reels(id), slot INTEGER NOT NULL,
    page INTEGER NOT NULL, fingerprint TEXT NOT NULL, UNIQUE(reel, slot), UNIQUE(reel, page)
);
CREATE TABLE IF NOT EXISTS witnesses (
    id TEXT PRIMARY KEY, left_reel TEXT NOT NULL, right_reel TEXT NOT NULL,
    role TEXT NOT NULL, signer TEXT NOT NULL, minute INTEGER NOT NULL, primary_reel TEXT NOT NULL, pairs TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, operator TEXT NOT NULL, scanner TEXT NOT NULL,
    start INTEGER NOT NULL, end INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY, session TEXT NOT NULL, frame TEXT NOT NULL, minute INTEGER NOT NULL,
    fingerprint TEXT NOT NULL, image TEXT NOT NULL, grade INTEGER NOT NULL, ticket TEXT, seq INTEGER
);
CREATE TABLE IF NOT EXISTS scan_checks (
    id TEXT PRIMARY KEY, scan TEXT NOT NULL, minute INTEGER NOT NULL, verdict TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS clock_evidence (
    id TEXT PRIMARY KEY, session TEXT NOT NULL, seq_map TEXT NOT NULL, boundaries TEXT NOT NULL,
    left_offsets TEXT NOT NULL, right_offsets TEXT NOT NULL, readings TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    id TEXT PRIMARY KEY, issue TEXT NOT NULL, page INTEGER NOT NULL, minute INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signoffs (
    id TEXT PRIMARY KEY, notice TEXT NOT NULL, scan TEXT, officer TEXT NOT NULL, minute INTEGER NOT NULL, action TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tenders (
    id TEXT PRIMARY KEY, issue TEXT NOT NULL, batch INTEGER NOT NULL REFERENCES batches(seq),
    proof TEXT NOT NULL DEFAULT 'exact'
);
CREATE TABLE IF NOT EXISTS versions (
    tender TEXT PRIMARY KEY REFERENCES tenders(id), issue TEXT NOT NULL, seq INTEGER NOT NULL,
    snapshot TEXT NOT NULL, sha TEXT NOT NULL, proof TEXT NOT NULL DEFAULT 'exact',
    UNIQUE(issue, seq)
);
CREATE TABLE IF NOT EXISTS snapshots (
    batch INTEGER NOT NULL REFERENCES batches(seq), issue TEXT NOT NULL REFERENCES issues(id),
    report TEXT NOT NULL, PRIMARY KEY(batch, issue)
);
"""


# These projections are independently compared with a clean evidence replay.
TABLES = (
    "batches", "events", "issues", "reels", "frames", "witnesses", "sessions",
    "scans", "scan_checks", "clock_evidence", "notices", "signoffs", "tenders",
    "versions", "snapshots",
)


def connect(path, *, readonly=False):
    if readonly:
        path = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(path, uri=readonly)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        db.executescript(SCHEMA)
        migrate(db)
    return db


def migrate(db):
    """Add columns introduced after the baseline without touching raw receipts."""
    columns = {table: {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
               for table in ("scans", "tenders", "versions")}
    if "seq" not in columns["scans"]:
        db.execute("ALTER TABLE scans ADD COLUMN seq INTEGER")
    if "proof" not in columns["tenders"]:
        db.execute("ALTER TABLE tenders ADD COLUMN proof TEXT NOT NULL DEFAULT 'exact'")
    if "proof" not in columns["versions"]:
        db.execute("ALTER TABLE versions ADD COLUMN proof TEXT NOT NULL DEFAULT 'exact'")


def rows(db, sql, *parameters):
    return [dict(row) for row in db.execute(sql, parameters)]


def one(db, sql, *parameters):
    result = rows(db, sql, *parameters)
    return result[0] if result else None


def insert(db, table, **fields):
    keys = ", ".join(fields)
    values = ", ".join("?" for _ in fields)
    db.execute(f"INSERT INTO {table} ({keys}) VALUES ({values})", tuple(fields.values()))


def text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def natural(value):
    return type(value) is int and 0 <= value < 2**63


def integer(value):
    return type(value) is int and -(2**63) <= value < 2**63


def positive(value):
    return natural(value) and value > 0


def fingerprint(value):
    return text(value) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def require(condition, message):
    if not condition:
        raise Rejected(message)


def latest(records):
    """Select the full last effective cohort, never an arbitrary tie winner."""
    minute = max((item["minute"] for item in records), default=None)
    return [item for item in records if item["minute"] == minute]
