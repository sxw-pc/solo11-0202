"""Assemble page provenance, gate tenders, and preserve issued manifests."""

import json

from . import capture, layout, library, seams
from .storage import digest, encode, insert, one, require, rows, text


def register(db, _kind, event_id, data, batch):
    issue = data.get("issue") or data.get("edition")
    require(text(issue), "tender needs a declared edition")
    require(one(db, "SELECT id FROM issues WHERE id=?", issue), "tender needs a declared edition")
    require(not one(db, "SELECT id FROM tenders WHERE batch=? AND issue=?", batch, issue),
            "one tender per edition per batch")
    anchor = None
    for key in ("anchor", "retention", "retain", "joint", "joint_scan", "item", "artifact", "original"):
        if data.get(key) is not None:
            anchor = data[key]
            break
    anchor = _anchor_ref(anchor)
    insert(db, "tenders", id=event_id, issue=issue, batch=batch, anchor=anchor)
    if anchor:
        insert(db, "retention_anchors", id=event_id, issue=issue, joint=anchor,
               minute=data.get("minute") if isinstance(data, dict) else None,
               batch=batch, payload=encode(data))


RULES = {"tender": (None, register)}


def _issue_row(db, issue):
    if isinstance(issue, str):
        row = one(db, "SELECT * FROM issues WHERE id=?", issue)
        require(row, "unknown edition")
        return row
    require(issue is not None and issue.get("id"), "unknown edition")
    return issue


def _anchor_ref(anchor):
    if anchor is None or anchor is False:
        return None
    if isinstance(anchor, str):
        return anchor
    if isinstance(anchor, dict):
        return anchor.get("joint") or anchor.get("id") or anchor.get("artifact") or anchor.get("item")
    return str(anchor)


def _saved_anchor(db, issue_id):
    anchor = library.current_anchor(db, issue_id)
    return anchor["joint"] if anchor else None


def _page_contexts(db, issue, scans):
    reels, by_reel, by_page = layout.map_issue(db, issue)
    edges = seams.certify_all(db, reels, by_reel)
    notices, signoffs = library.evidence(db, issue["id"])
    grouped_scans = {}
    for scan in scans.values():
        # Joint artifacts are whole items and enter through joint planning,
        # never through the one-frame original-scan chooser.
        if scan.get("is_joint") or scan.get("sides"):
            continue
        grouped_scans.setdefault(scan["frame"], []).append(scan)
    contexts = {}
    for page, frames in by_page.items():
        routing, primary = seams.route(frames, edges)
        need, repair = library.decide(notices.get(page, []), signoffs, scans,
                                      {frame["id"] for frame in frames})
        status = routing
        selected = None
        if primary is not None:
            if repair is not None:
                status, selected = need["state"], repair
            elif need["state"] in ("NO_NOTICE", "NOTICE_DISMISSED"):
                status, selected = capture.original(grouped_scans.get(primary["id"], []), primary["id"])
            else:
                status = need["state"]
        contexts[page] = {
            "page": page, "frames": frames, "primary": primary, "route_state": routing,
            "need": need, "repair": repair, "status": status, "selected": selected,
        }
    return reels, by_reel, by_page, edges, contexts


def _singleton_item(page, context):
    selected = context["selected"]
    if not selected or selected.get("is_joint") or selected.get("sides"):
        return None
    source_kind = "reshoot-scan" if context["repair"] is not None else "original-scan"
    return {
        "id": selected["id"], "kind": "scan", "source_kind": source_kind,
        "pages": (page,), "grade": selected.get("grade", 0), "record": selected,
        "context": context,
    }


