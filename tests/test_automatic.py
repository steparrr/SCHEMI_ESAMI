from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import fitz

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from common import create_exam, load_config, read_json, write_json
from model_settings import save_default_profile
from pipeline import Pipeline
from test_pipeline import FakeAPI, fake_render


class PlanningAPI(FakeAPI):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.omit_unit = False

    def create_batch(self, file_id, run_id, exam_id):
        self.created += 1
        batch_id = "batch-" + str(self.created)
        rows = []
        for line in self.files[file_id].decode().splitlines():
            request = json.loads(line)
            body = request["body"]
            phase = body["metadata"].get("phase")
            if phase == "analysis":
                source = json.loads(body["input"][0]["content"][0]["text"])
                groups = {"Alfa": [], "Beta": []}
                for unit in source["units"]:
                    groups["Beta" if "beta" in unit["testo"].lower() else "Alfa"].append(unit["id"])
                if self.omit_unit:
                    next(group for group in groups.values() if group).pop()
                value = {"argomenti": [{"titolo": name, "punti_essenziali": ["Definizione e distinzione documentate"], "keywords": [name.lower()], "unita": group, "priorita": 4, "note": ""} for name, group in groups.items() if group], "esclusioni": [], "lacune": [], "discordanze": []}
                text = json.dumps(value)
            elif phase == "plan":
                source = json.loads(body["input"][0]["content"][0]["text"])
                text = json.dumps({"schemi": [{"titolo": topic["titolo"], "argomenti": [topic["id"]], "pagine": 1, "keywords": topic["keywords"]} for topic in source["topics"]], "lacune": [], "discordanze": [], "nota_limite": ""})
            else:
                text = '<article class="study-schema"><h1>Schema tecnico</h1><p>Risultato del simulatore.</p></article>'
            rows.append({"custom_id": request["custom_id"], "response": {"status_code": 200, "request_id": request["custom_id"], "body": {"status": "completed", "output_text": text, "usage": {"input_tokens": 100, "output_tokens": 200}}}, "error": None})
        output = "output-" + batch_id
        self.files[output] = "\n".join(json.dumps(row) for row in reversed(rows)).encode()
        batch = {"id": batch_id, "status": self.status, "input_file_id": file_id, "output_file_id": output, "metadata": {"run_id": run_id, "exam_id": exam_id}, "request_counts": {"total": len(rows), "completed": len(rows), "failed": 0}}
        self.batches[batch_id] = batch
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("Risposta persa dopo accettazione")
        return batch


class AutomaticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        shutil.copytree(PROJECT / "config", self.base / "config")
        self.root = create_exam("Materia tecnica", self.base)
        for topic in ("alfa", "beta"):
            (self.root / "input/sbobine" / (topic + ".txt")).write_text((topic + " definizione criterio distinzione precisa. ") * 60)
        (self.root / "input/domande/domande.txt").write_text("Descrivi alfa\nDescrivi beta\n")

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, api=None, render=fake_render):
        return Pipeline(self.root, load_config(self.root, PROJECT), api=api, render=render)

    def fast(self, pages=2):
        exam = read_json(self.root / "esame.json")
        exam.update(modalita="veloce", max_pagine=pages)
        write_json(self.root / "esame.json", exam)

    def test_dry_run_prepares_analysis_without_manual_topics_or_api(self):
        self.engine().tick()
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(state["input_status"], "LETTURA_MATERIALI")
        self.assertEqual(state["latest"], {})
        self.assertTrue(all(job["kind"] == "analysis" and job["status"] == "READY" for job in state["jobs"].values()))

    def test_automatic_plan_generates_chapters_and_ordered_combined_pdf(self):
        api = PlanningAPI()
        self.engine(api).tick(True)
        state = read_json(self.root / "state/pipeline.json")
        plan = state["jobs"][state["planning_latest"]["plan"]]
        request = read_json(self.root / plan["request"])
        snapshot = read_json(self.root / plan["pack"])
        self.assertEqual(request["reasoning"]["effort"], "low")
        self.assertTrue(all("unita" not in topic for topic in snapshot["topics"]))
        report = read_json(self.root / "output/reports/pipeline_report.json")
        self.assertEqual([row["status"] for row in report["rows"]], ["PDF_OK", "PDF_OK"])
        self.assertTrue((self.root / "output/pdf/1 - Alfa.pdf").exists())
        self.assertTrue((self.root / "output/pdf/2 - Beta.pdf").exists())
        with fitz.open(self.root / report["exports"]["combined"]) as document:
            self.assertEqual(document.page_count, 2)
            self.assertEqual([entry[1] for entry in document.get_toc()], ["1 - Alfa", "2 - Beta"])
        packs = [read_json(path) for path in (self.root / "source_packs").glob("*/source_pack.json")]
        self.assertEqual(sum(len(pack["questions"]) for pack in packs), 2)
        count = api.created
        self.engine(api).tick(True)
        self.assertEqual(api.created, count)

    def test_missing_unit_in_analysis_is_rejected_with_only_one_retry(self):
        api = PlanningAPI()
        api.omit_unit = True
        for _ in range(4):
            self.engine(api).tick(True)
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(api.created, 2)
        self.assertEqual(state["latest"], {})
        self.assertIn("Unità non organizzate", next(iter(state["jobs"].values()))["error"])

    def test_analysis_lost_ack_is_reconciled_without_double_submission(self):
        api = PlanningAPI(lose_ack=True)
        self.engine(api).tick(True)
        for _ in range(3):
            self.engine(api).tick(True)
        self.assertEqual(api.created, 3)  # Un'analisi, un piano, un batch di due PDF.

    def test_old_analysis_cannot_be_used_as_new_sources(self):
        api = PlanningAPI(status="in_progress")
        self.engine(api).tick(True)
        old = read_json(self.root / "state/pipeline.json")["planning_latest"]["analysis:1"]
        (self.root / "input/sbobine/alfa.txt").write_text("alfa VERSIONE NUOVA documentata. " * 100)
        api.status = "completed"
        self.engine(api).tick(True)
        state = read_json(self.root / "state/pipeline.json")
        self.assertNotEqual(state["planning_latest"]["analysis:1"], old)
        pack = read_json(self.root / "source_packs/schema_01/source_pack.json")
        self.assertIn("VERSIONE NUOVA", " ".join(chunk["text"] for chunk in pack["chunks"]))

    def test_global_model_upgrade_does_not_change_existing_exam_or_bill(self):
        api = PlanningAPI()
        self.engine(api).tick(True)
        original = read_json(self.root / "esame.json")["pipeline"]["model"]
        cfg = read_json(self.base / "config/pipeline.json")
        cfg["model"] = "modello-futuro-di-prova"
        cfg["reasoning_effort"] = "high"
        save_default_profile(self.base, cfg)
        self.assertEqual(load_config(self.root, self.base)["model"], original)
        count = api.created
        self.engine(api).tick(True)
        self.assertEqual(api.created, count)
        new = create_exam("Nuova materia", self.base)
        self.assertEqual(load_config(new, self.base)["model"], "modello-futuro-di-prova")

    def test_fast_mode_rewrites_once_and_combined_pdf_obeys_total_limit(self):
        self.fast()
        def render(root, job, cfg, project):
            if not job["retry_count"]:
                with fitz.open() as document:
                    for _ in range(3):
                        document.new_page().insert_text((45, 65), "Troppo lungo")
                    document.save(root / job["pdf"])
                return {"ok": True, "pages": 3}
            return fake_render(root, job, cfg, project)
        api = PlanningAPI()
        for _ in range(3):
            self.engine(api, render).tick(True)
        report = read_json(self.root / "output/reports/pipeline_report.json")
        self.assertEqual(report["exports"]["pages"], 2)
        self.assertTrue(all(row["retry_count"] == 1 for row in report["rows"]))

    def test_too_tight_limit_stops_without_cutting_or_endless_retries(self):
        self.fast()
        def render(root, job, cfg, project):
            return {"ok": True, "pages": 3}
        api = PlanningAPI()
        for _ in range(4):
            self.engine(api, render).tick(True)
        report = read_json(self.root / "output/reports/pipeline_report.json")
        self.assertTrue(all(row["status"] == "LIMITE_PAGINE" for row in report["rows"]))
        self.assertIsNone(report["exports"]["combined"])
        before = api.created
        self.engine(api, render).tick(True)
        self.assertEqual(api.created, before)


if __name__ == "__main__":
    unittest.main()
