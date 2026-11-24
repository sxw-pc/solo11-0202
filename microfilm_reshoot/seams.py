"""Certify whole, oriented overlap edges with independent witness cohorts."""

import json

from .storage import encode, insert, latest, natural, require, rows, text


def register(db, _kind, event_id, data, _batch):
    require(data["left_reel"] != data["right_reel"], "seam requires two different reels")
    require(all(isinstance(p, list) and len(p) == 2 and all(text(x) for x in p) for p in data["pairs"]),
            "each overlap pair must contain two frame codes")
    insert(db, "witnesses", id=event_id, **(data | {"pairs": encode(data["pairs"])}))


RULES = {
    "witness": ({"left_reel": text, "right_reel": text, "primary_reel": text,
                 "role": lambda x: x in ("operator", "library"), "signer": text, "minute": natural,
                 "pairs": lambda x: isinstance(x, list)}, register),
}


def certify(db, left, right, left_frames, right_frames):
    shared = sorted(left_frames.keys() & right_frames.keys())
    result = {"reels": [left["id"], right["id"]], "pages": shared, "witnesses": []}
    if right["rank"] != left["rank"] + 1:
        return result | {"state": "REEL_RANK_GAP"}
    if (len(shared) < 2 or shared != list(range(shared[0], shared[-1] + 1))
            or shared[-1] != max(left_frames) or shared[0] != min(right_frames)
            or min(left_frames) >= min(right_frames) or max(left_frames) >= max(right_frames)):
        return result | {"state": "OVERLAP_NOT_ROLL_BOUNDARY"}
    expected = [[left_frames[p]["id"], right_frames[p]["id"]] for p in shared]
    if any(left_frames[p]["fingerprint"] != right_frames[p]["fingerprint"] for p in shared):
        return result | {"state": "IMAGE_MISMATCH"}
    witnesses = rows(db, "SELECT * FROM witnesses WHERE left_reel=? AND right_reel=? ORDER BY id",
                     left["id"], right["id"])
    cohorts = [latest([w for w in witnesses if w["role"] == role]) for role in ("operator", "library")]
    result["witnesses"] = [w["id"] for cohort in cohorts for w in cohort]
    if not all(cohorts):
        return result | {"state": "WITNESS_MISSING"}
    if {w["signer"] for w in cohorts[0]} & {w["signer"] for w in cohorts[1]}:
        return result | {"state": "WITNESSES_NOT_INDEPENDENT"}
    signatures = {(w["primary_reel"], w["pairs"]) for cohort in cohorts for w in cohort}
    if len(signatures) != 1:
        return result | {"state": "WITNESS_CONFLICT"}
    primary, pairs = signatures.pop()
    if primary not in result["reels"] or json.loads(pairs) != expected:
        return result | {"state": "PAIR_ORDER_OR_SCOPE_INVALID"}
    return result | {"state": "CERTIFIED", "primary": primary}


def certify_all(db, reels, by_reel):
    return [certify(db, left, right, by_reel[left["id"]], by_reel[right["id"]])
            for left, right in zip(reels, reels[1:])]


def route(frames, edges):
    if not frames:
        return "NO_FRAME", None
    if len(frames) == 1:
        return "ROUTED", frames[0]
    reel_pair = [frame["reel"] for frame in frames]
    edge = next((edge for edge in edges if edge["reels"] == reel_pair), None)
    if not edge or edge["state"] != "CERTIFIED" or frames[0]["page"] not in edge["pages"]:
        return "AMBIGUOUS_OVERLAP", None
    return "ROUTED", next(frame for frame in frames if frame["reel"] == edge["primary"])
