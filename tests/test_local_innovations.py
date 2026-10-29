from __future__ import annotations

import sys
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from common import read_json, write_json
from control_room import snapshot, format_snapshot, summarize_performance
from coverage_matrix import compare as compare_coverage
from diagnostic_report import build_sections, export, preview
from medical_illustrator import (create_manual, export_illustrated_pdf, list_opportunities, plan, revalidate_edited_svg,
                                 set_disabled, technical_qc, transition, update_proposal)
from materials_view import material_detail
from local_recovery import recover_local
from projects_view import project_cards
from telemetry import Telemetry
from visual_qc import inspect_visual


class LocalInnovationsTests(unittest.TestCase):
    def test_stage_percentiles_require_enough_real_measurements(self):
        events = [{"stage": "pdf_render", "status": "success", "duration_ms": value}
                  for value in range(1, 21)]
        self.assertIsNone(summarize_performance(events[:1])[0]["p95_ms"])
        summary = summarize_performance(events + [{"stage": "pdf_render", "status": "failure", "duration_ms": 999}])[0]
        self.assertEqual((summary["success"], summary["failed"]), (20, 1))
        self.assertEqual(summary["p90_ms"], 18.1)
        self.assertEqual(summary["p95_ms"], 19.1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        shutil.copytree(Path(__file__).resolve().parents[1] / "config", self.project / "config")
        self.root = self.project / "esami" / "neurologia"
        (self.root / "state").mkdir(parents=True)
        write_json(self.root / "esame.json", {"nome": "Neurologia", "execution_mode": "batch"})
        self.key = "a" * 64
        self.pack = self.root / "source_packs" / "pack.json"
        write_json(self.pack, {"scheme": {"numero": 1, "titolo": "Sindrome del tunnel carpale",
                                          "keywords": ["nervo mediano"]},
                               "chunks": [{"text": "Tunnel carpale e nervo mediano. Compressione del nervo mediano.",
                                           "document_name": "Fonte 1", "page_start": 2, "page_end": 3}]})
        write_json(self.root / "state/pipeline.json", {
            "version": 1,
            "jobs": {self.key: {"hash": self.key, "kind": "schema", "numero": 1,
                                "title": "Tunnel carpale", "status": "HTML_OK", "pack": str(self.pack.relative_to(self.root)),
                                "retry_count": 0}},
            "latest": {"1": self.key}, "batches": [
                {"stage": "submitted", "status": "in_progress", "batch_id": "batch_1", "mode": "batch",
                 "remote_checked_at": "2026-10-28T18:00:00+00:00"},
                {"stage": "uncertain", "mode": "urgent"}],
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_control_room_keeps_uncertain_batch_distinct_and_does_not_send(self):
        view = snapshot(self.root)
        self.assertEqual(view["counts"]["accepted_batches"], 1)
        self.assertEqual(view["counts"]["pdf_ok"], 0)
        self.assertEqual(view["batches"][1]["id"], None)
        self.assertTrue(any(row["kind"] == "REMOTE_ACCEPTANCE_UNCERTAIN" for row in view["incidents"]))
        self.assertIn("ID non confermato", format_snapshot(view))
        self.assertFalse(view["telemetry_available"])
        Telemetry(self.root).record("pdf_render_completed", "pdf_render", status="success", duration_ms=25)
        self.assertEqual(snapshot(self.root)["performance"][0]["median_ms"], 25)

    def test_illustration_remains_optional_and_requires_review(self):
        rows = plan(self.root)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["lesion_types"], ["neural", "spatial_relations", "functional"])
        self.assertEqual(plan(self.root), rows)
        self.assertEqual(list((self.root / "diagrams/medical").glob("*.svg")), [])
        row = transition(self.root, rows[0]["opportunity_id"], "accepted")
        self.assertEqual(row["status"], "medical_review_needed")
        svg = (self.root / row["artifact"]).read_bytes()
        self.assertEqual(technical_qc(svg), [])
        self.assertIn(b"NON VALIDATA CLINICAMENTE", svg)
        self.assertEqual(read_json(self.root / "state/pipeline.json")["jobs"][self.key]["status"], "HTML_OK")
        self.assertEqual(list((self.root / "output/pdf").glob("*.pdf")), [])
        self.assertEqual(transition(self.root, row["opportunity_id"], "approved")["status"], "approved")

    def test_changed_schema_and_tampered_svg_block_approval(self):
        row = transition(self.root, plan(self.root)[0]["opportunity_id"], "accepted")
        artifact = self.root / row["artifact"]
        artifact.write_bytes(artifact.read_bytes().replace(b"Normale (schema)", b"Test errato"))
        with self.assertRaisesRegex(ValueError, "SVG"):
            transition(self.root, row["opportunity_id"], "approved")
        artifact.write_bytes(b"<svg><script>alert(1)</script></svg>")
        self.assertTrue(technical_qc(artifact.read_bytes()))

    def test_manual_figure_edit_requires_fresh_medical_review(self):
        row = create_manual(self.root, 1, "Vasculite arteriosa", "vascular",
                            "Confrontare una situazione normale e una patologica.")
        self.assertTrue(row["requested_by_user"])
        self.assertEqual(row["lesion_types"], ["vascular"])
        row = update_proposal(self.root, row["opportunity_id"], {"view_type": "sezione schematica"})
        self.assertEqual(row["view_type"], "sezione schematica")
        row = transition(self.root, row["opportunity_id"], "accepted")
        artifact = self.root / row["artifact"]
        self.assertEqual(technical_qc(artifact.read_bytes()), [])
        artifact.write_bytes(artifact.read_bytes().replace(b"#b33f35", b"#ff0000"))
        with self.assertRaisesRegex(ValueError, "modificato"):
            transition(self.root, row["opportunity_id"], "approved")
        revised = revalidate_edited_svg(self.root, row["opportunity_id"])
        self.assertEqual(revised["editor_revision"], 1)
        self.assertEqual(transition(self.root, row["opportunity_id"], "approved")["status"], "approved")
        artifact.write_bytes(artifact.read_bytes().replace(b"#ff0000", b"#00aa00"))
        revised = revalidate_edited_svg(self.root, row["opportunity_id"])
        self.assertEqual(revised["status"], "medical_review_needed")
        self.assertNotIn("reviewed_at", revised)
        artifact.write_bytes(b'<svg viewBox="0 0 900 520"><script>alert(1)</script></svg>')
        with self.assertRaisesRegex(ValueError, "Controllo tecnico"):
            revalidate_edited_svg(self.root, row["opportunity_id"])
        artifact.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 900 520">'
                             b'<rect x="899" y="0" width="20" height="20"/>'
                             b'<text x="1" y="20" font-size="12">Normale (schema) NON VALIDATA CLINICAMENTE</text></svg>')
        self.assertIn("element_outside_canvas", technical_qc(artifact.read_bytes()))

    def test_approved_plate_exports_separate_pdf_without_changing_pipeline(self):
        import fitz
        row = transition(self.root, plan(self.root)[0]["opportunity_id"], "accepted")
        transition(self.root, row["opportunity_id"], "approved")
        state = read_json(self.root / "state/pipeline.json")
        job = state["jobs"][self.key]
        job.update(status="PDF_OK", pdf="output/pdf/versioni/one.pdf", public_pdf="output/pdf/one.pdf", page_budget=3)
        write_json(self.root / "state/pipeline.json", state)
        original = self.root / job["public_pdf"]
        version = self.root / job["pdf"]
        original.parent.mkdir(parents=True)
        version.parent.mkdir(parents=True)
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "Schema di prova con testo originale.")
        document.save(original)
        version.write_bytes(original.read_bytes())
        before = original.read_bytes()
        with patch("medical_illustrator.inspect_pdf", return_value={"ok": True}):
            illustrated = export_illustrated_pdf(self.root, row["opportunity_id"])
            self.assertEqual(export_illustrated_pdf(self.root, row["opportunity_id"]), illustrated)
        with fitz.open(illustrated) as output:
            self.assertEqual(output.page_count, 2)
            self.assertIn("Tavola illustrativa", output.get_toc()[-1][1])
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(read_json(self.root / "state/pipeline.json"), state)
        self.assertEqual(next(item for item in list_opportunities(self.root)
                              if item["opportunity_id"] == row["opportunity_id"])["status"], "published")

    def test_plate_export_respects_schema_page_budget(self):
        import fitz
        row = transition(self.root, plan(self.root)[0]["opportunity_id"], "accepted")
        transition(self.root, row["opportunity_id"], "approved")
        state = read_json(self.root / "state/pipeline.json")
        job = state["jobs"][self.key]
        job.update(status="PDF_OK", pdf="output/pdf/versioni/one.pdf", public_pdf="output/pdf/one.pdf", page_budget=1)
        write_json(self.root / "state/pipeline.json", state)
        original = self.root / job["public_pdf"]
        version = self.root / job["pdf"]
        original.parent.mkdir(parents=True)
        version.parent.mkdir(parents=True)
        document = fitz.open()
        document.new_page()
        document.save(original)
        version.write_bytes(original.read_bytes())
        with patch("medical_illustrator.inspect_pdf", return_value={"ok": True}):
            with self.assertRaisesRegex(ValueError, "limite di pagine"):
                export_illustrated_pdf(self.root, row["opportunity_id"])
        self.assertEqual(list((self.root / "output/pdf/illustrati").glob("*.pdf")), [])

    def test_opt_out_and_no_keyword_keep_planner_quiet(self):
        set_disabled(self.root, True)
        self.assertEqual(plan(self.root), [])
        set_disabled(self.root, False)
        write_json(self.pack, {"scheme": {"numero": 1, "titolo": "Principi generali", "keywords": []},
                               "chunks": [{"text": "Introduzione generale", "document_name": "Fonte"}]})
        self.assertEqual(plan(self.root), [])

    def test_proximal_median_nerve_does_not_become_carpal_tunnel(self):
        write_json(self.pack, {"scheme": {"numero": 1, "titolo": "Sindrome del pronatore", "keywords": ["nervo mediano"]},
                               "chunks": [{"text": "Il nervo mediano può avere lesioni in sedi prossimali.",
                                           "document_name": "Fonte"}]})
        self.assertEqual(plan(self.root), [])

    def test_diagnostic_export_omits_raw_error_and_source_content(self):
        state = read_json(self.root / "state/pipeline.json")
        state["jobs"][self.key]["error"] = "sk-test-very-secret user@example.com /Users/person/file.pdf"
        state["jobs"][self.key]["title"] = "Private study title"
        write_json(self.root / "state/pipeline.json", state)
        sections = build_sections(self.root)
        visible = preview(sections)
        for secret in ("sk-test-very-secret", "user@example.com", "/Users/person", "Private study title"):
            self.assertNotIn(secret, visible)
        archive = export(self.root, sections)
        import zipfile
        with zipfile.ZipFile(archive) as zipped:
            self.assertEqual(set(zipped.namelist()), set(sections))
            self.assertNotIn("sk-test-very-secret", "".join(zipped.read(name).decode() for name in zipped.namelist()))

    def test_visual_qc_detects_sparse_page_without_changing_pdf(self):
        import fitz
        path = self.root / "output/pdf/fixture.pdf"
        path.parent.mkdir(parents=True)
        doc = fitz.open()
        doc.new_page()
        doc.save(path)
        before = path.read_bytes()
        result = inspect_visual(path)
        self.assertEqual(result["pages"][0]["page"], 1)
        self.assertTrue(any("pochissimo testo" in warning for warning in result["pages"][0]["warnings"]))
        self.assertEqual(path.read_bytes(), before)

    def test_local_recovery_rerenders_current_html_without_api_or_new_batch(self):
        state = read_json(self.root / "state/pipeline.json")
        job = state["jobs"][self.key]
        job.update(status="RENDER_FAILED", html="output/raw/schema.raw.html",
                   pdf="output/pdf/versioni/schema.pdf", error="Renderer failed")
        write_json(self.root / "state/pipeline.json", state)
        html = self.root / job["html"]
        html.parent.mkdir(parents=True)
        html.write_text("<article class='study-schema'>Prova</article>")
        def fake_render(root, candidate, cfg, project):
            destination = root / candidate["pdf"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"PDF fixture")
            return {"ok": True, "pages": 1}
        with patch("local_recovery.inspect_pdf", return_value={"ok": True}), patch(
                "remote.OpenAIHTTP", side_effect=AssertionError("Nessuna API")):
            actions = recover_local(self.root, self.project, render=fake_render, signature="test-signature")
        self.assertEqual(actions[0]["outcome"], "success")
        after = read_json(self.root / "state/pipeline.json")
        self.assertEqual(after["jobs"][self.key]["status"], "PDF_OK")
        self.assertEqual(after["batches"], state["batches"])
        self.assertTrue((self.root / after["jobs"][self.key]["public_pdf"]).is_file())
        self.assertIn("rerender_local", (self.root / "state/local_recovery.jsonl").read_text())

    def test_project_home_flags_ambiguous_submission_without_api(self):
        from local_app import App
        app = App(self.project, api_factory=lambda cfg: self.fail("Nessuna API"))
        row = {"slug": "neurologia", "name": "Neurologia", "ready": 0, "total": 1,
               "busy": False, "scheduled": True}
        cards = project_cards(app, [row])
        self.assertEqual(cards[0]["phase"], "Invio incerto · riconciliare")
        self.assertEqual(cards[0]["active_batches"], 1)

    def test_material_preview_reads_index_without_reingest(self):
        import sqlite3
        file = self.root / "input/sbobine/Fonte.txt"
        file.parent.mkdir(parents=True)
        file.write_text("Testo di studio", encoding="utf-8")
        before = material_detail(self.root, "materiali", "Fonte.txt")
        self.assertIn("non ancora", before["status"])
        db_file = self.root / "corpus/corpus.db"
        db_file.parent.mkdir(parents=True)
        from ingest_helpers import create_empty_db
        create_empty_db(db_file)
        with sqlite3.connect(db_file) as db:
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("DOC_000001", "Fonte.txt", "input/sbobine/Fonte.txt", "txt", "sbobina", 15,
                        "a" * 64, None, 1, 0, "processed", "2026-10-28T00:00:00Z"))
            db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("CH_000001", "DOC_000001", "Fonte.txt", None, None, "Titolo", "Estratto verificabile",
                        4, "b" * 64, 1))
        after = material_detail(self.root, "materiali", "Fonte.txt")
        self.assertEqual(after["status"], "processed")
        self.assertIn("Estratto verificabile", after["text_preview"])

    def test_program_comparison_is_labelled_estimate_and_uses_existing_index(self):
        import sqlite3
        from ingest_helpers import create_empty_db
        db_file = self.root / "corpus/corpus.db"
        db_file.parent.mkdir(parents=True)
        create_empty_db(db_file)
        with sqlite3.connect(db_file) as db:
            for doc_id, name, kind in (("DOC_1", "Programma.txt", "programma"), ("DOC_2", "Sbobina.txt", "sbobina")):
                db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (doc_id, name, "input/" + name, "txt", kind, 100, "a" * 64,
                            None, 1, 0, "processed", "2026-10-29T00:00:00Z"))
            db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("CH_1", "DOC_1", "Programma.txt", None, None, "Programma",
                        "1. Tumori ovarici\n2. Fisiologia renale", 8, "b" * 64, 1))
            db.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
                       ("CH_2", "DOC_2", "Sbobina.txt", None, None, "Tumori",
                        "I tumori ovarici hanno caratteristiche diverse.", 8, "c" * 64, 1))
            db.execute("INSERT INTO questions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("Q_1", "Quali sono i tumori ovarici?", "", "Domande.txt", "DOC_3",
                        "", "", "", 1, "", "d" * 64, "txt"))
        result = compare_coverage(self.root)
        self.assertEqual(result["status"], "estimated")
        self.assertIn("non prova", result["message"].casefold())
        self.assertEqual(result["rows"][0]["source_count"], 1)
        self.assertEqual(result["rows"][0]["question_count"], 1)
        self.assertEqual(result["rows"][1]["source_count"], 0)


if __name__ == "__main__":
    unittest.main()