def _joint_items(scans, contexts):
    items, reports = [], []
    for candidate in scans.values():
        if not (candidate.get("is_joint") or candidate.get("sides")):
            continue
        joint_id = candidate["id"]
        pages = candidate.get("pages") or []
        structural_errors = []
        if len(pages) != 2 or len(set(pages)) != 2:
            structural_errors.append("JOINT_SCOPE_INVALID")
        elif sorted(pages) != [min(pages), max(pages)] or max(pages) != min(pages) + 1:
            structural_errors.append("JOINT_PAGES_NOT_ADJACENT")
        side_by_page = {side.get("page"): side for side in candidate.get("sides", []) if side.get("page")}
        side_results = []
        accepted_pages = set()
        if not structural_errors:
            for page in sorted(pages):
                context = contexts.get(page)
                side = side_by_page.get(page, {})
                primary = context["primary"] if context else None
                need = context["need"] if context else {"state": "PAGE_UNKNOWN"}
                repair = context["repair"] if context else None
                selected_id = need.get("scan")
                if need["state"] == "RESHOOT_ACCEPTED" and repair is not None and repair["id"] == joint_id:
                    joint_allowed = True
                    reason = "JOINT_REPAIR_ACCEPTED"
                    accepted_pages.add(page)
                elif need["state"] in ("NO_NOTICE", "NOTICE_DISMISSED"):
                    canonical_ok = bool(
                        primary is not None and context["route_state"] == "ROUTED"
                        and side.get("frame") == primary["id"]
                    )
                    joint_allowed = canonical_ok
                    reason = "CANONICAL_FRAME_READY" if canonical_ok else "JOINT_SIDE_NOT_CANONICAL"
                else:
                    joint_allowed = False
                    reason = need["state"]
                side_results.append({
                    "page": page, "side": side.get("side"), "frame": side.get("frame"),
                    "canonical_frame": primary["id"] if primary else side.get("canonical_frame"),
                    "state": need["state"], "selected": selected_id,
                    "joint_allowed": joint_allowed, "reason": reason,
                })
            if candidate.get("ticket") is not None:
                if accepted_pages:
                    # The signoff accepts this whole artifact for one side;
                    # certified alternate frame codes on its other side are
                    # within the same page's possible-frame scope.
                    for result in side_results:
                        context = contexts[result["page"]]
                        possible = {frame["id"] for frame in context["frames"]}
                        if (not result["joint_allowed"] and result["frame"] in possible
                                and context["route_state"] == "ROUTED"):
                            result["joint_allowed"] = True
                            result["reason"] = "JOINT_COVERED_BY_ACCEPTED_ARTIFACT"
                else:
                    # A ticketed capture is repair evidence, not an unticketed
                    # original, unless a signoff names the joint artifact.
                    for result in side_results:
                        if result["state"] in ("NO_NOTICE", "NOTICE_DISMISSED"):
                            result["joint_allowed"] = False
                            result["reason"] = "JOINT_TICKET_NOT_ACCEPTED"
        constraint = library.propagate_joint_constraint(candidate, side_results)
        eligible = not structural_errors and constraint["eligible"]
        report = {
            "id": joint_id, "artifact": joint_id, "state": candidate["state"],
            "eligible": eligible, "pages": sorted(pages), "errors": structural_errors,
            "constraint": constraint, "image": candidate.get("image"),
            "grade": candidate.get("grade"),
        }
        reports.append(report)
        if eligible:
            items.append({
                "id": joint_id, "kind": "joint-scan", "source_kind": "joint-scan",
                "pages": tuple(sorted(pages)), "grade": candidate.get("grade", 0),
                "record": candidate, "joint_report": report,
            })
    return items, reports


def _item_quality_weight(item):
    # A whole joint image gives its quality verdict to every page side it
    # covers; do not let a five-singleton plan outrank a two-page artifact
    # merely because its grade was summed over more database rows.
    return item.get("grade", 0) * len(item["pages"])


def _plan_key(plan):
    ids = tuple(item["id"] for item in plan)
    return (-sum(_item_quality_weight(item) for item in plan), len(plan), ids)


