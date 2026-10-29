from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bottleneck_detector import detect
from common import write_json


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
        self.assertEqual(codes, ["CURRENT_PDF_MISSING", "CURRENT_PUBLIC_PDF_MISSING", "PILOT_WAITING_FOR_USER"])

    def test_old_worker_heartbeat_is_observation_and_low_disk_is_explicit(self):
        write_json(self.root / "esame.json", {"attivo": True})
        write_json(self.root.parent.parent / "state/worker-heartbeat.json",
                   {"checked_at": "2026-10-29T10:00:00+00:00"})
        with patch("bottleneck_detector.shutil.disk_usage", return_value=SimpleNamespace(free=80 * 1024 * 1024)):
            incidents = detect(self.root, now=self.now, state={"jobs": {}, "latest": {}, "batches": []})
        self.assertEqual([item["code"] for item in incidents], ["LOCAL_HEARTBEAT_OLD", "DISK_SPACE_LOW"])
        self.assertEqual(incidents[0]["severity"], "info")
        self.assertEqual(incidents[1]["severity"], "critical")
        self.assertIn("non prova uno stallo", incidents[0]["suggestion"])


if __name__ == "__main__":
    unittest.main()
