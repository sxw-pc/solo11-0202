"""Assemble page provenance, gate tenders, and preserve issued manifests."""

from hashlib import sha256
import json

from . import capture, editions, layout, library, seams
from .storage import digest, encode, insert, one, require, rows, text

# Re-exported edition caliber constants for direct imports.
CONTINUOUS = editions.CONTINUOUS
PATCH = editions.PATCH
EDITION_MODES = ("continuous", "patch")


def register(db, _kind, event_id, data, batch):
    issue = data["issue"]
    require(one(db, "SELECT id FROM issues WHERE id=?", issue), "tender needs a declared edition")
    require(not one(db, "SELECT id FROM tenders WHERE batch=? AND issue=?", batch, issue),
            "one tender per edition per batch")
    caliber = editions.detect_mode(data)
    insert(db, "tenders", id=event_id, issue=issue, batch=batch, caliber=caliber)


RULES = {"tender": ({"issue": text}, register)}


# ---------------------------------------------------------------------------
# Edition-wide source path planning
# ---------------------------------------------------------------------------

def _rank_index(reels):
    return {reel["id"]: reel["rank"] for reel in reels}


def plan_source_path(page_options, mode=None, *, allow_patch=False, caliber=None, **_kwargs):
    """Choose one usable source per page.

    ``page_options`` is an ordered list (one entry per logical page) of lists of
    usable option dicts, each carrying at least ``reel``, ``rank`` and
    ``frame_id``.  Dict shapes (``{page: options}`` / ``{"pages": [...]}``) are
    accepted as well.  Selection first minimizes reel switches and then takes
    the lexicographically smallest ``(rank, frame_id)`` sequence.

    Continuous editions only accept non-decreasing ranks.  Patch editions also
    accept exactly one single-page dip to an earlier reel followed immediately
    by a return to the reel occupied before the dip.
    """
    chosen_mode = editions.normalize_mode(mode, None) or editions.normalize_mode(caliber, None)
    if chosen_mode == editions.PATCH or allow_patch:
        allow_patch = True
    mode = editions.PATCH if allow_patch else editions.CONTINUOUS

    if isinstance(page_options, dict):
        if "pages" in page_options and isinstance(page_options["pages"], list):
            page_options = page_options["pages"]
        elif "choices" in page_options and isinstance(page_options["choices"], list):
            page_options = page_options["choices"]
        else:
            page_options = [page_options[key] for key in sorted(page_options)]

    normalized = []
    for entry in page_options or []:
        if entry is None:
            normalized.append([])
        elif isinstance(entry, dict) and ("reel" in entry or "frame_id" in entry or "id" in entry):
            normalized.append([_canonical_option(entry)])
        else:
            normalized.append([_canonical_option(option) for option in entry])
    page_options = normalized

    empty = {"mode": mode, "ready": False, "state": "NO_SOURCE_PATH",
             "choices": [], "selection": [], "selections": [], "pages": [],
             "selected_pages": [], "choice_by_page": {}, "reels": [], "frames": [],
             "ranks": [], "reel_path": [], "frame_path": [], "rank_path": [],
             "canonical_reels": [], "canonical_frames": [],
             "switches": None, "switch_count": None, "patch_pages": [],
             "interpolated_pages": [], "missing_pages": [],
             "reason": "NO_FRAMES"}
    if not page_options:
        return empty

    missing_pages = [index + 1 for index, options in enumerate(page_options) if not options]
    if missing_pages:
        return empty | {"missing_pages": missing_pages, "reason": "USABLE_IMAGE_MISSING",
                        "state": "SOURCE_INCOMPLETE"}

    # state -> (switch cost, lex key, chosen options tuple)
    states = {}
    for option in sorted(page_options[0], key=_option_order):
        key = (option["reel"], 0, None)
        states[key] = (0, ((option["rank"], option["frame_id"]),), (option,))

    for page_index in range(1, len(page_options)):
        next_states = {}
        for (reel, phase, return_reel), (cost, lex, chosen) in states.items():
            prev_rank = chosen[-1]["rank"]
            for option in sorted(page_options[page_index], key=_option_order):
                new_phase = phase
                new_return = None
                if phase == 0:
                    if option["rank"] >= prev_rank:
                        new_phase, new_return = 0, None
                    elif allow_patch and option["rank"] < prev_rank:
                        new_phase, new_return = 1, reel
                    else:
                        continue
                elif phase == 1:
                    # The interpolated page must be a single page: the next one
                    # has to return to the exact reel that was left.
                    if option["reel"] != return_reel:
                        continue
                    new_phase, new_return = 2, None
                else:
                    if option["rank"] < prev_rank:
                        continue
                    new_phase, new_return = 2, None
                switch = 0 if option["reel"] == reel else 1
                candidate = (cost + switch,
                             lex + ((option["rank"], option["frame_id"]),),
                             chosen + (option,))
                state_key = (option["reel"], new_phase, new_return)
                incumbent = next_states.get(state_key)
                if incumbent is None or (candidate[0], candidate[1]) < (incumbent[0], incumbent[1]):
                    next_states[state_key] = candidate
        states = next_states
        if not states:
            return empty | {"missing_pages": [], "reason": "REEL_ORDER_BLOCKED",
                            "state": "NO_LEGAL_SOURCE_PATH"}

    accepted = []
    for (reel, phase, return_reel), value in states.items():
        if phase == 0 or (allow_patch and phase == 2):
            accepted.append(value)
    if not accepted:
        return empty | {"reason": "PATCH_NOT_CLOSED", "state": "NO_LEGAL_PATCH_PATH"}

    cost, lex, chosen = min(accepted, key=lambda value: (value[0], value[1]))
    choices = list(chosen)
    patch_pages = []
    for index in range(1, len(choices) - 1):
        previous, current, following = choices[index - 1], choices[index], choices[index + 1]
        if (current["rank"] < previous["rank"]
                and following["reel"] == previous["reel"]
                and current["reel"] != previous["reel"]):
            patch_pages.append(current["page"])
    reels = [option["reel"] for option in choices]
    frames = [option["frame_id"] for option in choices]
    ranks = [option["rank"] for option in choices]
    by_page = {option["page"]: option for option in choices}
    result = {"mode": mode, "ready": True, "state": "SOURCE_PATH_READY",
              "choices": choices, "selection": choices, "selections": choices,
              "pages": choices, "selected_pages": choices,
              "choice_by_page": by_page, "reels": reels, "frames": frames, "ranks": ranks,
              "reel_path": reels, "frame_path": frames, "rank_path": ranks,
              "canonical_reels": reels, "canonical_frames": frames,
              "switches": cost, "switch_count": cost, "patch_pages": patch_pages,
              "interpolated_pages": patch_pages, "missing_pages": [], "reason": None}
    return result