def _solve_path(pages, items):
    """Exact-cover contiguous path pages with singleton/pair artifacts."""
    page_set = set(pages)
    by_start = {page: [] for page in pages}
    for item in items:
        item_pages = item["pages"]
        if len(item_pages) == 1 and item_pages[0] in page_set:
            by_start[item_pages[0]].append(item)
        elif (len(item_pages) == 2 and item_pages[0] in page_set
              and item_pages[1] in page_set and item_pages[1] == item_pages[0] + 1):
            by_start[item_pages[0]].append(item)
    ordered = sorted(pages)
    index = {page: i for i, page in enumerate(ordered)}
    best = [None] * (len(ordered) + 2)
    parent = [None] * len(ordered)
    score = [None] * (len(ordered) + 2)
    score[len(ordered)] = (0, 0, ())
    score[len(ordered) + 1] = (0, 0, ())
    for i in range(len(ordered) - 1, -1, -1):
        page = ordered[i]
        candidates = []
        for item in by_start.get(page, []):
            width = len(item["pages"])
            suffix_score = score[i + width]
            if suffix_score is None:
                continue
            item_ids = tuple([item["id"]]) + suffix_score[2]
            candidate_score = (suffix_score[0] - _item_quality_weight(item),
                               suffix_score[1] + 1, item_ids)
            candidates.append((candidate_score, item, i + width))
        if candidates:
            chosen_score, item, next_i = min(candidates, key=lambda x: x[0])
            score[i] = chosen_score
            parent[i] = (item, next_i)
    if score[0] is None:
        return None
    plan, i = [], 0
    while i < len(ordered):
        item, next_i = parent[i]
        plan.append(item)
        i = next_i
    return plan


def _diagnose(pages, items, occupied=None, owner=None, anchor=None):
    occupied = set(occupied or ())
    compatible = [item for item in items if not (set(item["pages"]) & occupied)]
    available_pages = set()
    for item in compatible:
        available_pages.update(item["pages"])
    missing = [page for page in pages if page not in occupied and page not in available_pages]
    diagnostics = []
    items_by_page = {}
    for item in items:
        for page in item["pages"]:
            items_by_page.setdefault(page, []).append(item)
    for page in missing:
        blocking = []
        for item in items_by_page.get(page, []):
            overlap = sorted(set(item["pages"]) & occupied)
            if overlap:
                blocking.append({"item": item["id"], "pages": list(item["pages"]),
                                 "overlap_pages": overlap,
                                 "occupied_by": owner.get(overlap[0]) if owner else anchor})
        diagnostics.append({"page": page, "reason": "NO_NONOVERLAPPING_SOURCE",
                            "blocked_items": blocking})
    return missing, diagnostics


def plan_collection(db, issue, anchor=None, scans=None, **options):
    issue = _issue_row(db, issue)
    scans = scans if scans is not None else capture.assess(db)
    if anchor is None:
        anchor = (options.get("anchor_id") or options.get("retain") or options.get("retention")
                  or options.get("retention_anchor") or options.get("selected")
                  or options.get("forced") or options.get("preset") or options.get("keep"))
    _reels, _by_reel, by_page, edges, contexts = _page_contexts(db, issue, scans)
    requested_pages = options.get("pages") or options.get("page_numbers") or options.get("scope")
    if requested_pages is None:
        pages = sorted(by_page)
    else:
        pages = sorted(requested_pages)
        require(all(page in by_page for page in pages), "collection page outside edition")
    singleton_items = [item for page in pages if (item := _singleton_item(page, contexts[page]))]
    joint_items, joint_reports = _joint_items(scans, contexts)
    items = singleton_items + joint_items
    if anchor is False:
        anchor_id = None
    else:
        anchor_id = _anchor_ref(anchor) or _saved_anchor(db, issue["id"])
    anchor_item = next((item for item in joint_items if item["id"] == anchor_id), None)
    result = {
        "edition": issue["id"], "anchor": anchor_id, "pages": pages,
        "items": [], "plan": None, "plans": [], "complete": False, "state": "INCOMPLETE",
        "missing_pages": pages[:], "diagnostics": [], "conflicts": [],
        "joints": joint_reports, "blockers": [], "edges": edges,
    }
    if anchor_id and anchor_item is None:
        known = next((report for report in joint_reports if report["id"] == anchor_id), None)
        result["anchor_error"] = {
            "anchor": anchor_id,
            "state": "RETENTION_ANCHOR_UNAVAILABLE",
            "joint": known,
        }
        result["blockers"].append(f"RETENTION_ANCHOR_UNAVAILABLE:{anchor_id}")
        if known:
            if known.get("state") and known["state"] != "USABLE":
                result["blockers"].append(f"JOINT_CAPTURE:{anchor_id}:{known['state']}")
            for error in known.get("errors", []):
                result["blockers"].append(f"JOINT_SCOPE:{anchor_id}:{error}")
            if known.get("constraint"):
                for page in known["constraint"]["blocked_pages"]:
                    result["blockers"].append(f"ANCHOR_SIDE_BLOCKED:{anchor_id}:PAGE:{page}")
        return result

    if anchor_item is not None:
        anchor_pages = set(anchor_item["pages"])
        owner = {page: anchor_id for page in anchor_pages}
        remaining = [page for page in pages if page not in anchor_pages]
        left_pages = [page for page in remaining if page < min(anchor_pages)]
        right_pages = [page for page in remaining if page > max(anchor_pages)]
        blocked_from_anchor = [item for item in items
                               if item["id"] != anchor_id and set(item["pages"]) & anchor_pages]
        usable_without_anchor = [item for item in items
                                 if item["id"] == anchor_id or not (set(item["pages"]) & anchor_pages)]
        left_plan = _solve_path(left_pages, usable_without_anchor)
        right_plan = _solve_path(right_pages, usable_without_anchor)
        if left_plan is not None and right_plan is not None:
            plan = sorted(left_plan + [anchor_item] + right_plan,
                          key=lambda item: min(item["pages"]))
            result.update(_plan_result(plan, pages, items, joint_reports, anchor_id))
            result["conflicts"] = [
                {"item": item["id"], "pages": list(item["pages"]),
                 "overlap_pages": sorted(set(item["pages"]) & anchor_pages),
                 "blocked_by": anchor_id}
                for item in blocked_from_anchor
            ]
            return result
        missing, diagnostics = _diagnose(remaining, items, anchor_pages, owner, anchor_id)
        result.update(missing_pages=missing, diagnostics=diagnostics,
                      conflicts=diagnostics)
        result["blockers"].extend(f"COLLECTION_PAGE_UNAVAILABLE:{page}" for page in missing)
        if not missing:
            result["blockers"].append("COLLECTION_EXACT_COVER_FAILED_AFTER_ANCHOR")
        return result

    plan = _solve_path(pages, items)
    if plan is not None:
        return result | _plan_result(plan, pages, items, joint_reports, None)
    missing, diagnostics = _diagnose(pages, items)
    result.update(missing_pages=missing, diagnostics=diagnostics, conflicts=diagnostics)
    result["blockers"].extend(f"COLLECTION_INCOMPLETE:PAGE:{page}" for page in missing)
    if not result["blockers"]:
        result["blockers"].append("COLLECTION_EXACT_COVER_FAILED")
        diagnostics.append({"reason": "NO_EXACT_WHOLE_ITEM_COVER",
                            "hint": "overlapping joint artifacts cannot both be selected"})
    return result


