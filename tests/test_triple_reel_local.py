import json
import unittest
from hashlib import sha256

from microfilm_reshoot import capture, delivery, layout, ledger, seams as seams_mod
from microfilm_reshoot.storage import connect, one


def event(ident, kind, **data):
    return {"id": ident, "kind": kind, "data": data}


def hash_of(value):
    return sha256(value.encode()).hexdigest()




class TripleReelPathTests(unittest.TestCase):
    """Three-reel overlap routing and edition-wide source paths."""

    ISSUE = "TRIPLE-10"

    def setUp(self):
        self.db = connect(":memory:")
        self.addCleanup(self.db.close)

    def apply(self, *events):
        return ledger.apply(self.db, json.dumps(events).encode())

    def frame(self, slot, page):
        return {"slot": slot, "page": page, "fingerprint": hash_of(f"leaf-{page}")}

    def event(self, ident, kind, **data):
        return event(ident, kind, **data)

    def witness(self, ident, role, signer, minute, left, right, primary, pairs):
        return self.event(ident, "witness", left_reel=left, right_reel=right, role=role,
                          signer=signer, minute=minute, primary_reel=primary, pairs=pairs)

    # R1=1..6 forward, R2=3..8 reverse (slots map 8..3), R3=5..10 forward.
    P12 = [["R1-0003", "R2-0006"], ["R1-0004", "R2-0005"],
           ["R1-0005", "R2-0004"], ["R1-0006", "R2-0003"]]
    P23 = [["R2-0004", "R3-0001"], ["R2-0003", "R3-0002"],
           ["R2-0002", "R3-0003"], ["R2-0001", "R3-0004"]]

    def world(self, *, caliber_field=None, scans=True):
        data = {"leaves": 10, "rolls": 3}
        if caliber_field:
            data.update(caliber_field)
        events = [
            self.event(self.ISSUE, "issue", **data),
            self.event("R1", "reel", issue=self.ISSUE, rank=1, direction="forward",
                       frames=[self.frame(i, i) for i in range(1, 7)]),
            self.event("R2", "reel", issue=self.ISSUE, rank=2, direction="reverse",
                       frames=[self.frame(s, p) for s, p in
                               zip(range(1, 7), [8, 7, 6, 5, 4, 3])]),
            self.event("R3", "reel", issue=self.ISSUE, rank=3, direction="forward",
                       frames=[self.frame(i, i + 4) for i in range(1, 7)]),
            self.event("shift-1", "session", operator="O", scanner="S", start=0, end=1000),
            self.witness("w12o", "operator", "op-a", 10, "R1", "R2", "R1", self.P12),
            self.witness("w12l", "library", "lib-a", 11, "R1", "R2", "R1", self.P12),
            self.witness("w23o", "operator", "op-b", 12, "R2", "R3", "R3", self.P23),
            self.witness("w23l", "library", "lib-b", 13, "R2", "R3", "R3", self.P23),
        ]
        if scans:
            available = [("s1", "R1-0001", 1), ("s2", "R1-0002", 2), ("s3", "R1-0003", 3),
                         ("s4", "R1-0004", 4), ("s5", "R3-0001", 5), ("s6", "R1-0006", 6),
                         ("s7", "R3-0003", 7), ("s8", "R3-0004", 8), ("s9", "R3-0005", 9),
                         ("s10", "R3-0006", 10)]
            for minute, (sid, code, page) in enumerate(available, start=20):
                events.append(self.scan(sid, code, page, minute))
        self.apply(*events)

    def scan(self, ident, code, page, minute, grade=90):
        return self.event(ident, "scan", session="shift-1", frame=code, minute=minute,
                          fingerprint=hash_of(f"leaf-{page}"), image=hash_of("img-" + ident),
                          grade=grade, ticket=None)

    def report(self):
        return delivery.last_report(self.db, self.ISSUE)

    def page(self, number):
        return self.report()["pages"][number - 1]

    def compose(self, caliber=None):
        issue = one(self.db, "SELECT * FROM issues WHERE id=?", self.ISSUE)
        report, _ = delivery.compose(self.db, issue, capture.assess(self.db), caliber=caliber)
        return report

    def tender(self, ident, **data):
        self.apply(self.event(ident, "tender", issue=self.ISSUE, **data))
        return self.report()["tender"]

    def reels(self, report, pages=range(1, 11), key="reel"):
        rows = {page["page"]: page for page in report["pages"]}
        return [rows[p][key] for p in pages]

    def test_both_seams_certify_and_triple_pairs_join_same_middle_frame(self):
        self.world()
        report = self.compose()
        self.assertEqual([(["R1", "R2"], "CERTIFIED", "R1"),
                          (["R2", "R3"], "CERTIFIED", "R3")],
                         [(e["reels"], e["state"], e["primary"]) for e in report["edges"]])
        for number in (5, 6):
            page = self.page(number)
            self.assertTrue(page["triple"])
            self.assertEqual(["R1", "R3"], page["candidate_reels"])
            self.assertEqual(["R1-%04d" % number, "R2-%04d" % (9 - number),
                              "R3-%04d" % (number - 4)], page["chain"])
            self.assertEqual(2, len(page["evidence"]["seams"]))
            self.assertEqual(4, len(page["evidence"]["witnesses"]))
        # Direct seam router exposes the candidate frame set too.
        reels, by_reel, by_page = layout.map_issue(
            self.db, one(self.db, "SELECT * FROM issues WHERE id=?", self.ISSUE))
        state, decision = seams_mod.route(by_page[5], report["edges"])
        self.assertEqual("ROUTED", state)
        self.assertEqual(["R1-0005", "R3-0001"], [f["id"] for f in decision])
        self.assertEqual("R1-0005", decision["id"])

    def test_unique_zigzag_path_continuous_denied_patch_issued(self):
        self.world()
        report = self.compose()
        continuous, patch = report["paths"]["continuous"], report["paths"]["patch"]
        self.assertFalse(continuous["ready"])
        self.assertTrue(patch["ready"])
        self.assertEqual(["R1", "R1", "R1", "R1", "R3", "R1", "R3", "R3", "R3", "R3"],
                         patch["reels"])
        self.assertEqual([6], patch["patch_pages"])
        self.assertEqual(3, patch["switches"])
        # Neighbouring known canonical reels force R1,R3,R1,R3 on pages 4..7.
        self.assertEqual(["R1", "R3", "R1", "R3"], self.reels(report, range(4, 8)))
        # Every page independently has a usable image even under continuous.
        self.assertTrue(all(page["scan"] for page in report["pages"]))
        denied = self.tender("continuous-tender")
        self.assertEqual("TENDER_DENIED", denied["state"])
        self.assertTrue(any("SOURCE_PATH" in b and "CONTINUOUS" in b
                            for b in self.report()["blockers"]))
        # The library-licensed patch edition issues a complete provenance list.
        issued = self.tender("patch-tender", mode="patch")
        self.assertEqual("TENDER_ISSUED", issued["state"])
        manifest = delivery.manifest(self.db, self.ISSUE, None)["manifest"]
        pages = manifest["pages"]
        self.assertEqual(["R1", "R3", "R1", "R3"],
                         [pages[p - 1]["reel"] for p in (4, 5, 6, 7)])
        self.assertEqual("R3-0001", pages[4]["canonical_frame"])
        self.assertEqual("R1-0006", pages[5]["canonical_frame"])
        marks = [page["interpolated"] for page in pages]
        self.assertEqual([False] * 5 + [True] + [False] * 4, marks)
        self.assertTrue(pages[5]["patch"])
        self.assertEqual("INTERPOLATED", pages[5]["annotation"])
        self.assertEqual(["R3", "R1", "R3"], pages[5]["patch_detail"]["sandwich"])
        self.assertEqual(2, len(pages[5]["evidence"]["seams"]))
        self.assertIn("w12o", pages[5]["witnesses"])
        self.assertIn("w23l", pages[5]["witnesses"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_late_r1_page_five_scan_prefers_fewer_switch_r1_source(self):
        self.world()
        self.apply(self.scan("s5-r1", "R1-0005", 5, 40, grade=95))
        report = self.compose("continuous")
        path = report["source_path"]
        self.assertTrue(path["ready"])
        self.assertEqual(["R1"] * 6 + ["R3"] * 4, path["reels"])
        self.assertEqual(1, path["switches"])
        self.assertEqual("R1-0005", self.page(5)["route"])
        # Patch planning also prefers the fewer-switch continuous source.
        patch = self.compose("patch")["paths"]["patch"]
        self.assertEqual(["R1"] * 6 + ["R3"] * 4, patch["reels"])
        self.assertEqual([], patch["patch_pages"])
        issued = self.tender("r1-continuous", edition="continuous")
        self.assertEqual("TENDER_ISSUED", issued["state"])
        entry = delivery.manifest(self.db, self.ISSUE, None)["manifest"]["pages"][4]
        self.assertEqual(("R1-0005", "s5-r1"), (entry["canonical_frame"], entry["scan"]))
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_lost_one_certification_cannot_stitch_three_reel_source(self):
        self.world()
        self.tender("patch-first", mode="patch")
        self.assertEqual("CURRENT", self.report()["versions"][-1]["state"])
        conflict = self.witness("w23l2", "library", "lib-b", 30, "R2", "R3", "R2",
                                [pair for pair in self.P23])
        self.apply(conflict)
        report = self.report()
        self.assertEqual("WITNESS_CONFLICT", report["edges"][1]["state"])
        # Triple authorization is gone: no R1+R3 candidate set is reconstructed.
        for number in (5, 6):
            self.assertIsNone(self.page(number)["route"])
            self.assertEqual([], self.page(number)["candidate_reels"])
        self.assertTrue(any(b.startswith("SEAM:R2/R3:") for b in report["blockers"]))
        self.assertEqual("WITHDRAWN", report["versions"][-1]["state"])
        self.assertEqual("EVIDENCE_BLOCKED", report["transitions"][-1]["reason"])
        # The historical issued list remains independently verifiable.
        old = delivery.manifest(self.db, self.ISSUE, 1)
        self.assertEqual("WITHDRAWN", old["state"])
        self.assertTrue(old["manifest"]["pages"][5]["interpolated"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_restored_certification_allows_new_tender(self):
        self.world()
        self.tender("patch-first", mode="patch")
        self.apply(self.witness("w23l2", "library", "lib-b", 30, "R2", "R3", "R2", self.P23))
        self.assertEqual("WITHDRAWN", self.report()["versions"][-1]["state"])
        # Restore a consistent R3-primary cohort with fresh independent signers.
        self.apply(self.witness("w23o2", "operator", "op-b2", 31, "R2", "R3", "R3", self.P23),
                   self.witness("w23l3", "library", "lib-b2", 32, "R2", "R3", "R3", self.P23))
        self.assertEqual("CERTIFIED", self.report()["edges"][1]["state"])
        outcome = self.tender("patch-again", mode="patch")
        self.assertEqual("TENDER_ISSUED", outcome["state"])
        self.assertEqual(2, outcome["version"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_unusable_and_conflicting_images_block_candidates(self):
        self.world()
        # Revoke the only usable page-6 R1 scan.
        self.apply(self.event("revoke-6", "scan-check", scan="s6", minute=30, verdict="REVOKE"))
        patch = self.compose("patch")["paths"]["patch"]
        self.assertFalse(patch["ready"])
        self.assertEqual([6], patch["missing_pages"])
        self.assertEqual("TENDER_DENIED", self.tender("denied", mode="patch")["state"])
        # A fresh usable receipt restores the patch path.
        self.apply(self.scan("s6-b", "R1-0006", 6, 45, grade=92))
        self.assertTrue(self.compose("patch")["paths"]["patch"]["ready"])
        self.assertEqual("TENDER_ISSUED", self.tender("granted", mode="patch")["state"])
        self.assertEqual("s6-b", delivery.manifest(self.db, self.ISSUE, None)
                         ["manifest"]["pages"][5]["scan"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_same_grade_distinct_images_on_candidate_hold_for_tiebreak(self):
        self.world()
        # A same-grade, different-image receipt for R3 page 5 creates the
        # established equal-grade/different-image hold on that candidate.
        self.apply(self.event("tie5", "scan", session="shift-1", frame="R3-0001", minute=41,
                              fingerprint=hash_of("leaf-5"), image=hash_of("different"),
                              grade=90, ticket=None))
        page5 = self.compose("patch")["pages"][4]
        candidate = next(c for c in page5["candidates"] if c["reel"] == "R3")
        self.assertEqual("ORIGINAL_SCAN_CONFLICT", candidate["state"])
        patch = self.compose("patch")["paths"]["patch"]
        self.assertFalse(patch["ready"])
        self.assertIn(5, patch["missing_pages"])
        # Revoking one side resolves the tie and restores the unique zigzag.
        self.apply(self.event("revoke-tie", "scan-check", scan="tie5", minute=42,
                              verdict="REVOKE"))
        self.assertTrue(self.compose("patch")["paths"]["patch"]["ready"])
        self.assertEqual(["R1", "R3", "R1", "R3"],
                         self.reels(self.compose("patch"), range(4, 8)))

    def test_edition_caliber_intake_aliases_all_authorize_patch(self):
        # Caliber declared on the issue itself.
        self.world(caliber_field={"basis": "patch-edition"})
        self.assertEqual("patch", self.compose()["caliber"])
        self.assertEqual("TENDER_ISSUED", self.tender("t1")["state"])

        # Caliber declared by a standalone policy event.
        db = connect(":memory:")
        self.addCleanup(db.close)
        original_db = self.db
        self.db = db
        try:
            self.world()
            for kind in ("edition-policy", "patch-permit", "edition-caliber", "edition-type"):
                ident = "policy-" + kind
                self.apply(self.event(ident, kind, issue=self.ISSUE, allowed=True,
                                      caliber="patch", officer="curator-1"))
                issue = one(self.db, "SELECT * FROM issues WHERE id=?", self.ISSUE)
                report, _ = delivery.compose(self.db, issue, capture.assess(self.db))
                self.assertEqual("patch", report["caliber"], kind)
                row = one(self.db, "SELECT caliber FROM edition_policies WHERE id=?", ident)
                self.assertEqual("patch", row["caliber"])
                # A later batch replays cleanly.
                self.assertEqual("MATCH", ledger.audit(self.db)["audit"])
        finally:
            self.db = original_db

    def test_continuous_is_the_default_discipline(self):
        self.world()
        report = self.compose()
        self.assertEqual("continuous", report["caliber"])
        self.assertFalse(report["paths"]["continuous"]["ready"])
        self.assertTrue(report["paths"]["patch"]["ready"])

    def test_audit_match_on_full_lifecycle(self):
        self.world()
        self.tender("denied-continuous")
        self.tender("issued-patch", edition="patch")
        self.apply(self.scan("late-r1-5", "R1-0005", 5, 60, grade=99))
        self.assertEqual("MANIFEST_CHANGED", self.report()["transitions"][-1]["reason"])
        self.tender("reissued-continuous", mode="continuous")
        audit = ledger.audit(self.db)
        self.assertEqual("MATCH", audit["audit"])
        self.assertEqual([], audit["differences"])
