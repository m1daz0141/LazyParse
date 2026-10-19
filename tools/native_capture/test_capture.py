"""Meaningful capture-file checks against the native synthetic receive fixture."""
from collections import Counter
import json
import struct
from pathlib import Path
import subprocess
import tempfile
import unittest

from read_capture import HEADER, RECORD, decode_record, records, read_header

ROOT = Path(__file__).resolve().parent


class CaptureIntegrationTest(unittest.TestCase):
    def test_run_identity_context_survives_recording_boundaries(self):
        from tools.combat_core.__main__ import replay, follow
        from tools.combat_core.model import IdentityRegistry
        import contextlib
        from io import StringIO
        with tempfile.TemporaryDirectory(prefix="observer-", dir=ROOT / "captures") as work:
            output = Path(work) / "run-fixture"
            run = subprocess.run([str(ROOT / "bin/ObserverTestHost.exe"),
                str(ROOT / "bin/OverParseCaptureTest.dll"), str(output)], capture_output=True, text=True, timeout=40)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            reports = []
            for number in range(1, 5):
                capture = output / f"segment-{number:06d}" / "capture.opcap"
                report = replay(capture)
                self.assertEqual(report["capture_diagnostics"]["identity_written"], 0)
                self.assertIsNotNone(report["identity_context"])
                with contextlib.redirect_stdout(StringIO()):
                    followed = follow(capture, IdentityRegistry(), 10, lambda event: None)
                self.assertEqual(report, followed)
                reports.append(report)
            self.assertEqual(reports[0]["roles"]["self"]["damage_dealt"], 100)
            self.assertEqual(reports[0]["roles"]["enemy"]["damage_taken"], 100)
            self.assertEqual(reports[1]["roles"]["npc"]["damage_taken"], 200)
            self.assertEqual(reports[1]["roles"]["enemy"]["damage_taken"], 300)
            self.assertEqual(reports[1]["roles"]["self"]["damage_dealt"], 500)
            self.assertNotIn("self", reports[2]["roles"])
            self.assertNotIn("enemy", reports[2]["roles"])
            self.assertEqual(reports[2]["roles"]["player_unresolved"]["damage_dealt"], 50)
            self.assertEqual(reports[3]["roles"]["self"]["damage_dealt"], 400)
            self.assertEqual(reports[3]["identity_state"]["self"]["object_id"], 84)

    def test_identity_prefix_capture(self):
        header = struct.Struct("<8sIIQQ")
        record = struct.Struct("<QQQQII192s")
        with tempfile.TemporaryDirectory(prefix="identity-", dir=ROOT / "captures") as work:
            output = Path(work) / "recording"
            run = subprocess.run([str(ROOT / "bin/IdentityTestHost.exe"),
                str(ROOT / "bin/OverParseCaptureTest.dll"), str(output)], capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            for part, expected in [("part-1", 4), ("part-2", 1)]:
                raw = (output / part / "identity.opmeta").read_bytes()
                magic, version, size, frequency, _ = header.unpack_from(raw)
                self.assertEqual((magic, version, size), (b"OPMETA02", 2, 232))
                self.assertGreater(frequency, 0)
                self.assertEqual(len(raw), 32 + expected * 232)
                rows = [record.unpack_from(raw, offset) for offset in range(32, len(raw), 232)]
                self.assertEqual([row[0] for row in rows], list(range(1, expected + 1)))
                status = json.loads((output / part / "capture-status.json").read_text())
                self.assertEqual(status["identity_written"], expected)
                self.assertEqual(status["identity_enqueued"], expected)
                self.assertEqual(status["identity_dropped"], 0)
                self.assertEqual(status["identity_invalid"], 1 if part == "part-1" else 0)
                self.assertEqual(status["written"], 1 if part == "part-1" else 0)
                self.assertEqual(status["io_error"], 0)
                self.assertEqual(rows[0][5], 20)
                self.assertEqual(rows[0][6][20:], bytes(172))
                if part == "part-1":
                    self.assertEqual([(row[6][5], row[6][6]) for row in rows], [(6, 0), (8, 13), (8, 4), (8, 17)])
                    self.assertEqual(rows[2][4:6], (2176, 144))
                    self.assertEqual(rows[2][1], 1)
                    self.assertEqual(rows[3][4:6], (256, 192))
                    self.assertEqual(struct.unpack_from('<I', rows[3][6], 0xac)[0], 42)
            # Exercise the actual native sidecar through the shared combat
            # pipeline, including evidence and rejected unknown spawn layout.
            from tools.combat_core.__main__ import replay
            report = replay(output / "part-1/capture.opcap")
            self.assertEqual(report["roles"]["self"]["damage_dealt"], 100)
            self.assertEqual(report["roles"]["enemy"]["damage_taken"], 100)
            self.assertEqual(report["damage_by_direction"], {"player_to_enemy": 100})
            self.assertIn("identity_layout_unrecognized", report["quality"]["flags"])
            pet = next(entity for entity in report["identity_state"]["entities"] if entity["object_id"] == 77)
            self.assertTrue(pet["ownership_active"])
            self.assertEqual(pet["owner"]["object_id"], 42)

    def test_stop_restart_and_connection_replacement(self):
        with tempfile.TemporaryDirectory(prefix="continuity-", dir=ROOT / "captures") as work:
            output = Path(work) / "run"
            run = subprocess.run([str(ROOT / "bin/ContinuityTestHost.exe"),
                                  str(ROOT / "bin/OverParseCaptureTest.dll"), str(output)],
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            first = list(records(output / "part-1/capture.opcap"))
            second = list(records(output / "part-2/capture.opcap"))
            self.assertEqual([r["damage"] for r in first], list(range(1, 11)))
            self.assertEqual([r["damage"] for r in second if r["damage"] < 900000], list(range(11, 51)))
            self.assertNotIn(777777, [r["damage"] for r in second])
            self.assertNotIn(888888, [r["damage"] for r in second])
            for part, rows in [("part-1", first), ("part-2", second)]:
                status = json.loads((output / part / "capture-status.json").read_text())
                self.assertEqual(status["written"], len(rows))
                self.assertEqual(status["enqueued"], len(rows))
                self.assertEqual(status["dropped"], 0)
                self.assertEqual(status["io_error"], 0)
                self.assertEqual(status["virtual_protect_calls"], 0)
                self.assertFalse(status["recording"])
                self.assertTrue(status["stopped"])
                self.assertEqual({r["sequence"] for r in rows}, set(range(1, len(rows) + 1)))

    def test_native_hook_and_capture(self):
        self.check_native_hook_and_capture(mapped=False)

    def test_mapped_hook_and_capture(self):
        self.check_native_hook_and_capture(mapped=True)

    def test_callback_slot_with_immutable_code(self):
        self.check_native_hook_and_capture(mapped=True, slot=True)

    def check_native_hook_and_capture(self, mapped, slot=False):
        with tempfile.TemporaryDirectory(prefix="native-capture-", dir=ROOT / "captures") as work:
            output = Path(work) / "capture"
            command = [str(ROOT / "bin/CaptureTestHost.exe"),
                       str(ROOT / "bin/OverParseCaptureTest.dll"), str(output)]
            if mapped:
                command.append("--slot" if slot else "--mapped")
            run = subprocess.run(command,
                                 capture_output=True, text=True, timeout=20)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            status = json.loads((output / "capture-status.json").read_text())
            self.assertEqual(status["mode"], "synthetic_test")
            self.assertTrue(status["stopped"])
            self.assertFalse(status["recording"])
            self.assertEqual(status["observed_callbacks"], 1025)
            self.assertEqual(status["matching_damage_packets"], 1021)
            self.assertEqual(status["invalid_damage_size"], 2)
            self.assertEqual(status["dropped"], 0)
            self.assertEqual(status["written"], 1021)
            self.assertEqual(status["io_error"], 0)
            self.assertEqual(status["hook_status_name"], "not_used" if slot else "MH_OK")
            self.assertEqual(status["hook_method"], "bound_callback_slot" if slot else "minhook_code_patch")
            self.assertEqual(status["virtual_protect_calls"], 1 if slot else 3)
            trace = status["virtual_protect_trace"]
            self.assertEqual(len(trace), 1 if slot else 3)
            self.assertEqual([t["success"] for t in trace], [False] if slot else [True, True, False])
            self.assertTrue(all(t["query_success"] for t in trace))
            if not slot:
                self.assertEqual([t["requested_protection"] for t in trace], [0x40, 0x20, 0x40])
                self.assertEqual([t["effective_protection"] for t in trace],
                                 [0x80 if mapped else 0x40, 0x20, 0x40])
                self.assertEqual(trace[0]["type"], 0x40000 if mapped else 0x20000)
                self.assertEqual(trace[0]["protection"], 0x20)  # PAGE_EXECUTE_READ
                self.assertEqual(trace[0]["old_protection"], 0x20)
                self.assertEqual(trace[1]["protection"], 0x40)
                self.assertEqual(trace[1]["old_protection"], 0x40)
            self.assertEqual(trace[-1]["state"], 0x2000)  # intentional MEM_RESERVE failure probe
            self.assertNotEqual(trace[-1]["win32_error"], 0)
            self.assertNotEqual(trace[-1]["win32_error"], 0x12345678)
            rows = list(records(output / "capture.opcap"))
            self.assertEqual(len(rows), 1021)
            self.assertEqual({r["sequence"] for r in rows}, set(range(1, 1022)))
            self.assertEqual(Counter(r["attack_id"] for r in rows),
                             {777: 20, 778: 1, 1000: 250, 1001: 250, 1002: 250, 1003: 250})
            self.assertEqual([r["damage"] for r in rows[:21]], [12345] * 20 + [-234])
            for attack in range(1000, 1004):
                self.assertEqual(sorted(r["damage"] for r in rows if r["attack_id"] == attack),
                                 list(range(1, 251)))
            self.assertTrue(all(len(bytes.fromhex(r["packet_hex"])) == 84 for r in rows))
            self.assertTrue(all(not r["field_meanings_live_verified"] for r in rows))

    def test_truncated_record_and_bad_header_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "captures") as work:
            path = Path(work) / "bad.opcap"
            path.write_bytes(HEADER.pack(b"OPCAP01\0", 1, RECORD.size, 10000000, 0) + b"partial")
            with self.assertRaisesRegex(ValueError, "truncated"):
                list(records(path))
            path.write_bytes(HEADER.pack(b"badmagic", 1, RECORD.size, 10000000, 0))
            with path.open("rb") as stream, self.assertRaisesRegex(ValueError, "unsupported"):
                read_header(stream)

    def test_unrelated_header_is_rejected(self):
        packet = bytearray(84)
        packet[0] = 84
        packet[5] = 4
        packet[6] = 0x52
        packet[7] = 1
        with self.assertRaises(ValueError):
            decode_record(RECORD.pack(1, 0, 1, packet), 1000)


if __name__ == "__main__":
    (ROOT / "captures").mkdir(exist_ok=True)
    unittest.main()
