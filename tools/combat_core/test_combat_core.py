from dataclasses import replace
from io import StringIO
import contextlib
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tools.combat_core import CaptureDecoder, CombatEngine, IdentityRegistry, ObjectRef
from tools.combat_core.__main__ import follow, replay
from tools.combat_core.reader import HEADER, RECORD
from tools.combat_core.metadata import META_RECORD, META_RECORD_V2, read_metadata
from tools.combat_core.damage_policy import BURN_ATTACK_IDS, SOURCE_ZERO_BURN

SELF = ObjectRef(123, 4)  # Deliberately below the old 10,000,000 threshold.
OTHER = ObjectRef(456, 4)
ENEMY = ObjectRef(20000001, 6, 8)  # Deliberately above the old threshold.
PET = ObjectRef(77, 6, 8)


def packet(source=SELF, target=ENEMY, value=100, attack=99):
    data = bytearray(84)
    struct.pack_into("<I4B", data, 0, 84, 64, 4, 0x52, 0)
    for ref, offset in [(SELF, 8), (target, 0x14), (source, 0x20)]:
        struct.pack_into("<IIHH", data, offset, ref.object_id, 0, ref.entity_type, ref.map_id)
    struct.pack_into("<Ii", data, 0x2C, attack, value)
    return bytes(data)


def capture_bytes(rows):
    return HEADER.pack(b"OPCAP01\0", 1, 108, 1000, 1) + b"".join(
        RECORD.pack(sequence, 134367756000000000 + qpc * 10000, qpc, data) for sequence, qpc, data in rows)


def identity_packet(major, minor, ref):
    data = bytearray(20)
    struct.pack_into("<I4B", data, 0, 20, 0, major, minor, 0)
    struct.pack_into("<IIHH", data, 8, ref.object_id, 0, ref.entity_type, ref.map_id)
    return bytes(data)


def metadata_bytes(rows, version=1):
    record, prefix_size = (META_RECORD, 128) if version == 1 else (META_RECORD_V2, 192)
    return HEADER.pack(b"OPMETA01" if version == 1 else b"OPMETA02", version, record.size, 1000, 1) + b"".join(
        record.pack(sequence, 0, 134367756000000000 + qpc * 10000, qpc, len(data), min(len(data), prefix_size), data[:prefix_size].ljust(prefix_size, b"\0"))
        for sequence, qpc, data in rows)


def events(rows, capture_id="fixture"):
    decoder = CaptureDecoder(capture_id)
    result = decoder.feed(capture_bytes(rows))
    decoder.finish()
    return result


def registry():
    result = IdentityRegistry()
    result.set_self(SELF, "controlled_test")
    result.register(ENEMY, "enemy", "synthetic_spawn")
    return result


def engine(rows, identities=None):
    result = CombatEngine()
    result.begin_segment("fixture", identities)
    for item in events(rows):
        result.feed(item)
    return result


