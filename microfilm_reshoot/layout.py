"""Declare the roll order and address physical film frames."""

from . import editions
from .storage import fingerprint, insert, one, positive, require, rows, text


def register(db, kind, event_id, data, _batch):
    if kind == "issue":
        require(0 < data["leaves"] <= 10000, "edition page count outside 1..10000")
        require(data["rolls"] <= 9999, "edition roll count outside 1..9999")
        caliber = editions.detect_mode(data)
        insert(db, "issues", id=event_id, leaves=data["leaves"], rolls=data["rolls"],
               caliber=caliber)
        return
    issue = one(db, "SELECT * FROM issues WHERE id = ?", data["issue"])
    require(issue is not None, "reel must reference a declared edition")
    require(data["rank"] <= issue["rolls"], "reel rank outside edition")
    frames = data["frames"]
    require(bool(frames), "reel must contain at least one frame")
    for frame in frames:
        require(isinstance(frame, dict) and set(frame) == {"slot", "page", "fingerprint"}, "invalid frame fields")
        require(positive(frame["slot"]) and frame["slot"] <= 9999, "invalid physical slot")
        require(positive(frame["page"]) and frame["page"] <= issue["leaves"], "page outside edition")
        require(fingerprint(frame["fingerprint"]), "invalid frame fingerprint")
    ordered = [frame["page"] for frame in sorted(frames, key=lambda frame: frame["slot"])]
    require(ordered == sorted(set(ordered), reverse=data["direction"] == "reverse"),
            "physical slot sequence contradicts reel direction")
    insert(db, "reels", id=event_id, issue=data["issue"], rank=data["rank"], direction=data["direction"])
    for frame in frames:
        frame_id = f"{event_id}-{frame['slot']:04d}"
        insert(db, "frames", id=frame_id, reel=event_id, **frame)


RULES = {
    "issue": ({"leaves": positive, "rolls": positive}, register),
    "reel": ({"issue": text, "rank": positive, "direction": lambda x: x in ("forward", "reverse"),
              "frames": lambda x: isinstance(x, list)}, register),
}


def map_issue(db, issue):
    reels = rows(db, "SELECT * FROM reels WHERE issue=? ORDER BY rank", issue["id"])
    by_reel = {}
    by_page = {page: [] for page in range(1, issue["leaves"] + 1)}
    for reel in reels:
        frames = rows(db, "SELECT * FROM frames WHERE reel=? ORDER BY slot", reel["id"])
        by_reel[reel["id"]] = {frame["page"]: frame for frame in frames}
        for frame in frames:
            by_page[frame["page"]].append(frame)
    return reels, by_reel, by_page


def missing_ranks(issue, reels):
    registered = {reel["rank"] for reel in reels}
    return [rank for rank in range(1, issue["rolls"] + 1) if rank not in registered]


def catalog(issue, reels, by_reel, by_page):
    return {"edition": issue["id"], "expected_rolls": issue["rolls"], "expected_pages": issue["leaves"],
            "missing_ranks": missing_ranks(issue, reels), "without_frame": [p for p, f in by_page.items() if not f],
            "rolls": [reel | {"frames": list(by_reel[reel["id"]].values())} for reel in reels]}
