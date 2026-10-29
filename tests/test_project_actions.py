from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import read_json, write_json
from local_app import App
from project_actions import duplicate_settings, set_archived


class ProjectActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / "config", self.project / "config")
        self.app = App(self.project, api_factory=lambda cfg: self.fail("Nessuna API"))
        self.slug = self.app.save_exam({"name": "Neurologia", "mode": "veloce", "max_pages": 80,
                                        "short_title": "Neuro", "exam_date": "2027-02-01"})
        self.root = self.app.root(self.slug)

    def tearDown(self):
        self.app.stop.set()
        self.temp.cleanup()

    def test_duplicate_copies_only_allowlisted_settings(self):
        original = read_json(self.root / "esame.json")
        original["pipeline"]["api_key"] = "sk-test-private"
        original["pipeline"]["model"] = "gpt-test"
        original["istruzioni"] = "Indicazioni personali"
        write_json(self.root / "esame.json", original)
        source = self.root / "input/sbobine/Privato.txt"
        source.write_text("Dato didattico privato")
        new_slug = duplicate_settings(self.app, self.slug, "Neuro copia")
        copy = self.app.root(new_slug)
        metadata = read_json(copy / "esame.json")
        self.assertEqual(metadata["pipeline"]["model"], "gpt-test")
        self.assertEqual(metadata["max_pagine"], 80)
        self.assertNotIn("api_key", metadata["pipeline"])
        self.assertEqual(metadata["istruzioni"], "")
        self.assertIsNone(metadata["data_esame"])
        self.assertFalse(metadata["attivo"])
        self.assertEqual(list((copy / "input/sbobine").iterdir()), [])

    def test_archive_refuses_unfinished_batch_and_restores_as_draft(self):
        write_json(self.root / "state/pipeline.json", {"batches": [{"stage": "uncertain", "processed": False}]})
        with self.assertRaisesRegex(ValueError, "invio in corso"):
            set_archived(self.app, self.slug, True)
        self.assertFalse(read_json(self.root / "esame.json").get("archived", False))
        write_json(self.root / "state/pipeline.json", {"batches": [{"stage": "processed", "processed": True}]})
        self.assertTrue(set_archived(self.app, self.slug, True))
        self.assertTrue(self.app.list_exams()[0]["archived"])
        with self.assertRaisesRegex(ValueError, "archiviata"):
            self.app.run(self.slug)
        self.assertFalse(set_archived(self.app, self.slug, False))
        self.assertFalse(read_json(self.root / "esame.json")["attivo"])


if __name__ == "__main__":
    unittest.main()