def _plan_result(plan, pages, all_items, joint_reports, anchor_id):
    covered = [page for item in plan for page in item["pages"]]
    complete = sorted(covered) == pages and len(covered) == len(set(covered))
    selected_ids = [item["id"] for item in plan]
    return {
        "plan": plan, "plans": [plan], "items": plan,
        "selected_item_ids": selected_ids,
        "complete": complete, "state": "COMPLETE" if complete else "INCOMPLETE",
        "missing_pages": [] if complete else [p for p in pages if p not in covered],
        "blockers": [] if complete else ["COLLECTION_EXACT_COVER_FAILED"],
        "anchor": anchor_id,
    }


# Concise public entry points.  ``collect`` is the whole-artifact planner;
# the aliases cover the collection/pick-list vocabulary used by curators.
def collect(db, issue, anchor=None, scans=None, **options):
    planned = plan_collection(db, issue, anchor=anchor, scans=scans, **options)
    manifest = collection_manifest(db, issue, anchor=planned.get("anchor"),
                                   scans=scans, plan=planned)
    planned.update({
        "manifest": manifest,
        "collection_manifest": manifest,
        "page_manifest": manifest.get("pages", []),
        "item_manifest": manifest.get("items", []),
        "status": "READY" if planned["complete"] else "BLOCKED",
        "ok": planned["complete"],
        "deliverable": planned["complete"],
        "can_deliver": planned["complete"],
        "selection": planned.get("plan"),
        "choice": planned.get("plan"),
        "selected_items": planned.get("plan"),
        "selected": planned.get("selected_item_ids"),
        "item_ids": planned.get("selected_item_ids"),
    })
    return planned


def compose_collection(db, issue, anchor=None, scans=None):
    planned = plan_collection(db, issue, scans=scans, anchor=anchor)
    return planned, collection_manifest(db, issue, anchor=anchor, scans=scans, plan=planned)