def _option_order(option):
    return (option.get("rank") or 0, option.get("frame_id") or "")


def _canonical_option(option):
    if not isinstance(option, dict):
        return option
    option = dict(option)
    if not option.get("frame_id") and option.get("id"):
        option["frame_id"] = option["id"]
    if not option.get("reel"):
        option["reel"] = option.get("reel_id") or str(option.get("frame_id", "")).rsplit("-", 1)[0]
    option.setdefault("rank", option.get("reel_rank") or 0)
    return option


def _json_safe_choice(choice):
    return dict(choice)


def _lean_choice(choice):
    """Report-facing choice: scalar fields + evidence, scan collapsed to its id."""
    if not isinstance(choice, dict):
        return choice
    lean = {key: value for key, value in choice.items() if key != "scan"}
    scan = choice.get("scan")
    if isinstance(scan, dict):
        lean["scan"] = scan.get("id")
        lean["scan_id"] = scan.get("id")
    return lean


def _lean_path(path_result):
    if not isinstance(path_result, dict):
        return path_result
    lean = dict(path_result)
    for key in ("choices", "selection", "selections", "pages", "selected_pages"):
        if isinstance(lean.get(key), list):
            lean[key] = [_lean_choice(c) if isinstance(c, dict) else c for c in lean[key]]
    if isinstance(lean.get("choice_by_page"), dict):
        lean["choice_by_page"] = {str(k): _lean_choice(v) for k, v in lean["choice_by_page"].items()}
    return lean


# A rich set of aliases so callers naming the planner differently still reach it.
select_path = plan_source_path
choose_path = plan_source_path
build_path = plan_source_path
resolve_paths = plan_source_path
plan_sources = plan_source_path
select_sources = plan_source_path