class AttributionTests(unittest.TestCase):
    def test_source_zero_burn_levels_excluded_but_identified_sources_and_other_attacks_stay(self):
        rows = []
        for attack in sorted(BURN_ATTACK_IDS):
            rows.append((len(rows) + 1, len(rows) * 100, packet(ObjectRef(0, 0), value=1000, attack=attack)))
            rows.append((len(rows) + 1, len(rows) * 100, packet(SELF, value=10, attack=attack)))
            rows.append((len(rows) + 1, len(rows) * 100, packet(OTHER, value=20, attack=attack)))
        rows.append((len(rows) + 1, len(rows) * 100, packet(ObjectRef(0, 0), value=7, attack=99)))
        e = CombatEngine(); e.begin_segment('fixture', registry())
        audit = [e.feed(event) for event in events(rows)]
        report = e.finish()
        self.assertEqual(report['counts']['records'], 16)
        self.assertEqual(report['counts']['positive_sum'], 5157)
        self.assertEqual(report['damage_totals']['positive_sum'], 157)
        self.assertEqual(report['damage_totals']['excluded_positive_records'], 5)
        self.assertEqual(report['damage_totals']['excluded_positive_sum'], 5000)
        self.assertEqual(report['roles']['self']['damage_dealt'], 50)
        self.assertEqual(report['roles']['other_player']['damage_dealt'], 100)
        self.assertEqual(report['roles']['unknown']['damage_dealt'], 7)
        self.assertEqual(sum(a['damage_dealt'] for a in report['attacks']), 157)
        excluded = [event for event in audit if event['exclusion_reason'] == SOURCE_ZERO_BURN]
        self.assertEqual(len(excluded), 5)
        self.assertTrue(all(not event['included_in_damage'] for event in excluded))
        self.assertTrue(all(event['packet_hex'] == rows[event['sequence'] - 1][2].hex() for event in audit))
        self.assertFalse(report['quality']['sequence_gaps'])

    def test_source_zero_burn_does_not_create_or_extend_damage_encounters(self):
        burn = min(BURN_ATTACK_IDS)
        only = engine([(1, 0, packet(ObjectRef(0, 6, 8), SELF, 1000, burn))], registry()).finish()
        self.assertFalse(only['actors'])
        self.assertFalse(only['attacks'])
        self.assertFalse(only['encounters'])
        self.assertFalse(only['timing']['spans'])
        self.assertIsNone(only['timing']['dps'])
        result = engine([(1, 0, packet(value=100)), (2, 1000, packet(value=200)),
            (3, 9000, packet(ObjectRef(0, 0), value=1000, attack=burn))], registry()).finish()
        self.assertEqual(result['roles']['self']['hits_dealt'], 2)
        self.assertEqual(result['actors'][0]['max_hit'], 200)
        self.assertEqual(result['encounters'][0]['elapsed_seconds'], 1)
        self.assertEqual(result['encounters'][0]['positive_damage'], 300)
        self.assertEqual(result['damage_by_direction'], {'player_to_enemy': 300})
        self.assertAlmostEqual(result['timing']['dps'], 300 / 9)

    def test_self_other_enemy_incoming_and_self_damage(self):
        result = engine([(1, 1000, packet()), (2, 2000, packet(OTHER, value=200)),
                         (3, 3000, packet(ENEMY, SELF, 30)), (4, 4000, packet(SELF, SELF, 4))], registry()).finish()
        self.assertEqual(result["roles"]["self"]["damage_dealt"], 104)
        self.assertEqual(result["roles"]["self"]["damage_taken"], 34)
        self.assertEqual(result["roles"]["other_player"]["damage_dealt"], 200)
        self.assertEqual(result["roles"]["enemy"]["damage_dealt"], 30)
        self.assertEqual(result["damage_by_direction"], {"player_to_enemy": 300, "enemy_to_player": 30, "self_damage": 4})

    def test_receiver_does_not_automatically_establish_self(self):
        result = engine([(1, 0, packet())]).finish()
        self.assertNotIn("self", result["roles"])
        self.assertEqual(result["roles"]["player_unresolved"]["damage_dealt"], 100)
        self.assertEqual(result["roles"]["object"]["damage_taken"], 100)
        self.assertNotIn("enemy", result["roles"])

    def test_owner_credit_preserves_raw_source_and_target(self):
        identities = registry()
        identities.register(PET, "pet", "synthetic_owner", owner=SELF)
        result = engine([(1, 0, packet(PET)), (2, 1000, packet(ENEMY, PET, 20))], identities).finish()
        self.assertEqual(result["roles"]["self"]["damage_dealt"], 100)
        self.assertEqual(result["roles"]["self"]["damage_taken"], 0)
        self.assertEqual(result["roles"]["pet"]["damage_taken"], 20)
        raw = next(row for row in result["raw_source_damage"] if row["source_key"] == PET.key)
        self.assertEqual(raw["credited_source_key"], SELF.key)

    def test_ownership_cycle_and_conflicting_types_rejected(self):
        identities = registry()
        other_object = ObjectRef(78, 6, 8)
        identities.register(PET, "pet", "fixture", owner=other_object)
        with self.assertRaises(ValueError):
            identities.register(other_object, "pet", "fixture", owner=PET)
        with self.assertRaises(ValueError):
            identities.register(SELF, "enemy", "fixture")
        self.assertEqual(identities.credited_source(PET), other_object)

    def test_map_identity_prevents_accidental_owner_reuse(self):
        identities = registry()
        identities.register(PET, "pet", "fixture", owner=SELF)
        moved = ObjectRef(PET.object_id, 6, 9)
        result = engine([(1, 0, packet(moved))], identities).finish()
        self.assertNotIn("self", result["roles"])
        self.assertEqual(result["roles"]["object"]["damage_dealt"], 100)

    def test_negative_zero_and_attack_zero_not_silently_discarded(self):
        capture = events([(1, 0, packet(value=-30)), (2, 1000, packet(value=0)),
                          (3, 2000, packet(value=5, attack=0))])
        result = CombatEngine()
        result.begin_segment("fixture", registry())
        audit = [result.feed(item) for item in capture]
        report = result.finish()
        self.assertEqual(report["counts"], {"records": 3, "positive_records": 1, "positive_sum": 5,
            "negative_records": 1, "negative_sum": -30, "zero_records": 1})
        self.assertEqual(audit[0]["value_kind"], "healing_candidate")
        self.assertFalse(audit[0]["included_in_damage"])
        self.assertEqual(report["negative_values"][0]["signed_sum"], -30)
        # Attack ID 0 is excluded by the default CSV, so it is not a warning.
        self.assertNotIn("zero_source_or_attack_id", report["quality"]["flags"])

    def test_zero_id_warning_only_for_counted_damage_dealt(self):
        def flags(*rows):
            return engine([(n, n * 1000, data) for n, data in enumerate(rows, 1)], registry()).finish()["quality"]["flags"]
        unknown = ObjectRef(0, 6, 8)
        self.assertEqual([], flags(packet(source=unknown, target=SELF, value=90)))
        self.assertEqual([], flags(packet(source=unknown, value=90, attack=min(BURN_ATTACK_IDS))))
        self.assertEqual(["zero_source_or_attack_id"], flags(packet(source=unknown, value=90)))


