from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bottleneck_detector import detect


class BottleneckDetectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "esami" / "materia"
        self.root.mkdir(parents=True)
        self.now = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_uncertain_post_is_not_presented_as_unsent_or_retried(self):
        state = {"jobs": {}, "latest": {}, "batches": [
            {"stage": "uncertain", "run_id": "one"}, {"stage": "uncertain", "run_id": "two"}]}
        incidents = detect(self.root, now=self.now, state=state)
        self.assertEqual([item["code"] for item in incidents], ["REMOTE_ACCEPTANCE_UNCERTAIN"])
        self.assertIn("Non inviare", incidents[0]["suggestion"])

    def test_old_remote_check_is_observation_warning_not_server_stall(self):
        state = {"jobs": {}, "latest": {}, "batches": [{"stage": "submitted", "batch_id": "batch_1",
                   "status": "queued", "remote_checked_at": "2026-10-29T02:00:00+00:00"}]}
        incidents = detect(self.root, now=self.now, state=state)
        self.assertEqual(incidents[0]["code"], "REMOTE_OBSERVATION_OLD")
        self.assertEqual(incidents[0]["severity"], "info")
        self.assertIn("non prova uno stallo", incidents[0]["suggestion"])
        state["batches"][0]["remote_checked_at"] = "2026-10-29T11:59:00+00:00"
        self.assertEqual(detect(self.root, now=self.now, state=state), [])

    def test_missing_local_files_are_errors_but_pilot_review_is_user_wait(self):
        key = "a" * 64
        state = {"jobs": {key: {"status": "HTML_OK", "html": "output/raw/one.html"}},
                 "latest": {"1": key}, "batches": []}
        self.assertEqual(detect(self.root, now=self.now, state=state)[0]["code"], "CURRENT_HTML_MISSING")
        state["jobs"][key] = {"status": "PDF_OK", "pdf": "output/pdf/versioni/one.pdf"}
        state["pilot_review"] = {"approved_hash": None}
        codes = [item["code"] for item in detect(self.root, now=self.now, state=state)]
        self.assertEqual(codes, ["CURRENT_PDF_MISSING", "PILOT_WAITING_FOR_USER"])


if __name__ == "__main__":
    unittest.main()
