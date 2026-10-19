from pathlib import Path
import json
import os
import struct
import subprocess
import tempfile
import unittest

from .metadata import IdentityEvent
from .quest_probe import inspect_notice, survey
from .__main__ import replay

ROOT = Path(__file__).resolve().parents[1] / 'native_capture'
HOST = ROOT / 'bin/ObserverTestHost.exe'


class QuestProbeTests(unittest.TestCase):
    def test_candidate_has_no_automatic_reset_authority(self):
        packet = bytearray(128)
        struct.pack_into('<I4B', packet, 0, 128, 0, 3, 0x24, 0)
        struct.pack_into('<IIHH', packet, 96, 7007, 0, 48, 8)
        row = inspect_notice(IdentityEvent(1, 0, 1, 1, 1000, 128, bytes(packet)))
        self.assertEqual(row['quest_object_candidate']['object_id'], 7007)
        self.assertFalse(row['safe_for_automatic_reset'])
        self.assertFalse(row['interpretation_live_verified'])

    def test_short_quest_prefix_and_map_transfer_are_not_invented_boundaries(self):
        for major, minor, expected in [(3, 0, False), (0xb, 0x14, True), (0xb, 0x10, True)]:
            packet = struct.pack('<I4B', 84, 0, major, minor, 0)
            row = inspect_notice(IdentityEvent(1, 0, 1, 1, 1000, 84, packet))
            self.assertEqual(row is not None, expected)
            if row:
                self.assertIsNone(row['quest_object_candidate'])

    def test_world_and_party_candidates_keep_repeated_quest_distinct_without_reset(self):
        rows = []
        for world_id in (32659, 32702):
            packet = bytearray(128)
            struct.pack_into('<I4B', packet, 0, 2656, 0, 3, 0x24, 0)
            struct.pack_into('<IIHH', packet, 8, 125635, 0, 5, 0)
            struct.pack_into('<IIHH', packet, 84, world_id, 0, 16, 0)
            struct.pack_into('<IIHH', packet, 96, 475, 0, 11, 0)
            struct.pack_into('<IIHH', packet, 108, 60782, 0, 13, 4103)
            row = inspect_notice(IdentityEvent(1, 0, 1, 1, 1000, 2656, bytes(packet)))
            rows.append(row)
            self.assertFalse(row['safe_for_automatic_reset'])
            self.assertEqual(row['initial_map_object_candidate']['object_id'], 125635)
            self.assertEqual(row['party_object_candidate']['map_id'], 4103)
        self.assertEqual(rows[0]['quest_object_candidate'], rows[1]['quest_object_candidate'])
        self.assertNotEqual(rows[0]['world_object_candidate'], rows[1]['world_object_candidate'])
        # A truncated diagnostic prefix must not be read as a complete ObjectHeader.
        partial = inspect_notice(IdentityEvent(1, 0, 1, 1, 1000, 2656, bytes(packet[:119])))
        self.assertIsNone(partial['party_object_candidate'])
        self.assertIsNotNone(partial['quest_object_candidate'])

    @unittest.skipUnless(os.name == 'nt' and HOST.exists(), 'native synthetic capture fixture required')
    def test_real_capture_bounds_quest_headers_and_keeps_chat_out(self):
        with tempfile.TemporaryDirectory() as work:
            run = Path(work) / 'run-fixture'
            result = subprocess.run([str(HOST), str(ROOT / 'bin/OverParseCaptureTest.dll'),
                                     str(run), '--quest-notices'], capture_output=True, text=True, timeout=40,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            data = survey(run / 'identity-context/identity.opmeta')
            self.assertEqual([v['packet_id'] for v in data['notices']], ['03:24', '0b:14', '0b:10'])
            self.assertEqual([v['stored_size'] for v in data['notices']], [128, 8, 8])
            self.assertEqual(data['notices'][0]['quest_object_candidate']['object_id'], 7007)
            first = replay(run / 'segment-000001/capture.opcap')
            self.assertEqual(first['roles']['self']['damage_dealt'], 100)
            self.assertEqual(first['counts']['records'], 1)
            self.assertFalse(data['automatic_reset_enabled'])


if __name__ == '__main__':
    unittest.main()