class NameOwnershipTests(unittest.TestCase):
    def spawn(self, minor, ref, name, owner_id=0):
        size = 2176 if minor == 4 else 256 if minor == 17 else 120 if minor == 16 else 184
        data = bytearray(size)
        struct.pack_into('<I4BIIHH', data, 0, size, 0, 8, minor, 0,
                         ref.object_id, 0, ref.entity_type, ref.map_id)
        if minor == 4:
            data[0x24:0x2e] = b'Character\0'
            encoded = name.encode('utf-16-le')
            data[0x70:0x70+len(encoded)] = encoded
        else:
            encoded = name.encode('ascii')
            data[0x24:0x24+len(encoded)] = encoded
        if minor in {16, 17}:
            struct.pack_into('<IIHH', data, 0xac if minor == 17 else 0x5c, owner_id, 0, 4, 0)
        return bytes(data)

    def replay_fixture(self, directory, identities, damage, version=2):
        capture = Path(directory) / 'capture.opcap'
        capture.write_bytes(capture_bytes(damage))
        capture.with_name('identity.opmeta').write_bytes(metadata_bytes(identities, version))
        capture.with_name('capture-status.json').write_text(json.dumps({'stopped': True, 'written': len(damage),
            'identity_written': len(identities)}))
        audit = []
        result = replay(capture, on_event=audit.append)
        with contextlib.redirect_stdout(StringIO()):
            followed = follow(capture, IdentityRegistry(), 10, lambda row: None)
        self.assertEqual(result, followed)
        return result, audit

    def test_unicode_names_pet_owner_and_healing_exclusion(self):
        long_name = 'あいうえおかきくけこさしすせそた'
        with tempfile.TemporaryDirectory() as directory:
            report, audit = self.replay_fixture(directory, [
                (1, 100, identity_packet(6, 0, SELF)),
                (2, 200, self.spawn(4, SELF, long_name)),
                (3, 300, self.spawn(13, ENEMY, 'EnemyFixture')),
                (4, 400, self.spawn(17, PET, 'PetFixture', SELF.object_id))],
                [(1, 1000, packet(PET, value=100)), (2, 2000, packet(ENEMY, PET, 20)),
                 (3, 3000, packet(SELF, SELF, -100)), (4, 4000, packet(PET, value=200))])
        self.assertEqual(audit[0]['credited_source']['name'], long_name)
        self.assertEqual(audit[0]['source']['name'], 'PetFixture')
        self.assertEqual(audit[0]['target']['name_kind'], 'internal')
        self.assertEqual(audit[0]['target']['name'], 'EnemyFixture')
        self.assertTrue(audit[0]['ownership_applied'])
        self.assertEqual(audit[0]['source']['object_id'], PET.object_id)
        self.assertEqual(report['roles']['self']['damage_dealt'], 300)
        self.assertEqual(report['roles']['self']['damage_taken'], 0)
        self.assertEqual(report['roles']['pet']['damage_taken'], 20)
        self.assertEqual(report['roles']['self']['hits_dealt'], 2)
        self.assertEqual(audit[2]['value_kind'], 'healing_candidate')
        self.assertFalse(audit[2]['included_in_damage'])
        self.assertEqual(report['aggregation_policy']['non_positive_values'], 'excluded_before_aggregation')

    def test_legacy_prefix_does_not_turn_truncated_name_into_a_name(self):
        for name, expected in [('Demo', 'Demo'), ('長い名前あいうえおかきく', None)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                report, _ = self.replay_fixture(directory, [(1, 100, identity_packet(6, 0, SELF)),
                    (2, 200, self.spawn(4, SELF, name))], [], version=1)
                self.assertEqual(report['identity_state']['self']['name'], expected)
                self.assertEqual(report['identity_metadata']['decisions'][-1]['name_status'],
                                 'complete' if expected else 'prefix_truncated')

    def test_pending_owner_and_player_id_reuse_do_not_credit_stale_owner(self):
        identities = IdentityRegistry()
        identities.register(OTHER, 'player', 'fixture', name='First')
        identities.register(PET, 'pet', 'fixture', owner=OTHER)
        self.assertEqual(identities.credited_source(PET), OTHER)
        identities.remove(OTHER)
        identities.register(OTHER, 'player', 'fixture', name='Reused')
        self.assertEqual(identities.credited_source(PET), PET)
        self.assertFalse(identities.describe(PET)['ownership_active'])
        with tempfile.TemporaryDirectory() as directory:
            report, audit = self.replay_fixture(directory, [(1, 100, self.spawn(17, PET, 'PetFixture', OTHER.object_id)),
                (2, 200, self.spawn(13, ENEMY, 'EnemyFixture')),
                (3, 400, self.spawn(4, OTHER, 'Owner'))],
                [(1, 300, packet(PET)), (2, 500, packet(PET))])
        self.assertFalse(audit[0]['ownership_applied'])
        self.assertTrue(audit[1]['ownership_applied'])
        self.assertEqual(audit[1]['credited_source']['name'], 'Owner')
        self.assertEqual(report['roles']['pet']['damage_dealt'], 100)
        self.assertEqual(report['roles']['player_unresolved']['damage_dealt'], 100)

    def test_vehicle_owner_invalid_name_and_short_pet_record(self):
        broken = bytearray(self.spawn(4, SELF, 'Name'))
        broken[0x70:0x74] = b'\x00\xd8\x00\x00'  # Unpaired UTF-16 surrogate.
        with tempfile.TemporaryDirectory() as directory:
            report, audit = self.replay_fixture(directory, [(1, 100, identity_packet(6, 0, SELF)),
                (2, 200, bytes(broken)), (3, 300, self.spawn(16, PET, 'Vehicle', SELF.object_id))],
                [(1, 400, packet(PET))])
        self.assertIsNone(audit[0]['credited_source']['name'])
        self.assertEqual(report['identity_metadata']['decisions'][1]['name_status'], 'invalid_encoding')
        self.assertTrue(audit[0]['ownership_applied'])
        with tempfile.TemporaryDirectory() as directory:
            report, audit = self.replay_fixture(directory, [(1, 100, identity_packet(6, 0, SELF)),
                (2, 200, self.spawn(17, PET, 'PetFixture', SELF.object_id))],
                [(1, 400, packet(PET))], version=1)
        self.assertFalse(audit[0]['ownership_applied'])
        self.assertIn('identity_layout_unrecognized', report['quality']['flags'])

    def test_owner_must_match_full_player_header(self):
        for entity_type, map_id in [(6, 0), (4, 99)]:
            data = bytearray(self.spawn(17, PET, 'PetFixture', SELF.object_id))
            struct.pack_into('<HH', data, 0xb4, entity_type, map_id)
            with self.subTest(entity_type=entity_type, map_id=map_id), tempfile.TemporaryDirectory() as directory:
                report, audit = self.replay_fixture(directory, [(1, 100, identity_packet(6, 0, SELF)),
                    (2, 200, bytes(data))], [(1, 300, packet(PET))])
                self.assertFalse(audit[0]['ownership_applied'])
                self.assertNotIn('self', report['roles'])


class AggregationTests(unittest.TestCase):
    def test_idle_and_healing_do_not_compress_continuous_dps_time(self):
        report = engine([(1, 0, packet()), (2, 2000, packet(value=300)),
                         (3, 9000, packet(value=-1)), (4, 12000, packet(value=500)),
                         (5, 13000, packet(value=100)), (6, 30000, packet(value=-3))], registry()).finish()
        self.assertEqual(report["timing"]["elapsed_seconds"], 30)
        self.assertEqual(report["timing"]["dps"], 1000 / 30)
        self.assertEqual([r["closed_reason"] for r in report["encounters"]], ["idle_timeout", "idle_timeout"])

    def test_idle_map_transfer_and_no_damage_time_remain_in_denominator(self):
        result = engine([(1, 1000, packet()), (2, 2000, packet(value=300))], registry())
        result.end_encounter('map_transfer')
        result.observe_time(21000, 1000)
        self.assertEqual(result.snapshot()['timing']['elapsed_seconds'], 20)
        self.assertEqual(result.snapshot()['roles']['self']['dps'], 20)
        result.feed(events([(3, 22000, packet(value=200))])[0])
        report = result.finish(31000, 1000)
        self.assertEqual(report['timing']['elapsed_seconds'], 30)
        self.assertEqual(report['roles']['self']['dps'], 20)

    def test_incoming_hit_does_not_start_first_dealt_damage_timer(self):
        result = engine([(1, 0, packet(ENEMY, SELF, 50))], registry())
        result.observe_time(1000, 1000)
        self.assertEqual(result.snapshot()['timing']['elapsed_seconds'], 0)
        result.feed(events([(2, 2000, packet())])[0])
        self.assertEqual(result.finish(6000, 1000)['roles']['self']['dps'], 25)

    def test_stop_uses_exact_boundary_and_freezes_a_single_hit(self):
        result = engine([(1, 1000, packet())], registry())
        result.observe_time(15000, 1000)
        self.assertEqual(result.snapshot()['timing']['elapsed_seconds'], 14)
        report = result.finish(3000, 1000)
        self.assertEqual(report['timing']['elapsed_seconds'], 2)
        self.assertEqual(report['roles']['self']['dps'], 50)
        result.observe_time(50000, 1000)
        self.assertEqual(result.snapshot(), report)

    def test_single_hit_and_no_hit_dps_are_null(self):
        self.assertIsNone(engine([(1, 0, packet())]).finish()["timing"]["dps"])
        report = engine([(1, 0, packet(value=-50))]).finish()
        self.assertIsNone(report["timing"]["dps"])
        self.assertEqual(report["encounters"], [])

    def test_duplicate_delivery_versus_real_multihit(self):
        capture = events([(1, 0, packet()), (2, 0, packet())])
        result = CombatEngine()
        result.begin_segment("fixture")
        self.assertIsNotNone(result.feed(capture[0]))
        self.assertIsNone(result.feed(capture[0]))
        result.feed(capture[1])
        self.assertEqual(result.snapshot()["counts"]["positive_sum"], 200)
        with self.assertRaises(ValueError):
            result.feed(replace(capture[1], value=101))
        with self.assertRaises(ValueError):
            result.feed(capture[0])

    def test_sequence_gap_splits_encounter_and_is_visible(self):
        report = engine([(1, 0, packet()), (3, 1000, packet())]).finish()
        self.assertEqual(report["quality"]["sequence_gaps"][0]["first_missing"], 2)
        self.assertEqual(len(report["encounters"]), 2)
        self.assertIn("sequence_gap", report["quality"]["flags"])

    def test_manual_boundary_new_segment_and_reset(self):
        result = engine([(1, 0, packet()), (2, 1000, packet())], registry())
        result.end_encounter()
        result.feed(events([(3, 2000, packet())])[0])
        result.begin_segment("second")
        result.feed(events([(1, 500, packet())], "second")[0])
        report = result.finish()
        self.assertEqual(len(report["encounters"]), 3)
        self.assertEqual(report["roles"]["self"]["damage_dealt"], 300)
        self.assertEqual(report["roles"]["player_unresolved"]["damage_dealt"], 100)
        result.reset()
        self.assertEqual(result.snapshot()["counts"]["records"], 0)
        self.assertEqual(result.snapshot()["actors"], [])

    def test_large_totals_and_invalid_clock(self):
        report = engine([(1, 1000, packet(value=2147483647)), (2, 2000, packet(value=2147483647))]).finish()
        self.assertEqual(report["counts"]["positive_sum"], 4294967294)
        result = engine([(1, 2000, packet())])
        with self.assertRaises(ValueError):
            result.feed(events([(2, 1000, packet())])[0])
        with self.assertRaises(ValueError):
            CombatEngine(float("nan"))


class ReaderTests(unittest.TestCase):
    def test_incremental_every_byte_equals_batch_replay(self):
        data = capture_bytes([(1, 1000, packet()), (2, 1000, packet(value=-3)), (3, 2000, packet(value=500))])
        batch = CaptureDecoder("fixture").feed(data)
        reader = CaptureDecoder("fixture")
        streamed = []
        for value in data:
            streamed.extend(reader.feed(bytes([value])))
        reader.finish()
        self.assertEqual(batch, streamed)
        reports = []
        for rows in (batch, streamed):
            result = CombatEngine()
            result.begin_segment("fixture", registry())
            for row in rows:
                result.feed(row)
            reports.append(result.finish())
        self.assertEqual(reports[0], reports[1])

    def test_truncation_and_corrupt_header_rejected(self):
        data = capture_bytes([(1, 1000, packet())])
        for length in (0, 16, len(data) - 1):
            reader = CaptureDecoder("fixture")
            reader.feed(data[:length])
            with self.assertRaises(ValueError):
                reader.finish()
        with self.assertRaises(ValueError):
            CaptureDecoder("fixture").feed(b"BADMAGIC" + data[8:])

    def test_identity_annotations_bound_to_exact_capture(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 0, packet())]))
            profile = Path(root) / "identities.json"
            profile.write_text(json.dumps({"schema_version": 1, "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
                "self": {**SELF.as_dict(), "evidence": "fixture"}}), encoding="utf-8")
            self.assertEqual(IdentityRegistry.from_file(profile, capture).self_ref, SELF)
            capture.write_bytes(capture_bytes([(1, 0, packet(value=101))]))
            with self.assertRaises(ValueError):
                IdentityRegistry.from_file(profile, capture)

    def test_saved_follow_equals_replay_including_mid_capture_gap(self):
        with tempfile.TemporaryDirectory() as root:
            segment = Path(root) / "segment-000001"
            segment.mkdir()
            capture = segment / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 0, packet()), (2, 1000, packet()), (3, 2000, packet()), (4, 3000, packet())]))
            status = {"stopped": True, "written": 4, "dropped": 0}
            (segment / "capture-status.json").write_text(json.dumps(status))
            (segment.parent / "continuity-events.jsonl").write_text(json.dumps({"segment": 1, "state": "waiting_for_binding", "filetime": 134367756015000000}) + "\n")
            expected = replay(capture, registry())
            with contextlib.redirect_stdout(StringIO()):
                actual = follow(capture, registry(), 10, lambda event: None)
            self.assertEqual(actual, expected)

    def test_growing_capture_partial_writes_use_same_pipeline(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.touch()
            status_path = capture.with_name("capture-status.json")
            status_path.write_text(json.dumps({"stopped": False, "written": 0}))
            data = capture_bytes([(1, 1000, packet()), (2, 2000, packet(value=-2)), (3, 3000, packet(value=400))])
            errors = []

            def write():
                try:
                    with capture.open("wb") as stream:
                        for start in range(0, len(data), 13):
                            stream.write(data[start:start + 13])
                            stream.flush()
                            time.sleep(0.002)
                    temp = status_path.with_suffix(".tmp")
                    temp.write_text(json.dumps({"stopped": True, "written": 3}))
                    temp.replace(status_path)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=write, daemon=True)
            thread.start()
            audit = []
            with contextlib.redirect_stdout(StringIO()):
                actual = follow(capture, registry(), 10, audit.append)
            thread.join(2)
            self.assertEqual(errors, [])
            self.assertEqual(len(audit), 3)
            self.assertEqual(actual, replay(capture, registry()))

    def test_real_cli_report_and_signed_audit(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 0, packet()), (2, 1000, packet(value=-5))]))
            audit, report = Path(root) / "audit.jsonl", Path(root) / "report.json"
            process = subprocess.run([sys.executable, "-m", "tools.combat_core", str(capture),
                "--self-id", str(SELF.object_id), "--events", str(audit), "--output", str(report)],
                cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            value = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(value["roles"]["self"]["damage_dealt"], 100)
            rows = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["signed_value"] for r in rows], [100, -5])
            self.assertEqual(rows[1]["direction"], "not_classified_as_damage")


