"""Command line entry point."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

from . import capture, delivery, layout, ledger, seams
from .storage import Rejected, connect, one, require

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
DEMO = FIXTURES / "demo"


def demo(db):
    imports = [ledger.apply(db, path.read_bytes()) for path in sorted(DEMO.glob("*.json"))]
    require(imports, "demo fixture files missing")
    issue = "NEWS-42"
    history = ledger.history(db, issue)
    timeline = [{key: row[key] for key in ("batch", "blockers", "tender", "transitions")}
                for row in history if row["tender"] or row["transitions"]]
    return {"edition": issue, "timeline": timeline, "delivery": delivery.manifest(db, issue, None)}


def parser():
    result = argparse.ArgumentParser(description="Microfilm roll/reshoot ledger")
    result.add_argument("--db", required=True, help="SQLite path or :memory:")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("init", "demo", "audit", "scans"):
        commands.add_parser(name)
    for name in (*ledger.DOMAINS, "ingest"):
        commands.add_parser(name).add_argument("file", type=Path)
    for name in ("catalog", "seam-check", "show", "history", "manifest"):
        command = commands.add_parser(name)
        command.add_argument("issue")
        if name == "manifest":
            command.add_argument("--version", type=int)
    return result


def dispatch(db, args):
    command = args.command
    if command == "init":
        return {"state": "INITIALIZED"}
    if command == "demo":
        return demo(db)
    if command == "audit":
        return ledger.audit(db)
    if command == "scans":
        return capture.assess(db)
    if command in ("catalog", "seam-check"):
        issue = one(db, "SELECT * FROM issues WHERE id=?", args.issue)
        require(issue, "unknown edition")
        reels, by_reel, by_page = layout.map_issue(db, issue)
        if command == "catalog":
            return layout.catalog(issue, reels, by_reel, by_page)
        edges = seams.certify_all(db, reels, by_reel)
        routes = {p: seams.resolve_page(f, edges, page=p) for p, f in by_page.items()}
        return {"edition": args.issue, "edges": edges,
                "routes": {p: {"state": info["state"],
                               "frame": info["primary"]["id"] if info["primary"] else None,
                               "candidates": [f["id"] for f in info["candidates"]],
                               "triple": info["triple"],
                               "candidate_reels": info["candidate_reels"]}
                           for p, info in routes.items()}}
    if command == "show":
        report = delivery.last_report(db, args.issue)
        require(report, "unknown edition")
        return report
    if command == "history":
        return ledger.history(db, args.issue)
    if command == "manifest":
        return delivery.manifest(db, args.issue, args.version)
    return ledger.apply(db, args.file.read_bytes(), None if command == "ingest" else command)


def main():
    args = parser().parse_args()
    try:
        readonly = args.command == "audit" and args.db != ":memory:"
        if readonly:
            require(Path(args.db).is_file(), "audit database does not exist")
        with closing(connect(args.db, readonly=readonly)) as db:
            result = dispatch(db, args)
        print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
        return int(result.get("audit") == "MISMATCH")
    except (Rejected, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
