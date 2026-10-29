from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from common import read_json, write_json
from local_app import App
from model_settings import effective_profile_sources, format_effective_profile, save_default_profile
from native_app import NativeSession, diagnostics
from project_wizard import validate_wizard
from update_native import install_overlay


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        shutil.copytree(PROJECT / "config", self.base / "config")
        self.app = App(self.base, api_factory=lambda cfg: self.fail("Una lettura locale non deve chiamare l'API."))
        self.session = NativeSession(self.app)
        self.file = self.base / "Fonte.txt"
        self.file.write_text("Argomento alfa e criteri precisi.\n", encoding="utf-8")
        self.data = {"name": "Materia nativa", "mode": "veloce", "max_pages": "8", "questions_text": "Definisci alfa.", "instructions": "Per un orale"}

    def tearDown(self):
        self.app.stop.set()
        self.temporary.cleanup()

    def add(self):
        self.session.add_files("materiali", [self.file])

    def test_save_uploads_locally_without_browser_or_http(self):
        self.add()
        with patch("webbrowser.open", side_effect=AssertionError("Browser vietato")), patch("local_app.ThreadingHTTPServer", side_effect=AssertionError("Server vietato")):
            result = self.session.save(self.data)
        self.assertFalse(result["exam"]["attivo"])
        self.assertEqual(result["exam"]["max_pagine"], 8)
        self.assertEqual(self.file.read_bytes(), (self.app.root(self.session.selected) / "input/sbobine/Fonte.txt").read_bytes())
        self.assertFalse(self.session.pending["materiali"])

    def test_failed_upload_is_retryable_without_second_exam(self):
        self.add()
        with patch.object(self.app, "upload", side_effect=OSError("Copia interrotta")):
            with self.assertRaises(OSError):
                self.session.save(self.data)
        slug = self.session.selected
        self.assertEqual(len(self.session.pending["materiali"]), 1)
        self.session.save(self.data)
        self.assertEqual(self.session.selected, slug)
        self.assertEqual(len(self.app.list_exams()), 1)

    def test_now_starts_once_after_sources_are_saved(self):
        self.add()
        with patch.object(self.app, "run") as run:
            self.session.save(self.data, "now")
        run.assert_called_once_with(self.session.selected)
        self.assertTrue(self.app.detail(self.session.selected)["exam"]["attivo"])

    def test_night_schedules_without_immediate_api(self):
        self.add()
        with patch.object(self.app, "run") as run:
            self.session.save(self.data, "night")
        run.assert_not_called()
        self.assertTrue(self.app.detail(self.session.selected)["exam"]["attivo"])

    def test_missing_materials_stops_before_api(self):
        with patch.object(self.app, "run") as run:
            with self.assertRaisesRegex(ValueError, "almeno un file"):
                self.session.save(self.data, "now")
        run.assert_not_called()
        self.assertFalse(self.app.detail(self.session.selected)["exam"]["attivo"])

    def test_model_changes_only_on_explicit_upgrade(self):
        self.add()
        result = self.session.save(self.data)
        original = result["exam"]["pipeline"]["model"]
        defaults = self.app.defaults()
        save_default_profile(self.base, dict(defaults, model="future-compatible-model"))
        self.assertEqual(self.app.detail(self.session.selected)["exam"]["pipeline"]["model"], original)
        sources = {row["key"]: row for row in effective_profile_sources(self.app.root(self.session.selected), self.base)}
        self.assertEqual((sources["model"]["value"], sources["model"]["source"]), (original, "materia"))
        self.assertEqual(sources["micro_batch_size"]["source"], "globale")
        self.assertIn("origine: materia", format_effective_profile(list(sources.values())))
        with patch.object(self.app, "run") as run:
            self.session.upgrade()
        run.assert_called_once_with(self.session.selected)
        self.assertEqual(self.app.detail(self.session.selected)["exam"]["pipeline"]["model"], "future-compatible-model")

    def test_diagnostics_show_identifiers_and_actual_errors(self):
        self.session.save(self.data)
        root = self.app.root(self.session.selected)
        write_json(root / "state/pipeline.json", {"input_status": "LETTURA_MATERIALI", "batches": [{"batch_id": "batch_test_123", "stage": "submitted", "status": "in_progress", "error": "Errore di prova"}], "jobs": {}})
        (root / "logs/pipeline.log").write_text("BATCH INVIATO: batch_test_123\n")
        text = diagnostics(self.app, self.session.selected)
        self.assertIn("batch_test_123", text)
        self.assertIn("in_progress", text)
        self.assertIn("Errore di prova", text)

    def test_resume_does_not_send_drafts_or_completed_batches(self):
        self.add()
        self.session.save(self.data, "night")
        root = self.app.root(self.session.selected)
        with patch.object(self.app, "run") as run:
            self.session.resume()
        run.assert_not_called()
        write_json(root / "state/pipeline.json", {"batches": [{"stage": "submitted", "processed": False}]})
        with patch.object(self.app, "run") as run:
            self.session.resume()
        run.assert_called_once_with(self.session.selected)

    def test_remove_archives_source_and_pauses_subject(self):
        self.add()
        self.session.save(self.data, "night")
        self.session.remove("materiali", "Fonte.txt")
        root = self.app.root(self.session.selected)
        self.assertFalse((root / "input/sbobine/Fonte.txt").exists())
        self.assertEqual(len(list((root / "state/input_archivio").iterdir())), 1)
        self.assertFalse(read_json(root / "esame.json")["attivo"])

    def test_overlay_preserves_sources_jobs_config_and_previous_launcher(self):
        target = self.base
        (target / "scripts").mkdir()
        (target / "scripts/local_app.py").write_text("# Motore esistente\n")
        (target / "scripts/exports.py").write_text("# Export precedente\n")
        (target / ".venv/bin").mkdir(parents=True)
        (target / ".venv/bin/python").symlink_to(sys.executable)
        (target / "APRI.command").write_text("# Launcher precedente\n")
        self.add()
        self.session.save(self.data)
        exam = self.app.root(self.session.selected)
        write_json(exam / "state/pipeline.json", {"batches": [{"batch_id": "batch_existing", "stage": "submitted"}]})
        preserve = [target / "config/pipeline.json", exam / "esame.json", exam / "input/sbobine/Fonte.txt", exam / "state/pipeline.json"]
        before = {path: path.read_bytes() for path in preserve}
        install_overlay(PROJECT, target)
        self.assertEqual(before, {path: path.read_bytes() for path in preserve})
        self.assertEqual((target / "scripts/local_app.py").read_bytes(), (PROJECT / "scripts/local_app.py").read_bytes())
        self.assertEqual((target / "scripts/exports.py").read_bytes(), (PROJECT / "scripts/exports.py").read_bytes())
        self.assertEqual((target / "scripts/model_settings.py").read_bytes(),
                         (PROJECT / "scripts/model_settings.py").read_bytes())
        self.assertEqual((target / "renderer/render_pdf.py").read_bytes(),
                         (PROJECT / "renderer/render_pdf.py").read_bytes())
        self.assertEqual(len(list((target / "state/aggiornamenti-nativa").rglob("local_app.py"))), 1)
        archived_exports = list((target / "state/aggiornamenti-nativa").rglob("exports.py"))
        self.assertEqual(len(archived_exports), 1)
        self.assertEqual(archived_exports[0].read_text(), "# Export precedente\n")
        self.assertIn("native.py", (target / "APRI.command").read_text())
        archived = list((target / "state/aggiornamenti-nativa").rglob("APRI.command"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_text(), "# Launcher precedente\n")

    def test_wizard_validates_before_create_and_persists_optional_identity(self):
        values = {"name": "Ginecologia sintesi", "short_title": "Ginecologia", "exam_date": "2027-01-15",
                  "mode": "veloce", "max_pages": "100", "execution": "batch", "illustrator_enabled": False}
        with self.assertRaisesRegex(ValueError, "Aggiungi almeno"):
            validate_wizard(values, {}, start=True)
        self.assertEqual(self.app.list_exams(), [])
        self.add()
        planned, paths = validate_wizard(values, self.session.pending, start=True)
        self.assertEqual(paths["materiali"], [self.file.resolve()])
        with patch.object(self.app, "run") as run:
            detail = self.session.save(dict(planned, questions_text=""), "save")
        run.assert_not_called()
        self.assertEqual(detail["exam"]["data_esame"], "2027-01-15")
        self.assertEqual(detail["exam"]["titolo_breve"], "Ginecologia")
        self.assertEqual(detail["exam"]["max_pagine"], 100)

    def test_invalid_exam_date_and_duplicate_material_names_do_not_create_project(self):
        values = {"name": "Materia nuova", "exam_date": "2027-02-30", "mode": "completa", "execution": "batch"}
        with self.assertRaisesRegex(ValueError, "data dell'esame"):
            validate_wizard(values, {})
        with self.assertRaisesRegex(ValueError, "data dell'esame"):
            self.app.save_exam(dict(values, exam_date="2027-02-30"))
        with self.assertRaisesRegex(ValueError, "AAAA-MM-GG"):
            validate_wizard(dict(values, exam_date="20270228"), {})
        self.assertEqual(self.app.list_exams(), [])
        values["exam_date"] = "2027-02-28"
        second = self.base / "altra" / self.file.name
        second.parent.mkdir()
        second.write_text("Un'altra fonte", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stesso nome"):
            validate_wizard(values, {"materiali": [self.file, second]})
        self.assertEqual(self.app.list_exams(), [])


if __name__ == "__main__":
    unittest.main()
