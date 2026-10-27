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
from common import create_exam, read_json, write_json


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

    def test_config_with_secret_field_is_rejected(self):
        config = read_json(self.project / "config/pipeline.json")
        config["api_key"] = "fixture-secret"
        write_json(self.project / "config/pipeline.json", config)
        with self.assertRaisesRegex(ValueError, "credenziale"):
            build_package(self.project, self.root.name, self.package)

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
