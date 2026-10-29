from __future__ import annotations

import os
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
    def test_finder_path_keeps_existing_entries_and_finds_homebrew_tools(self):
        with patch.object(sys, "platform", "darwin"), patch.dict(os.environ, {"PATH": "/custom/bin:/usr/bin"}):
            native.configure_local_tools()
            entries = os.environ["PATH"].split(os.pathsep)
            self.assertEqual(entries[0], "/custom/bin")
            self.assertEqual(entries.count("/usr/bin"), 1)
            self.assertIn("/opt/homebrew/bin", entries)
            self.assertIn("/usr/local/bin", entries)

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

    def test_system_app_is_kept_when_opening_a_preview_build(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "home"
            system_apps = Path(folder) / "system-Applications"
            installed = system_apps / "Schemi Esami.app/Contents/MacOS/Schemi Esami"
            preview = Path(folder) / "dist/Schemi Esami.app/Contents/MacOS/Schemi Esami"
            installed.parent.mkdir(parents=True)
            preview.parent.mkdir(parents=True)
            installed.touch()
            preview.touch()
            agent = home / "Library/LaunchAgents/it.schemi-esami.worker.plist"
            agent.parent.mkdir(parents=True)
            agent.write_bytes(plistlib.dumps({"ProgramArguments": [str(installed), "--worker"]}))
            with patch.object(Path, "home", return_value=home), \
                 patch.object(native, "application_roots", return_value=(home / "Applications", system_apps)), \
                 patch.object(sys, "executable", str(preview)):
                self.assertEqual(native.worker_executable(agent), installed.resolve())
                agent.unlink()
                self.assertEqual(native.worker_executable(agent), installed.resolve())
                with patch.object(sys, "executable", str(installed)):
                    self.assertEqual(native.worker_executable(agent), installed.resolve())

    def test_replacing_app_at_same_path_changes_worker_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            installed = home / "Applications/Schemi Esami.app/Contents/MacOS/Schemi Esami"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"old build")
            agent_path = home / "Library/LaunchAgents/it.schemi-esami.worker.plist"
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(Path, "home", return_value=home), \
                 patch.object(sys, "executable", str(installed)):
                _, before = native.worker_agent(home / "project", agent_path)
                installed.write_bytes(b"newer installed build")
                _, after = native.worker_agent(home / "project", agent_path)
            self.assertEqual(before["ProgramArguments"], after["ProgramArguments"])
            self.assertNotEqual(before["EnvironmentVariables"], after["EnvironmentVariables"])

    def test_installed_replacement_restarts_running_worker(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            installed = home / "Applications/Schemi Esami.app/Contents/MacOS/Schemi Esami"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"old build")
            project = home / "Library/Application Support/Schemi Esami"
            (project / "state").mkdir(parents=True)
            agent_path = home / "Library/LaunchAgents/it.schemi-esami.worker.plist"
            agent_path.parent.mkdir(parents=True)
            success = subprocess.CompletedProcess(["launchctl"], 0, "", "")
            with patch.object(sys, "platform", "darwin"), patch.object(sys, "frozen", True, create=True), \
                 patch.object(Path, "home", return_value=home), patch.object(sys, "executable", str(installed)), \
                 patch.object(native, "_launchctl", return_value=success) as launch, \
                 patch.object(native, "_bootstrap_agent", return_value=success) as bootstrap:
                _, old = native.worker_agent(project, agent_path)
                agent_path.write_bytes(plistlib.dumps(old))
                installed.write_bytes(b"newer installed build")
                native.ensure_login_worker(project)
            self.assertNotEqual(plistlib.loads(agent_path.read_bytes()), old)
            launch.assert_any_call("bootout", f"gui/{os.getuid()}/it.schemi-esami.worker")
            bootstrap.assert_called_once_with(agent_path)

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
                other_data = Path(folder) / "separate-data"
                _, separate = native.worker_agent(other_data, home / "unused.plist")
            path = home / "Library/LaunchAgents/it.schemi-esami.source-worker.plist"
            agent = plistlib.loads(path.read_bytes())
            self.assertEqual(agent["Label"], "it.schemi-esami.source-worker")
            self.assertEqual(agent["ProgramArguments"], [str(python), str(project / "native.py"), "--worker"])
            self.assertEqual(agent["EnvironmentVariables"]["SCHEMI_PROJECT_DIR"], str(project))
            self.assertEqual(separate["ProgramArguments"][0], str(python))
            self.assertEqual(separate["EnvironmentVariables"]["SCHEMI_PROJECT_DIR"], str(other_data))
            self.assertEqual(installed_agent.read_text(), "app installata: conservare\n")


if __name__ == "__main__":
    unittest.main()