class LiveCaptureRegressionTests(unittest.TestCase):
    def test_private_live_pet_names_owner_and_healing_exclusion(self):
        run = Path(__file__).resolve().parents[1] / "native_capture/captures/classic-auto-fixture-001/run-100000000000000001-1-0"
        if not run.exists():
            self.skipTest("private Maron capture is not present in this checkout")
        expected = {'segment-000001/capture.opcap': '79f87f5cd632c61d6d311df08372bdb29025ae77ebc1a8508ea0fa34d1724ff6',
            'segment-000001/identity.opmeta': '2e6c0b441c24e590f1ea84ef70d7ff4fbe0998505f0b17f59adcd8f1a797aee9',
            'identity-context/identity.opmeta': '6548c4b1932da09151af3bea09c99c8739090b140b41c26e21e03735435fcdd6'}
        for path, digest in expected.items():
            self.assertEqual(hashlib.sha256((run / path).read_bytes()).hexdigest(), digest)
        capture = run / 'segment-000001/capture.opcap'
        audit = []
        report = replay(capture, on_event=audit.append)
        self.assertEqual(report['counts']['records'], 15)
        self.assertEqual([event['signed_value'] for event in audit[:3]], [64, 4752, 14007])
        pet_hits = [event for event in audit if event['included_in_damage'] and event['source']['role'] == 'pet']
        self.assertEqual(len(pet_hits), 10)
        self.assertEqual(sum(event['signed_value'] for event in pet_hits), 92209)
        self.assertTrue(all(event['source']['object_id'] == 15402 and event['source']['name'] == 'PetLongearD' and
            event['ownership_applied'] and event['credited_source']['object_id'] == 10000001 and
            event['credited_source']['name'] == 'Demo' for event in pet_hits))
        self.assertEqual(report['roles']['self']['damage_dealt'], 92341)
        self.assertEqual(report['roles']['self']['damage_taken'], 0)
        self.assertEqual(report['roles']['pet']['damage_taken'], 1)
        healing = next(event for event in audit if event['signed_value'] < 0)
        self.assertEqual(healing['signed_value'], -539)
        self.assertEqual(healing['value_kind'], 'healing_candidate')
        self.assertFalse(healing['included_in_damage'])
        self.assertEqual(report['quality']['flags'], [])
        self.assertEqual(report, replay(capture, chunk_size=11))
        with contextlib.redirect_stdout(StringIO()):
            followed = follow(capture, IdentityRegistry(), 10, lambda event: None)
        self.assertEqual(report, followed)
        for path, digest in expected.items():
            self.assertEqual(hashlib.sha256((run / path).read_bytes()).hexdigest(), digest)

    def test_private_live_identity_state_before_start_and_after_pause(self):
        run = Path(__file__).resolve().parents[1] / "native_capture/captures/classic-auto-fixture-002/run-100000000000000002-1-0"
        if not run.exists():
            self.skipTest("private initial identity live capture is not present in this checkout")
        expected = {"identity-context/identity.opmeta": "84535e923d9136167d6a3574a691ce36d1dc1d762c0a7c3d8a5a8eca70e8af53",
            "segment-000001/capture.opcap": "11b278b359314c119c18fd2c2e56150a83c8714e237a6698b2d6fe56bd446e8a",
            "segment-000002/capture.opcap": "fc60d14448b4d15603d8505dc5539ddde562e99056fd2f788965692adcef1383",
            "segment-000002/identity.opmeta": "665dd13252f9b69585665eb90a815d54874d569949774ea929659ba4e111e4b3"}
        for path, digest in expected.items():
            self.assertEqual(hashlib.sha256((run / path).read_bytes()).hexdigest(), digest)
        initial = replay(run / "segment-000001/capture.opcap")
        self.assertEqual(initial["counts"]["records"], 0)
        self.assertEqual(initial["identity_state"]["self"]["object_id"], 10000001)
        audit = []
        capture = run / "segment-000002/capture.opcap"
        report = replay(capture, on_event=audit.append)
        self.assertEqual(report["counts"]["records"], 42)
        self.assertEqual(report["counts"]["negative_records"], 18)
        self.assertEqual(report["roles"]["self"]["damage_taken"], 6)
        self.assertEqual(report["damage_by_direction"]["player_to_enemy"], 170186)
        self.assertNotIn('player_to_object', report['damage_by_direction'])
        self.assertEqual(report['damage_totals']['excluded_positive_sum'], 11)
        self.assertEqual(report['damage_totals']['excluded_positive_sum'],
                         sum(e['signed_value'] for e in audit if e['attack_id'] == 0 and e['signed_value'] > 0))
        first = next(event for event in audit if event["signed_value"] > 0 and event["source"]["role"] == "self")
        self.assertEqual((first["sequence"], first["signed_value"], first["target"]["role"], first["target"]["object_id"]),
                         (3, 4281, "enemy", 303))
        spawn = [decision for decision in report["identity_metadata"]["decisions"]
                 if decision["action"] == "register_entity" and decision.get("kind") == "enemy"
                 and decision["object"]["object_id"] == 303 and decision["qpc"] <= first["qpc"]]
        self.assertLess(spawn[-1]["qpc"], report["identity_context"]["reference"]["start_qpc"])
        # Its zero-ID records are attack ID 0 (excluded), so no warning remains.
        self.assertEqual(report["quality"]["flags"], [])
        self.assertEqual(report, replay(capture, chunk_size=11))
        with contextlib.redirect_stdout(StringIO()):
            followed = follow(capture, IdentityRegistry(), 10, lambda event: None)
        self.assertEqual(report, followed)
        for path, digest in expected.items():
            self.assertEqual(hashlib.sha256((run / path).read_bytes()).hexdigest(), digest)

    def test_private_live_automatic_self_enemy_attribution(self):
        capture = Path(__file__).resolve().parents[1] / "native_capture/captures/classic-auto-fixture-003/run-100000000000000003-1-0/segment-000001/capture.opcap"
        if not capture.exists():
            self.skipTest("private identity live capture is not present in this checkout")
        expected_hashes = [(capture, "e7100fd20d74feddd1bdd63af894d359db8e688d7c2465a1278d4655716a4c87"),
            (capture.with_name("identity.opmeta"), "59f9d5d1e528a19d0820fb2c3e5178dba7315fd185bf61941ecb7a390f1e33ca")]
        for path, digest in expected_hashes:
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        report = replay(capture)  # No manual self ID or entity annotations.
        self.assertEqual(report["counts"]["records"], 22)
        self.assertEqual(report["counts"]["negative_records"], 12)
        self.assertEqual(report["roles"]["self"]["damage_dealt"], 209057)
        self.assertEqual(report["roles"]["self"]["hits_dealt"], 9)
        self.assertEqual(report["roles"]["self"]["damage_taken"], 1)
        self.assertEqual(report["roles"]["enemy"]["damage_dealt"], 1)
        self.assertEqual(report["roles"]["enemy"]["damage_taken"], 209057)
        self.assertEqual(report["damage_by_direction"], {"enemy_to_player": 1, "player_to_enemy": 209057})
        self.assertEqual(report["identity_metadata"]["packet_counts"]["06:00"], 2)
        self.assertEqual(report["identity_metadata"]["packet_counts"]["08:0d"], 9)
        self.assertEqual(report["quality"]["flags"], [])
        self.assertEqual(report, replay(capture, chunk_size=11))
        with contextlib.redirect_stdout(StringIO()):
            followed = follow(capture, IdentityRegistry(), 10, lambda event: None)
        self.assertEqual(report, followed)
        for path, digest in expected_hashes:
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_private_saved_captures(self):
        root = Path(__file__).resolve().parents[1] / "native_capture" / "captures"
        fixtures = [
            (root / "classic-auto-fixture-004" / "capture.opcap", 17, 6, 11, 257770,
             "4c2e8d27b7a74ad31cae72693e9007d3d9656bb2e21e6fdd116d93c2184cb3d3"),
            (root / "classic-auto-fixture-005" / "run-100000000000000004-1-0" / "segment-000002" / "capture.opcap",
             28, 15, 13, 272366, "3dd5db76bdfd92061ab15fbf789f97d9217e3e14f7488bc094b6f0b4b790535f")]
        if not all(path.exists() for path, *_ in fixtures):
            self.skipTest("private live captures are not present in this checkout")
        for path, count, positives, negatives, total, digest in fixtures:
            with self.subTest(path=path):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
                result = replay(path)
                self.assertEqual(result["counts"]["records"], count)
                self.assertEqual(result["counts"]["positive_records"], positives)
                self.assertEqual(result["counts"]["negative_records"], negatives)
                self.assertEqual(result["counts"]["positive_sum"], total)
                self.assertEqual(result["roles"]["player_unresolved"]["damage_dealt"], total)
                self.assertNotIn("self", result["roles"])
                self.assertEqual(result, replay(path, chunk_size=13))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


