"""Map receive-related strings and code references in an on-disk x64 PE.

Reads files only. RVAs describe the on-disk image; they are not live hook sites.
"""

import argparse
import bisect
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import sys

sys.path.insert(0, str(Path(__file__).parent / "vendor"))
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_64


class StaticImage:
    def __init__(self, path, sections=None):
        self.path = Path(path)
        self.data = self.path.read_bytes()
        self.pe = pefile.PE(data=self.data, fast_load=True)
        self.base = self.pe.OPTIONAL_HEADER.ImageBase
        self.overlays = sections or {}
        names = {s.Name.rstrip(b"\0").decode("ascii", "replace"): s for s in self.pe.sections}
        for name, payload in self.overlays.items():
            if name not in names or len(payload) > names[name].Misc_VirtualSize:
                raise ValueError(f"invalid section overlay: {name}")
        self.functions = []
        directory = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
        table = self.pe.get_data(directory.VirtualAddress, directory.Size)
        for offset in range(0, len(table) - 11, 12):
            begin, end, unwind = struct.unpack_from("<III", table, offset)
            if 0 < begin < end <= self.pe.OPTIONAL_HEADER.SizeOfImage:
                self.functions.append((begin, end, unwind))
        self.functions.sort()
        self.starts = [entry[0] for entry in self.functions]

    def function_at(self, rva):
        index = bisect.bisect_right(self.starts, rva) - 1
        if index >= 0 and rva < self.functions[index][1]:
            return self.functions[index]
        return None

    def unwind_chain(self, rva):
        region = self.function_at(rva)
        chain = []
        while region and region not in chain:
            chain.append(region)
            header = self.pe.get_data(region[2], 4)
            if len(header) < 4 or not (header[0] >> 3) & 4:
                break
            chained_rva = region[2] + 4 + ((header[2] + 1) & ~1) * 2
            region = struct.unpack("<III", self.pe.get_data(chained_rva, 12))
        return chain

    def rva(self, offset):
        return self.pe.get_rva_from_offset(offset)

    def read_rva(self, rva, size):
        for section in self.pe.sections:
            name = section.Name.rstrip(b"\0").decode("ascii", "replace")
            payload = self.overlays.get(name)
            offset = rva - section.VirtualAddress
            if payload is not None and 0 <= offset < len(payload):
                return payload[offset:offset + size]
        return self.pe.get_data(rva, size)

    def disassemble(self, rva, count):
        # RUNTIME_FUNCTION entries can split one routine into several unwind
        # regions. Do not silently stop a requested byte range at their edges.
        code = self.read_rva(rva, count)
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        return [
            {"rva": hex(i.address - self.base), "bytes": i.bytes.hex(),
             "instruction": f"{i.mnemonic} {i.op_str}".rstrip()}
            for i in decoder.disasm(code, self.base + rva)
        ]

    def references(self, targets):
        """Find candidate RIP-relative MOV/LEA references, then decode them.

        Candidates can be data mistaken for instructions. Function membership
        and disassembly are supplied as evidence, not proof of a code path.
        """
        references = []
        decoder = Cs(CS_ARCH_X86, CS_MODE_64)
        rip_pattern = re.compile(rb"[\x40-\x4f][\x8b\x8d][\x05\x0d\x15\x1d\x25\x2d\x35\x3d].{4}", re.S)
        for section in self.pe.sections:
            if not section.Characteristics & 0x20000000:
                continue
            name = section.Name.rstrip(b"\0").decode("ascii", "replace")
            code = self.overlays.get(name, section.get_data())
            for match in rip_pattern.finditer(code):
                rva = section.VirtualAddress + match.start()
                target = rva + 7 + struct.unpack_from("<i", match[0], 3)[0]
                if target not in targets:
                    continue
                instructions = list(decoder.disasm(match[0], self.base + rva, count=1))
                if not instructions or instructions[0].size != 7:
                    continue
                function = self.function_at(rva)
                references.append({
                    "rva": hex(rva), "target_rva": hex(target),
                    "function": [hex(v) for v in function] if function else None,
                    "unwind_chain": [[hex(v) for v in item] for item in self.unwind_chain(rva)],
                    "instruction": f"{instructions[0].mnemonic} {instructions[0].op_str}",
                })
        return references

    def report(self, terms):
        strings = []
        for term in terms:
            encoded = term.encode("ascii")
            offset = 0
            while (offset := self.data.find(encoded, offset)) >= 0:
                start = offset
                while start > 0 and 32 <= self.data[start - 1] < 127 and offset - start < 200:
                    start -= 1
                end = offset + len(encoded)
                while end < len(self.data) and 32 <= self.data[end] < 127 and end - start < 400:
                    end += 1
                strings.append({"term": term, "rva": hex(self.rva(start)),
                                "match_rva": hex(self.rva(offset)),
                                "text": self.data[start:end].decode("ascii")})
                offset += len(encoded)
        targets = {int(item[key], 16) for item in strings for key in ("rva", "match_rva")}
        sections = []
        for section in self.pe.sections:
            raw = section.get_data()
            frequencies = Counter(raw)
            entropy = -sum((n / len(raw)) * math.log2(n / len(raw)) for n in frequencies.values()) if raw else 0
            sections.append({"name": section.Name.rstrip(b"\0").decode("ascii", "replace"),
                             "rva": hex(section.VirtualAddress), "raw_size": len(raw),
                             "virtual_size": section.Misc_VirtualSize,
                             "entropy": round(entropy, 4), "first_bytes": raw[:32].hex()})
        pointers = []
        for target in sorted(targets):
            needle = struct.pack("<Q", self.base + target)
            offset = 0
            while (offset := self.data.find(needle, offset)) >= 0:
                pointers.append({"rva": hex(self.rva(offset)), "target_rva": hex(target)})
                offset += 8
        return {"path": str(self.path), "sha256": hashlib.sha256(self.data).hexdigest(),
                "image_base": hex(self.base), "entry_point_rva": hex(self.pe.OPTIONAL_HEADER.AddressOfEntryPoint),
                "section_overlays": {name: {"size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest()} for name, payload in self.overlays.items()},
                "runtime_function_count": len(self.functions), "sections": sections,
                "strings": strings, "candidate_code_references": self.references(targets),
                "absolute_pointer_references": pointers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--term", action="append")
    parser.add_argument("--section", action="append", default=[], metavar="NAME=PATH",
                        help="use an offline-expanded section for code reads")
    parser.add_argument("--rva", type=lambda value: int(value, 0))
    parser.add_argument("--bytes", type=int, default=512)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sections = {}
    for value in args.section:
        name, path = value.split("=", 1)
        sections[name] = Path(path).read_bytes()
    image = StaticImage(args.image, sections)
    if args.rva is not None:
        result = {"function": image.function_at(args.rva),
                  "disassembly": image.disassemble(args.rva, args.bytes)}
    else:
        result = image.report(args.term or ["AesExt::Decrypt", "AesExt::Encrypt",
                                           "Aqua::Network::Socket::CommandBuffer", "WSARecv"])
    output = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
