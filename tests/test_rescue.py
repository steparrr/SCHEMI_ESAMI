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
from rescue import search_and_rescue


ARTICLE = '<article class="study-schema"><h1>Schema ritrovato</h1><p>Test.</p></article>'


def fake_render(root, job, cfg, project):
    path = root / job["pdf"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open() as document:
        document.new_page().insert_text((45, 65), "PDF recuperato")
        document.save(path)
    return {"ok": True, "pages": 1, "text_chars": 200}


class ExistingAPI:
    def __init__(self, remote=None):
        self.remote = remote
        self.lookups = 0
        self.downloads = 0

    def find_batch(self, input_file_id, run_id, exam_id):
        self.lookups += 1
        return self.remote

    def get_batch(self, batch_id):
        return self.remote

    def download(self, file_id):
        self.downloads += 1
        row = {"custom_id": "cid-1", "response": {"status_code": 200,
               "request_id": "req-1", "body": {"status": "completed", "output_text": ARTICLE}}}
        return (json.dumps(row) + "\n").encode()


class RescueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        shutil.copytree(PROJECT / "config", self.project / "config")
        shutil.copytree(PROJECT / "prompts", self.project / "prompts")
        self.root = create_exam("Pediatria", self.project)
        self.html = "output/raw/schema_01/h1/schema.raw.html"
        self.pdf = "output/pdf/versioni/schema_01_h1.pdf"
        self.job = {"hash": "h1", "kind": "schema", "numero": 1, "title": "Argomento uno",
                    "status": "SUBMISSION_UNCERTAIN", "retry_count": 0,
                    "pack": "output/raw/schema_01/h1/pack.json",
                    "html": self.html, "pdf": self.pdf, "manifest": "output/raw/schema_01/h1/images.jsonl"}
        (self.root / self.job["pack"]).parent.mkdir(parents=True)
        write_json(self.root / self.job["pack"], {})
        self.batch = {"run_id": "run-1", "input_file_id": "file-input", "stage": "uncertain",
                      "mode": "batch", "requests": [{"cid": "cid-1", "hash": "h1", "attempt": 0}]}
        self.save_state()

    def tearDown(self):
        self.temp.cleanup()

    def save_state(self, batches=None):
        write_json(self.root / "state/pipeline.json", {"version": 1, "jobs": {"h1": self.job},
                   "latest": {"1": "h1"}, "batches": [self.batch] if batches is None else batches})

    def run_rescue(self, api=None):
        return search_and_rescue(self.root, load_config(self.root, self.project),
                                 PROJECT, api=api, render=fake_render)

    def test_uncertain_batch_found_and_pdf_recovered_without_post(self):
        remote = {"id": "batch-existing", "status": "completed", "output_file_id": "file-output",
                  "request_counts": {"total": 1, "completed": 1, "failed": 0}}
        api = ExistingAPI(remote)
        result = self.run_rescue(api)
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(result["recovered_pdfs"], [1])
        self.assertTrue((self.root / self.html).is_file())
        self.assertTrue((self.root / self.pdf).is_file())
        self.assertEqual(state["batches"][0]["batch_id"], "batch-existing")
        self.assertEqual(state["jobs"]["h1"]["status"], "PDF_OK")
        self.assertEqual(api.lookups, 1)
        self.assertEqual(api.downloads, 1)

    def test_missing_batch_is_not_resubmitted(self):
        api = ExistingAPI()
        result = self.run_rescue(api)
        state = read_json(self.root / "state/pipeline.json")
        self.assertEqual(result["recovered_pdfs"], [])
        self.assertEqual(state["batches"][0]["stage"], "uncertain")
        self.assertEqual(state["jobs"]["h1"]["status"], "SUBMISSION_UNCERTAIN")
        self.assertEqual(api.lookups, 1)

    def test_local_saved_result_rebuilds_missing_html_and_pdf(self):
        row = {"custom_id": "cid-1", "response": {"status_code": 200,
               "body": {"status": "completed", "output_text": ARTICLE}}}
        path = self.root / "batch/batch-existing_output.jsonl"
        path.write_text(json.dumps(row) + "\n")
        self.job["status"] = "LOCAL_HTML_MISSING"
        self.batch.update(stage="processed", processed=True, batch_id="batch-existing",
                          output_path="batch/batch-existing_output.jsonl")
        self.save_state()
        result = self.run_rescue()
        self.assertEqual(result["restored_html"], [1])
        self.assertEqual(result["recovered_pdfs"], [1])

    def test_no_sources_is_reported_without_api_submission(self):
        self.job["status"] = "FONTI_MANCANTI"
        self.save_state(batches=[])
        result = self.run_rescue()
        self.assertEqual(result["rows"][0]["status"], "FONTI_MANCANTI")
        self.assertIn("fonte", result["rows"][0]["detail"].lower())
        self.assertFalse((self.root / self.pdf).exists())

    def test_failed_plan_is_reported_when_no_scheme_exists(self):
        write_json(self.root / "state/pipeline.json", {"version": 1,
                   "jobs": {"plan-1": {"hash": "plan-1", "kind": "plan", "key": "plan",
                   "status": "FAILED_API", "error": "Pianificazione non valida: max_output_tokens"}},
                   "planning_latest": {"plan": "plan-1"}, "latest": {}, "batches": []})
        result = self.run_rescue()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["planning"][0]["status"], "FAILED_API")


if __name__ == "__main__":
    unittest.main()
