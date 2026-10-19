"""Read candidate identity-packet descriptors from the supported on-disk Classic DLL.

Descriptor existence/size does not validate historical body fields or traffic.
Does not load a DLL, connect to a process, or change game files.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from expand_classic_for_analysis import SUPPORTED_SHA256
from map_receive_static import StaticImage


CANDIDATES = [
    (6, 0, "local_player_id_candidate"),
    (8, 4, "character_spawn_candidate"),
    (8, 0xC, "npc_spawn_candidate"),
    (8, 0xD, "enemy_spawn_candidate"),
    (8, 0x10, "vehicle_owner_historical_candidate"),
    (8, 0x11, "pet_owner_historical_candidate"),
    (0xF, 0xD, "inventory_self_id_historical_candidate"),
    (4, 0x15, "object_action_owner_historical_candidate"),
    (4, 6, "object_despawn_candidate"),
    (4, 0x3B, "player_despawn_candidate"),
    (3, 0, "map_transfer_candidate"),
]


def inspect(path):
    image = StaticImage(path)
    if hashlib.sha256(image.data).hexdigest() != SUPPORTED_SHA256:
        raise ValueError("unsupported Classic DLL hash")
    rows = []
    for major, minor, purpose in CANDIDATES:
        group_va = struct.unpack("<Q", image.read_rva(0x3CC6C20 + major * 16, 8))[0]
        descriptor_rva = group_va - image.base + minor * 0x78
        data = image.read_rva(descriptor_rva, 0x78)
        kind, flags, size = struct.unpack_from("<HHI", data)
        rows.append({"packet_id": f"{major:02x}:{minor:02x}", "purpose": purpose,
            "descriptor_rva": hex(descriptor_rva), "kind": kind, "flags": flags,
            "definition_size": size, "descriptor_hex": data.hex(),
            "current_body_meaning_verified": False, "live_seen": False})
    return {"source_sha256": SUPPORTED_SHA256, "mode": "offline_static_analysis",
            "note": "Candidate labels come from historical/public definitions; descriptors alone do not verify those labels or body fields.",
            "candidates": rows}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = inspect(args.image)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text, end="")
