import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class UpdateStartupHealthTests(unittest.TestCase):
    def test_gui_startup_reports_healthy_when_authorization_server_is_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            update_dir = root / "LocalAppData" / "PhotoCollect" / "updates"
            update_dir.mkdir(parents=True)
            token = "1" * 32
            marker = update_dir / f"health-{token}.json"
            environment = os.environ.copy()
            environment.update({
                "LOCALAPPDATA": str(root / "LocalAppData"),
                "APPDATA": str(root / "AppData"),
                "QT_QPA_PLATFORM": "offscreen",
                "PHOTOCOLLECT_AUTH_ENABLED": "1",
                "PHOTOCOLLECT_AUTH_API_URL": "http://127.0.0.1:9",
            })
            process = subprocess.Popen(
                [
                    sys.executable,
                    "con.py",
                    "--post-update-health",
                    str(marker),
                    "--post-update-token",
                    token,
                ],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not marker.is_file():
                    if process.poll() is not None:
                        break
                    time.sleep(0.1)
                self.assertTrue(
                    marker.is_file(),
                    "GUI initialized but update health was blocked by authorization",
                )
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
