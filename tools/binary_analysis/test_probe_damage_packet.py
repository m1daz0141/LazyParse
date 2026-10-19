"""Synthetic format checks; these fixtures are not captured game traffic."""

import struct
import unittest

if __package__:
    from .probe_damage_packet import decode
else:
    from probe_damage_packet import decode


def packet(layout="current", size=None):
    size = size or (84 if layout == "current" else 80)
    data = bytearray(size)
    struct.pack_into("<I", data, 0, size)
    if layout == "current":
        struct.pack_into("<BBH", data, 4, 0x40, 4, 0x52)
    else:
        data[4:8] = bytes((4, 0x52, 0x40, 0))
    struct.pack_into("<I", data, 0x08, 10000001)
    struct.pack_into("<I", data, 0x14, 20000002)
    struct.pack_into("<I", data, 0x20, 10000003)
    struct.pack_into("<Ii", data, 0x2C, 123, 12345)
    return data


class DamagePacketChecks(unittest.TestCase):
    def test_current_header_and_damage(self):
        result = decode(bytes(packet()))
        self.assertEqual((result["header_layout"], result["header_flags"], result["damage"]),
                         ("current", 0x40, 12345))
        self.assertTrue(result["matches_known_definition_size"])

    def test_legacy_input_remains_readable(self):
        result = decode(bytes(packet("legacy")))
        self.assertEqual((result["header_layout"], result["inflicter_id"]), ("legacy", 10000003))

    def test_signed_damage_is_preserved(self):
        data = packet()
        struct.pack_into("<i", data, 0x30, -123)
        self.assertEqual(decode(bytes(data))["damage"], -123)

    def test_wrong_layout_and_wrong_subtype_fail(self):
        with self.assertRaises(ValueError):
            decode(bytes(packet()), "legacy")
        data = packet()
        data[7] = 1  # 0x0152 must not be accepted as subtype 0x0052.
        with self.assertRaises(ValueError):
            decode(bytes(data))

    def test_invalid_frames_fail(self):
        for value in (bytes(packet())[:-1], bytes(packet(size=85)), bytes(8)):
            with self.subTest(length=len(value)), self.assertRaises(ValueError):
                decode(value)

    def test_unexpected_size_is_explicit(self):
        result = decode(bytes(packet(size=88)))
        self.assertFalse(result["matches_known_definition_size"])


if __name__ == "__main__":
    unittest.main()
