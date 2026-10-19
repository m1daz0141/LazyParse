"""Decode a single *decrypted* PSO2 Classic damage packet for format checks.

The current header and 84-byte descriptor were checked in the installed DLL.
Core body fields are compared with historical implementations; several field
meanings remain provisional. This tool does not acquire packets.
"""

import argparse
import json
import struct
from pathlib import Path


def decode(data: bytes, layout: str = "auto") -> dict:
    if len(data) < 0x50:
        raise ValueError(f"packet is too short: {len(data)} bytes (need at least 80)")
    declared_size = struct.unpack_from("<I", data, 0)[0]
    if declared_size != len(data):
        raise ValueError(f"header size {declared_size} differs from actual size {len(data)}")
    if declared_size > 0xFFFFFF or declared_size % 4:
        raise ValueError("size must fit in 24 bits and be a multiple of four")
    current_id = (data[5], struct.unpack_from("<H", data, 6)[0])
    legacy_id = (data[4], data[5])
    if layout == "auto":
        if current_id == (4, 0x52):
            layout = "current"
        elif legacy_id == (4, 0x52):
            layout = "legacy"
        else:
            raise ValueError("neither header layout contains damage packet ID (0x04, 0x52)")
    if layout not in ("current", "legacy"):
        raise ValueError(f"unknown header layout: {layout}")
    packet_id = current_id if layout == "current" else legacy_id
    if packet_id != (4, 0x52):
        raise ValueError(f"{layout} packet ID is {packet_id}, expected (0x04, 0x52)")
    expected_size = 84 if layout == "current" else 80
    return {
        "size": declared_size,
        "header_layout": layout,
        "known_definition_size": expected_size,
        "matches_known_definition_size": declared_size == expected_size,
        "packet_id": "04:52",
        "header_flags": data[4] if layout == "current" else data[6],
        "receiver_id": struct.unpack_from("<I", data, 0x08)[0],
        "target_id": struct.unpack_from("<I", data, 0x14)[0],
        "target_instance_or_map_id": struct.unpack_from("<H", data, 0x1E)[0],
        "inflicter_id": struct.unpack_from("<I", data, 0x20)[0],
        "attack_id": struct.unpack_from("<I", data, 0x2C)[0],
        "damage": struct.unpack_from("<i", data, 0x30)[0],
        "new_hp": struct.unpack_from("<I", data, 0x34)[0],
        "byte_0x44_unverified": data[0x44],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--hex", help="one complete, decrypted packet as hex")
    source.add_argument("--file", type=Path, help="one complete, decrypted packet as raw bytes")
    ap.add_argument("--layout", choices=("auto", "current", "legacy"), default="auto",
                    help="current uses flags/type/u16 subtype at +4/+5/+6; legacy uses type/subtype at +4/+5")
    args = ap.parse_args()
    try:
        data = bytes.fromhex(args.hex) if args.hex is not None else args.file.read_bytes()
        print(json.dumps(decode(data, args.layout), indent=2))
    except (OSError, ValueError) as exc:
        ap.exit(2, f"Invalid packet: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
