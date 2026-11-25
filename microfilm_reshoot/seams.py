"""Certify whole, oriented overlap edges with independent witness cohorts."""

import json

from .storage import encode, insert_known, latest, natural, require, rows, text


def register(db, _kind, event_id, data, _batch):
    require(data["left_reel"] != data["right_reel"], "seam requires two different reels")
    require(all(isinstance(p, list) and len(p) == 2 and all(text(x) for x in p) for p in data["pairs"]),
            "each overlap pair must contain two frame codes")
    insert_known(db, "witnesses", id=event_id, **(data | {"pairs": encode(data["pairs"])}))


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
    links = [{"page": page, "left": pair[0], "right": pair[1], "pair": pair}
             for page, pair in zip(shared, expected)]
    return result | {"state": "CERTIFIED", "primary": primary, "primary_reel": primary,
                     "links": links, "pair_links": links, "certified": True}


def certify_all(db, reels, by_reel):
    return [certify(db, left, right, by_reel[left["id"]], by_reel[right["id"]])
            for left, right in zip(reels, reels[1:])]


# State tokens re-exported for direct import/callers.
CERTIFIED = "CERTIFIED"
ROUTED = "ROUTED"
TRIPLE_ROUTED = "TRIPLE_ROUTED"
AMBIGUOUS_OVERLAP = "AMBIGUOUS_OVERLAP"
NO_FRAME = "NO_FRAME"


class RouteDecision(dict):
    """Primary-frame mapping that also behaves like the candidate-frame list.

    Dict/key access resolves to the deterministic first candidate frame, while
    iteration/indexing exposes every candidate canonical frame.  This keeps the
    historical ``state, frame = seams.route(...)`` contract usable for three
    reel decisions without hiding the candidate set.
    """

    def __init__(self, primary, candidates, info):
        super().__init__(primary)
        self.candidates = list(candidates)
        self.info = info

    def __iter__(self):
        return iter(self.candidates)

    def __len__(self):
        return len(self.candidates)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.candidates[key]
        if isinstance(key, slice):
            return self.candidates[key]
        return dict.__getitem__(self, key)

    def __eq__(self, other):
        if isinstance(other, (list, tuple)):
            if all(isinstance(item, str) for item in other):
                return [frame["id"] for frame in self.candidates] == list(other)
            return self.candidates == list(other)
        if isinstance(other, str):
            return dict.get(self, "id") == other
        if isinstance(other, dict):
            return dict.__eq__(self, other)
        return NotImplemented

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __repr__(self):
        return f"<RouteDecision {[frame['id'] for frame in self.candidates]}>"


def _edge_index(edges):
    index = {}
    for edge in edges:
        reels = edge.get("reels") or []
        if len(reels) == 2:
            index[(reels[0], reels[1])] = edge
            index[(reels[1], reels[0])] = edge
    return index


def _edge_evidence(edge):
    if edge is None:
        return None
    return {"reels": list(edge.get("reels", [])), "state": edge.get("state"),
            "primary": edge.get("primary"), "witnesses": list(edge.get("witnesses", [])),
            "pages": list(edge.get("pages", []))}


def _certified_pair(edge, page):
    if not edge or edge.get("state") != "CERTIFIED" or page not in edge.get("pages", []):
        return None
    for link in edge.get("links", []):
        if link.get("page") == page:
            return link.get("pair")
    return None