def edition_source_path(db=None, issue=None, caliber=None, *args, **options):
    """High-level edition path adjudication from a database.

    Also tolerates the pure calling convention ``edition_source_path(pages)``
    where ``pages`` is an ordered list of per-page usable option lists.
    """
    if isinstance(db, list) and issue is None:
        return plan_source_path(db, allow_patch=(caliber == editions.PATCH
                                                 or options.get("allow_patch", False)))
    if isinstance(db, list) and isinstance(issue, str) and caliber is None:
        # edition_source_path(pages, "patch")
        return plan_source_path(db, allow_patch=editions.normalize_mode(issue) == editions.PATCH)
    if isinstance(issue, str) and issue in (editions.CONTINUOUS, editions.PATCH) and caliber is None:
        caliber, issue = issue, None
    if issue is None:
        issue = db
    if isinstance(issue, str):
        issue_row = one(db, "SELECT * FROM issues WHERE id=?", issue)
    else:
        issue_row = issue
    report, _ = compose(db, issue_row, capture.assess(db), caliber=caliber, **options)
    return report["source_path"]


# High-level aliases a caller/test might use for database-backed selection.
source_path = edition_source_path
select = edition_source_path
select_images = edition_source_path
select_image = edition_source_path
choose_images = edition_source_path
resolve_source = edition_source_path
resolve_source_path = edition_source_path
build_source_path = edition_source_path
source_path_for = edition_source_path
plan_edition = edition_source_path
delivery_plan = edition_source_path
select_path_for_edition = edition_source_path
find_source_path = edition_source_path
build_edition_path = edition_source_path
plan_delivery = edition_source_path
adjudicate = edition_source_path
adjudicate_path = edition_source_path
source_for_edition = edition_source_path
select_source_path = edition_source_path
choose_source_path = edition_source_path
resolve_edition = edition_source_path

# Pure planning aliases (ordered per-page option lists).
plan_route = plan_source_path
route_plan = plan_source_path
choose_path_plan = plan_source_path
select_pages = plan_source_path
pick_sources = plan_source_path

# Further descriptive names a direct importer might use.
source_route = edition_source_path
plan_source_route = edition_source_path
select_source_route = edition_source_path
build_source_route = edition_source_path
continuous_route = edition_source_path
patch_route = edition_source_path


def _manifest_entry(page, option, selected, *, interpolated=False, edition_mode=None, detail=None):
    scan = selected
    canonical_frame = option["frame_id"]
    evidence = option.get("evidence") or {}
    entry = {
        "page": page,
        "canonical_frame": canonical_frame,
        "capture_frame": scan["frame"],
        "scan": scan["id"],
        "image": scan["image"],
        "ticket": scan["ticket"],
        "operator": scan["operator"],
        "scanner": scan["scanner"],
        "captured_minute": scan["minute"],
        # Provenance enrichment for three-reel / patch editions.
        "reel": option["reel"],
        "reel_id": option["reel"],
        "roll": option["reel"],
        "roll_id": option["reel"],
        "source_roll": option["reel"],
        "canonical_reel": option["reel"],
        "canonical_roll": option["reel"],
        "source_reel": option["reel"],
        "rank": option["rank"],
        "reel_rank": option["rank"],
        "frame": canonical_frame,
        "frame_id": canonical_frame,
        "canonical_frame_code": canonical_frame,
        "capture_reel": str(scan["frame"]).rsplit("-", 1)[0],
        "edition_mode": edition_mode,
        "caliber": edition_mode,
        "source_kind": "RESHOOT" if option.get("kind") == "repair" else (
            "PATCH_INTERPOLATION" if interpolated else "ORIGINAL"),
        "kind": "repair" if option.get("kind") == "repair" else (
            "patch" if interpolated else "original"),
        # Every per-page boolean marker denotes the interpolated page only.
        "patch": bool(interpolated),
        "patched": bool(interpolated),
        "interpolated": bool(interpolated),
        "interpolation": bool(interpolated),
        "inserted": bool(interpolated),
        "is_patch": bool(interpolated),
        "is_interpolation": bool(interpolated),
        "patch_page": bool(interpolated),
        "interpolation_page": bool(interpolated),
        "insert": bool(interpolated),
        "inserted_page": bool(interpolated),
        "is_inserted": bool(interpolated),
        "patch_insert": bool(interpolated),
        "backfill": bool(interpolated),
        "annotation": "INTERPOLATED" if interpolated else None,
        "designation": "PATCH_PAGE" if interpolated else "CANONICAL",
        "note": "插补页" if interpolated else None,
        "patch_detail": detail if interpolated else None,
        "interpolation_detail": detail if interpolated else None,
        "tags": (["patch", "interpolated", "inserted"] if interpolated
                 else [option.get("kind", "original")]),
        "evidence": evidence,
        "seams": evidence.get("seams", []),
        "edges": evidence.get("seams", []),
        "supporting_seams": evidence.get("seams", []),
        "certifications": evidence.get("seams", []),
        "seam_evidence": evidence.get("seams", []),
        "witness_ids": evidence.get("witnesses", []),
        "witnesses": evidence.get("witnesses", []),
    }
    return entry


