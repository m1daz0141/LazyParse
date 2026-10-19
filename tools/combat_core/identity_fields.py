"""Bounded fields for the supported Classic layout; never scan for plausible IDs."""
import unicodedata
from .model import ObjectRef


def fixed_text(prefix, offset, byte_count, encoding):
    unit = 2 if encoding == "utf-16-le" else 1
    raw = prefix[offset:offset + byte_count]
    end = next((index for index in range(0, len(raw) - unit + 1, unit)
                if raw[index:index + unit] == bytes(unit)), None)
    if end is None:
        if len(raw) < byte_count:
            return None, "prefix_truncated"
        end = len(raw)
    try:
        name = raw[:end].decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return None, "invalid_encoding"
    if not name:
        return None, "empty"
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in name):
        return None, "control_character"
    return name, "complete"


def spawn_name(event):
    if event.packet_id == (8, 4):
        # Classic CharacterSpawn + Character.name (16 UTF-16 units).
        if event.packet_size != 2176 or event.prefix[0x24:0x2e] != b"Character\0":
            return None, None, "layout_not_supported"
        name, status = fixed_text(event.prefix, 0x70, 0x20, "utf-16-le")
        return name, "display", status
    name, status = fixed_text(event.prefix, 0x24, 0x20, "ascii")
    return name, "internal", status


def spawn_owner(event):
    if event.packet_id == (8, 0x10):
        if event.packet_size != 120:
            raise ValueError("unsupported vehicle owner layout")
        offset = 0x5c
    elif event.packet_id == (8, 0x11):
        offset = 0xac
    else:
        raise ValueError("unsupported owner packet")
    if event.packet_size < offset + 12 or len(event.prefix) < offset + 12:
        raise ValueError("owner field not captured")
    owner = ObjectRef.from_packet(event.prefix, offset)
    if not owner.object_id:
        return None
    if owner.entity_type != 4:
        raise ValueError("owner header is not a player")
    return owner
