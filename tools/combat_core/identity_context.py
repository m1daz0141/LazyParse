"""Bind a segment to the identity journal belonging to the same game run."""
import json
from pathlib import Path

from .metadata import META_FORMATS, read_metadata
from .reader import HEADER
from .shared_files import open_read, read_text


class IdentityContext:
    def __init__(self, capture: Path):
        self.capture = capture.resolve()
        self.reference = self.capture.with_name("identity-context.json")
        self.run = self.capture.parent.parent
        self.signature = None

    def read(self, capture_status=None, *, journal=None):
        ref = json.loads(read_text(self.reference))
        if (ref.get("schema_version") != 1 or ref.get("run_name") != self.run.name or
                ref.get("context_path") != "../identity-context/identity.opmeta" or
                type(ref.get("pid")) is not int or ref["pid"] <= 0 or
                type(ref.get("context_start_filetime")) is not int or ref["context_start_filetime"] <= 0 or
                not isinstance(ref.get("source_sha256"), str) or not ref["source_sha256"]):
            raise ValueError("identity context reference does not belong to this run")
        path = (self.capture.parent / ref["context_path"]).resolve()
        if not path.is_relative_to(self.run) or path.parent != self.run / "identity-context":
            raise ValueError("identity context path escapes its run")
        if journal is None:
            status = json.loads(read_text(path.with_name("context-status.json")))
            with open_read(path) as stream:
                raw_header = stream.read(HEADER.size)
        else:
            if path != journal.path:
                raise ValueError("identity journal does not belong to this segment")
            status, raw_header = journal.status, journal.header
        if len(raw_header) != HEADER.size:
            raise ValueError("incomplete identity context header")
        magic, version, size, frequency, started = HEADER.unpack(raw_header)
        if (status.get("schema_version") != 1 or (magic, version, size) not in META_FORMATS or not frequency or
                ref.get("qpc_frequency") != frequency or ref.get("context_start_filetime") != started or
                status.get("start_filetime") != started or status.get("pid") != ref.get("pid") or
                status.get("source_sha256") != ref.get("source_sha256")):
            raise ValueError("identity context header/process/version mismatch")
        if capture_status and capture_status.get("source_sha256") not in {None, ref["source_sha256"]}:
            raise ValueError("identity context and damage capture versions differ")
        signature = (ref["pid"], started, frequency, ref["source_sha256"], ref["start_qpc"], magic, version, size)
        if self.signature is not None and self.signature != signature:
            raise ValueError("identity context reference changed during reading")
        self.signature = signature
        start, end = ref.get("start_qpc"), ref.get("end_qpc")
        if (type(start) is not int or start <= 0 or
                (end is not None and (type(end) is not int or end < start))):
            raise ValueError("invalid identity context time bounds")
        through = status.get("published_through_qpc")
        if type(through) is not int or through < 0:
            raise ValueError("invalid identity context publication time")
        events = (read_metadata(path, allow_partial=not status.get("stopped", False), upto=status["written"])
                  if journal is None else journal.events)
        flags = {f"identity_context_{name}" for name in ("dropped", "invalid", "io_error") if status.get(name)}
        return events, ref, status, through, flags
