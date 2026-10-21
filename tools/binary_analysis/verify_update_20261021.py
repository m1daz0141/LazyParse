"""Reproduce the 2026-10-21 profile checks using on-disk files only.

Does not load game DLLs, open a process, install a loader, or contact a server.
Expanded section files must come from expand_classic_for_analysis.expand with
the exact source hash in the profile (expected_sha256 keyword argument).
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from inspect_pe import inspect
from map_receive_static import StaticImage
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86_const import X86_OP_MEM, X86_REG_RIP

ROOT = Path(__file__).resolve().parent
PROFILE = ROOT.parent / 'native_capture/profiles/classic-2026-10-21.json'


def verify(game, expanded):
    profile = json.loads(PROFILE.read_text(encoding='utf-8'))
    layout = {k: int(v, 0) for k, v in profile['layout'].items()}
    evidence = []
    images = {}
    for name, filename in [('classic', 'expanded-0x600.bin'), ('bridge', 'expanded-0x400.bin')]:
        text = (expanded / ('pso2' + name) / filename).read_bytes()
        if hashlib.sha256(text).hexdigest() != profile['expanded_' + name + '_text_sha256']:
            raise ValueError(f'wrong expanded {name} section')
        image = StaticImage(game / ('pso2' + name + '.dll'), {'.text': text})
        if hashlib.sha256(image.data).hexdigest() != profile[name + '_sha256']:
            raise ValueError(f'wrong {name} source DLL')
        if image.pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError('not x64')
        images[name] = image
    classic, bridge = images['classic'], images['bridge']

    def instruction(image, rva, expected):
        row = image.disassemble(rva, 15)[0]
        if row['instruction'] != expected:
            raise ValueError(f'instruction differs at {rva:#x}: {row}')
        evidence.append({'module': image.path.name, **row})

    # Instruction boundaries were found from constructors/registered methods,
    # rather than applying a uniform RVA displacement to the old version.
    for rva, expected in {
        0xc5148e: 'mov qword ptr [rip + 0x3097203], rsi',
        0xc5149c: 'lea rax, [rip + 0x295123d]',
        0xc514a3: 'mov qword ptr [rsi], rax',
        0xc51ccd: 'lea rcx, [r14 + 0x48]',
        0xc51cd6: 'lea rax, [rip + 0x29508e3]',
        0xc51ce8: 'mov qword ptr [r14], rax',
        0xc51cf2: 'lea rax, [rip + 0x2950947]',
        0xc51cf9: 'mov qword ptr [r14 + 0x48], rax',
        0xc582a8: 'mov rcx, qword ptr [rdi + 0x1498]',
        0xc582f9: 'call 0x180c51cb0',
        0xc582fe: 'mov qword ptr [rdi + 0x1498], rax',
        0xc58317: 'lea rax, [rip + 0x9ab2]',
        0xc5831e: 'mov qword ptr [rsp + 0x40], rdi',
        0xc5837c: 'lea rax, [rip + 0x286666d]',
        0xc58389: 'vmovups ymmword ptr [rbx + 0x10], ymm0',
        0xc583a7: 'add rax, 0x101d8',
        0xc583c7: 'mov qword ptr [rax], rbx',
        0x3111c7d: 'mov rcx, qword ptr [rbx + 0x10190]',
        0x3111c8f: 'call qword ptr [r8 + 8]',
        0xc61beb: 'movzx edx, word ptr [rdx + 6]',
        0xc61bef: 'movzx ecx, byte ptr [rbp + 5]',
        0x488ee9: 'cmp ax, 0x52',
        0x488ef9: 'sub eax, dword ptr [r8 + 0x30]',
    }.items():
        instruction(classic, rva, expected)

    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True

    def rip_target(image, rva, expected):
        decoded = next(decoder.disasm(image.read_rva(rva, 15), image.base + rva))
        targets = [decoded.address + decoded.size + operand.mem.disp - image.base
                   for operand in decoded.operands
                   if operand.type == X86_OP_MEM and operand.mem.base == X86_REG_RIP]
        if targets != [expected]:
            raise ValueError(f'profile reference differs at {rva:#x}: {targets}')

    for rva, field in [(0xc5148e, 'client_global_rva'), (0xc5149c, 'client_vtable_rva'),
                       (0xc51cd6, 'network_vtable_rva'), (0xc51cf2, 'socket_vtable_rva'),
                       (0xc58317, 'callback_rva'), (0xc5837c, 'listener_vtable_rva')]:
        rip_target(classic, rva, layout[field])
    callback = bytes.fromhex('33c0488981cc140000440fb642020fb70241c1e010440bc0440181bc140000e9dcfdffff')
    adapter = bytes.fromhex('488bc1486349184803482048ff6010')
    for field, expected in [('callback_rva', callback), ('listener_adapter_rva', adapter)]:
        if classic.read_rva(layout[field], len(expected)) != expected:
            raise ValueError(f'{field} body differs')
    actual_adapter = struct.unpack('<Q', classic.read_rva(layout['listener_vtable_rva'] + 8, 8))[0] - classic.base
    if actual_adapter != layout['listener_adapter_rva']:
        raise ValueError('listener vtable adapter differs')
    if layout['required_image_size'] != layout['client_global_rva'] + 8:
        raise ValueError('image size guard no longer covers the global pointer')

    # Compare packet definition headers only; serializer function addresses move.
    old = json.loads((ROOT / 'identity-packets-2026-10-18.json').read_text(encoding='utf-8'))
    candidates = [(int(c['packet_id'][:2], 16), int(c['packet_id'][3:], 16),
                   c['kind'], c['flags'], c['definition_size']) for c in old['candidates']]
    candidates.append((4, 0x52, 3, 0x40, 84))
    descriptors = []
    for major, minor, kind, flags, size in candidates:
        group = struct.unpack('<Q', classic.read_rva(0x3cc45b0 + major * 16, 8))[0] - classic.base
        rva = group + minor * 0x78
        header = struct.unpack('<HHI', classic.read_rva(rva, 8))
        if header != (kind, flags, size):
            raise ValueError(f'packet {major:02x}:{minor:02x} descriptor differs: {header}')
        if (major, minor) == (4, 0x52) and rva != layout['damage_descriptor_rva']:
            raise ValueError('profile damage descriptor address differs')
        descriptors.append({'id': f'{major:02x}:{minor:02x}', 'rva': hex(rva),
                            'kind': kind, 'flags': flags, 'definition_size': size})

    for rva, expected in {
        0x10fb1: 'je 0x180010fce',
        0x10fb6: 'lea rcx, [rip + 0x563a5b]',
        0x10fbf: 'call qword ptr [rip + 0x56211b]',
        0x10fdd: 'test rax, rax',
        0x11017: 'call qword ptr [rip + 0x58bb0b]',
    }.items():
        instruction(bridge, rva, expected)
    rip_target(bridge, 0x10fb6, 0x574a18)
    rip_target(bridge, 0x11017, 0x59cb28)
    name = 'eos_gaming.dll\0'.encode('utf-16le')
    if bridge.read_rva(0x574a18, len(name)) != name:
        raise ValueError('bridge loader name differs')
    inventory = inspect(game / 'pso2bridge.dll')
    imports = inventory['delay_imports'].get('eos_gaming.dll', [])
    if len(imports) != 7 or '?EosInitialize@@YAJXZ' not in imports:
        raise ValueError('bridge delay import interface differs')
    bridge.pe.parse_data_directories(directories=[13])
    initialize_slot = [item.address - bridge.base
                       for entry in bridge.pe.DIRECTORY_ENTRY_DELAY_IMPORT
                       if entry.dll == b'eos_gaming.dll'
                       for item in entry.imports if item.name == b'?EosInitialize@@YAJXZ']
    if initialize_slot != [0x59cb28]:
        raise ValueError('EosInitialize IAT target differs')
    return {'profile': profile, 'mode': 'offline_static_analysis',
            'live_verified': False, 'packet_body_live_verified': False,
            'checked_instructions': evidence, 'packet_descriptors': descriptors,
            'bridge_delay_imports': imports, 'bridge_export_count': len(inventory['exports'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('game_directory', type=Path)
    parser.add_argument('--expanded', type=Path, default=ROOT / 'scratch/update-2026-10-21')
    parser.add_argument('--output', type=Path, default=ROOT / 'scratch/update-2026-10-21/profile-verification.json')
    args = parser.parse_args()
    result = verify(args.game_directory, args.expanded)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"PASS: {len(result['checked_instructions'])} instructions, {len(result['packet_descriptors'])} packet descriptors; {args.output}")


if __name__ == '__main__':
    main()
