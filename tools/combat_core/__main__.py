"""python -m tools.combat_core capture.opcap [--self-id ID] [--follow]"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import time

from .engine import CombatEngine
from .model import IdentityRegistry, ObjectRef
from .reader import CaptureDecoder
from .metadata import MetadataTimeline, read_metadata
from .identity_context import IdentityContext
from .diagnostics import capture_context


def replay(capture, registry=None, idle_seconds=10, chunk_size=65536, on_event=None):
    engine = CombatEngine(idle_seconds)
    flags, boundaries, status = capture_context(capture)
    engine.begin_segment(str(capture.resolve()), registry, flags)
    decoder = CaptureDecoder(engine.capture_id)
    meta_path = capture.with_name("identity.opmeta")
    context = IdentityContext(capture) if capture.with_name("identity-context.json").exists() else None
    context_ref = context_status = None
    context_through = None
    timeline = MetadataTimeline() if context or meta_path.exists() else None
    if not timeline and status and status.get("identity_written", 0):
        raise ValueError("capture status records identity data, but identity.opmeta is missing")
    if context:
        meta_events, context_ref, context_status, context_through, context_flags = context.read(status)
        engine.quality_flags.update(context_flags)
        engine.quality_by_capture[engine.capture_id].update(context_flags)
        timeline.update(meta_events)
        if context_through < context_ref["start_qpc"]:
            raise ValueError("identity context has not published the segment start yet; use --follow")
        timeline.seed(context_ref["start_qpc"], context_ref["qpc_frequency"], engine)
    elif timeline:
        timeline.update(read_metadata(meta_path))
        if status and status.get("stopped") and len(timeline.events) != status.get("identity_written"):
            raise ValueError("identity record count differs from finalized capture status")
    boundary_index = 0
    with capture.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            for event in decoder.feed(chunk):
                if context:
                    if event.qpc < context_ref["start_qpc"] or (context_ref["end_qpc"] is not None and event.qpc > context_ref["end_qpc"]):
                        raise ValueError("damage timestamp lies outside its identity context reference")
                    if event.qpc > context_through:
                        raise ValueError("identity context is catching up; use --follow")
                while boundary_index < len(boundaries) and boundaries[boundary_index] <= event.filetime:
                    engine.mark_gap("binding_gap")
                    boundary_index += 1
                if timeline:
                    timeline.before(event, engine)
                result = engine.feed(event)
                if result and on_event:
                    on_event(result)
    decoder.finish()
    if context and decoder.frequency != context_ref["qpc_frequency"]:
        raise ValueError("identity context and damage header clocks differ")
    for _ in boundaries[boundary_index:]:
        engine.mark_gap("binding_gap")
    if context:
        upper = context_ref["end_qpc"]
        if upper is None:
            if status and status.get("stopped"):
                raise ValueError("identity context reference has not finalized the segment")
            upper = engine.last_event.qpc if engine.last_event else context_ref["start_qpc"]
        if upper > context_through:
            raise ValueError("identity context is catching up; use --follow")
        timeline.through(upper, context_ref["qpc_frequency"], engine)
    elif timeline:
        timeline.finish(engine)
    result = engine.finish(context_ref['end_qpc'] if context_ref else None,
                           context_ref['qpc_frequency'] if context_ref else None)
    result["capture_diagnostics"] = status
    result["identity_metadata"] = timeline.report() if timeline else None
    result["identity_context"] = {"reference": context_ref, "status": context_status} if context else None
    if status and status.get("stopped") and result["counts"]["records"] != status.get("written"):
        raise ValueError("decoded record count differs from finalized capture-status.json")
    return result


def positive_id(text):
    value = int(text, 0)
    if not 0 < value <= 0xFFFFFFFF:
        raise argparse.ArgumentTypeError("ID must be in 1..4294967295")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    identities = parser.add_mutually_exclusive_group()
    identities.add_argument("--self-id", type=positive_id, help="explicit local player ID; never inferred from numeric size")
    identities.add_argument("--identities", type=Path, help="SHA-256-bound identity annotations")
    parser.add_argument("--idle-seconds", type=float, default=10)
    parser.add_argument("--follow", action="store_true", help="tail this segment and emit snapshots; Ctrl+C stops viewing")
    parser.add_argument("--events", type=Path, help="write all signed events and attribution as JSONL (new file)")
    parser.add_argument("--output", type=Path, help="write final report as JSON (new file)")
    args = parser.parse_args()
    if args.follow and args.identities:
        parser.error("SHA-256-bound annotations require a completed capture; use --self-id for tailing")
    audit = None
    try:
        registry = IdentityRegistry.from_file(args.identities, args.capture) if args.identities else IdentityRegistry()
        if args.self_id:
            registry.set_self(ObjectRef(args.self_id, 4), "explicit_cli_self_id")
        if args.output and args.output.exists():
            raise ValueError("output already exists")
        if args.events:
            audit = args.events.open("x", encoding="utf-8")

        def on_event(event):
            timestamp = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=event["filetime"] // 10)
            event["time_jst"] = timestamp.astimezone(timezone(timedelta(hours=9))).isoformat()
            if audit:
                audit.write(json.dumps(event, ensure_ascii=False) + "\n")
                audit.flush()

        if not args.follow:
            report = replay(args.capture, registry, args.idle_seconds, on_event=on_event)
        else:
            report = follow(args.capture, registry, args.idle_seconds, on_event)
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(text)
        if not args.follow:
            print(text, end="")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Cannot aggregate capture: {exc}", file=sys.stderr)
        return 2
    finally:
        if audit:
            audit.close()


def follow(capture, registry, idle_seconds, on_event):
    # Files remain read-only. The same decoder/engine is used by replay above.
    engine = CombatEngine(idle_seconds)
    flags, _, _ = capture_context(capture)
    engine.begin_segment(str(capture.resolve()), registry, flags)
    decoder = CaptureDecoder(engine.capture_id)
    meta_path = capture.with_name("identity.opmeta")
    context = IdentityContext(capture) if capture.with_name("identity-context.json").exists() else None
    context_ref = context_status = None
    context_through = None
    context_seeded = False
    timeline = MetadataTimeline() if context or meta_path.exists() else None
    pending_damage = []
    consumed_damage = 0
    last_status = None
    last_identity_position = -1
    last_measurement_clock = None
    seen_boundaries = set()
    try:
        with capture.open("rb") as stream:
            while True:
                flags, boundaries, status = capture_context(capture)
                engine.quality_flags.update(flags)
                engine.quality_by_capture[engine.capture_id].update(flags)
                chunk = stream.read(65536)
                pending_damage.extend(decoder.feed(chunk))
                if context and context_ref and decoder.frequency is not None and decoder.frequency != context_ref["qpc_frequency"]:
                    raise ValueError("identity context and damage header clocks differ")
                watermark = status.get("written", consumed_damage) if timeline and status else consumed_damage + len(pending_damage)
                if context:
                    meta_events, context_ref, context_status, context_through, context_flags = context.read(status)
                    timeline.update(meta_events)
                    engine.quality_flags.update(context_flags)
                    engine.quality_by_capture[engine.capture_id].update(context_flags)
                    if context_status.get("io_error"):
                        raise ValueError("identity context writer failed")
                    if not context_seeded and context_through >= context_ref["start_qpc"]:
                        timeline.seed(context_ref["start_qpc"], context_ref["qpc_frequency"], engine)
                        context_seeded = True
                elif timeline:
                    finalized = bool(status and status.get("stopped"))
                    meta_events = read_metadata(meta_path, allow_partial=not finalized,
                        upto=None if finalized else status.get("identity_written", 0) if status else 0)
                    if finalized and len(meta_events) != status.get("identity_written"):
                        raise ValueError("identity count differs from finalized capture status")
                    timeline.update(meta_events)
                available = min(len(pending_damage), max(0, watermark - consumed_damage))
                if context:
                    allowed = 0
                    for event in pending_damage[:available]:
                        if not context_seeded or event.qpc > context_through:
                            break
                        allowed += 1
                    available = allowed
                events, pending_damage = pending_damage[:available], pending_damage[available:]
                for event in events:
                    if context and (event.qpc < context_ref["start_qpc"] or
                        (context_ref["end_qpc"] is not None and event.qpc > context_ref["end_qpc"])):
                        raise ValueError("damage timestamp lies outside its identity context reference")
                    for boundary in boundaries:
                        if boundary not in seen_boundaries and boundary <= event.filetime:
                            engine.mark_gap("binding_gap")
                            seen_boundaries.add(boundary)
                    if timeline:
                        timeline.before(event, engine)
                    item = engine.feed(event)
                    consumed_damage += 1
                    if item:
                        on_event(item)
                identity_position = timeline.position if timeline else 0
                if context and context_seeded and not status.get('stopped'):
                    engine.observe_time(context_through, context_ref['qpc_frequency'])
                if events or status != last_status or identity_position != last_identity_position or engine.measurement_now != last_measurement_clock:
                    print(json.dumps({"type": "snapshot", "report": engine.snapshot()}, ensure_ascii=False, allow_nan=False), flush=True)
                    last_status = status
                    last_identity_position = identity_position
                    last_measurement_clock = engine.measurement_now
                if not chunk and status and status.get("stopped"):
                    if context and (context_ref["end_qpc"] is None or context_through < context_ref["end_qpc"]):
                        if context_status.get("stopped"):
                            raise ValueError("identity context ended before this segment was finalized")
                        time.sleep(0.1)
                        continue
                    decoder.finish()
                    if pending_damage:
                        raise ValueError("damage records exceed finalized status watermark")
                    for boundary in boundaries:
                        if boundary not in seen_boundaries:
                            engine.mark_gap("binding_gap")
                            seen_boundaries.add(boundary)
                    if context:
                        timeline.through(context_ref["end_qpc"], context_ref["qpc_frequency"], engine)
                    elif timeline:
                        timeline.finish(engine)
                    if engine.counts["records"] != status.get("written"):
                        raise ValueError("decoded count differs from finalized capture status")
                    break
                if not chunk:
                    time.sleep(0.1)
    except KeyboardInterrupt:
        engine.quality_flags.add("viewing_interrupted")
    final_flags, _, status = capture_context(capture)
    engine.quality_flags.update(final_flags)
    report = engine.finish(context_ref['end_qpc'] if context_ref else None,
                           context_ref['qpc_frequency'] if context_ref else None)
    report["capture_diagnostics"] = status
    report["identity_metadata"] = timeline.report() if timeline else None
    report["identity_context"] = {"reference": context_ref, "status": context_status} if context else None
    print(json.dumps({"type": "final", "report": report}, ensure_ascii=False, allow_nan=False), flush=True)
    return report


if __name__ == "__main__":
    raise SystemExit(main())
