from __future__ import annotations

import plistlib
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


if __name__ == "__main__":
    unittest.main()
