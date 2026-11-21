"""Catalog readers reject incomplete/ambiguous data; game fixtures are local only."""
from pathlib import Path
import struct
import unittest

from .local_text import read_text
from .packets import HEADER, RECORD, join_names, parse_quests, read_catalog


def packet(minor=24, entries=((475, 20010),)):
    stride, xor, sub = (1076, 0x1db0, 0xc5) if minor == 24 else (404, 0x292c, 0x5b)
    result = bytearray(12 + stride * len(entries))
    struct.pack_into('<IBBH', result, 0, len(result), 4, 11, minor)
    struct.pack_into('<I', result, 8, (len(entries) + sub) ^ xor)
    for index, (quest, name_id) in enumerate(entries):
        struct.pack_into('<IIHHI', result, 12 + index * stride + 32, quest, 0, 11, 0, name_id)
        if minor == 24:
            result[12 + index * stride + 255] = 12
    return bytes(result)


def stream(*packets):
    result = HEADER.pack(b'QCAT0001', 1, RECORD.size, 10_000_000, 1)
    return result + b''.join(RECORD.pack(i, 1, i * 10, len(p), 0) + p for i, p in enumerate(packets, 1))


class CatalogPacketTests(unittest.TestCase):
    def test_large_list_and_duplicate_difficulty_join(self):
        entries = [(475, 20010)] + [(1100 + i, 130000 + i) for i in range(165)]
        records = read_catalog(stream(packet(entries=entries), packet(26)))
        self.assertEqual(len(records[0].packet), 178628)
        rows = join_names(records, {20010: '森林探索'})
        self.assertEqual(len(rows), 166)
        row = next(r for r in rows if r['quest_id'] == 475)
        self.assertEqual((row['name'], row['status'], row['packet_ids']), ('森林探索', 'matched', '0b:18|0b:1a'))
        self.assertEqual(row['category_id'], 12)

    def test_count_and_exact_size_required(self):
        original = packet()
        bad_count = bytearray(original)
        struct.pack_into('<I', bad_count, 8, (2 + 0xc5) ^ 0x1db0)
        for bad in (original[:-4], original + bytes(4), bytes(bad_count)):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                parse_quests(bad)

    def test_truncated_tail_only_allowed_for_live_reads(self):
        raw = stream(packet(), packet(26))
        for cut in (1, 12, 35, 400):
            with self.subTest(cut=cut):
                self.assertEqual(len(read_catalog(raw[:-cut], allow_partial=True)), 1)
                with self.assertRaises(ValueError):
                    read_catalog(raw[:-cut])

    def test_allowlist_and_size_validation(self):
        bad = bytearray(packet()); bad[5] = 7
        with self.assertRaises(ValueError):
            read_catalog(stream(bad))
        bad = bytearray(packet()); struct.pack_into('<I', bad, 0, 8)
        with self.assertRaises(ValueError):
            read_catalog(stream(bad))
        raw = stream(packet(), packet())
        offset = HEADER.size + RECORD.size + len(packet())
        raw = bytearray(raw); struct.pack_into('<Q', raw, offset, 1)
        with self.assertRaises(ValueError):
            read_catalog(bytes(raw))

    def test_conflict_and_missing_names_are_not_guessed(self):
        records = read_catalog(stream(packet(), packet(26, ((475, 90060),))))
        rows = join_names(records, {20010: '森林探索', 90060: '独極訓練：狂想と幻創'})
        self.assertEqual({r['status'] for r in rows}, {'conflicting_name_ids'})
        row, = join_names(read_catalog(stream(packet())), {})
        self.assertEqual((row['name'], row['status']), ('', 'name_not_found'))

    def test_stopper_and_empty_category(self):
        stopper = struct.pack('<IBBH', 8, 0, 11, 27)
        self.assertEqual(parse_quests(stopper), [])
        self.assertEqual(parse_quests(packet(entries=())), [])

    def test_zero_object_id_keeps_name_without_guessing_gameplay_mapping(self):
        records = read_catalog(stream(packet(entries=((0, 30010),))))
        row, = join_names(records, {30010: 'ザウーダン討伐'})
        self.assertEqual((row['quest_id'], row['name_id'], row['name'], row['status']),
                         (0, 30010, 'ザウーダン討伐', 'zero_quest_id_unverified'))
        row, = join_names(read_catalog(stream(packet(entries=((42, 0),)))), {})
        self.assertEqual(row['status'], 'missing_name_id')


class LocalTextTests(unittest.TestCase):
    def test_invalid_text_does_not_yield_names(self):
        for value in (b'', b'bad text', bytes(128)):
            with self.assertRaises(ValueError):
                read_text(value)

    def test_private_installed_titles_and_pointer_bounds(self):
        root = Path(__file__).with_name('research') / 'extracted'
        if not root.exists():
            self.skipTest('Private installed text fixtures are absent')
        titles = {}
        for path in root.rglob('quest_*.text'):
            if not path.stem[6:].isdecimal():
                continue
            for entry in read_text(path.read_bytes()):
                if entry.category == 'Basic' and entry.key == 'Title' and entry.language == 0:
                    titles[int(path.stem[6:])] = entry.text
        self.assertGreater(len(titles), 1300)
        self.assertEqual(titles[51240], '憎悪に歪む原初の闇')
        expected = {10420: '決闘', 20010: '森林探索', 90060: '独極訓練：狂想と幻創',
                    90080: '独極訓練：異界と超界', 130030: '領域調査：異世界の残滓', 130050: '最終決戦：時空の狭間'}
        for key, name in expected.items():
            self.assertEqual(titles[key], name)
        source = next(root.rglob('quest_00020010.text')).read_bytes()
        for truncated in (source[:10], source[:len(source) // 2]):
            with self.assertRaises(ValueError):
                read_text(truncated)
        bad_pointer = bytearray(source)
        nifl = struct.unpack_from('<I', source, 12)[0]
        base = nifl + struct.unpack_from('<I', source, nifl + 12)[0]
        struct.pack_into('<I', bad_pointer, base + 8, 0xffffffff)
        with self.assertRaises(ValueError):
            read_text(bytes(bad_pointer))


if __name__ == '__main__':
    unittest.main()