class IdentityContextTests(unittest.TestCase):
    def fixture(self, root, rows=None):
        run = Path(root) / "run-fixture"
        segment = run / "segment-000001"
        context = run / "identity-context"
        segment.mkdir(parents=True)
        context.mkdir()
        capture = segment / "capture.opcap"
        rows = [(1, 1000, packet()), (2, 2000, packet())] if rows is None else rows
        capture.write_bytes(capture_bytes(rows))
        capture.with_name("capture-status.json").write_text(json.dumps({"stopped": True, "written": len(rows), "source_sha256": "fixture"}))
        ref = {"schema_version": 1, "run_name": run.name, "pid": 1234, "source_sha256": "fixture",
            "context_start_filetime": 1, "context_path": "../identity-context/identity.opmeta",
            "qpc_frequency": 1000, "start_qpc": 900, "end_qpc": 3000}
        capture.with_name("identity-context.json").write_text(json.dumps(ref))
        context.joinpath("identity.opmeta").write_bytes(metadata_bytes([(1, 100, identity_packet(6, 0, SELF)),
            (2, 200, identity_packet(8, 13, ENEMY)), (3, 5000, identity_packet(6, 0, OTHER))]))
        status = {"schema_version": 1, "pid": 1234, "source_sha256": "fixture", "start_filetime": 1,
            "written": 3, "dropped": 0, "invalid": 0, "io_error": 0, "published_through_qpc": 6000, "stopped": True}
        context.joinpath("context-status.json").write_text(json.dumps(status))
        return capture, context, ref, status

    def test_initial_identity_applies_without_new_notifications_and_future_stays_out(self):
        with tempfile.TemporaryDirectory() as root:
            capture, _, _, _ = self.fixture(root)
            report = replay(capture)
            self.assertEqual(report["roles"]["self"]["damage_dealt"], 200)
            self.assertEqual(report["roles"]["enemy"]["damage_taken"], 200)
            self.assertEqual(report["identity_state"]["self"]["object_id"], SELF.object_id)
            self.assertEqual(report["identity_metadata"]["records_applied"], 2)
            with contextlib.redirect_stdout(StringIO()):
                actual = follow(capture, IdentityRegistry(), 10, lambda event: None)
            self.assertEqual(actual, report)

    def test_identity_is_available_before_any_damage(self):
        with tempfile.TemporaryDirectory() as root:
            capture, _, _, _ = self.fixture(root, [])
            report = replay(capture)
            self.assertEqual(report["counts"]["records"], 0)
            self.assertEqual(report["identity_state"]["self"]["object_id"], SELF.object_id)
            self.assertEqual(report["identity_state"]["entities"][0]["kind"], "enemy")

    def test_wrong_run_process_header_and_escape_rejected(self):
        for change in ({"run_name": "run-other"}, {"pid": 999}, {"context_start_filetime": 999},
                       {"context_path": "../../other/identity.opmeta"}, {"qpc_frequency": 999}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root:
                capture, _, ref, _ = self.fixture(root)
                ref.update(change)
                capture.with_name("identity-context.json").write_text(json.dumps(ref))
                with self.assertRaises(ValueError):
                    replay(capture)

    def test_delayed_context_publication_waits_in_follow(self):
        with tempfile.TemporaryDirectory() as root:
            capture, context, _, status = self.fixture(root)
            status.update(published_through_qpc=500, stopped=False)
            path = context / "context-status.json"
            path.write_text(json.dumps(status))
            with self.assertRaises(ValueError):
                replay(capture)

            def publish():
                time.sleep(0.05)
                status.update(published_through_qpc=6000, stopped=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(status))
                temp.replace(path)

            thread = threading.Thread(target=publish, daemon=True)
            thread.start()
            with contextlib.redirect_stdout(StringIO()):
                report = follow(capture, IdentityRegistry(), 10, lambda event: None)
            thread.join(2)
            self.assertEqual(report, replay(capture))


class MetadataTests(unittest.TestCase):
    def test_time_ordered_self_and_enemy_recognition_without_future_leak(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 1000, packet()), (2, 2000, packet())]))
            meta = capture.with_name("identity.opmeta")
            meta.write_bytes(metadata_bytes([(1, 500, identity_packet(6, 0, SELF)),
                (2, 1500, identity_packet(8, 13, ENEMY))]))
            audit = []
            report = replay(capture, on_event=audit.append)
            self.assertEqual(audit[0]["source"]["role"], "self")
            self.assertEqual(audit[0]["target"]["kind"], "object")
            self.assertEqual(audit[1]["target"]["kind"], "enemy")
            self.assertNotEqual(audit[0]["target"]["actor_key"], audit[1]["target"]["actor_key"])
            self.assertEqual(report["roles"]["object"]["damage_taken"], 100)
            self.assertEqual(report["roles"]["enemy"]["damage_taken"], 100)
            self.assertEqual(report["identity_metadata"]["packet_counts"], {"06:00": 1, "08:0d": 1})

    def test_map_transfer_does_not_reuse_enemy_classification(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 1000, packet()), (2, 2000, packet())]))
            capture.with_name("identity.opmeta").write_bytes(metadata_bytes([
                (1, 500, identity_packet(8, 13, ENEMY)), (2, 1500, identity_packet(3, 0, SELF))]))
            audit = []
            report = replay(capture, on_event=audit.append)
            self.assertEqual([row["target"]["kind"] for row in audit], ["enemy", "object"])
            self.assertEqual(len(report["encounters"]), 2)
            self.assertEqual(report["encounters"][0]["closed_reason"], "map_transfer")

    def test_metadata_gap_and_unsupported_layout_visible(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 1000, packet())]))
            malformed_self = bytearray(identity_packet(6, 0, SELF))
            struct.pack_into("<I", malformed_self, 0, 24)
            malformed_self += bytes(4)
            capture.with_name("identity.opmeta").write_bytes(metadata_bytes([(2, 500, bytes(malformed_self))]))
            report = replay(capture)
            self.assertIn("identity_sequence_gap", report["quality"]["flags"])
            self.assertIn("identity_layout_unrecognized", report["quality"]["flags"])
            self.assertNotIn("self", report["roles"])

    def test_metadata_follow_watermarks_and_report_match(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 1000, packet()), (2, 2000, packet())]))
            capture.with_name("identity.opmeta").write_bytes(metadata_bytes([(1, 500, identity_packet(6, 0, SELF)),
                (2, 1500, identity_packet(8, 13, ENEMY))]))
            capture.with_name("capture-status.json").write_text(json.dumps({"stopped": True, "written": 2, "identity_written": 2}))
            with contextlib.redirect_stdout(StringIO()):
                actual = follow(capture, IdentityRegistry(), 10, lambda event: None)
            self.assertEqual(actual, replay(capture))

    def test_live_damage_waits_for_identity_file_publication(self):
        with tempfile.TemporaryDirectory() as root:
            capture = Path(root) / "capture.opcap"
            capture.write_bytes(capture_bytes([(1, 1000, packet()), (2, 2000, packet())]))
            meta = capture.with_name("identity.opmeta")
            raw = metadata_bytes([(1, 100, identity_packet(6, 0, SELF)), (2, 200, identity_packet(8, 13, ENEMY))])
            meta.write_bytes(raw[:32])
            status = capture.with_name("capture-status.json")
            status.write_text(json.dumps({"stopped": False, "written": 0, "identity_written": 0}))
            errors = []

            def publish():
                try:
                    time.sleep(0.03)
                    with meta.open("ab") as stream:
                        stream.write(raw[32:])
                        stream.flush()
                    time.sleep(0.03)
                    temp = status.with_suffix(".tmp")
                    temp.write_text(json.dumps({"stopped": True, "written": 2, "identity_written": 2}))
                    temp.replace(status)
                except Exception as exc:
                    errors.append(exc)

            thread = threading.Thread(target=publish, daemon=True)
            thread.start()
            audit = []
            with contextlib.redirect_stdout(StringIO()):
                actual = follow(capture, IdentityRegistry(), 10, audit.append)
            thread.join(2)
            self.assertEqual(errors, [])
            self.assertEqual([event["source"]["role"] for event in audit], ["self", "self"])
            self.assertEqual([event["target"]["kind"] for event in audit], ["enemy", "enemy"])
            self.assertEqual(actual, replay(capture))

    def test_metadata_corruption_and_partial_record(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "identity.opmeta"
            data = metadata_bytes([(1, 0, identity_packet(6, 0, SELF))])
            path.write_bytes(data[:-1])
            self.assertEqual(read_metadata(path, allow_partial=True), [])
            with self.assertRaises(ValueError):
                read_metadata(path)
            path.write_bytes(data)
            self.assertEqual(read_metadata(path)[0].packet_id, (6, 0))


if __name__ == "__main__":
    unittest.main()