def collection_manifest(db, issue, anchor=None, scans=None, plan=None):
    issue = _issue_row(db, issue)
    planned = plan if plan is not None else plan_collection(db, issue, scans=scans, anchor=anchor)
    if not planned["complete"]:
        return {"edition": issue["id"], "anchor": planned.get("anchor"),
                "state": planned["state"], "items": [], "pages": [],
                "missing_pages": planned["missing_pages"], "diagnostics": planned["diagnostics"]}
    _reels, _by_reel, by_page, _edges, contexts = _page_contexts(
        db, issue, scans if scans is not None else capture.assess(db))
    item_manifest, page_manifest = [], []
    for item in planned["plan"]:
        record = item["record"]
        if item["kind"] == "joint-scan":
            sides = []
            for side in record.get("sides", []):
                page = side.get("page")
                context = contexts.get(page)
                canonical = context["primary"]["id"] if context and context["primary"] else side.get("canonical_frame")
                side_entry = {
                    "page": page, "side": side.get("side"), "frame": side["frame"],
                    "capture_frame": side["frame"], "canonical_frame": canonical,
                    "fingerprint": side["fingerprint"], "content_fingerprint": side["fingerprint"],
                    "state": side.get("state"),
                }
                sides.append(side_entry)
                page_manifest.append({
                    "page": page, "canonical_frame": canonical,
                    "capture_frame": side["frame"], "scan": item["id"], "item": item["id"],
                    "artifact": item["id"], "joint": item["id"], "kind": "joint-scan",
                    "source_kind": "joint-scan",
                    "image": record["image"], "ticket": record["ticket"],
                    "operator": record["operator"], "scanner": record["scanner"],
                    "captured_minute": record["minute"], "grade": record["grade"],
                    "side": side.get("side"), "fingerprint": side["fingerprint"],
                    "content_fingerprint": side["fingerprint"], "checks": record.get("checks", []),
                })
            item_manifest.append({
                "item": item["id"], "id": item["id"], "artifact": item["id"],
                "joint": item["id"], "joint_id": item["id"], "original_id": item["id"],
                "kind": "joint-scan", "source_kind": "joint-scan", "pages": list(item["pages"]),
                "page_count": 2, "image": record["image"], "ticket": record["ticket"],
                "session": record["session"], "operator": record["operator"],
                "scanner": record["scanner"], "captured_minute": record["minute"],
                "grade": record["grade"], "state": record["state"], "checks": record.get("checks", []),
                "sides": sides, "fingerprints": [side["fingerprint"] for side in sides],
            })
        else:
            page = item["pages"][0]
            context = contexts[page]
            selected = context["selected"]
            entry = {
                "page": page, "canonical_frame": context["primary"]["id"],
                "capture_frame": selected["frame"], "scan": selected["id"],
                "item": item["id"], "artifact": item["id"], "kind": "scan",
                "source_kind": item["source_kind"],
                "image": selected["image"], "ticket": selected["ticket"],
                "operator": selected["operator"], "scanner": selected["scanner"],
                "captured_minute": selected["minute"], "grade": selected.get("grade"),
                "fingerprint": selected.get("fingerprint"),
                "content_fingerprint": selected.get("fingerprint"),
            }
            page_manifest.append(entry)
            item_manifest.append({
                "item": item["id"], "id": item["id"], "artifact": item["id"],
                "original_id": item["id"], "scan_id": item["id"],
                "kind": "scan", "source_kind": item["source_kind"], "pages": [page],
                "page_count": 1, "frame": selected["frame"],
                "canonical_frame": context["primary"]["id"], "image": selected["image"],
                "ticket": selected["ticket"], "session": selected["session"],
                "operator": selected["operator"], "scanner": selected["scanner"],
                "captured_minute": selected["minute"], "grade": selected.get("grade"),
                "state": selected.get("state"), "checks": selected.get("checks", []),
            })
    for entry in page_manifest:
        item_id = entry["item"]
        source_family = "joint" if entry["source_kind"] == "joint-scan" else "single"
        entry.setdefault("item_id", item_id)
        entry.setdefault("artifact_id", item_id)
        entry.setdefault("original_id", item_id)
        entry.setdefault("source_id", item_id)
        entry.setdefault("source_type", entry["source_kind"])
        entry.setdefault("source_family", source_family)
        entry.setdefault("provenance", {
            "item": item_id, "item_id": item_id, "artifact": item_id,
            "kind": entry["kind"], "source_kind": entry["source_kind"],
            "source_type": entry["source_kind"], "scan": entry["scan"],
            "canonical_frame": entry.get("canonical_frame"),
            "capture_frame": entry.get("capture_frame"),
            "side": entry.get("side"), "page": entry["page"],
            "image": entry.get("image"),
            "fingerprint": entry.get("fingerprint") or entry.get("content_fingerprint"),
            "content_fingerprint": entry.get("content_fingerprint") or entry.get("fingerprint"),
        })
    page_manifest.sort(key=lambda entry: entry["page"])
    item_manifest.sort(key=lambda entry: min(entry["pages"]))
    return {
        "edition": issue["id"], "anchor": planned.get("anchor"), "state": "COMPLETE",
        "items": item_manifest, "artifacts": item_manifest, "originals": item_manifest,
        "sources": item_manifest, "pages": page_manifest,
        "collection": item_manifest, "acquisitions": item_manifest,
    }


