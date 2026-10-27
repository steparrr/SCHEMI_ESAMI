from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
from cloud_handoff import build_package, code_fingerprint, extract_package, verify_package
from cloud_inbox import accept_package
from common import create_exam, load_config, read_json, write_json
from pipeline import Pipeline
from test_automatic import PlanningAPI
from test_pipeline import fake_render


class CloudHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.project = self.base / "app"
        for folder in ("config", "prompts", "scripts", "renderer"):
            shutil.copytree(PROJECT / folder, self.project / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(PROJECT / "native.py", self.project / "native.py")
        self.root = create_exam("Materia cloud", self.project)
        (self.root / "input/sbobine/fonte.txt").write_text("Una fonte di prova", encoding="utf-8")
        self.package = self.base / "job.zip"

    def tearDown(self):
        self.temporary.cleanup()

    def test_round_trip_keeps_input_and_rejects_another_code_version(self):
        result = build_package(self.project, self.root.name, self.package)
        manifest = verify_package(self.package, self.project)
        self.assertEqual(manifest["job_id"], result["job_id"])
        self.assertEqual(manifest["code_fingerprint"], code_fingerprint(self.project))
        self.assertEqual(len(result["sha256"]), 64)
        destination = self.base / "received"
        extract_package(self.package, destination, self.project)
        self.assertEqual((destination / "esami" / self.root.name / "input/sbobine/fonte.txt").read_text(),
                         "Una fonte di prova")
        with self.assertRaises(FileExistsError):
            extract_package(self.package, destination, self.project)
        (self.project / "scripts/cloud_handoff.py").write_text("# another version\n")
        with self.assertRaisesRegex(ValueError, "Versione del motore"):
            verify_package(self.package, self.project)

    def test_existing_remote_state_is_not_transferred_as_new_job(self):
        state = {"jobs": {"hash": {"status": "SUBMITTED"}}, "batches": [{"batch_id": "batch-existing"}]}
        write_json(self.root / "state/pipeline.json", state)
        with self.assertRaisesRegex(ValueError, "Esame già avviato"):
            build_package(self.project, self.root.name, self.package)
        self.assertFalse(self.package.exists())

    def test_extracted_job_runs_existing_pipeline_with_fake_api(self):
        exam = read_json(self.root / "esame.json")
        exam["pipeline"]["review_first_schema"] = False
        exam["pipeline"].pop("schema_page_min", None)
        exam["pipeline"].pop("schema_page_max", None)
        write_json(self.root / "esame.json", exam)
        (self.root / "input/domande/domande.txt").write_text("Descrivi alfa\n", encoding="utf-8")
        build_package(self.project, self.root.name, self.package)
        remote_project = self.base / "remote-project"
        extract_package(self.package, remote_project, self.project)
        remote_root = remote_project / "esami" / self.root.name
        api = PlanningAPI()
        pipeline = Pipeline(remote_root, load_config(remote_root, remote_project), remote_project,
                            api=api, render=fake_render)
        pipeline.tick(True)
        report = read_json(remote_root / "output/reports/pipeline_report.json")
        self.assertTrue(report["rows"])
        self.assertTrue(all(row["status"] == "PDF_OK" for row in report["rows"]))
        self.assertGreater(api.created, 0)

    def test_config_with_secret_field_is_rejected(self):
        config = read_json(self.project / "config/pipeline.json")
        config["api_key"] = "fixture-secret"
        write_json(self.project / "config/pipeline.json", config)
        with self.assertRaisesRegex(ValueError, "credenziale"):
            build_package(self.project, self.root.name, self.package)

    def test_durable_acceptance_is_idempotent_and_rejects_conflicts(self):
        result = build_package(self.project, self.root.name, self.package)
        storage = self.base / "cloud-data"
        first = accept_package(self.package, result["sha256"], storage, self.project)
        second = accept_package(self.package, result["sha256"], storage, self.project)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "accepted")
        self.assertTrue((storage / "jobs" / result["job_id"] / "esami" / self.root.name / "esame.json").is_file())
        with self.assertRaisesRegex(ValueError, "impronta attesa"):
            accept_package(self.package, "0" * 64, storage, self.project)
        (self.root / "input/sbobine/fonte.txt").write_text("Una fonte diversa", encoding="utf-8")
        changed = self.base / "changed.zip"
        other = build_package(self.project, self.root.name, changed, job_id=result["job_id"])
        with self.assertRaisesRegex(ValueError, "ID lavoro"):
            accept_package(changed, other["sha256"], storage, self.project)

    def test_retry_completes_after_archive_saved_without_receipt(self):
        result = build_package(self.project, self.root.name, self.package)
        storage = self.base / "cloud-data"
        (storage / "packages").mkdir(parents=True)
        shutil.copy2(self.package, storage / "packages" / (result["job_id"] + ".zip"))
        receipt = accept_package(self.package, result["sha256"], storage, self.project)
        self.assertEqual(receipt["sha256"], result["sha256"])
        self.assertTrue((storage / "jobs" / result["job_id"]).is_dir())

    def test_extra_or_changed_files_are_rejected(self):
        build_package(self.project, self.root.name, self.package)
        with zipfile.ZipFile(self.package) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        altered = self.base / "altered.zip"
        target = f"esami/{self.root.name}/input/sbobine/fonte.txt"
        members[target] = b"X" * len(members[target])
        with zipfile.ZipFile(altered, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, "Integrità"):
            verify_package(altered)
        members[target] = b"Una fonte di prova"
        members["esami/../outside.txt"] = b"No"
        with zipfile.ZipFile(altered, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(ValueError, "Elenco dei file"):
            verify_package(altered)


if __name__ == "__main__":
    unittest.main()
