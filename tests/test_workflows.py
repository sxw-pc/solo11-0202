from hashlib import sha256
import json
from pathlib import Path
import unittest

from microfilm_reshoot import capture, delivery, layout, ledger
from microfilm_reshoot.storage import Rejected, connect, one

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DEMO = sorted((FIXTURES / "demo").glob("*.json"))
ISSUE = "NEWS-42"


def event(id, kind, **data):
    return {"id": id, "kind": kind, "data": data}


def hash_of(value):
    return sha256(value.encode()).hexdigest()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)

    def stages(self, first, last):
        for path in DEMO[first - 1:last]:
            ledger.apply(self.db, path.read_bytes())

    def fixture(self, name, domain):
        return ledger.apply(self.db, (FIXTURES / f"{name}.json").read_bytes(), domain)

    def apply(self, *events):
        return ledger.apply(self.db, json.dumps(events).encode())

    def report(self):
        return delivery.last_report(self.db, ISSUE)

    def page(self, number):
        return self.report()["pages"][number - 1]

    def tender(self, id):
        self.apply(event(id, "tender", issue=ISSUE))
        return self.report()["tender"]

    def signoff(self, id, action, minute, scan=None):
        return event(id, "signoff", notice="missing-3", scan=scan, officer="curator-F", minute=minute, action=action)

    def scan(self, id, code, page, minute, ticket=None, grade=90):
        return event(id, "scan", session="shift-1", frame=code, minute=minute, ticket=ticket,
                     fingerprint=hash_of(f"leaf-{page}"), image=hash_of(id), grade=grade)

    def test_layout_direction_and_edition_extent(self):
        self.stages(1, 1)
        issue = one(self.db, "SELECT * FROM issues WHERE id=?", ISSUE)
        report = layout.catalog(issue, *layout.map_issue(self.db, issue))
        self.assertEqual([5, 4, 3, 2], [f["page"] for f in report["rolls"][1]["frames"]])
        self.assertEqual("R02-0004", report["rolls"][1]["frames"][-1]["id"])
        self.assertEqual([], report["missing_ranks"])
        with self.assertRaisesRegex(Rejected, "direction"):
            self.fixture("bad_layout", "layout")
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_tender_needs_declared_roll_even_if_every_leaf_has_scan(self):
        self.apply(event(ISSUE, "issue", leaves=1, rolls=2), event("R01", "reel", issue=ISSUE, rank=1,
                   direction="forward", frames=[{"slot": 1, "page": 1, "fingerprint": hash_of("leaf-1")}]),
                   event("shift-1", "session", scanner="S1", operator="O1", start=1, end=10),
                   self.scan("first", "R01-0001", 1, 2))
        self.assertEqual("ORIGINAL_READY", self.page(1)["state"])
        self.assertEqual("TENDER_DENIED", self.tender("before-second-roll")["state"])
        self.assertEqual(["MISSING_REEL_RANK:2"], self.report()["blockers"])

    def test_witness_disagreement_withdraws_and_re_routes(self):
        self.stages(1, 4)
        old = delivery.manifest(self.db, ISSUE, None)
        self.assertEqual("R01-0002", old["manifest"]["pages"][1]["canonical_frame"])
        self.stages(5, 6)
        self.assertEqual("WITNESS_CONFLICT", self.report()["edges"][0]["state"])
        self.assertEqual("TENDER_DENIED", self.report()["tender"]["state"])
        self.assertEqual("WITHDRAWN", delivery.manifest(self.db, ISSUE, 1)["state"])
        self.stages(7, 7)
        self.assertEqual("R02-0004", self.page(2)["route"])
        self.assertEqual("back-2", self.page(2)["scan"])
        self.assertEqual(old["manifest"], delivery.manifest(self.db, ISSUE, 1)["manifest"])
        with self.assertRaisesRegex(Rejected, "no current"):
            delivery.manifest(self.db, ISSUE, None)

    def test_matching_witnesses_still_need_complete_logical_pair_order(self):
        self.stages(1, 4)
        self.fixture("bad_seam", "seam")
        self.assertEqual("PAIR_ORDER_OR_SCOPE_INVALID", self.report()["edges"][0]["state"])
        self.assertIsNone(self.page(2)["scan"])
        self.assertEqual("WITHDRAWN", self.report()["versions"][0]["state"])

    def test_two_witness_roles_need_distinct_signers(self):
        self.stages(1, 3)
        witness = json.loads(DEMO[1].read_bytes())[1]["data"]
        self.apply(event("non-independent", "witness", **(witness | {"signer": "operator-7", "minute": 20})))
        self.assertEqual("WITNESSES_NOT_INDEPENDENT", self.report()["edges"][0]["state"])

    def test_layout_shared_page_identity_conflicting_fingerprint(self):
        events = json.loads(DEMO[0].read_bytes())
        events[2]["data"]["frames"][2]["fingerprint"] = hash_of("different-leaf-3")
        self.apply(*events)
        self.stages(2, 2)
        self.assertEqual("IMAGE_MISMATCH", self.report()["edges"][0]["state"])

    def test_original_quality_tie_holds_distinct_images_then_recovers(self):
        self.stages(1, 4)
        self.apply(self.scan("tie-1", "R01-0001", 1, 28))
        self.assertEqual("ORIGINAL_SCAN_CONFLICT", self.page(1)["state"])
        self.apply(event("revoke-tie", "scan-check", scan="tie-1", minute=30, verdict="REVOKE"))
        self.assertEqual("front-1", self.page(1)["scan"])
        self.assertEqual("TENDER_ISSUED", self.tender("renewed")["state"])
        self.assertEqual(["WITHDRAWN", "CURRENT"], [v["state"] for v in self.report()["versions"]])
        self.assertEqual(self.report()["versions"][0]["sha"], self.report()["versions"][1]["sha"])

    def test_capture_session_and_fingerprint_are_independent(self):
        self.stages(1, 3)
        self.fixture("bad_capture", "capture")
        result = capture.assess(self.db)
        self.assertEqual("USABLE", result["front-1"]["state"])
        self.assertEqual("OUTSIDE_SESSION", result["at-close"]["state"])
        self.assertEqual("CONTENT_MISMATCH", result["wrong-image"]["state"])
        self.assertEqual("front-1", self.page(1)["scan"])

    def test_scan_first_session_and_quality_check_later(self):
        self.stages(1, 2)
        self.apply(self.scan("early-receipt", "R01-0001", 1, 23),
                   event("pre-capture-check", "scan-check", scan="early-receipt", minute=22, verdict="REVOKE"))
        self.assertEqual("SESSION_MISSING", capture.assess(self.db)["early-receipt"]["state"])
        self.apply(event("shift-1", "session", operator="real-operator", scanner="S1", start=23, end=24))
        self.assertEqual("early-receipt", self.page(1)["scan"])
        self.assertEqual([], capture.assess(self.db)["early-receipt"]["checks"])

    def test_library_approval_arrives_before_scan_and_can_use_other_certified_roll(self):
        self.stages(1, 8)
        self.assertEqual("RESHOOT_SCAN_PENDING", self.page(3)["state"])
        self.assertEqual("TENDER_DENIED", self.tender("pending-tender")["state"])
        self.stages(9, 10)
        approved = delivery.manifest(self.db, ISSUE, None)["manifest"]["pages"][2]
        self.assertEqual(("R02-0003", "R01-0003", "missing-3"),
                         (approved["canonical_frame"], approved["capture_frame"], approved["ticket"]))

    def test_library_wrong_ticket_wrong_page_and_effective_time(self):
        self.stages(1, 10)
        for i, (code, page, minute, ticket) in enumerate((("R01-0002", 2, 41, "missing-3"),
                                                          ("R01-0003", 3, 39, "missing-3"),
                                                          ("R01-0003", 3, 46, "other-ticket"),
                                                          ("R01-0003", 3, 99, "missing-3"))):
            with self.subTest(i=i):
                self.apply(self.scan(f"bad-{i}", code, page, minute, ticket),
                           self.signoff(f"accept-{i}", "ACCEPT", 70 + i, f"bad-{i}"))
                self.assertEqual("RESHOOT_SCOPE_MISMATCH", self.page(3)["state"])
        self.fixture("bad_library", "library")
        self.assertEqual("RESHOOT_SCOPE_MISMATCH", self.page(3)["state"])

    def test_new_notice_supersedes_and_same_time_conflicts(self):
        self.stages(1, 10)
        self.apply(event("same-time", "notice", issue=ISSUE, page=3, minute=40))
        self.assertEqual("NOTICE_CONFLICT", self.page(3)["state"])
        self.apply(event("new-notice", "notice", issue=ISSUE, page=3, minute=56))
        self.assertEqual("RESHOOT_REQUIRED", self.page(3)["state"])
        self.assertEqual("WITHDRAWN", delivery.manifest(self.db, ISSUE, 2)["state"])

    def test_library_equal_time_conflict_then_notice_dismissal(self):
        self.stages(1, 10)
        self.apply(self.signoff("conflicting-reopen", "REOPEN", 49))
        self.assertEqual("SIGNOFF_CONFLICT", self.page(3)["state"])
        self.apply(self.signoff("dismiss", "DISMISS", 50))
        self.assertEqual("NOTICE_DISMISSED", self.page(3)["library"]["state"])
        self.assertEqual("back-3", self.page(3)["scan"])
        self.apply(self.signoff("old-reopen", "REOPEN", 48))
        self.assertEqual("back-3", self.page(3)["scan"])
        self.apply(self.signoff("fresh-reopen", "REOPEN", 51))
        self.assertEqual("RESHOOT_REOPENED", self.page(3)["state"])

    def test_quality_dispute_requires_fresh_library_acceptance_and_tender(self):
        self.stages(1, 11)
        self.assertEqual("QUALITY_CONFLICT", capture.assess(self.db)["repair-3"]["state"])
        self.assertEqual("RESHOOT_SCAN_UNUSABLE", self.page(3)["state"])
        self.assertEqual("WITHDRAWN", delivery.manifest(self.db, ISSUE, 2)["state"])
        self.stages(12, 12)
        self.assertEqual("RESHOOT_SCAN_UNUSABLE", self.page(3)["state"])
        self.stages(13, 13)
        self.assertEqual("RESHOOT_ACCEPTED", self.page(3)["state"])
        with self.assertRaisesRegex(Rejected, "no current"):
            delivery.manifest(self.db, ISSUE, None)
        self.stages(14, 14)
        self.assertEqual(3, delivery.manifest(self.db, ISSUE, None)["seq"])
        self.assertEqual("repair-3", delivery.manifest(self.db, ISSUE, 2)["manifest"]["pages"][2]["scan"])

    def test_manifest_change_without_page_block_withdraws_current(self):
        self.stages(1, 4)
        self.apply(self.scan("higher-grade", "R01-0001", 1, 34, grade=95))
        self.assertEqual([], self.report()["blockers"])
        self.assertEqual("MANIFEST_CHANGED", self.report()["transitions"][0]["reason"])
        self.assertEqual("higher-grade", self.page(1)["scan"])

    def test_tender_of_unchanged_manifest_reuses_current(self):
        self.stages(1, 4)
        self.assertEqual({"id": "unchanged", "state": "ALREADY_CURRENT", "version": 1}, self.tender("unchanged"))
        self.assertEqual(1, len(self.report()["versions"]))

    def test_event_collision_rolls_back_full_batch_and_exact_retry_is_idempotent(self):
        self.stages(1, 4)
        same = ledger.apply(self.db, DEMO[3].read_bytes(), "tender")
        self.assertEqual(("ALREADY_IMPORTED", 4), (same["state"], same["batch"]))
        with self.assertRaisesRegex(Rejected, "immutable event id conflict"):
            self.apply(event("uncommitted", "issue", leaves=1, rolls=1), event(ISSUE, "issue", leaves=4, rolls=2))
        self.assertIsNone(one(self.db, "SELECT id FROM issues WHERE id='uncommitted'"))
        self.assertEqual(4, len(ledger.history(self.db, ISSUE)))
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_domain_and_input_validation_do_not_persist(self):
        with self.assertRaisesRegex(Rejected, "out-of-scope"):
            self.fixture("demo/01_layout", "capture")
        with self.assertRaisesRegex(Rejected, "duplicate JSON key"):
            ledger.apply(self.db, b'[{"id":"first", "id":"second"}]')
        with self.assertRaisesRegex(Rejected, "invalid leaves"):
            self.apply(event(ISSUE, "issue", leaves=True, rolls=1))
        self.assertEqual(0, ledger.audit(self.db)["batches"])

    def test_replay_detects_historical_version_and_raw_receipt_tampering(self):
        self.stages(1, 14)
        self.assertEqual({"audit": "MATCH", "batches": 14, "events": 26, "manifests": 3, "differences": []},
                         ledger.audit(self.db))
        with self.db:
            self.db.execute("UPDATE versions SET snapshot='{}' WHERE seq=1")
            self.db.execute("UPDATE snapshots SET report='{}' WHERE batch=4")
        self.assertEqual(["versions", "snapshots"], ledger.audit(self.db)["differences"])
        with self.assertRaisesRegex(Rejected, "checksum mismatch"):
            delivery.manifest(self.db, ISSUE, 1)
        with self.db:
            self.db.execute("UPDATE batches SET raw=X'5B5D' WHERE seq=2")
        self.assertEqual("raw SHA mismatch", ledger.audit(self.db)["reason"])


