"""Recheck the build-specific bound-callback layout without loading game code."""
import hashlib
import os
import json
from pathlib import Path
import struct

from expand_classic_for_analysis import SUPPORTED_SHA256
from map_receive_static import StaticImage
from verify_classic_static import TEXT_SHA256

ROOT = Path(__file__).resolve().parent
GAME = Path(os.environ.get("PSO2_CLASSIC_DLL", "pso2classic.dll"))


def inspect():
    text = (ROOT / "scratch/expanded-0x600.bin").read_bytes()
    if hashlib.sha256(text).hexdigest() != TEXT_SHA256:
        raise ValueError("unexpected text section")
    image = StaticImage(GAME, {".text": text})
    if hashlib.sha256(image.data).hexdigest() != SUPPORTED_SHA256:
        raise ValueError("unsupported game version")
    checks = {
        0xC5381E: "mov qword ptr [rip + 0x30974d3], rsi",
        0xC5382C: "lea rax, [rip + 0x2951e4d]",
        0xC5A61E: "mov qword ptr [rdi + 0x1498], rax",
        0xC5405D: "lea rcx, [r14 + 0x48]",
        0xC54066: "lea rax, [rip + 0x29514f3]",
        0xC54082: "lea rax, [rip + 0x2951557]",
        0xC5A637: "lea rax, [rip + 0x9ab2]",
        0xC5A63E: "mov qword ptr [rsp + 0x40], rdi",
        0xC5A69C: "lea rax, [rip + 0x286734d]",
        0xC5A6A9: "vmovups ymmword ptr [rbx + 0x10], ymm0",
        0xC5A6C7: "add rax, 0x101d8",
        0xC5A6E7: "mov qword ptr [rax], rbx",
        0x311406D: "mov rcx, qword ptr [rbx + 0x10190]",
        0x311407F: "call qword ptr [r8 + 8]",
        0x1770E3: "movsxd rcx, dword ptr [rcx + 0x18]",
        0x1770E7: "add rcx, qword ptr [rax + 0x20]",
        0x1770EB: "jmp qword ptr [rax + 0x10]",
    }
    evidence = []
    for rva, expected in checks.items():
        row = image.disassemble(rva, 15)[0]
        if row["instruction"] != expected:
            raise ValueError(f"unexpected instruction at {rva:#x}: {row}")
        evidence.append(row)
    if struct.unpack("<Q", image.read_rva(0x34C19F8, 8))[0] - image.base != 0x1770E0:
        raise ValueError("unexpected listener adapter")
    return {"source_sha256": SUPPORTED_SHA256, "mode": "offline_static_analysis",
            "client_global_rva": "0x3ceacf8", "client_vtable_rva": "0x35a5680",
            "network_pointer_offset": "0x1498", "network_vtable_rva": "0x35a5560",
            "socket_subobject_offset": "0x48", "socket_vtable_rva": "0x35a55e0",
            "listener_pointer_offset_from_network": "0x101d8",
            "listener_vtable_rva": "0x34c19f0", "adapter_rva": "0x1770e0",
            "method_slot_offset": "0x10", "this_adjustment_offset": "0x18",
            "owner_offset": "0x20", "expected_method_rva": "0xc640f0",
            "live_layout_verified": False, "checked_instructions": evidence}


if __name__ == "__main__":
    output = ROOT / "binding-layout-2026-10-17.json"
    output.write_text(json.dumps(inspect(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified bound callback layout; wrote {output}")
