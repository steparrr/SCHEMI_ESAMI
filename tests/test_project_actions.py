from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import read_json, write_json
from local_app import App
from project_actions import duplicate_settings, set_archived, set_favorite
from projects_view import arrange_cards, pdf_versions, project_cards


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

    def test_favorite_and_home_filters_are_local_and_reversible(self):
        self.assertTrue(set_favorite(self.app, self.slug, True))
        self.assertTrue(self.app.list_exams()[0]["favorite"])
        cards = project_cards(self.app)
        self.assertEqual([card["slug"] for card in arrange_cards(cards, "Preferite")], [self.slug])
        self.assertFalse(set_favorite(self.app, self.slug, False))
        self.assertEqual(arrange_cards(project_cards(self.app), "Preferite"), [])

    def test_home_counts_only_current_pdf_with_real_file(self):
        current = "a" * 64
        old = "b" * 64
        write_json(self.root / "state/pipeline.json", {"latest": {"1": current},
            "jobs": {current: {"numero": 1, "status": "PDF_OK", "public_pdf": "output/pdf/current.pdf"},
                     old: {"numero": 1, "status": "PDF_OK", "public_pdf": "output/pdf/old.pdf"}}})
        cards = project_cards(self.app)
        self.assertEqual((cards[0]["ready"], cards[0]["total"]), (0, 1))
        (self.root / "output/pdf/current.pdf").write_bytes(b"fixture")
        cards = project_cards(self.app)
        self.assertEqual((cards[0]["ready"], cards[0]["total"]), (1, 1))

    def test_version_browser_lists_current_and_old_files_without_leaving_pdf_folder(self):
        current, old, unsafe = "a" * 64, "b" * 64, "c" * 64
        folder = self.root / "output/pdf/versioni"
        folder.mkdir(parents=True)
        (folder / "current.pdf").write_bytes(b"current")
        (folder / "old.pdf").write_bytes(b"old")
        (self.project / "outside.pdf").write_bytes(b"private")
        write_json(self.root / "state/pipeline.json", {"latest": {"1": current}, "jobs": {
            current: {"kind": "schema", "numero": 1, "title": "Tema", "status": "PDF_OK", "pdf": "output/pdf/versioni/current.pdf"},
            old: {"kind": "schema", "numero": 1, "title": "Tema vecchio", "status": "SUPERSEDED", "pdf": "output/pdf/versioni/old.pdf"},
            unsafe: {"kind": "schema", "numero": 1, "title": "Fuori cartella", "status": "PDF_OK", "pdf": "../../outside.pdf"}}})
        rows = pdf_versions(self.root)
        self.assertEqual([row["request_hash"] for row in rows], [current[:12], old[:12]])
        self.assertEqual([row["current"] for row in rows], [True, False])


if __name__ == "__main__":
    unittest.main()
