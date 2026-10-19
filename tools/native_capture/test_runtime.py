"""Exercise the real file-control worker and PowerShell client with a stub backend."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent
HOST = ROOT / "bin/AutoRuntimeTestHost.exe"
PWSH = shutil.which("pwsh")


class RuntimeIntegrationTest(unittest.TestCase):
    def wait_state(self, root, process, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            try:
                latest = json.loads((root / "autostart-state.json").read_text(encoding="utf-8"))
                if latest["pid"] == process.pid and predicate(latest):
                    return latest
            except (FileNotFoundError, PermissionError, json.JSONDecodeError):
                pass
            if process.poll() is not None:
                self.fail(f"Worker exited {process.returncode}; state={latest}")
            time.sleep(0.05)
        self.fail(f"Timed out; state={latest}")

    def control(self, root, action):
        run = subprocess.run([PWSH, "-NoProfile", "-File", str(ROOT / "runtime_client.ps1"),
                              "-Root", str(root), "-ExpectedProcessName", "AutoRuntimeTestHost", "-Action", action],
                             capture_output=True, text=True, timeout=18, encoding="utf-8")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return run.stdout

    def raw_command(self, state, command_id, kind):
        directory = Path(state["run_directory"])
        temporary = directory / "test-command.tmp"
        temporary.write_bytes(f"OPC-CONTROL-1\n{command_id}\n{kind}\n".encode("ascii"))
        os.replace(temporary, directory / "control.command")

    def test_strict_command_parser(self):
        run = subprocess.run([str(HOST), "--parser-test"], capture_output=True, text=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    @unittest.skipUnless(PWSH, "PowerShell 7 required for the runtime client integration")
    def test_commands_reconnect_pause_resume_writer_error_and_relaunch(self):
        with tempfile.TemporaryDirectory(prefix="runtime-", dir=ROOT / "captures") as work:
            root = Path(work) / "運用"
            root.mkdir()
            process = subprocess.Popen([str(HOST), str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                initial = self.wait_state(root, process, lambda s: s["state"] == "binding_ready_waiting_for_start")
                self.assertTrue(initial["identity_observing"])
                context = Path(initial["run_directory"]) / "identity-context/identity.opmeta"
                self.assertTrue(context.exists())
                self.control(root, "Start")
                first = self.wait_state(root, process, lambda s: s["recording"])
                first_capture = Path(first["capture_directory"]) / "capture.opcap"
                digest = hashlib.sha256(first_capture.read_bytes()).hexdigest()
                (root / "disconnect.fixture").touch()
                lost = self.wait_state(root, process, lambda s: s["state"] == "waiting_for_binding")
                self.assertFalse(lost["recording"])
                self.assertTrue(lost["desired_recording"])
                status = json.loads(self.control(root, "Status"))
                self.assertFalse(status["effective_recording"])
                (root / "disconnect.fixture").unlink()
                restored = self.wait_state(root, process, lambda s: s["recording"] and s["binding_generation"] > first["binding_generation"])
                self.assertGreater(restored["connection_gaps"], 0)
                self.control(root, "Stop")
                self.control(root, "Stop")
                paused = self.wait_state(root, process, lambda s: s["state"] == "paused")
                self.assertTrue(paused["identity_observing"])
                reference = json.loads((first_capture.parent / "identity-context.json").read_text())
                self.assertEqual(reference["run_name"], initial["run_name"])
                self.assertEqual(reference["pid"], process.pid)
                self.assertGreaterEqual(reference["end_qpc"], reference["start_qpc"])
                self.control(root, "Start")
                second = self.wait_state(root, process, lambda s: s["recording"] and s["segment_number"] == 2)
                self.assertNotEqual(second["capture_directory"], first["capture_directory"])
                self.assertEqual(hashlib.sha256(first_capture.read_bytes()).hexdigest(), digest)
                skipped = second["last_command_id"] + 2
                self.raw_command(second, skipped, "stop")
                rejected = self.wait_state(root, process, lambda s: s["last_command_id"] == skipped)
                self.assertEqual(rejected["last_command_error"], 13)
                self.assertTrue(rejected["recording"])
                self.raw_command(rejected, 1, "stop")  # an old command must not execute again
                time.sleep(0.65)
                replay = self.wait_state(root, process, lambda s: s["last_command_id"] == skipped)
                self.assertTrue(replay["recording"])
                (root / "writer-error.fixture").touch()
                failed = self.wait_state(root, process, lambda s: s["state"] == "writer_error")
                self.assertFalse(failed["recording"])
                self.assertFalse(failed["desired_recording"])
                (root / "writer-error.fixture").unlink()
                self.control(root, "Start")
                recovered = self.wait_state(root, process, lambda s: s["recording"] and s["segment_number"] == 3)
                self.control(root, "Stop")
                command_file = Path(recovered["run_directory"]) / "control.command"
                command_file.write_text("malformed", encoding="ascii")
                self.wait_state(root, process, lambda s: s["state"] == "control_error")
                self.control(root, "Shutdown")
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
                events = [json.loads(line) for line in (Path(initial["run_directory"]) / "continuity-events.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertTrue(any(e["state"] == "waiting_for_binding" for e in events))
                self.assertTrue(any(e["state"] == "writer_error" for e in events))
                self.assertEqual(events[-1]["state"], "shutdown")
                inactive = json.loads(self.control(root, "Status"))
                self.assertFalse(inactive["effective_recording"])
                self.assertFalse(inactive["runtime"]["identity_observing"])
                process.stderr.close()
                process = subprocess.Popen([str(HOST), str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                restarted = self.wait_state(root, process, lambda s: s["state"] == "binding_ready_waiting_for_start")
                self.assertNotEqual(restarted["run_directory"], initial["run_directory"])
                self.assertEqual(restarted["last_command_id"], 0)
                self.assertFalse(restarted["recording"])
                self.assertTrue(restarted["identity_observing"])
                self.assertNotEqual(Path(restarted["run_directory"]) / "identity-context/identity.opmeta", context)
                self.control(root, "Start")
                self.wait_state(root, process, lambda s: s["recording"] and s["segment_number"] == 1)
                self.control(root, "Shutdown")
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()  # only this owned fixture, never the game
                    process.wait(timeout=5)
                process.stderr.close()


if __name__ == "__main__":
    unittest.main()
