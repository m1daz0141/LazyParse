"""Read fixed-size raw Classic damage captures. Field meanings remain provisional."""
import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import struct
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "binary_analysis"))
from probe_damage_packet import decode

HEADER = struct.Struct("<8sIIQQ")
RECORD = struct.Struct("<QQQ84s")
EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def read_header(stream):
    data = stream.read(HEADER.size)
    if len(data) != HEADER.size:
        raise ValueError("incomplete capture header")
    magic, version, record_size, frequency, started = HEADER.unpack(data)
    if magic != b"OPCAP01\0" or version != 1 or record_size != RECORD.size or not frequency:
        raise ValueError("unsupported or corrupt capture header")
    return {"version": version, "qpc_frequency": frequency, "start_filetime": started}


def decode_record(data, frequency):
    if len(data) != RECORD.size:
        raise ValueError("incomplete capture record")
    sequence, filetime, qpc, packet = RECORD.unpack(data)
    if not sequence:
        raise ValueError("invalid sequence number")
    fields = decode(packet, "current")
    if not fields["matches_known_definition_size"]:
        raise ValueError("unexpected packet size")
    return {
        "sequence": sequence,
        "utc": (EPOCH + timedelta(microseconds=filetime // 10)).isoformat(),
        "filetime": filetime,
        "qpc": qpc,
        "qpc_frequency": frequency,
        **fields,
        "packet_hex": packet.hex(),
        "field_meanings_live_verified": False,
    }


def records(path, follow=False):
    with path.open("rb") as stream:
        header = read_header(stream)
        pending = b""
        while True:
            pending += stream.read(RECORD.size - len(pending))
            if len(pending) == RECORD.size:
                yield decode_record(pending, header["qpc_frequency"])
                pending = b""
            elif follow:
                time.sleep(0.1)
            elif pending:
                raise ValueError(f"truncated final record ({len(pending)} of {RECORD.size} bytes)")
            else:
                return


def summarize(rows):
    sources = defaultdict(lambda: {"damage": 0, "hits": 0, "negative_values": 0})
    first = last = None
    count = 0
    for row in rows:
        count += 1
        seconds = row["qpc"] / row["qpc_frequency"]
        first = seconds if first is None else min(first, seconds)
        last = seconds if last is None else max(last, seconds)
        source = sources[str(row["inflicter_id"])]
        if row["damage"] > 0:
            source["damage"] += row["damage"]
            source["hits"] += 1
        elif row["damage"] < 0:
            source["negative_values"] += 1
    elapsed = 0 if first is None else last - first
    for source in sources.values():
        source["dps"] = source["damage"] / elapsed if elapsed > 0 else None
    return {"records": count, "elapsed_between_packets_seconds": elapsed,
            "sources_using_provisional_ids": dict(sources), "field_meanings_live_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--summary", action="store_true")
    mode.add_argument("--follow", action="store_true", help="print each new packet; Ctrl+C ends viewing")
    parser.add_argument("--extract-dir", type=Path, help="save each 84-byte packet separately")
    args = parser.parse_args()
    try:
        if args.extract_dir:
            args.extract_dir.mkdir(parents=True, exist_ok=False)
        rows = records(args.capture, args.follow)
        if args.summary:
            if args.extract_dir:
                parser.error("--extract-dir cannot be combined with --summary")
            print(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
        else:
            for index, row in enumerate(rows, 1):
                if args.extract_dir:
                    (args.extract_dir / f"{index:06d}.bin").write_bytes(bytes.fromhex(row["packet_hex"]))
                print(json.dumps(row, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, OverflowError) as exc:
        parser.exit(2, f"Invalid capture: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