class JointCaptureWorkflowTests(unittest.TestCase):
    ISSUE = "JOINT-4"

    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)

    def apply(self, *events):
        return ledger.apply(self.db, json.dumps(events).encode())

    def issue_events(self, issue=ISSUE):
        return [
            event(issue, "issue", leaves=4, rolls=1),
            event("R1", "reel", issue=issue, rank=1, direction="forward", frames=[
                {"slot": page, "page": page, "fingerprint": hash_of(f"leaf-{page}")}
                for page in range(1, 5)
            ]),
            event("joint-shift", "session", operator="operator-J", scanner="camera-J",
                  start=0, end=200),
        ]

    def single(self, page, scan_id=None):
        scan_id = scan_id or f"p{page}"
        return event(scan_id, "scan", session="joint-shift", frame=f"R1-{page:04d}",
                     minute=10 + page, fingerprint=hash_of(f"leaf-{page}"),
                     image=hash_of(f"image-{scan_id}"), grade=90, ticket=None)

    def joint(self, joint_id, left, right, kind="joint-scan", minute=None):
        return event(joint_id, kind, session="joint-shift", minute=minute or (30 + left),
                     image=hash_of(f"image-{joint_id}"), grade=90, sides=[
                         {"side": "left", "frame": f"R1-{left:04d}", "page": left,
                          "fingerprint": hash_of(f"leaf-{left}")},
                         {"side": "right", "frame": f"R1-{right:04d}", "page": right,
                          "fingerprint": hash_of(f"leaf-{right}")},
                     ])

    def seed(self, singles=(1, 2, 4), joints=("J23", "J34")):
        events = self.issue_events()
        events.extend(self.single(page) for page in singles)
        if "J23" in joints:
            events.append(self.joint("J23", 2, 3))
        if "J34" in joints:
            events.append(self.joint("J34", 3, 4))
        self.apply(*events)

    def plan(self, anchor=None):
        return delivery.collect(self.db, self.ISSUE, anchor=anchor)

    def test_joint_receipt_records_one_image_two_sides_and_whole_qc(self):
        self.seed()
        assessed = capture.assess(self.db)
        j23 = assessed["J23"]
        self.assertEqual("USABLE", j23["state"])
        self.assertEqual([2, 3], j23["pages"])
        self.assertEqual(["R1-0002", "R1-0003"], j23["frame_codes"])
        self.assertEqual(hash_of("image-J23"), j23["image"])
        self.assertEqual("operator-J", j23["operator"])
        self.assertEqual("camera-J", j23["scanner"])
        self.assertEqual([hash_of("leaf-2"), hash_of("leaf-3")],
                         [side["fingerprint"] for side in j23["sides"]])

        self.apply(event("revoke-j23", "scan-check", scan="J23", minute=60, verdict="REVOKE"))
        self.assertEqual("SCAN_REVOKED", capture.assess(self.db)["J23"]["state"])
        self.assertFalse(self.plan("J23")["complete"])

    def test_exact_whole_cover_supports_either_retention_anchor(self):
        self.seed()
        unanchored = self.plan(anchor=False)
        self.assertTrue(unanchored["complete"], unanchored["blockers"])
        covered = [page for item in unanchored["items"] for page in item["pages"]]
        self.assertEqual([1, 2, 3, 4], sorted(covered))
        self.assertEqual(4, len(covered))
        self.assertFalse({"J23", "J34"} <= set(unanchored["selected_item_ids"]))

        expected = {
            "J23": {"p1", "J23", "p4"},
            "J34": {"p1", "p2", "J34"},
        }
        for anchor, ids in expected.items():
            with self.subTest(anchor=anchor):
                plan = self.plan(anchor)
                self.assertTrue(plan["complete"], plan["blockers"])
                self.assertEqual(ids, set(plan["selected_item_ids"]))
                manifest = delivery.collection_manifest(self.db, self.ISSUE, plan=plan)
                by_page = {entry["page"]: entry for entry in manifest["pages"]}
                self.assertEqual([1, 2, 3, 4], sorted(by_page))
                self.assertEqual(anchor, by_page[int(anchor[1])]["item"])
                self.assertEqual(anchor, by_page[int(anchor[2])]["item"])
                self.assertEqual(by_page[int(anchor[1])]["image"],
                                 by_page[int(anchor[2])]["image"])
                self.assertEqual(2, manifest["items"][[i["item"] for i in manifest["items"]].index(anchor)]["page_count"])
                for page, entry in by_page.items():
                    self.assertEqual(page, entry["page"])
                    self.assertIn(entry["kind"], ("scan", "joint-scan"))
                    self.assertIn(entry["source_kind"],
                                  ("original-scan", "reshoot-scan", "joint-scan"))
                    self.assertTrue(entry["item"])

    def test_persisted_anchor_drives_manifest_and_tender(self):
        self.seed()
        self.apply(event("keep-j23", "retention-anchor", issue=self.ISSUE,
                         joint="J23", minute=40),
                   event("tender-j23", "tender", issue=self.ISSUE))
        report = delivery.last_report(self.db, self.ISSUE)
        self.assertEqual("J23", report["anchor"])
        self.assertEqual("TENDER_ISSUED", report["tender"]["state"])
        current = delivery.manifest(self.db, self.ISSUE, None)
        self.assertEqual({"p1", "J23", "p4"},
                         {item["item"] for item in current["manifest"]["items"]})
        self.assertEqual("J23", current["manifest"]["pages"][1]["item"])
        self.assertEqual("J23", current["manifest"]["pages"][2]["item"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_anchor_failure_names_page_lost_to_occupied_joint(self):
        self.seed(singles=(1, 2))  # no independent page-4 original scan
        self.apply(event("keep-j23", "anchor", issue=self.ISSUE, joint="J23"))
        plan = self.plan()
        self.assertFalse(plan["complete"])
        self.assertEqual([4], plan["missing_pages"])
        diagnostic = plan["diagnostics"][0]
        self.assertEqual(4, diagnostic["page"])
        blocked = diagnostic["blocked_items"][0]
        self.assertEqual("J34", blocked["item"])
        self.assertEqual([3], blocked["overlap_pages"])
        self.assertEqual("J23", blocked["occupied_by"])

        report, _manifest = delivery.compose(self.db, self.ISSUE)
        self.assertEqual(["COLLECTION_PAGE_UNAVAILABLE:4"], report["blockers"])
        self.apply(event("denied", "tender", issue=self.ISSUE))
        self.assertEqual("TENDER_DENIED", delivery.last_report(self.db, self.ISSUE)["tender"]["state"])

    def test_independent_page_two_signoff_blocks_entire_j23(self):
        self.seed()
        self.apply(event("keep-j23", "retention-anchor", issue=self.ISSUE, joint="J23"),
                   event("tender-before-notice", "tender", issue=self.ISSUE))
        self.assertEqual("TENDER_ISSUED",
                         delivery.last_report(self.db, self.ISSUE)["tender"]["state"])

        self.apply(
            event("missing-2", "notice", issue=self.ISSUE, page=2, minute=40),
            event("repair-2", "scan", session="joint-shift", frame="R1-0002", minute=45,
                  fingerprint=hash_of("leaf-2"), image=hash_of("repair-2"), grade=95,
                  ticket="missing-2"),
            event("accept-2", "signoff", notice="missing-2", scan="repair-2",
                  officer="curator-J", minute=50, action="ACCEPT"),
        )
        report = delivery.last_report(self.db, self.ISSUE)
        self.assertEqual("RESHOOT_ACCEPTED", report["pages"][1]["library"]["state"])
        joint_report = next(joint for joint in report["joints"] if joint["id"] == "J23")
        self.assertFalse(joint_report["eligible"])
        self.assertEqual([2], joint_report["constraint"]["blocked_pages"])
        self.assertEqual([2], joint_report["constraint"]["independently_signed_pages"])
        self.assertTrue(any("RETENTION_ANCHOR_UNAVAILABLE:J23" in blocker
                            for blocker in report["blockers"]))
        self.assertEqual("WITHDRAWN", report["versions"][0]["state"])
        self.assertEqual({"p1", "J23", "p4"},
                         {item["item"] for item in delivery.manifest(self.db, self.ISSUE, 1)
                          ["manifest"]["items"]})

        alternative = self.plan(anchor=False)
        self.assertTrue(alternative["complete"])
        self.assertEqual({"p1", "repair-2", "J34"}, set(alternative["selected_item_ids"]))

        # Dismissing the late notice restores the unticketed J23 proof; a new
        # tender is required because evidence recovery does not self-issue.
        self.apply(event("dismiss-2", "signoff", notice="missing-2", scan=None,
                         officer="curator-J", minute=70, action="DISMISS"),
                   event("tender-recovered", "tender", issue=self.ISSUE))
        outcome = delivery.last_report(self.db, self.ISSUE)["tender"]
        self.assertEqual("TENDER_ISSUED", outcome["state"])
        self.assertEqual(2, outcome["version"])
        self.assertEqual({"p1", "J23", "p4"},
                         {item["item"] for item in delivery.manifest(self.db, self.ISSUE, None)
                          ["manifest"]["items"]})
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_late_revocation_and_recovery_preserve_signed_anchor_manifest(self):
        self.seed()
        self.apply(event("keep-j23", "retention-anchor", issue=self.ISSUE, joint="J23"),
                   event("tender-1", "tender", issue=self.ISSUE))
        version = delivery.manifest(self.db, self.ISSUE, None)
        self.assertEqual(1, version["seq"])

        self.apply(event("late-revoke-p4", "scan-check", scan="p4", minute=60,
                         verdict="REVOKE"))
        report = delivery.last_report(self.db, self.ISSUE)
        self.assertTrue(any(blocker == "COLLECTION_PAGE_UNAVAILABLE:4"
                            for blocker in report["blockers"]))
        self.assertEqual("WITHDRAWN", report["versions"][0]["state"])
        with self.assertRaisesRegex(Rejected, "no current"):
            delivery.manifest(self.db, self.ISSUE, None)
        old = delivery.manifest(self.db, self.ISSUE, 1)
        self.assertEqual(version["manifest"], old["manifest"])

        self.apply(event("late-clear-p4", "scan-check", scan="p4", minute=70,
                         verdict="CLEAR"),
                   event("tender-2", "tender", issue=self.ISSUE))
        outcome = delivery.last_report(self.db, self.ISSUE)["tender"]
        self.assertEqual("TENDER_ISSUED", outcome["state"])
        self.assertEqual(2, outcome["version"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_late_joint_revocation_withdraws_whole_artifact_and_resigns(self):
        self.seed()
        self.apply(event("keep-j34", "retention-anchor", issue=self.ISSUE, joint="J34"),
                   event("tender-j34-1", "tender", issue=self.ISSUE))
        self.assertEqual("J34", delivery.manifest(self.db, self.ISSUE, None)
                         ["manifest"]["items"][-1]["item"])

        self.apply(event("revoke-j34", "scan-check", scan="J34", minute=60, verdict="REVOKE"))
        self.assertEqual("SCAN_REVOKED", capture.assess(self.db)["J34"]["state"])
        report = delivery.last_report(self.db, self.ISSUE)
        self.assertTrue(any("RETENTION_ANCHOR_UNAVAILABLE:J34" in b for b in report["blockers"]))
        self.assertEqual("WITHDRAWN", report["versions"][0]["state"])
        self.assertEqual({"p1", "p2", "J34"},
                         {i["item"] for i in delivery.manifest(self.db, self.ISSUE, 1)
                          ["manifest"]["items"]})

        self.apply(event("clear-j34", "scan-check", scan="J34", minute=70, verdict="CLEAR"),
                   event("tender-j34-2", "tender", issue=self.ISSUE))
        outcome = delivery.last_report(self.db, self.ISSUE)["tender"]
        self.assertEqual("TENDER_ISSUED", outcome["state"])
        self.assertEqual(2, outcome["version"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_certified_seam_gates_canonical_frames_for_joint_sides(self):
        issue = "SEAM-JOINT"
        events = [
            event(issue, "issue", leaves=4, rolls=2),
            event("R1", "reel", issue=issue, rank=1, direction="forward", frames=[
                {"slot": 1, "page": 1, "fingerprint": hash_of("leaf-1")},
                {"slot": 2, "page": 2, "fingerprint": hash_of("leaf-2")},
                {"slot": 3, "page": 3, "fingerprint": hash_of("leaf-3")},
            ]),
            event("R2", "reel", issue=issue, rank=2, direction="reverse", frames=[
                {"slot": 1, "page": 4, "fingerprint": hash_of("leaf-4")},
                {"slot": 2, "page": 3, "fingerprint": hash_of("leaf-3")},
                {"slot": 3, "page": 2, "fingerprint": hash_of("leaf-2")},
            ]),
            event("joint-shift", "session", operator="O", scanner="C", start=0, end=200),
            event("p1", "scan", session="joint-shift", frame="R1-0001", minute=11,
                  fingerprint=hash_of("leaf-1"), image=hash_of("img1"), grade=90, ticket=None),
            event("p2", "scan", session="joint-shift", frame="R1-0002", minute=12,
                  fingerprint=hash_of("leaf-2"), image=hash_of("img2"), grade=90, ticket=None),
            event("p4", "scan", session="joint-shift", frame="R2-0001", minute=14,
                  fingerprint=hash_of("leaf-4"), image=hash_of("img4"), grade=90, ticket=None),
            self.joint("J23", 2, 3),
        ]
        # self.joint uses self.ISSUE only indirectly? Frame IDs are reel R1, so okay.
        self.apply(*events)
        blocked = delivery.collect(self.db, issue, anchor="J23")
        self.assertFalse(blocked["complete"])
        self.assertIn("RETENTION_ANCHOR_UNAVAILABLE:J23", blocked["blockers"])

        pairs = [["R1-0002", "R2-0003"], ["R1-0003", "R2-0002"]]
        self.apply(
            event("op-witness", "witness", left_reel="R1", right_reel="R2",
                  role="operator", signer="op-1", minute=10, primary_reel="R1", pairs=pairs),
            event("lib-witness", "witness", left_reel="R1", right_reel="R2",
                  role="library", signer="lib-1", minute=11, primary_reel="R1", pairs=pairs),
        )
        plan = delivery.collect(self.db, issue, anchor="J23")
        self.assertTrue(plan["complete"], plan["blockers"])
        self.assertEqual({"p1", "p4", "J23"}, set(plan["selected_item_ids"]))
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_double_scan_alias_and_left_right_receipt_shape(self):
        fp2, fp3 = hash_of("leaf-2"), hash_of("leaf-3")
        self.apply(
            event(self.ISSUE, "issue", leaves=3, rolls=1),
            event("R1", "reel", issue=self.ISSUE, rank=1, direction="forward", frames=[
                {"slot": 2, "page": 2, "fingerprint": fp2},
                {"slot": 3, "page": 3, "fingerprint": fp3},
            ]),
            event("shift", "session", operator="O", scanner="C", start=0, end=10),
            event("D23", "double-scan", shift="shift", captured_minute=5,
                  image_digest=hash_of("D23"), quality=90,
                  left={"code": "R1-0002", "logical_page": 2, "content_fingerprint": fp2},
                  right={"code": "R1-0003", "logical_page": 3, "content_fingerprint": fp3}),
        )
        self.assertEqual("USABLE", capture.assess(self.db)["D23"]["state"])
        self.assertEqual([2, 3], capture.assess(self.db)["D23"]["pages"])