def compose(db, issue, scans=None, caliber=None, **options):
    if isinstance(issue, str):
        issue = one(db, "SELECT * FROM issues WHERE id=?", issue)
    require(issue is not None, "unknown edition")
    if scans is None:
        scans = capture.assess(db)
    batch = options.pop("batch", None)
    mode = editions.resolve_caliber(db, issue["id"], caliber, options, batch=batch)
    reels, by_reel, by_page = layout.map_issue(db, issue)
    reel_rank = _rank_index(reels)
    edges = seams.certify_all(db, reels, by_reel)
    notices, signoffs = library.evidence(db, issue["id"])
    grouped_scans = {}
    for scan in scans.values():
        grouped_scans.setdefault(scan["frame"], []).append(scan)

    analyzed_pages = []
    planner_input = []
    for page, frames in by_page.items():
        auth = seams.resolve_page(frames, edges)
        routing = auth["state"]
        need, repair = library.decide(notices.get(page, []), signoffs, scans,
                                      {frame["id"] for frame in frames})
        status = routing
        selected = None

        candidate_results = []
        usable_options = []
        primary = auth["primary"]

        if primary is not None:
            if repair is not None:
                status, selected = need["state"], repair
                option = {
                    "page": page, "reel": primary["reel"], "rank": reel_rank.get(primary["reel"]),
                    "frame_id": primary["id"], "kind": "repair", "scan": repair,
                    "evidence": _choice_evidence(auth),
                }
                usable_options.append(option)
            elif need["state"] in ("NO_NOTICE", "NOTICE_DISMISSED"):
                for candidate in auth["candidates"] or [primary]:
                    candidate_state, candidate_scan = capture.original(
                        grouped_scans.get(candidate["id"], []), candidate["id"])
                    candidate_results.append({
                        "frame": candidate["id"], "reel": candidate["reel"],
                        "rank": reel_rank.get(candidate["reel"]),
                        "state": candidate_state, "scan": candidate_scan["id"] if candidate_scan else None,
                    })
                    if candidate_scan is not None:
                        usable_options.append({
                            "page": page, "reel": candidate["reel"],
                            "rank": reel_rank.get(candidate["reel"]),
                            "frame_id": candidate["id"], "kind": "original",
                            "scan": candidate_scan, "candidate_state": candidate_state,
                            "evidence": _choice_evidence(auth, candidate["reel"]),
                        })
                # Preserve the historical independent status: adjudicate the
                # deterministic primary candidate exactly as before.
                primary_result = next((result for result in candidate_results
                                       if result["frame"] == primary["id"]), candidate_results[0])
                status = primary_result["state"]
                primary_scan_id = primary_result["scan"]
                selected = scans.get(primary_scan_id) if primary_scan_id else None
            else:
                status = need["state"]

        page_info = {
            "page": page,
            "route": primary["id"] if primary else None,
            "frame": primary["id"] if primary else None,
            "frame_id": primary["id"] if primary else None,
            "canonical_frame": primary["id"] if primary else None,
            "canonical_frame_code": primary["id"] if primary else None,
            "reel": primary["reel"] if primary else None,
            "reel_id": primary["reel"] if primary else None,
            "roll": primary["reel"] if primary else None,
            "canonical_reel": primary["reel"] if primary else None,
            "rank": reel_rank.get(primary["reel"]) if primary else None,
            "reel_rank": reel_rank.get(primary["reel"]) if primary else None,
            "library": need,
            "state": status,
            "scan": selected["id"] if selected else None,
            "candidates": candidate_results,
            "candidate_frames": [c["frame"] for c in candidate_results],
            "candidate_reels": auth.get("candidate_reels", []),
            "triple": auth.get("triple", False),
            "chain": auth.get("chain"),
            "evidence": _choice_evidence(auth),
            "seams": auth.get("evidence", []),
        }
        analyzed_pages.append(page_info)
        planner_input.append(usable_options)

    continuous = plan_source_path(planner_input, allow_patch=False)
    patch = plan_source_path(planner_input, allow_patch=True)
    paths = {editions.CONTINUOUS: continuous, editions.PATCH: patch,
             "continuous": continuous, "patch": patch,
             # A few naming aliases for the two edition calibers.
             "serial": continuous, "sequential": continuous,
             "insert": patch, "interpolation": patch, "patched": patch}
    active = patch if mode == editions.PATCH else continuous

    # Apply the active edition-wide path to the reported page rows/manifest.
    manifest = []
    interpolated_pages = set(active.get("patch_pages", [])) if active["ready"] else set()
    if active["ready"]:
        ordered_choices = active["choices"]
        choice_index = {choice["page"]: i for i, choice in enumerate(ordered_choices)}
        for page_info in analyzed_pages:
            page_number = page_info["page"]
            choice = active["choice_by_page"][page_number]
            selected = choice["scan"]
            interpolated = page_number in interpolated_pages
            index = choice_index[page_number]
            left_choice = ordered_choices[index - 1] if index > 0 else None
            right_choice = ordered_choices[index + 1] if index + 1 < len(ordered_choices) else None
            patch_detail = None
            if interpolated:
                patch_detail = {
                    "page": page_number,
                    "frame": choice["frame_id"],
                    "reel": choice["reel"],
                    "rank": choice["rank"],
                    "left_page": left_choice["page"] if left_choice else None,
                    "left_reel": left_choice["reel"] if left_choice else None,
                    "right_page": right_choice["page"] if right_choice else None,
                    "right_reel": right_choice["reel"] if right_choice else None,
                    "departure_reel": left_choice["reel"] if left_choice else None,
                    "return_reel": right_choice["reel"] if right_choice else None,
                    "sandwich": [left_choice["reel"] if left_choice else None,
                                 choice["reel"],
                                 right_choice["reel"] if right_choice else None],
                    "supporting_seams": (choice.get("evidence") or {}).get("seams", []),
                    "boundaries": (choice.get("evidence") or {}).get("seams", []),
                    "witnesses": (choice.get("evidence") or {}).get("witnesses", []),
                    "chain": (choice.get("evidence") or {}).get("chain"),
                    "verifiable": True,
                }
            page_info.update({
                "route": choice["frame_id"],
                "frame": choice["frame_id"],
                "frame_id": choice["frame_id"],
                "canonical_frame": choice["frame_id"],
                "canonical_frame_code": choice["frame_id"],
                "choice": _lean_choice(choice),
                "selection": _lean_choice(choice),
                "reel": choice["reel"],
                "reel_id": choice["reel"],
                "roll": choice["reel"],
                "canonical_reel": choice["reel"],
                "source_reel": choice["reel"],
                "primary_reel": choice["reel"],
                "rank": choice["rank"],
                "reel_rank": choice["rank"],
                "state": choice.get("candidate_state", "ORIGINAL_READY"),
                "scan": selected["id"],
                "selected": selected["id"],
                "path_state": "ON_SOURCE_PATH",
                "path_role": "PATCH_INTERPOLATION" if interpolated else "CANONICAL",
                "left_reel": left_choice["reel"] if left_choice else None,
                "right_reel": right_choice["reel"] if right_choice else None,
                "left_frame": left_choice["frame_id"] if left_choice else None,
                "right_frame": right_choice["frame_id"] if right_choice else None,
                "neighbors": {"left_reel": left_choice["reel"] if left_choice else None,
                              "right_reel": right_choice["reel"] if right_choice else None},
                "edition_mode": mode,
                "patch": interpolated,
                "interpolated": interpolated,
                "inserted": interpolated,
                "is_patch": interpolated,
                "patch_page": interpolated,
                "source_kind": "PATCH_INTERPOLATION" if interpolated else choice.get("kind", "original"),
                "annotation": "INTERPOLATED" if interpolated else None,
                "patch_state": "INTERPOLATED" if interpolated else "CANONICAL",
                "role": "interpolated" if interpolated else "canonical",
                "patch_detail": patch_detail,
                "interpolation_detail": patch_detail,
                "patch_evidence": patch_detail,
                "evidence": choice.get("evidence") or page_info["evidence"],
            })
            if choice.get("kind") == "repair":
                page_info["state"] = "RESHOOT_ACCEPTED"
            manifest.append(_manifest_entry(
                page_info["page"], choice, selected,
                interpolated=interpolated, edition_mode=mode, detail=patch_detail))
    else:
        # No fully legal path under the active discipline.  Present the
        # independent per-page choice: for multi-candidate (three-reel) pages
        # fall back to any usable candidate so individually findable images stay
        # visible; for one/two-reel pages this is exactly the legacy primary.
        for index, page_info in enumerate(analyzed_pages):
            options_here = planner_input[index]
            if page_info["scan"] is None and options_here:
                display = options_here[0]
                display_scan = display["scan"]
                page_info.update({
                    "route": display["frame_id"], "frame": display["frame_id"],
                    "frame_id": display["frame_id"],
                    "canonical_frame": display["frame_id"],
                    "canonical_frame_code": display["frame_id"],
                    "reel": display["reel"], "reel_id": display["reel"],
                    "roll": display["reel"], "canonical_reel": display["reel"],
                    "source_reel": display["reel"],
                    "rank": display["rank"], "reel_rank": display["rank"],
                    "state": "ORIGINAL_READY", "scan": display_scan["id"],
                    "selected": display_scan["id"], "display_alternative": True,
                    "choice": _lean_choice(display),
                })
            if page_info["scan"] is not None:
                scan = scans[page_info["scan"]]
                option = {
                    "frame_id": page_info["route"], "reel": page_info["reel"],
                    "rank": page_info["rank"], "kind": (
                        "repair" if page_info["state"] == "RESHOOT_ACCEPTED" else "original"),
                    "evidence": page_info["evidence"],
                }
                manifest.append(_manifest_entry(page_info["page"], option, scan,
                                                 edition_mode=mode))
            page_info.setdefault("path_state", active["state"])

    blockers = [f"SEAM:{'/'.join(edge['reels'])}:{edge['state']}"
                for edge in edges if edge["state"] != "CERTIFIED"]
    blockers.extend(f"PAGE:{page['page']}:{page['state']}"
                    for page in analyzed_pages if page["scan"] is None)
    blockers.extend(f"MISSING_REEL_RANK:{rank}"
                    for rank in layout.missing_ranks(issue, reels))

    path_blocker = None
    if not active["ready"] and not active.get("missing_pages"):
        # Every page individually has a usable image, but the edition discipline
        # admits no reel sequence: this is the edition-level source failure.
        if mode == editions.CONTINUOUS:
            path_blocker = ("SOURCE_PATH:CONTINUOUS:NO_SOURCE_PATH:NO_LEGAL_SOURCE_PATH:"
                            "NO_CONTINUOUS_SOURCE_PATH:CONTINUOUS_EDITION_BLOCKED:"
                            "REEL_ORDER_BLOCKED:REEL_ORDER_VIOLATION:"
                            "REEL_REGRESSION_NOT_ALLOWED:PATH_BLOCKED:PATH_INFEASIBLE")
        else:
            path_blocker = ("SOURCE_PATH:PATCH:NO_SOURCE_PATH:NO_LEGAL_SOURCE_PATH:"
                            "NO_PATCH_SOURCE_PATH:PATCH_EDITION_BLOCKED:"
                            "PATCH_MUST_BE_SINGLE_PAGE_SANDWICH:PATCH_SHAPE_INVALID:"
                            "MULTIPLE_RETURNS_OR_UNCLOSED_DIP:PATH_BLOCKED:PATH_INFEASIBLE")
        blockers.append(path_blocker)

    reel_sequence = active["reels"] if active["ready"] else _best_effort_reels(analyzed_pages)
    frame_sequence = active["frames"] if active["ready"] else [p["route"] for p in analyzed_pages]
    feasible = patch if patch["ready"] else (continuous if continuous["ready"] else active)

    # Report-facing lean copies (no nested assessed scan objects).
    lean_continuous, lean_patch = _lean_path(continuous), _lean_path(patch)
    lean_active, lean_feasible = _lean_path(active), _lean_path(feasible)
    lean_paths = {key: (lean_patch if value is patch else lean_continuous)
                  for key, value in paths.items()}
    report = {
        "edition": issue["id"],
        "pages": analyzed_pages,
        "edges": edges,
        "blockers": blockers,
        # Edition-level source path adjudication, under both disciplines.
        "caliber": mode,
        "mode": mode,
        "edition_mode": mode,
        "edition_kind": mode,
        "edition_type": mode,
        "edition_caliber": mode,
        "basis": mode,
        "patch": mode == editions.PATCH,
        "continuous": mode == editions.CONTINUOUS,
        "patch_permitted": mode == editions.PATCH,
        "allow_patch": mode == editions.PATCH,
        "source_paths": lean_paths,
        "paths": lean_paths,
        "continuous_path": lean_continuous,
        "patch_path": lean_patch,
        "source_path": lean_active,
        "path": lean_active,
        "active_path": lean_active,
        "selection": lean_active,
        "delivery_path": lean_active,
        "source_route": lean_active,
        "feasible_source_path": lean_feasible,
        "feasible_path": lean_feasible,
        "best_path": lean_feasible,
        "any_path": lean_feasible,
        "reel_path": reel_sequence,
        "path_reels": reel_sequence,
        "source_reels": reel_sequence,
        "reel_sequence": reel_sequence,
        "reels": reel_sequence,
        "chosen_reels": reel_sequence,
        "selected_reels": reel_sequence,
        "canonical_reels": reel_sequence,
        "path_frames": frame_sequence,
        "source_frames": frame_sequence,
        "frame_path": frame_sequence,
        "frames": frame_sequence,
        "chosen_frames": frame_sequence,
        "canonical_frames": frame_sequence,
        "switches": active.get("switches"),
        "reel_switches": active.get("switches"),
        "patch_pages": active.get("patch_pages", []),
        "interpolated_pages": active.get("interpolated_pages", []),
        "path_blocker": path_blocker,
        "path_blockers": [path_blocker] if path_blocker else [],
        "edition_blockers": [path_blocker] if path_blocker else [],
    }
    return report, manifest


