from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from telemetry import Telemetry


class TelemetryTests(unittest.TestCase):
    def test_span_uses_monotonic_duration_and_does_not_store_unlisted_values(self):
        with tempfile.TemporaryDirectory() as temp:
            ticks = iter((10_000_000, 13_500_000))
            telemetry = Telemetry(Path(temp) / "esami" / "prova", clock_ns=lambda: next(ticks))
            with telemetry.span("pdf_render", metrics={"pages": 2, "secret": "sk-test-private"}):
                pass
            events = telemetry.recent()
            self.assertEqual([row["event_name"] for row in events],
                             ["pdf_render_completed", "pdf_render_started"])
            self.assertEqual(events[0]["duration_ms"], 3.5)
            self.assertEqual(json.loads(events[0]["metrics_json"]), {"pages": 2})
            self.assertNotIn("sk-test-private", telemetry.path.read_bytes().decode("utf-8", "ignore"))

    def test_failure_is_observed_without_changing_the_original_error(self):
        with tempfile.TemporaryDirectory() as temp:
            ticks = iter((0, 2_000_000))
            telemetry = Telemetry(Path(temp) / "materia", clock_ns=lambda: next(ticks))
            with self.assertRaisesRegex(RuntimeError, "renderer failed"):
                with telemetry.span("pdf_render"):
                    raise RuntimeError("renderer failed")
            self.assertEqual(telemetry.recent()[0]["status"], "failure")

    def test_unavailable_database_does_not_interrupt_work(self):
        with tempfile.TemporaryDirectory() as temp:
            telemetry = Telemetry(Path(temp) / "materia")
            telemetry.path.parent.mkdir(parents=True)
            telemetry.path.mkdir()
            completed = False
            with telemetry.span("pdf_render"):
                completed = True
            self.assertTrue(completed)
            self.assertEqual(telemetry.recent(), [])


if __name__ == "__main__":
    unittest.main()