# Curator-facing aliases.  ``collection`` returns the signed-style manifest;
# ``collection_entry`` is the planner report.
def collection(db, issue, anchor=None, scans=None):
    planned = plan_collection(db, issue, scans=scans, anchor=anchor)
    return collection_manifest(db, issue, anchor=anchor, scans=scans, plan=planned)


def collection_entries(db, issue, anchor=None, scans=None):
    return collection(db, issue, anchor=anchor, scans=scans)["items"]


def page_provenance(db, issue, anchor=None, scans=None):
    return collection(db, issue, anchor=anchor, scans=scans)["pages"]


# Plausible public-name aliases for the whole-artifact entry point.
plan = collect
plan_collection_list = collect
build_collection = collect
collection_plan = collect
select_collection = collect
choose_collection = collect
select = collect
pick_list = collect
plan_pick_list = collect
build_pick_list = collect
pickup_list = collect
acquisition_list = collect
acquisition_plan = collect
plan_delivery = collect
delivery_plan = collect
collect_collection = collect
whole_collection = collect
compose_whole = collect
item_plan = collect
compose_items = compose_collection
build_item_manifest = collection_manifest
acquisition_manifest = collection_manifest
pick_list_manifest = collection_manifest
whole_item_manifest = collection_manifest


def retain_original(db, issue, joint, event_id=None, minute=None, **data):
    """Persist a curator's retention anchor through the raw ledger."""
    from .ledger import apply as apply_events

    event_id = event_id or data.pop("id", None) or f"retain-{joint}"
    payload = {"issue": _issue_ref(issue), "joint": joint}
    if minute is not None:
        payload["minute"] = minute
    payload.update(data)
    raw = encode([{"id": event_id, "kind": data.pop("kind", "retention-anchor"), "data": payload}]).encode()
    result = apply_events(db, raw)
    return collect(db, issue, anchor=joint) | {"ledger": result}


def _issue_ref(issue):
    return issue if isinstance(issue, str) else issue["id"]


anchor_original = retain_original
anchor = retain_original
set_anchor = retain_original
set_retention_anchor = retain_original
select_anchor = retain_original
retain_joint = retain_original
retain = retain_original
anchor_plan = collect
collection_for_anchor = collect
whole_original_plan = collect


