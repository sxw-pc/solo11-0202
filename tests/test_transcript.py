from contextlib import closing
import json
import unittest

from microfilm_reshoot import ledger, transcript
from microfilm_reshoot.storage import Rejected, connect
from test_workflows import DEMO, FIXTURES, event


class TranscriptTests(unittest.TestCase):
    def test_transcript_preview_and_withdrawn_capture(self):
        with closing(connect(":memory:")) as db:
            for path in DEMO[:3]:
                ledger.apply(db, path.read_bytes())
            packet = json.loads((FIXTURES / "transcript.json").read_bytes())
            report = transcript.collect(db, packet)
            self.assertEqual(report["text"], "The office\nopens")
            self.assertEqual([(s["start"], s["end"]) for s in report["strips"]], [(0, 3), (4, 10), (11, 16)])
            self.assertEqual((report["scan"], report["frame"], report["page"]), ("front-1", "R01-0001", 1))
            invalid = event("no-front", "scan-check", scan="front-1", minute=40, verdict="REVOKE")
            ledger.apply(db, json.dumps([invalid]).encode())
            self.assertEqual(transcript.collect(db, packet)["state"], "SOURCE_PENDING")

    def test_overlapping_boxes_require_a_fresh_packet(self):
        with closing(connect(":memory:")) as db:
            for path in DEMO[:3]:
                ledger.apply(db, path.read_bytes())
            packet = json.loads((FIXTURES / "transcript.json").read_bytes())
            packet["strips"][1]["right"] = 5
            with self.assertRaisesRegex(Rejected, "overlapping strip boxes"):
                transcript.collect(db, packet)
