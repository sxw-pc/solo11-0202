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

    # ----- shift-wide clock calibration and delivery time proofs -----

    CLOCK_EXPOSURES = {"front-1": 1, "front-2": 2, "front-3": 3, "back-2": 4,
                       "back-3": 5, "back-4": 6, "back-5": 7, "repair-3": 8}

    def calibration(self, id="clock-review", **overrides):
        data = {"session": "shift-1", "seq_map": self.CLOCK_EXPOSURES, "boundaries": [7],
                "left_offsets": [0], "right_offsets": [0, 4]}
        data.update(overrides)
        return event(id, "clock-calibration", **data)

    def tender_proof(self, id, proof):
        self.apply(event(id, "tender", issue=ISSUE, proof=proof))
        return self.report()["tender"]

    def window_scenario(self):
        self.stages(1, 9)
        self.apply(self.calibration())
        self.assertEqual(44, one(self.db, "SELECT minute FROM scans WHERE id='repair-3'")["minute"])
        clock = capture.possible_minutes(self.db, "repair-3")
        self.assertEqual([44, 48], clock)
        accepted = self.page(3)
        self.assertEqual("RESHOOT_ACCEPTED", accepted["state"])
        self.assertEqual([44, 48], accepted["library"]["clock"]["possible_minutes"])
        self.assertEqual([44, 48], accepted["library"]["clock"]["time_window"])
        self.assertTrue(accepted["library"]["clock"]["all_authorized"])

        denied = self.tender_proof("exact-before-reading", "exact")
        self.assertEqual("TENDER_DENIED", denied["state"])
        self.assertTrue(any("EXACT_TIME_PROOF_REQUIRED" in b for b in self.report()["blockers"]))

        issued = self.tender_proof("window-delivery", "window")
        self.assertEqual("TENDER_ISSUED", issued["state"])
        window_version = issued["version"]
        proof_page = delivery.manifest(self.db, ISSUE, None)["manifest"]["pages"][2]
        self.assertEqual("repair-3", proof_page["scan"])
        self.assertEqual(44, proof_page["captured_minute"])
        self.assertEqual(44, proof_page["original_minute"])
        self.assertEqual([44, 48], proof_page["possible_minutes"])
        self.assertEqual([44, 48], proof_page["time_window"])
        self.assertEqual("window", proof_page["time_proof"])
        self.assertIsNone(proof_page["corrected_minute"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])
        return window_version

    def test_window_proof_issues_for_minutes_44_or_48_exact_awaits_proof(self):
        self.window_scenario()

    def test_supplemented_reading_pins_48_and_exact_proof_issues(self):
        window_version = self.window_scenario()
        self.apply(event("reading-48", "clock-reading", session="shift-1", seq=8, minute=48))
        self.assertEqual([48], capture.possible_minutes(self.db, "repair-3"))
        last_change = self.report()["transitions"][-1]
        self.assertEqual(window_version, last_change["version"])
        self.assertEqual("WITHDRAWN", last_change["to"])

        issued = self.tender_proof("exact-after-reading", "exact")
        self.assertEqual("TENDER_ISSUED", issued["state"])
        proof_page = delivery.manifest(self.db, ISSUE, None)["manifest"]["pages"][2]
        self.assertEqual([48], proof_page["possible_minutes"])
        self.assertEqual([48, 48], proof_page["time_window"])
        self.assertEqual(48, proof_page["corrected_minute"])
        self.assertEqual("exact", proof_page["time_proof"])
        # The withdrawn window manifest remains independently verifiable.
        old = delivery.manifest(self.db, ISSUE, window_version)
        self.assertEqual("WITHDRAWN", old["state"])
        self.assertEqual([44, 48], old["manifest"]["pages"][2]["possible_minutes"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def seed_clock_world(self, *, start=0, end=1000, notice_minute=0, signoff_minute=50):
        self.apply(event(ISSUE, "issue", leaves=1, rolls=1),
                   event("R1", "reel", issue=ISSUE, rank=1, direction="forward", frames=[
                       {"slot": 1, "page": 1, "fingerprint": hash_of("leaf-1")}]),
                   event("clock-shift", "session", operator="O", scanner="B", start=start, end=end),
                   event("solo", "scan", session="clock-shift", frame="R1-0001", minute=10,
                         fingerprint=hash_of("leaf-1"), image=hash_of("img-solo"), grade=90, ticket="n1"),
                   event("partner", "scan", session="clock-shift", frame="R1-0001", minute=12,
                         fingerprint=hash_of("leaf-1"), image=hash_of("img-partner"), grade=90, ticket=None),
                   event("n1", "notice", issue=ISSUE, page=1, minute=notice_minute),
                   event("accept-1", "signoff", notice="n1", scan="solo", officer="C",
                         minute=signoff_minute, action="ACCEPT"))

    def test_individual_offsets_plausible_but_no_shared_shift_interpretation(self):
        self.seed_clock_world()
        # Seq 1 raw 10 needs offset 0; seq 2 raw 12 needs offset +5. Both share
        # the single left offset, so no whole-shift timeline survives.
        self.apply(event("no-common-clock", "calibration", session="clock-shift",
                         exposures=[{"scan": "solo", "seq": 1}, {"scan": "partner", "seq": 2}],
                         boundaries=[2], left_offsets=[0, 5], right_offsets=[0],
                         readings=[{"seq": 1, "minute": 10}, {"seq": 2, "minute": 17}]))
        shift = capture.calibrate(self.db, "clock-shift")
        self.assertEqual("CLOCK_UNCALIBRATABLE", shift["state"])
        self.assertEqual([], shift["timelines"])
        self.assertEqual("CLOCK_UNCALIBRATABLE", self.page(1)["state"])
        self.assertEqual("TENDER_DENIED", self.tender_proof("deny-no-common", "window")["state"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_reversed_corrected_order_is_excluded_but_same_minute_tie_allowed(self):
        self.seed_clock_world()
        # Seq 1 raw 10 + 20 = 30 while seq 2 raw 12 + 0 = 12: individually in
        # session, but the shared timeline puts later exposure earlier -> reject.
        self.apply(event("reverse-clock", "calibration", session="clock-shift",
                         seq_map={"solo": 1, "partner": 2}, boundaries=[1],
                         left_offsets=[20], right_offsets=[0]))
        shift = capture.calibrate(self.db, "clock-shift")
        self.assertEqual("CLOCK_UNCALIBRATABLE", shift["state"])
        self.assertEqual([], shift["timelines"])
        self.assertEqual("CLOCK_UNCALIBRATABLE", self.page(1)["state"])

        # +2 on the first exposure ties both at minute 12; ties are permitted.
        tied = capture.enumerate_timelines({1: 10, 2: 12}, [1], [2], [0])
        self.assertEqual([{1: 12, 2: 12}], [t["minutes"] for t in tied])
        reversed_only = capture.enumerate_timelines({1: 10, 2: 12}, [1], [20], [0])
        self.assertEqual([], reversed_only)

    def test_legal_timeline_crossing_signoff_is_a_distinct_state(self):
        self.stages(1, 9)
        self.apply(self.calibration("crossing-clock", right_offsets=[0, 6]))
        self.assertEqual([44, 50], capture.possible_minutes(self.db, "repair-3"))
        page = self.page(3)
        self.assertEqual("RESHOOT_SIGNOFF_CROSSED", page["state"])
        clock = page["library"]["clock"]
        self.assertTrue(clock["crosses_signoff"])
        self.assertFalse(clock["all_authorized"])
        # The covering window [44, 50] leaves the notice-40/signoff-49 authority.
        self.assertEqual([44, 50], clock["time_window"])
        self.assertEqual("TENDER_DENIED", self.tender_proof("window-crossing", "window")["state"])
        self.assertEqual("TENDER_DENIED", self.tender_proof("exact-crossing", "exact")["state"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_material_missing_is_distinguishable_from_uncalibratable(self):
        self.stages(1, 9)
        self.apply(self.calibration("dangling-reading", readings=[{"seq": 99, "minute": 45}]))
        page = self.page(3)
        self.assertEqual("CLOCK_MATERIAL_MISSING", page["state"])
        self.assertNotEqual("CLOCK_UNCALIBRATABLE", page["state"])
        self.assertEqual("TENDER_DENIED", self.tender_proof("missing-material", "window")["state"])

        # A model without candidate offsets is likewise incomplete material.
        db2 = connect(":memory:")
        self.addCleanup(db2.close)
        ledger.apply(db2, json.dumps([
            event("s", "session", operator="O", scanner="B", start=0, end=10),
            event("c", "clock-calibration", session="s", seq_map={"x": 1}, boundaries=[0])]).encode())
        self.assertEqual("CLOCK_MATERIAL_MISSING", capture.calibrate(db2, "s")["state"])

    def test_named_scan_without_sequence_is_material_missing(self):
        self.stages(1, 9)
        self.apply(self.calibration("repair-unlisted", seq_map={"front-1": 1}, boundaries=[1]))
        self.assertEqual("CLOCK_MATERIAL_MISSING", self.page(3)["state"])

    def test_late_quality_revocation_withdraws_delivered_window_version(self):
        self.stages(1, 9)
        self.apply(self.calibration())
        issued = self.tender_proof("window-first", "window")
        self.assertEqual("TENDER_ISSUED", issued["state"])
        version = issued["version"]
        # Late trusted QC revocation at minute 48 binds on every possible minute.
        self.apply(event("late-revoke", "scan-check", scan="repair-3", minute=48, verdict="REVOKE"))
        page = self.page(3)
        self.assertEqual("RESHOOT_SCAN_UNUSABLE", page["state"])
        self.assertIn("SCAN_REVOKED", page["library"]["capture_states"])
        transition = self.report()["transitions"][-1]
        self.assertEqual(version, transition["version"])
        self.assertEqual("EVIDENCE_BLOCKED", transition["reason"])
        self.assertIsNone(next((v for v in self.report()["versions"] if v["state"] == "CURRENT"), None))
        with self.assertRaisesRegex(Rejected, "no current"):
            delivery.manifest(self.db, ISSUE, None)
        self.assertEqual("TENDER_DENIED", self.tender_proof("window-after-revoke", "window")["state"])
        self.assertEqual("MATCH", ledger.audit(self.db)["audit"])

    def test_quality_cohort_is_compared_at_corrected_minute(self):
        self.seed_clock_world(end=200)
        # Raw 10 + 10 = corrected 20. A check at minute 15 follows the raw
        # capture but precedes the corrected one, so it must not take effect.
        self.apply(event("shift-1", "clock-calibration", session="clock-shift",
                         seq_map={"solo": 1}, boundaries=[0], left_offsets=[0], right_offsets=[10]))
        self.apply(event("early-revoke", "scan-check", scan="solo", minute=15, verdict="REVOKE"))
        clock = capture.calibrated_scan_clocks(self.db)["solo"]
        self.assertEqual([(20, "USABLE")], [(t["minute"], t["state"]) for t in clock["timelines"]])
        # Same corrected-minute CLEAR+REVOKE cohort stays suspended.
        self.apply(event("same-revoke", "scan-check", scan="solo", minute=20, verdict="REVOKE"),
                   event("same-clear", "scan-check", scan="solo", minute=20, verdict="CLEAR"))
        clock = capture.calibrated_scan_clocks(self.db)["solo"]
        self.assertEqual("QUALITY_CONFLICT", clock["timelines"][0]["state"])
        self.assertEqual("RESHOOT_SCAN_UNUSABLE", self.page(1)["state"])

    def test_session_window_is_left_closed_right_open_under_correction(self):
        self.seed_clock_world(start=40, end=48, signoff_minute=49)
        # solo raw minute 10 + offset 38 lands exactly on the exclusive end 48.
        self.apply(event("edge-clock", "calibration", session="clock-shift",
                         seq_map={"solo": 1, "partner": 2}, boundaries=[2],
                         left_offsets=[38], right_offsets=[0]))
        clock = capture.calibrated_scan_clocks(self.db)["solo"]
        self.assertEqual([(48, "OUTSIDE_SESSION")],
                         [(t["minute"], t["state"]) for t in clock["timelines"]])
        self.assertIn("OUTSIDE_SESSION", self.page(1)["library"]["capture_states"])
        self.assertEqual("TENDER_DENIED", self.tender_proof("closed-right", "window")["state"])

    def test_calibration_rejects_more_than_eight_offsets_and_duplicate_sequence(self):
        nine = list(range(9))
        with self.assertRaisesRegex(Rejected, "at most 8"):
            self.apply(self.calibration("too-many", right_offsets=nine))
        with self.assertRaisesRegex(Rejected, "duplicate exposure sequence"):
            self.apply(event("dup-seq", "calibration", session="shift-1",
                             exposures=[{"scan": "a", "seq": 1}, {"scan": "b", "seq": 1}],
                             boundaries=[1], left_offsets=[0], right_offsets=[0]))
        self.assertEqual(0, one(self.db, "SELECT count(*) AS n FROM clock_evidence")["n"])

    def test_calibration_accepts_alternative_field_names_and_scan_seq(self):
        self.stages(1, 9)
        # A scan may carry its exported sequence directly; evidence uses the
        # before/after naming and trusted_readings alias.
        alt = event("alt-clock", "time-calibration", shift="shift-1",
                    exposures=["front-1", "front-2", "front-3", "back-2", "back-3",
                              "back-4", "back-5", "repair-3"],
                    jump_after=[7], offsets_before=[0], offsets_after=[0, 4],
                    trusted_readings=[{"exposure": 8, "trusted_minute": 48}])
        self.apply(alt)
        self.assertEqual([48], capture.possible_minutes(self.db, "repair-3"))
        self.assertEqual("TENDER_ISSUED", self.tender_proof("alt-exact", "exact")["state"])

    def test_scan_carried_exported_sequence_drives_calibration(self):
        self.apply(event(ISSUE, "issue", leaves=1, rolls=1),
                   event("R1", "reel", issue=ISSUE, rank=1, direction="forward", frames=[
                       {"slot": 1, "page": 1, "fingerprint": hash_of("leaf-1")}]),
                   event("clock-shift", "session", operator="O", scanner="B", start=0, end=1000),
                   event("solo", "scan", session="clock-shift", frame="R1-0001", minute=10, seq=1,
                         fingerprint=hash_of("leaf-1"), image=hash_of("img-solo"), grade=90, ticket="n1"),
                   event("partner", "scan", session="clock-shift", frame="R1-0001", minute=12, seq=2,
                         fingerprint=hash_of("leaf-1"), image=hash_of("img-partner"), grade=90, ticket=None),
                   event("n1", "notice", issue=ISSUE, page=1, minute=0),
                   event("accept-1", "signoff", notice="n1", scan="solo", officer="C",
                         minute=50, action="ACCEPT"),
                   event("seq-clock", "clock-calibration", session="clock-shift", boundaries=[1],
                         left_offsets=[0], right_offsets=[3]))
        self.assertEqual([10], capture.possible_minutes(self.db, "solo"))
        self.assertEqual([15], capture.possible_minutes(self.db, "partner"))
        self.assertEqual("RESHOOT_ACCEPTED", self.page(1)["state"])

    def test_uncalibrated_shift_keeps_singleton_raw_minute_proof(self):
        self.stages(1, 4)
        self.tender("raw-proof")
        page = delivery.manifest(self.db, ISSUE, None)["manifest"]["pages"][0]
        self.assertEqual([21], page["possible_minutes"])
        self.assertEqual([21, 21], page["time_window"])
        self.assertEqual(21, page["corrected_minute"])
        self.assertEqual("exact", page["time_proof"])