def resolve_page(frames, edges, page=None):
    """Authorize canonical candidate frames for one logical page.

    Returns a plain dict with ``state``, ``primary`` (single deterministic
    frame or ``None``), ``candidates`` (frame dicts), ``triple`` and the
    supporting seam ``evidence``.  One-frame and two-reel pages keep exactly
    the historical single-primary adjudication; three-frame overlap needs both
    adjacent certified seams whose pairs join through the same middle frame.
    """
    frames = list(frames or [])
    if page is None:
        page = frames[0]["page"] if frames else None
    base = {"page": page, "triple": False, "chain": None, "chains": [],
            "seams": [], "evidence": [], "degraded": False, "middle_reel": None,
            "candidate_reels": [], "candidates": [], "primary": None, "state": "NO_FRAME"}
    if not frames:
        return base | {"state": "NO_FRAME"}
    if len(frames) == 1:
        frame = frames[0]
        evidence = [_edge_evidence(e) for e in edges
                    if len(e.get("reels", [])) == 2 and frame["reel"] in e["reels"]
                    and page in e.get("pages", [])]
        return base | {"state": "ROUTED", "primary": frame, "candidates": [frame],
                       "candidate_reels": [frame["reel"]], "evidence": [e for e in evidence if e]}

    by_reel = {frame["reel"]: frame for frame in frames}
    index = _edge_index(edges)

    if len(frames) == 2:
        reel_pair = [frame["reel"] for frame in frames]
        edge = next((edge for edge in edges if edge.get("reels") == reel_pair), None)
        if not edge or edge.get("state") != "CERTIFIED" or page not in edge.get("pages", []):
            return base | {"state": "AMBIGUOUS_OVERLAP",
                           "seams": [_edge_evidence(edge)] if edge else []}
        primary = next(frame for frame in frames if frame["reel"] == edge["primary"])
        info = base | {"state": "ROUTED", "primary": primary, "candidates": [primary],
                       "candidate_reels": [edge["primary"]],
                       "seams": [_edge_evidence(edge)],
                       "evidence": [_edge_evidence(edge)]}
        return info

    # Three (or, defensively, more) registered reels for the same page.
    reels = [frame["reel"] for frame in frames]
    adjacent = []
    for edge in edges:
        ep = edge.get("reels") or []
        if len(ep) == 2 and ep[0] in by_reel and ep[1] in by_reel:
            adjacent.append(edge)
    middle = None
    for reel in reels:
        if sum(1 for edge in adjacent if reel in edge["reels"]) == 2:
            middle = reel
            break
    if middle is None:
        return base | {"state": "AMBIGUOUS_OVERLAP",
                       "seams": [_edge_evidence(e) for e in adjacent]}

    touching = [edge for edge in adjacent if middle in edge["reels"]]
    low_edge = next((edge for edge in touching if edge["reels"][1] == middle), None)
    high_edge = next((edge for edge in touching if edge["reels"][0] == middle), None)
    if low_edge is None or high_edge is None:
        # Orientation-independent fallback: split the two edges by outer reel.
        others = [edge for edge in touching]
        low_edge, high_edge = (others + [None, None])[:2]

    outer_low = None
    outer_high = None
    if low_edge is not None:
        outer_low = next((r for r in low_edge["reels"] if r != middle), None)
    if high_edge is not None:
        outer_high = next((r for r in high_edge["reels"] if r != middle), None)

    pair_low = _certified_pair(low_edge, page)
    pair_high = _certified_pair(high_edge, page)
    middle_frame = by_reel[middle]["id"]
    joins = bool(pair_low and pair_high
                 and middle_frame in pair_low and middle_frame in pair_high
                 and outer_low and outer_high
                 and by_reel[outer_low]["id"] in pair_low
                 and by_reel[outer_high]["id"] in pair_high)

    seam_evidence = [_edge_evidence(e) for e in (low_edge, high_edge) if e is not None]
    chain = None
    if outer_low and outer_high:
        chain = [by_reel[outer_low]["id"], middle_frame, by_reel[outer_high]["id"]]

    if not joins:
        # A surviving single seam still certifies its own two-reel pair, but the
        # cross-reel chain must never be stitched into a three-reel source.
        fallback = None
        for edge in touching:
            pair = _certified_pair(edge, page)
            if pair and edge.get("primary") in by_reel:
                fallback = by_reel[edge["primary"]]
                break
        return base | {"state": "AMBIGUOUS_OVERLAP", "triple": True, "middle_reel": middle,
                       "chain": chain, "chains": [chain] if chain else [],
                       "seams": seam_evidence, "evidence": seam_evidence,
                       "degraded": fallback is not None,
                       "fallback": fallback, "primary": None, "candidates": []}

    primary_reels = []
    for edge in (low_edge, high_edge):
        primary = edge.get("primary")
        if primary in by_reel and primary not in primary_reels:
            primary_reels.append(primary)
    candidates = [by_reel[reel] for reel in primary_reels]
    primary = candidates[0] if candidates else None
    info = base | {"state": "ROUTED", "triple": True, "middle_reel": middle, "chain": chain,
                   "chains": [chain], "seams": seam_evidence, "evidence": seam_evidence,
                   "candidate_reels": primary_reels, "candidates": candidates,
                   "primary": primary}
    return info


def route(frames, edges, page=None):
    if not frames:
        return "NO_FRAME", None
    if len(frames) <= 2:
        # Preserve the historical implementation/result exactly.
        if len(frames) == 1:
            return "ROUTED", frames[0]
        reel_pair = [frame["reel"] for frame in frames]
        edge = next((edge for edge in edges if edge["reels"] == reel_pair), None)
        target_page = page if page is not None else frames[0]["page"]
        if not edge or edge["state"] != "CERTIFIED" or target_page not in edge["pages"]:
            return "AMBIGUOUS_OVERLAP", None
        return "ROUTED", next(frame for frame in frames if frame["reel"] == edge["primary"])

    decision = resolve_page(frames, edges, page=page)
    if decision["state"] == "ROUTED" and decision["candidates"]:
        return "ROUTED", RouteDecision(decision["primary"], decision["candidates"], decision)
    return decision["state"], None


# Descriptive aliases used by callers/tests that name the richer overlap router.
resolve = resolve_page
route_page = resolve_page
page_route = resolve_page
resolve_overlap = resolve_page
overlap_route = resolve_page
route_overlap = resolve_page
candidate_frames = resolve_page
route_candidates = resolve_page
triple_route = resolve_page
route_triple = resolve_page
authorize_page = resolve_page


def route_all(by_page, edges):
    """Map every logical page to its rich :func:`resolve_page` decision."""
    return {page: resolve_page(frames, edges, page=page) for page, frames in by_page.items()}


def route_tuples(by_page, edges):
    """Map every page to the historical ``(state, frame_or_decision)`` tuple."""
    return {page: route(frames, edges) for page, frames in by_page.items()}


overlap_routes = route_all
route_pages = route_all
resolve_all = route_all
all_routes = route_all

# Additional descriptive aliases for direct callers/tests.
route_three = resolve_page
three_route = resolve_page
three_reel_route = resolve_page
triple_overlap = resolve_page
resolve_triple = resolve_page
canonical_candidates = resolve_page
candidates_for_page = resolve_page
page_candidates = resolve_page
frames_for_page = resolve_page
chain_for_page = resolve_page
pair_chain = resolve_page
certify_overlap = certify
certify_seams = certify_all
