"""Read-only PE inventory for the installed PSO2 binaries."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "vendor"))
import pefile


def inspect(path: Path) -> dict:
    pe = pefile.PE(str(path), fast_load=True)
    pe.parse_data_directories(
        directories=[
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"],
        ]
    )
    imports = {}
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        dll = entry.dll.decode("ascii", "replace")
        imports[dll] = [
            item.name.decode("ascii", "replace") if item.name else f"#{item.ordinal}"
            for item in entry.imports
        ]
    exports = [
        item.name.decode("ascii", "replace") if item.name else f"#{item.ordinal}"
        for item in getattr(getattr(pe, "DIRECTORY_ENTRY_EXPORT", None), "symbols", [])
    ]
    delay_imports = {}
    for entry in getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", []):
        delay_imports[entry.dll.decode("ascii", "replace")] = [
            item.name.decode("ascii", "replace") if item.name else f"#{item.ordinal}"
            for item in entry.imports
        ]
    with path.open("rb") as stream:
        sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "path": str(path),
        "sha256": sha256,
        "machine": f"0x{pe.FILE_HEADER.Machine:04x}",
        "image_base": f"0x{pe.OPTIONAL_HEADER.ImageBase:x}",
        "entry_point_rva": f"0x{pe.OPTIONAL_HEADER.AddressOfEntryPoint:x}",
        "sections": [
            {
                "name": sec.Name.rstrip(b"\0").decode("ascii", "replace"),
                "rva": f"0x{sec.VirtualAddress:x}",
                "virtual_size": sec.Misc_VirtualSize,
                "raw_size": sec.SizeOfRawData,
            }
            for sec in pe.sections
        ],
        "imports": imports,
        "delay_imports": delay_imports,
        "exports": exports,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    json.dump([inspect(path) for path in args.paths], sys.stdout, ensure_ascii=False, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
