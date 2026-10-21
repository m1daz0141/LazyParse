"""Expand this investigated PSO2 Classic build into data files for static analysis.

Never loads or executes game code. Does not create a launchable replacement DLL.
The profile is deliberately restricted to the exact file investigated here.
"""

import argparse
import hashlib
import json
import lzma
from pathlib import Path
import struct
import sys

sys.path.insert(0, str(Path(__file__).parent / "vendor"))
import pefile
from Crypto.Cipher import AES
from capstone import Cs, CS_ARCH_X86, CS_MODE_64
from capstone.x86 import X86_OP_MEM, X86_OP_IMM, X86_REG_RBP


SUPPORTED_SHA256 = "b2a87ec9000caa7667677d22f51859c449f4bb686f10b876370be7b861243332"


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def decrypt_cbc(data, key, iv):
    # The investigated loader decrypts complete blocks and leaves a short tail.
    count = len(data) & ~15
    return AES.new(key, AES.MODE_CBC, iv).decrypt(data[:count]) + data[count:]


def expand_lzma(properties, size, compressed):
    if size > 128 * 1024 * 1024:
        raise ValueError("unexpected expanded size")
    result = lzma.decompress(properties + struct.pack("<Q", size) + compressed,
                             format=lzma.FORMAT_ALONE, memlimit=64 * 1024 * 1024)
    if len(result) != size:
        raise ValueError("expanded size differs from the container")
    return result


def section_public_key(loader):
    """Recover the public key assembled by MOV instructions, without execution."""
    image = pefile.PE(data=loader, fast_load=True)
    decoder = Cs(CS_ARCH_X86, CS_MODE_64)
    decoder.detail = True
    key = bytearray(256)
    assigned = set()
    # Build-specific routine: parameters for the section TARA decoder.
    for ins in decoder.disasm(image.get_data(0x155AA, 0x160), 0x155AA):
        operands = ins.operands
        if (ins.mnemonic != "mov" or len(operands) != 2
                or operands[0].type != X86_OP_MEM
                or operands[0].mem.base != X86_REG_RBP
                or operands[1].type != X86_OP_IMM):
            continue
        offset = operands[0].mem.disp + 0x80
        size = operands[0].size
        if 0 <= offset and offset + size <= 256:
            key[offset:offset + size] = (operands[1].imm % (1 << (size * 8))).to_bytes(size, "little")
            assigned.update(range(offset, offset + size))
    n = int.from_bytes(key[:128], "big")
    e = int.from_bytes(key[128:], "big")
    if len(assigned) != 256 or n.bit_length() != 1024 or e != 65537:
        raise ValueError("section public key does not match the investigated routine")
    return n, e


def expand_tara(record, modulus, exponent):
    if len(record) < 0xA0 or record[:4] != b"TARA" or u32(record, 4) != 3:
        raise ValueError("not a TARA v3 container")
    if u32(record, 0x0C) != 128:
        raise ValueError("unexpected RSA block size")
    compressed_size = u32(record, 0x10)
    expanded_size = u32(record, 0x14)
    end = 0xA0 + ((compressed_size + 15) & ~15)
    if end > len(record):
        raise ValueError("truncated TARA container")
    checksum = sum(struct.unpack(f"<{end // 4}I", record[:end])) & 0xFFFFFFFF
    if checksum != 0xFFFFFFFF:
        raise ValueError("TARA additive checksum mismatch")
    ciphertext = int.from_bytes(record[0x20:0xA0], "big")
    if ciphertext >= modulus:
        raise ValueError("RSA block exceeds modulus")
    material = pow(ciphertext, exponent, modulus).to_bytes(128, "big")
    separator = material.find(b"\0", 2)
    if material[:2] != b"\0\x02" or separator < 10:
        raise ValueError("invalid RSA padding")
    key = material[separator + 1:]
    if len(key) != 32:
        raise ValueError("unexpected AES key size")
    compressed = decrypt_cbc(record[0xA0:end], key, bytes(16))[:compressed_size]
    expanded = expand_lzma(record[0x18:0x1D], expanded_size, compressed)
    return expanded, {"compressed_size": compressed_size, "expanded_size": expanded_size,
                      "checksum_valid": True, "rsa_padding_valid": True, "sha256": sha256(expanded)}


def expand(path, output_directory, *, expected_sha256=SUPPORTED_SHA256):
    data = path.read_bytes()
    if sha256(data) != expected_sha256:
        raise ValueError("unsupported DLL hash: investigate this build before extending the profile")
    output_directory = output_directory.resolve()
    if output_directory.is_relative_to(path.resolve().parent):
        raise ValueError("analysis output must be outside the game directory")
    image = pefile.PE(data=data, fast_load=True)
    # The last PE section is the storage loader's configuration and payload.
    config_rva = image.sections[-1].VirtualAddress
    config = image.get_data(config_rva, 0x1F000)
    stage_rva, stage_size, stage_entry = struct.unpack_from("<III", config, 0xAC)
    stage = decrypt_cbc(image.get_data(config_rva + stage_rva, stage_size),
                        config[0xB8:0xC8], config[0xC8:0xD8])
    metadata = decrypt_cbc(config[0x1EA34:0x1EAA8], config[0x1EA14:0x1EA24],
                           config[0x1EA24:0x1EA34])
    compressed_rva, compressed_size = struct.unpack_from("<II", metadata)
    loader = expand_lzma(metadata[0x10:0x15], u32(metadata, 0x18),
                         image.get_data(compressed_rva, compressed_size))
    if loader[:2] != b"MZ":
        raise ValueError("expanded storage loader is not a PE image")
    n, e = section_public_key(loader)
    outputs = {"loader-stage.bin": stage, "storage-loader.bin": loader}
    containers = []
    # Two section payloads and a small original-image metadata container.
    offsets = [s.PointerToRawData for s in image.sections if s.Name.rstrip(b"\0") in (b".text", b"_RDATA")]
    tail_rva = u32(metadata, 8) + 0x3000
    offsets.append(image.get_offset_from_rva(tail_rva))
    for offset in offsets:
        expanded, info = expand_tara(data[offset:], n, e)
        filename = f"expanded-{hex(offset)}.bin"
        outputs[filename] = expanded
        containers.append({"file_offset": hex(offset), "rva": hex(image.get_rva_from_offset(offset)),
                           "output": filename, **info})
    manifest = {"source_path": str(path), "source_sha256": sha256(data),
                "config_rva": hex(config_rva), "stage_entry_offset": hex(stage_entry),
                "storage_loader_sha256": sha256(loader), "containers": containers}
    output_directory.mkdir(parents=True, exist_ok=True)
    for filename, payload in outputs.items():
        (output_directory / filename).write_bytes(payload)
    (output_directory / "expansion-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--output-directory", type=Path,
                        default=Path(__file__).parent / "scratch")
    args = parser.parse_args()
    try:
        result = expand(args.image, args.output_directory)
    except (OSError, ValueError, lzma.LZMAError) as exc:
        parser.exit(2, f"Expansion failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
