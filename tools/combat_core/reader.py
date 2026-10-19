"""One incremental OPCAP v1 decoder for file replay and tailing."""
import struct

from ..binary_analysis.probe_damage_packet import decode
from .model import DamageEvent, ObjectRef

HEADER = struct.Struct("<8sIIQQ")
RECORD = struct.Struct("<QQQ84s")


class CaptureDecoder:
    def __init__(self, capture_id):
        if not capture_id:
            raise ValueError("capture_id is required")
        self.capture_id = capture_id
        self.pending = bytearray()
        self.frequency = None
        self.start_filetime = None

    def feed(self, data):
        self.pending.extend(data)
        if self.frequency is None:
            if len(self.pending) < HEADER.size:
                return []
            magic, version, size, frequency, started = HEADER.unpack_from(self.pending)
            if magic != b"OPCAP01\0" or version != 1 or size != RECORD.size or not frequency:
                raise ValueError("unsupported or corrupt OPCAP header")
            self.frequency, self.start_filetime = frequency, started
            del self.pending[:HEADER.size]
        result = []
        consumed = 0
        while len(self.pending) - consumed >= RECORD.size:
            sequence, filetime, qpc, packet = RECORD.unpack_from(self.pending, consumed)
            consumed += RECORD.size
            if not sequence:
                raise ValueError("zero sequence number")
            fields = decode(packet, "current")
            if not fields["matches_known_definition_size"]:
                raise ValueError("unexpected damage packet size")
            result.append(DamageEvent(self.capture_id, sequence, filetime, qpc, self.frequency,
                ObjectRef.from_packet(packet, 8), ObjectRef.from_packet(packet, 0x20),
                ObjectRef.from_packet(packet, 0x14), fields["attack_id"], fields["damage"], packet.hex()))
        del self.pending[:consumed]
        return result

    def finish(self):
        if self.frequency is None or self.pending:
            raise ValueError("incomplete OPCAP header or final record")