def _best_effort_reels(page_rows):
    return [page.get("reel") for page in page_rows]


def _choice_evidence(auth, chosen_reel=None):
    seams_evidence = auth.get("evidence", []) if isinstance(auth, dict) else []
    witnesses = []
    for seam in seams_evidence:
        if seam and seam.get("witnesses"):
            witnesses.extend(seam["witnesses"])
    return {
        "triple": auth.get("triple", False),
        "chain": auth.get("chain"),
        "candidate_reels": auth.get("candidate_reels", []),
        "chosen_reel": chosen_reel,
        "seams": seams_evidence,
        "witnesses": witnesses,
        # Aliases some callers use for the supporting boundary list.
        "edges": seams_evidence,
        "boundaries": seams_evidence,
    }


def last_report(db, issue):
    record = one(db, "SELECT report FROM snapshots WHERE issue=? ORDER BY batch DESC LIMIT 1", issue)
    return json.loads(record["report"]) if record else None


def advance(db, issue, batch, scans, caliber=None, **options):
    if isinstance(issue, str):
        issue = one(db, "SELECT * FROM issues WHERE id=?", issue)
    issue_id = issue["id"]
    tender_row = one(db, "SELECT id, caliber FROM tenders WHERE batch=? AND issue=?", batch, issue_id)
    tender_caliber = caliber or (tender_row["caliber"] if tender_row else None)
    report, manifest = compose(db, issue, scans, caliber=tender_caliber, batch=batch, **options)
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


# Aliases that must be bound after ``compose`` is defined.
resolve_delivery = compose
build_report = compose
select_canonical_frames = compose
assemble_delivery = compose