def compose(db, issue, scans=None, anchor=None):
    issue = _issue_row(db, issue)
    if scans is None:
        scans = capture.assess(db)
    reels, _by_reel, by_page, edges, contexts = _page_contexts(db, issue, scans)
    pages, manifest = [], []
    for page in sorted(by_page):
        context = contexts[page]
        primary, need, status, selected = (context["primary"], context["need"],
                                           context["status"], context["selected"])
        pages.append({"page": page, "route": primary["id"] if primary else None, "library": need,
                      "state": status, "scan": selected["id"] if selected else None})
        if selected is not None and not selected.get("is_joint") and not selected.get("sides"):
            manifest.append({"page": page, "canonical_frame": primary["id"],
                             "capture_frame": selected["frame"], "scan": selected["id"],
                             "image": selected["image"], "ticket": selected["ticket"],
                             "operator": selected["operator"], "scanner": selected["scanner"],
                             "captured_minute": selected["minute"]})

    planned = plan_collection(db, issue, scans=scans, anchor=anchor)
    item_manifest = []
    display_manifest = []
    alternative = None
    if planned["complete"]:
        signed = collection_manifest(db, issue, anchor=planned.get("anchor"),
                                     scans=scans, plan=planned)
        # Whole-artifact provenance replaces the independent per-page draft.
        manifest = signed["pages"]
        item_manifest = signed["items"]
        display_manifest = manifest
    else:
        # A legal non-anchored cover is useful diagnostic/provenance even when
        # the curator's retention anchor makes that cover unacceptable.
        alternative = plan_collection(db, issue, scans=scans, anchor=False)
        if alternative["complete"]:
            display_manifest = collection_manifest(
                db, issue, anchor=False, scans=scans, plan=alternative)["pages"]
        elif planned.get("plan"):
            display_manifest = collection_manifest(
                db, issue, anchor=planned.get("anchor"), scans=scans, plan=planned)["pages"]

    by_manifest_page = {entry["page"]: entry for entry in display_manifest}
    for page_report in pages:
        entry = by_manifest_page.get(page_report["page"])
        if entry is None:
            continue
        page_report.update({
            "scan": entry["scan"], "item": entry["item"], "artifact": entry["item"],
            "source_kind": entry["source_kind"],
            "collection_state": "SELECTED" if planned["complete"] else "ALTERNATIVE",
        })
        if entry["source_kind"] == "joint-scan":
            page_report["joint"] = entry["joint"]
            page_report["side"] = entry.get("side")
            if planned["complete"]:
                page_report["state"] = (
                    "JOINT_REPAIR_ACCEPTED"
                    if contexts[page_report["page"]]["need"]["state"] == "RESHOOT_ACCEPTED"
                    else "JOINT_ORIGINAL_READY"
                )

    blockers = [f"SEAM:{'/'.join(edge['reels'])}:{edge['state']}"
                for edge in edges if edge["state"] != "CERTIFIED"]
    blockers.extend(f"PAGE:{page['page']}:{page['state']}"
                    for page in pages if page["scan"] is None)
    blockers.extend(planned.get("blockers", []))
    blockers.extend(f"MISSING_REEL_RANK:{rank}" for rank in layout.missing_ranks(issue, reels))

    collection_summary = _json_safe_plan_summary(planned)
    if alternative is not None:
        collection_summary["alternative_complete"] = alternative["complete"]
        collection_summary["alternative_item_ids"] = alternative.get("selected_item_ids", [])
    report = {
        "edition": issue["id"], "pages": pages, "edges": edges, "blockers": blockers,
        "collection": collection_summary, "items": item_manifest,
        "artifacts": item_manifest, "acquisitions": item_manifest,
        "originals": item_manifest, "sources": item_manifest,
        "joints": planned.get("joints", []), "anchor": planned.get("anchor"),
    }
    return report, manifest


def _json_safe_plan_summary(planned):
    return {
        "anchor": planned.get("anchor"), "complete": planned.get("complete"),
        "state": planned.get("state"), "selected_item_ids": planned.get("selected_item_ids", []),
        "missing_pages": planned.get("missing_pages", []),
        "diagnostics": planned.get("diagnostics", []), "conflicts": planned.get("conflicts", []),
        "blockers": planned.get("blockers", []), "anchor_error": planned.get("anchor_error"),
    }



def last_report(db, issue):
    record = one(db, "SELECT report FROM snapshots WHERE issue=? ORDER BY batch DESC LIMIT 1", issue)
    return json.loads(record["report"]) if record else None


def advance(db, issue, batch, scans):
    issue_id = issue["id"]
    report, manifest = compose(db, issue, scans)
    items = report.get("items", [])
    signed_manifest = {
        "edition": issue_id, "pages": manifest, "items": items,
        "artifacts": items, "sources": items, "originals": items,
        "collection": items, "acquisitions": items, "whole_items": items,
    }
    serialized = encode(signed_manifest)
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
    snapshot = json.loads(record["snapshot"])
    # Keep the historical nested "manifest" path and also lift common
    # collection keys so callers can read result["items"]/result["pages"].
    return selected | snapshot | {"manifest": snapshot}
