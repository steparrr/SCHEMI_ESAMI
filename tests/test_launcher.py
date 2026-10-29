from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import native


class LauncherTests(unittest.TestCase):
    def test_preview_copy_keeps_installed_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            installed = home / "Applications/Schemi Esami.app/Contents/MacOS/Schemi Esami"
            preview = home / "outputs/Schemi Esami.app/Contents/MacOS/Schemi Esami"
            installed.parent.mkdir(parents=True)
            preview.parent.mkdir(parents=True)
            installed.touch()
            preview.touch()
            agent = home / "Library/LaunchAgents/it.schemi-esami.worker.plist"
            agent.parent.mkdir(parents=True)
            agent.write_bytes(plistlib.dumps({"ProgramArguments": [str(installed), "--worker"]}))
            with patch.object(Path, "home", return_value=home), patch.object(sys, "executable", str(preview)):
                self.assertEqual(native.worker_executable(agent), installed.resolve())

    def test_installed_update_takes_current_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            current = home / "Applications/Schemi Esami 2026.10.app/Contents/MacOS/Schemi Esami"
            current.parent.mkdir(parents=True)
            current.touch()
            with patch.object(Path, "home", return_value=home), patch.object(sys, "executable", str(current)):
                self.assertEqual(native.worker_executable(home / "missing.plist"), current.resolve())

    def test_source_launch_agent_uses_venv_and_a_separate_label(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            project = Path(folder) / "schemi-esami-v2"
            python = project / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)
            (project / "state").mkdir()
            (project / "native.py").write_text("# ingresso di prova\n")
            installed_agent = home / "Library/LaunchAgents/it.schemi-esami.worker.plist"
            installed_agent.parent.mkdir(parents=True)
            installed_agent.write_text("app installata: conservare\n")
            success = subprocess.CompletedProcess(["launchctl"], 0, "", "")
            with patch.object(sys, "platform", "darwin"), patch.object(sys, "frozen", False, create=True), \
                 patch.object(Path, "home", return_value=home), patch.object(native, "resource_root", return_value=project), \
                 patch.object(native, "_launchctl", return_value=success), \
                 patch.object(native, "_bootstrap_agent", return_value=success):
                native.ensure_login_worker(project)
            path = home / "Library/LaunchAgents/it.schemi-esami.source-worker.plist"
            agent = plistlib.loads(path.read_bytes())
            self.assertEqual(agent["Label"], "it.schemi-esami.source-worker")
            self.assertEqual(agent["ProgramArguments"], [str(python), str(project / "native.py"), "--worker"])
            self.assertEqual(agent["EnvironmentVariables"]["SCHEMI_PROJECT_DIR"], str(project))
            self.assertEqual(installed_agent.read_text(), "app installata: conservare\n")


if __name__ == "__main__":
    unittest.main()
