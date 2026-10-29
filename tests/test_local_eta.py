from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from local_eta import estimate_local_rendering


class LocalEtaTests(unittest.TestCase):
    def setUp(self):
        self.signature = "a" * 64
        self.jobs = [{"numero": 1, "status": "HTML_OK", "page_budget": 28}]
        self.events = [{"stage": "pdf_render", "status": "success", "duration_ms": 1000 * index,
                        "metrics_json": json.dumps({"pages": 28, "renderer_signature": self.signature})}
                       for index in range(1, 21)]

    def estimate(self, jobs=None, batches=None, pilot=None, events=None, signature=None):
        return estimate_local_rendering(jobs if jobs is not None else self.jobs,
                                        batches if batches is not None else [],
                                        {"1": "current"}, pilot if pilot is not None else {},
                                        events if events is not None else self.events,
                                        signature if signature is not None else self.signature)

    def test_estimate_requires_twenty_same_renderer_similar_page_samples(self):
        result = self.estimate()
        self.assertEqual(result["status"], "estimated")
        self.assertEqual((result["low_ms"], result["high_ms"], result["samples_min"]),
                         (2900, 18100, 20))
        self.assertEqual(self.estimate(events=self.events[:19])["status"], "unavailable")
        other = [dict(event, metrics_json=json.dumps({"pages": 40, "renderer_signature": self.signature}))
                 for event in self.events]
        self.assertEqual(self.estimate(events=other)["status"], "unavailable")
        old_renderer = [dict(event, metrics_json=json.dumps({"pages": 28, "renderer_signature": "b" * 64}))
                        for event in self.events]
        self.assertEqual(self.estimate(events=old_renderer)["status"], "unavailable")

    def test_remote_uncertainty_and_pilot_review_override_local_estimate(self):
        remote = self.estimate(batches=[{"stage": "uncertain", "processed": False}])
        self.assertEqual(remote["status"], "unavailable")
        self.assertIn("OpenAI", remote["reason"])
        jobs = [{"numero": 1, "status": "PDF_OK"}, {"numero": 2, "status": "HTML_OK", "page_budget": 28}]
        waiting = self.estimate(jobs=jobs, pilot={"approved_hash": None})
        self.assertEqual(waiting["status"], "waiting_user")
        self.assertIsNone(waiting["low_ms"])

    def test_no_plan_and_old_telemetry_never_get_a_fabricated_eta(self):
        self.assertEqual(self.estimate(jobs=[])["status"], "unavailable")
        old_events = [dict(event, metrics_json="{}") for event in self.events]
        self.assertEqual(self.estimate(events=old_events)["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
