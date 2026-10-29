from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import pymupdf as fitz
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.modules["fitz"] = fitz
sys.path.insert(0, str(PROJECT / "scripts"))
from common import create_exam, load_config, read_json, write_json
from pipeline import Pipeline, linearize_tables
from local_app import App


class FakeAPI:
    def __init__(self, status="completed", incomplete=False, lose_ack=False):
        self.files = {}
        self.batches = {}
        self.created = 0
        self.status = status
        self.incomplete = incomplete
        self.lose_ack = lose_ack

    def upload(self, path):
        file_id = "file-" + str(len(self.files))
        self.files[file_id] = path.read_bytes()
        return {"id": file_id}

    def create_batch(self, file_id, run_id, exam_id):
        self.created += 1
        batch_id = "batch-" + str(self.created)
        rows = []
        for line in self.files[file_id].decode().splitlines():
            request = json.loads(line)
            body = {"status": "completed", "output_text": '<article class="study-schema"><h1>Risultato tecnico</h1><p>Contenuto restituito dal simulatore.</p></article>', "usage": {"input_tokens": 100, "output_tokens": 200}}
            if self.incomplete and (self.incomplete == "always" or request["custom_id"].endswith("-r0")):
                body = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output_text": "<article", "usage": {"input_tokens": 100, "output_tokens": 200}}
            rows.append({"custom_id": request["custom_id"], "response": {"status_code": 200, "request_id": request["custom_id"], "body": body}, "error": None})
        out = "output-" + batch_id
        self.files[out] = "\n".join(json.dumps(row) for row in reversed(rows)).encode()
        batch = {"id": batch_id, "status": self.status, "input_file_id": file_id, "output_file_id": out, "metadata": {"run_id": run_id, "exam_id": exam_id}, "request_counts": {"total": len(rows), "completed": len(rows), "failed": 0}}
        self.batches[batch_id] = batch
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("Risposta persa dopo l'accettazione del batch")
        return batch

    def get_batch(self, batch_id):
        return {**self.batches[batch_id], "status": self.status}

    def download(self, file_id):
        return self.files[file_id]

    def find_batch(self, file_id, run_id, exam_id):
        return next((b for b in self.batches.values() if b["input_file_id"] == file_id and b["metadata"] == {"run_id": run_id, "exam_id": exam_id}), None)


class FakeUrgent:
    def __init__(self, lose_ack=False):
        self.created = 0
        self.lose_ack = lose_ack
        self.status = "in_progress"

    def create_urgent_response(self, body, run_id, exam_id):
        self.created += 1
        if self.lose_ack:
            raise TimeoutError("Conferma persa")
        return {"id": "resp-test", "status": self.status}

    def get_response(self, response_id):
        return {"id": response_id, "status": self.status,
                "output_text": '<article class="study-schema"><h1>Risultato</h1><p>Contenuto di prova.</p></article>',
                "usage": {"input_tokens": 10, "output_tokens": 20}}


