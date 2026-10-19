"""Reproduce selected static findings for the investigated Classic DLL.

Checks file/section hashes, decoded instructions, and packet-table entries.
No process access, DLL loading, or network traffic is involved.
"""

import argparse
import hashlib
import json
from pathlib import Path
import struct

from expand_classic_for_analysis import SUPPORTED_SHA256
from map_receive_static import StaticImage


TEXT_SHA256 = "8586541c589a63a0575fc6e3353015b5d4c44b2371597e99c65e59365fbf8c9b"


def verify(image_path, text_path):
    text = text_path.read_bytes()
    if hashlib.sha256(text).hexdigest() != TEXT_SHA256:
        raise ValueError("expanded text section does not match the investigated build")
    image = StaticImage(image_path, {".text": text})
    if hashlib.sha256(image.data).hexdigest() != SUPPORTED_SHA256:
        raise ValueError("unsupported source DLL hash")

    checks = {
        0x305D931: "call 0x183111630",
        0x305D949: "call qword ptr [rax + 0x68]",
        0x3114246: "call 0x18315e900",
        0x3114052: "call 0x183160100",
        0x311407C: "mov rdx, rsi",
        0x311407F: "call qword ptr [r8 + 8]",
        0xC5A637: "lea rax, [rip + 0x9ab2]",
        0x1770EB: "jmp qword ptr [rax + 0x10]",
        0xC6410F: "jmp 0x180c63ef0",
        0xC63F0B: "movzx edx, word ptr [rdx + 6]",
        0xC63F0F: "movzx ecx, byte ptr [rbp + 5]",
        0x303FBD0: "movzx eax, byte ptr [rcx + 4]",
        0x7ACDB9: "add rcx, 0x14",
        0x48B6D9: "cmp ax, 0x52",
        0x48B6E9: "sub eax, dword ptr [r8 + 0x30]",
    }
    evidence = []
    for rva, expected in checks.items():
        instruction = image.disassemble(rva, 15)[0]
        if instruction["instruction"] != expected:
            raise ValueError(f"instruction differs at {rva:#x}: {instruction}")
        evidence.append(instruction)

    # The game registers a table of type groups, then indexes 0x78-byte
    # descriptors by subtype (verified at RVAs 0x30403f0 and 0x3040360).
    descriptor_groups_rva = 0x3CC6C20
    group4_rva = struct.unpack("<Q", image.read_rva(descriptor_groups_rva + 4 * 16, 8))[0] - image.base
    descriptor_rva = group4_rva + 0x52 * 0x78
    kind, flags, size = struct.unpack("<HHI", image.read_rva(descriptor_rva, 8))
    if (kind, flags, size) != (3, 0x40, 84):
        raise ValueError("damage descriptor differs")
    major, _, group_handler = struct.unpack("<iiQ", image.read_rva(0x355BA80, 16))
    if (major, group_handler - image.base) != (4, 0x7ACDB0):
        raise ValueError("object packet handler table differs")
    listener_method = struct.unpack("<Q", image.read_rva(0x34C19F0 + 8, 8))[0] - image.base
    if listener_method != 0x1770E0:
        raise ValueError("packet listener callback differs")
    decrypt_root = image.unwind_chain(0x3111B24)[-1][0]
    if decrypt_root != 0x3111630:
        raise ValueError("decrypt error string does not lead to the expected unwind root")

    return {
        "source_path": str(image_path), "source_sha256": SUPPORTED_SHA256,
        "expanded_text_sha256": TEXT_SHA256, "mode": "offline_static_analysis",
        "live_capture_tested": False, "hook_compatibility_tested": False,
        "receive_path_rvas": {
            "receive_completion": "0x305d730", "aes_ext_decrypt": hex(decrypt_root),
            "socket_accept_plaintext": "0x31141f0", "command_buffer_ingest": "0x315e900",
            "socket_dispatch_queued_packets": "0x3113fa0", "bound_packet_callback": "0xc640f0",
            "packet_dispatcher": "0xc63ef0",
        },
        "packet_callback_arguments": {"RCX": "bound client object", "RDX": "complete packet pointer"},
        "header": {"length_offset": 0, "length_bits": 24, "byte_3": "zero in accepted normal frames",
                   "flags_offset": 4, "type_offset": 5, "subtype_offset": 6,
                   "subtype_width": 2, "byte_order": "little", "header_size": 8},
        "damage_descriptor": {"id": "04:52", "group_rva": hex(group4_rva),
                              "descriptor_rva": hex(descriptor_rva), "kind": kind,
                              "flags": flags, "definition_size": size},
        "body_evidence": {
            "target_object_header_offset": {"offset": 0x14, "evidence": "type 4 routing passes packet+0x14 to object lookup"},
            "damage_offset": {"offset": 0x30, "evidence": "subtype 0x52 branch subtracts this 32-bit value from object state; signed interpretation follows historical definitions"},
            "receiver_source_attack_id_meanings": "historical definitions; not live validated",
            "byte_0x44_flags": "unverified",
        },
        "checked_instructions": evidence,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--text", type=Path, default=Path(__file__).parent / "scratch" / "expanded-0x600.bin")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.image, args.text)
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Verified {len(result['checked_instructions'])} instruction sites and packet-table entries; wrote {args.output}")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
