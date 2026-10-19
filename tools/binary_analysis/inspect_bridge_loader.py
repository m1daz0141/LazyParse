"""Inspect the matching bridge DLL's Epic delay-load path, without executing it."""
import argparse
import json
from pathlib import Path
import struct

from expand_classic_for_analysis import (
    decrypt_cbc, expand_lzma, expand_tara, pefile, section_public_key, sha256, u32,
)
from map_receive_static import StaticImage

SUPPORTED_BRIDGE_SHA256 = "d20e83107079a4d65d490e27d272fd1e195a22235f5ee860cc9f9e1160e6cf9c"


def inspect(path, output):
    data = path.read_bytes()
    if sha256(data) != SUPPORTED_BRIDGE_SHA256:
        raise ValueError("unsupported bridge hash")
    if output.resolve().is_relative_to(path.resolve().parent):
        raise ValueError("output must be outside the game directory")
    pe = pefile.PE(data=data)
    config_rva = pe.sections[-1].VirtualAddress
    config = pe.get_data(config_rva, 0x1F000)
    metadata = decrypt_cbc(config[0x1EA34:0x1EAA8], config[0x1EA14:0x1EA24], config[0x1EA24:0x1EA34])
    compressed_rva, compressed_size = struct.unpack_from("<II", metadata)
    loader = expand_lzma(metadata[0x10:0x15], u32(metadata, 0x18),
                         pe.get_data(compressed_rva, compressed_size))
    if sha256(loader) != "2d4a26e56ec3f941cf5d20dae7434a06d71c0aef579ed3bd123940a65724fc59":
        raise ValueError("unexpected bridge storage loader")
    n, e = section_public_key(loader)
    text = next(s for s in pe.sections if s.Name.rstrip(b"\0") == b".text")
    expanded, text_info = expand_tara(text.get_data(), n, e)
    if sha256(expanded) != "0d1e878cce4d1db764d025f18888172dbee9b63a2d642f17bfd1e64f7152cde3":
        raise ValueError("unexpected expanded bridge text")
    image = StaticImage(path, sections={".text": expanded})
    delays = []
    for entry in pe.DIRECTORY_ENTRY_DELAY_IMPORT:
        delays.append({"dll": entry.dll.decode(), "functions": [
            {"name": i.name.decode() if i.name else f"#{i.ordinal}",
             "iat_rva": hex(i.address - pe.OPTIONAL_HEADER.ImageBase)} for i in entry.imports]})
    exports = []
    for symbol in pe.DIRECTORY_ENTRY_EXPORT.symbols:
        name = symbol.name.decode() if symbol.name else ""
        if any(s in name for s in ("Initialize@BridgeEpic", "IsEnable@BridgeEpic", "LoadEdition@",
                                    "??0BridgeInitializer", "InitializeXLiveSystem@")):
            exports.append({"name": name, "rva": hex(symbol.address),
                            "instructions": image.disassemble(symbol.address, 0x200)})
    report = {"mode": "offline_static_analysis", "source": str(path), "sha256": sha256(data),
              "storage_loader_sha256": sha256(loader), "text": text_info,
              "delay_imports": delays, "selected_exports": exports}
    output.mkdir(parents=True, exist_ok=True)
    (output / "bridge-text.bin").write_bytes(expanded)
    (output / "bridge-loader-map.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bridge", type=Path)
    parser.add_argument("--output-directory", type=Path, default=Path(__file__).parent / "scratch/bridge")
    args = parser.parse_args()
    try:
        result = inspect(args.bridge, args.output_directory)
    except Exception as exc:
        parser.exit(2, f"Bridge inspection failed: {exc}\n")
    print(json.dumps({k: v for k, v in result.items() if k != "selected_exports"}, indent=2))


if __name__ == "__main__":
    main()