def fake_render(root, job, cfg, project):
    path = root / job["pdf"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        document.new_page().insert_text((45, 65), "PDF simulato " + job["hash"], fontsize=10)
        document.save(path)
    return {"ok": True, "pages": 1, "text_chars": 1000}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        shutil.copytree(PROJECT / "config", self.project / "config")
        shutil.copytree(PROJECT / "prompts", self.project / "prompts")
        self.root = self.new_exam("Pediatria", "ALFA_PEDIATRIA")
        exam = read_json(self.root / "esame.json")
        exam["pipeline"]["review_first_schema"] = False  # Existing workflow tests.
        write_json(self.root / "esame.json", exam)

    def tearDown(self):
        self.temp.cleanup()

    def new_exam(self, name, token):
        root = create_exam(name, self.project)
        (root / "input" / "programma.txt").write_text("Argomento alfa | alfa\n", encoding="utf-8")
        text = "Argomento alfa. " + token + ". " + "Il materiale descrive il concetto alfa, i suoi elementi e il percorso di studio. " * 50
        (root / "input" / "sbobine" / "sbobina.txt").write_text(text, encoding="utf-8")
        (root / "input" / "domande" / "domande.txt").write_text("Spiega il concetto alfa\n", encoding="utf-8")
        return root

    def engine(self, root=None, api=None, render=fake_render):
        root = root or self.root
        return Pipeline(root, load_config(root, self.project), PROJECT, api=api, render=render)

    def test_urgent_response_recovers_after_restart_without_duplicate(self):
        exam = read_json(self.root / "esame.json")
        exam["execution_mode"] = "urgent"
        write_json(self.root / "esame.json", exam)
        api = FakeUrgent()
        self.engine(api=api).tick(True)
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(state["batches"][0]["response_id"], "resp-test")
        self.assertEqual(api.created, 1)
        api.status = "completed"
        self.engine(api=api).tick(True)
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(api.created, 1)
        self.assertTrue(state["batches"][0]["processed"])
        self.assertEqual(state["jobs"][state["latest"]["1"]]["status"], "PDF_OK")

    def test_urgent_lost_ack_is_never_automatically_resubmitted(self):
        exam = read_json(self.root / "esame.json")
        exam["execution_mode"] = "urgent"
        write_json(self.root / "esame.json", exam)
        api = FakeUrgent(lose_ack=True)
        self.engine(api=api).tick(True)
        self.engine(api=api).tick(True)
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(api.created, 1)
        self.assertEqual(state["batches"][0]["stage"], "uncertain")

    def test_dry_run_and_exam_isolation(self):
        other = self.new_exam("Medicina legale", "BETA_LEGALE")
        api = FakeAPI()
        self.engine(api=api).tick()
        self.engine(other, api).tick()
        self.assertEqual(api.created, 0)
        first = (self.root / "source_packs" / "schema_01" / "source_pack.json").read_text()
        second = (other / "source_packs" / "schema_01" / "source_pack.json").read_text()
        self.assertIn("ALFA_PEDIATRIA", first)
        self.assertNotIn("BETA_LEGALE", first)
        self.assertIn("BETA_LEGALE", second)
        with sqlite3.connect(self.root / "corpus" / "corpus.db") as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 2)

    def test_repeat_run_does_not_rebill(self):
        api = FakeAPI()
        self.engine(api=api).tick(True)
        self.engine(api=api).tick(True)
        self.assertEqual(api.created, 1)
        state = read_json(self.root / "state" / "pipeline.json")
        self.assertEqual(state["jobs"][state["latest"]["1"]]["status"], "PDF_OK")

    def test_render_failure_never_resubmits_api(self):
        api = FakeAPI()
        def fail(*args):
            raise ValueError("Errore di impaginazione")
        self.engine(api=api, render=fail).tick(True)
        self.engine(api=api).tick(True)
        self.assertEqual(api.created, 1)
        self.assertTrue((self.root / "output" / "pdf" / "1 - Argomento alfa.pdf").exists())

    def test_recover_accepted_batch_after_lost_ack(self):
        api = FakeAPI(lose_ack=True)
        self.engine(api=api).tick(True)
        self.engine(api=api).tick(True)
        self.assertEqual(api.created, 1)
        self.assertTrue((self.root / "output" / "pdf" / "1 - Argomento alfa.pdf").exists())

    def test_changed_sources_cannot_take_old_result_hash(self):
        api = FakeAPI(status="in_progress")
        self.engine(api=api).tick(True)
        state = read_json(self.root / "state" / "pipeline.json")
        old_hash = state["latest"]["1"]
        path = self.root / "input" / "sbobine" / "sbobina.txt"
        path.write_text(path.read_text() + "\n\nAlfa: nuova spiegazione del docente.")
        self.engine(api=api).tick(True)
        state = read_json(self.root / "state" / "pipeline.json")
        new_hash = state["latest"]["1"]
        self.assertNotEqual(new_hash, old_hash)
        self.assertEqual(api.created, 1)
        api.status = "completed"
        self.engine(api=api).tick(True)
        state = read_json(self.root / "state" / "pipeline.json")
        self.assertEqual(api.created, 2)
        self.assertEqual(state["jobs"][old_hash]["status"], "PDF_OK")
        self.assertEqual(state["jobs"][new_hash]["status"], "PDF_OK")
        with fitz.open(self.root / "output" / "pdf" / "1 - Argomento alfa.pdf") as document:
            self.assertIn(new_hash, document[0].get_text())

    def test_truncation_has_only_one_retry(self):
        api = FakeAPI(incomplete=True)
        self.assertTrue(self.engine(api=api).tick(True))
        self.engine(api=api).tick(True)
        self.engine(api=api).tick(True)
        self.assertEqual(api.created, 2)
        state = read_json(self.root / "state" / "pipeline.json")
        self.assertEqual(state["jobs"][state["latest"]["1"]]["retry_count"], 1)
        records = state["jobs"][state["latest"]["1"]]["usage_attempts"]
        self.assertEqual(len(records), 2)
        self.assertEqual({row["attempt"] for row in records.values()}, {0, 1})
        self.assertEqual({row["mode"] for row in records.values()}, {"batch"})
        self.assertTrue(all(row["model"] for row in records.values()))
        self.assertEqual(sum(row["usage"]["input_tokens"] for row in records.values()), 200)
        engine = self.engine(api=api)
        engine.consume(engine.batches[-1], {"status": "completed"})
        repeated = read_json(self.root / "state" / "pipeline.json")
        self.assertEqual(len(repeated["jobs"][repeated["latest"]["1"]]["usage_attempts"]), 2)

    def test_schema_numbers_are_not_limited_to_27_or_99(self):
        write_json(self.root / "input" / "schemi.json", [{"numero": 101, "titolo": "Alfa", "keywords": ["alfa"]}])
        self.engine().tick()
        state = read_json(self.root / "state" / "pipeline.json")
        self.assertIn("101", state["latest"])

    def test_changed_prompt_invalidates_generation(self):
        api = FakeAPI()
        self.engine(api=api).tick(True)
        (self.root / "prompt.txt").write_text("Istruzione aggiornata: sviluppa il concetto alfa.")
        self.engine(api=api).tick(True)
        self.assertEqual(api.created, 2)

    def test_same_filename_in_two_folders_is_not_overwritten(self):
        folder = self.root / "input" / "sbobine" / "seconda"
        folder.mkdir()
        shutil.copy2(self.root / "input" / "sbobine" / "sbobina.txt", folder / "sbobina.txt")
        self.engine().tick()
        with sqlite3.connect(self.root / "corpus" / "corpus.db") as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM documents WHERE source_type='sbobina'").fetchone()[0], 2)

    def test_changed_program_does_not_submit_obsolete_dry_run(self):
        self.engine().tick()
        (self.root / "input" / "programma.txt").write_text("Nuovo titolo alfa | alfa\n")
        api = FakeAPI()
        self.engine(api=api).tick(True)
        self.assertEqual(api.created, 1)
        self.assertIn("Nuovo titolo alfa", next(iter(api.files.values())).decode())

    def test_corrupt_state_does_not_silently_restart_billing(self):
        self.engine().tick()
        (self.root / "state" / "pipeline.json").write_text("invalid JSON")
        with self.assertRaises(json.JSONDecodeError):
            self.engine(api=FakeAPI())

    def test_micro_batches_limit_active_requests(self):
        (self.root / "input" / "programma.txt").write_text("\n".join(f"Alfa {i} | alfa" for i in range(10)))
        api = FakeAPI(status="in_progress")
        self.engine(api=api).tick(True)
        self.assertEqual(api.created, 2)
        self.assertTrue(all(b["request_counts"]["total"] <= 3 for b in api.batches.values()))
        api.status = "completed"
        self.engine(api=api).tick(True)
        state = read_json(self.root / "state" / "pipeline.json")
        self.assertTrue(all(state["jobs"][key]["status"] == "PDF_OK" for key in state["latest"].values()))

    def test_removed_source_is_removed_from_current_corpus(self):
        self.engine().tick()
        (self.root / "input" / "sbobine" / "sbobina.txt").unlink()
        self.engine().tick()
        state = read_json(self.root / "state" / "pipeline.json")
        self.assertEqual(state["jobs"][state["latest"]["1"]]["status"], "FONTI_MANCANTI")

    def test_second_truncation_stops_without_third_charge(self):
        api = FakeAPI(incomplete="always")
        for _ in range(4):
            self.engine(api=api).tick(True)
        self.assertEqual(api.created, 2)
        state = read_json(self.root / "state" / "pipeline.json")
        self.assertEqual(state["jobs"][state["latest"]["1"]]["status"], "FAILED_API")

    def test_pilot_blocks_other_schemes_until_pdf_approval(self):
        exam = read_json(self.root / "esame.json")
        exam["pipeline"]["review_first_schema"] = True
        write_json(self.root / "esame.json", exam)
        write_json(self.root / "input/schemi.json", [
            {"numero": n, "titolo": f"Alfa {n}", "keywords": ["alfa"]} for n in range(1, 4)])
        self.engine().tick(False)
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(len(state["batches"]), 1)
        self.assertEqual(state["batches"][0]["mode"], "urgent")
        self.assertEqual([state["jobs"][state["latest"][str(n)]]["status"] for n in (2, 3)], ["READY", "READY"])
        api = FakeUrgent()
        api.status = "completed"
        self.engine(api=api).tick(True)
        report = read_json(self.root / "output/reports/pipeline_report.json")
        self.assertEqual(report["pilot_review"]["status"], "ready_for_review")
        self.assertEqual(len(state["batches"]), 1)
        app = App(self.project, api_factory=lambda cfg: api)
        app.run = lambda slug: {"started": False}
        old_hash = report["pilot_review"]["hash"]
        app.review_pilot(self.root.name, "revise", "Aumenta la leggibilità dei titoli e delle tabelle.")
        self.engine().tick(False)
        state = read_json(self.root / "state/pipeline.json")
        self.assertNotEqual(state["latest"]["1"], old_hash)
        self.assertEqual(state["batches"][-1]["mode"], "urgent")
        api.status = "completed"
        self.engine(api=api).tick(True)
        app.review_pilot(self.root.name, "approve")
        self.engine().tick(False)
        state = read_json(self.root / "state/pipeline.json")
        self.assertTrue(any(b["mode"] == "batch" for b in state["batches"][2:]))
        second = state["jobs"][state["latest"]["2"]]
        self.assertIn("Aumenta la leggibilità", read_json(self.root / second["request"])["instructions"])

    def test_safe_table_layout_preserves_cells_without_table_elements(self):
        html = '<article class="study-schema"><table><tr><th>Voce</th><th>Esito</th></tr><tr><td>TSO</td><td><strong>Controllo</strong> clinico</td></tr></table></article>'
        result = linearize_tables(html)
        self.assertNotIn("<table", result)
        for text in ("Voce", "Esito", "TSO", "Controllo", "clinico"):
            self.assertIn(text, result)


if __name__ == "__main__":
    unittest.main()
