"""Read bounded PSO2 NIFL text tables without modifying client data.

Layout reference: Shadowth117/PSO2-Aqua-Library, PSO2Text.ReadNIFLFile.
This module only reads the format; it does not include upstream source code.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class TextEntry:
    category: str
    language: int
    key: str
    text: str


def read_text(data: bytes) -> list[TextEntry]:
    def uint(pos: int) -> int:
        if pos < 0 or pos + 4 > len(data):
            raise ValueError('Truncated text header')
        return struct.unpack_from('<I', data, pos)[0]

    nifl = 0 if data[:4] == b'NIFL' else uint(12)
    if data[nifl:nifl + 4] != b'NIFL':
        raise ValueError('Expected NIFL text table')
    base = nifl + uint(nifl + 12)
    limit = nifl + uint(nifl + 20)
    if data[base:base + 4] != b'REL0' or data[limit:limit + 4] != b'NOF0':
        raise ValueError('Invalid REL0/NOF0 boundaries')

    def span(offset: int, size: int) -> int:
        pos = base + offset
        if size < 0 or pos < base + 16 or pos + size > limit:
            raise ValueError('Text pointer outside REL0 data')
        return pos

    def string(offset: int, wide: bool = False) -> str:
        pos = span(offset, 2 if wide else 1)
        end = pos
        step = 2 if wide else 1
        while end + step <= limit:
            if data[end:end + step] == bytes(step):
                return data[pos:end].decode('utf-16-le' if wide else 'utf-8')
            end += step
        raise ValueError('Unterminated text')

    root = span(uint(base + 8), 8)
    categories, count = uint(root), uint(root + 4)
    if count > 4096:
        raise ValueError('Excessive text category count')
    category_table = span(categories, count * 12)
    entries = []
    for index in range(count):
        pos = category_table + index * 12
        category = string(uint(pos))
        group_count = uint(pos + 8)
        if group_count > 256:
            raise ValueError('Excessive language group count')
        groups = span(uint(pos + 4), group_count * 12)
        for group in range(group_count):
            pos = groups + group * 12
            table, language, pair_count = uint(pos), uint(pos + 4), uint(pos + 8)
            if language > 255 or pair_count > 100_000 or len(entries) + pair_count > 200_000:
                raise ValueError('Excessive text entry count')
            pairs = span(table, pair_count * 8) if pair_count else 0
            for pair in range(pair_count):
                pos = pairs + pair * 8
                entries.append(TextEntry(category, language, string(uint(pos)), string(uint(pos + 4), True)))
    return entries
