"""Check scratchpad-verifier against real Claude Code sessions.

tests/meta/record_test_session.sh records sessions into
test_claude_code_sessions/: <id>.jsonl is the transcript and <id>/scratchpad/
is a copy of the session's scratchpad taken after it ended. For every file in
that copy, the tool must either rebuild the same content from the transcript,
or at least list it by name when a program wrote it (git, a Python script).

A session passes when no file is missing and no rebuilt file differs. Content
is compared after normalizing line endings (CRLF -> LF) and dropping a UTF-8
BOM; files that only differ there are reported but pass.

Each run also saves what `scratchpad-verifier extract` produces for every
session in <id>/scratchpad_verifier_output_<datestamp>/, to inspect by hand.

Run: python tests/meta/test_claude_code_sessions.py -v
"""
import datetime
import hashlib
import os
import json
import re
import sys
import unittest
from contextlib import redirect_stderr
from dataclasses import dataclass, field
from pathlib import Path

try:
    from scratchpad_verifier import cli
    from scratchpad_verifier.transcript import analyze, canonical
except ImportError:  # running from a clone without installing
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from scratchpad_verifier import cli
    from scratchpad_verifier.transcript import analyze, canonical

RECORDINGS = Path(__file__).with_name("test_claude_code_sessions")
STAMP = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def digest(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


@dataclass
class Report:
    session: str
    mode: str
    matched: list = field(default_factory=list)      # content rebuilt exactly
    line_endings: list = field(default_factory=list)  # rebuilt, differs only in CRLF/BOM
    by_name: list = field(default_factory=list)       # listed, content not in the transcript
    missing: list = field(default_factory=list)       # on disk, unknown to the tool
    differs: list = field(default_factory=list)       # rebuilt content is wrong
    absent: list = field(default_factory=list)        # tool says it exists, not on disk

    @property
    def ok(self) -> bool:
        return not self.missing and not self.differs

    def summary(self) -> str:
        lines = [
            f"{self.session} ({self.mode}): {len(self.matched)} rebuilt, "
            f"{len(self.line_endings)} rebuilt up to line endings, {len(self.by_name)} listed by name, "
            f"{len(self.missing)} missing, {len(self.differs)} differ, {len(self.absent)} not on disk",
        ]
        for label, items in (("missing", self.missing), ("differs", self.differs),
                             ("listed by name", self.by_name), ("not on disk", self.absent)):
            lines += [f"    {label}: {p}" for p in items]
        return "\n".join(lines)


def save_extract(transcript: Path) -> Path:
    """Run `extract` with default options into <id>/scratchpad_verifier_output_<datestamp>/."""
    args = cli.build_parser().parse_args(["extract", str(transcript)])
    session, files = cli._load(args)
    dest = RECORDINGS / transcript.stem / f"scratchpad_verifier_output_{STAMP}"
    with open(os.devnull, "w") as quiet, redirect_stderr(quiet):
        cli.extract(session, files, args, dest)
    return dest


def compare(transcript: Path) -> Report:
    sid = transcript.stem
    meta = json.loads((RECORDINGS / sid / "meta.json").read_text(encoding="utf-8"))
    snapshot = RECORDINGS / sid / "scratchpad"
    excluded = meta.get("excluded", [])

    windows = bool(re.match(r"^[A-Za-z]:[\\/]", meta["scratchpad"]))
    root = canonical(meta["scratchpad"], windows).rstrip("/") + "/"

    def rel(path: str):
        return path[len(root):] if path.startswith(root) else None

    def is_excluded(p: str) -> bool:
        return any(p == e or p.startswith(e + "/") for e in excluded)

    session = analyze(transcript)
    known = {rel(p): st for p, st in session.files.items() if rel(p)}
    produced = {rel(p) for p in session.produced if rel(p)}

    report = Report(sid, meta.get("mode", "?"))
    on_disk = set()
    for f in sorted(snapshot.rglob("*")):
        if not f.is_file():
            continue
        p = f.relative_to(snapshot).as_posix()
        on_disk.add(p)
        st = known.get(p)
        if st is not None and st.complete:
            data = f.read_bytes()
            content = st.content.encode("utf-8")
            if content == data:
                report.matched.append(p)
            elif digest(content) == digest(data):
                report.line_endings.append(p)
            else:
                report.differs.append(p)
        elif st is not None or p in produced:
            report.by_name.append(p)
        else:
            report.missing.append(p)

    report.absent = sorted(
        p for p, st in known.items()
        if not st.deleted and p not in on_disk and not is_excluded(p)
    )
    return report


class RecordedSessionTests(unittest.TestCase):
    def test_recorded_sessions(self):
        transcripts = sorted(RECORDINGS.glob("*.jsonl"))
        if not transcripts:
            self.skipTest("no recordings; run tests/meta/record_test_session.sh")
        for transcript in transcripts:
            with self.subTest(session=transcript.stem):
                save_extract(transcript)
                report = compare(transcript)
                print("\n" + report.summary(), file=sys.stderr)
                self.assertEqual(report.missing, [], "files on disk the tool doesn't know about")
                self.assertEqual(report.differs, [], "files the tool rebuilt with the wrong content")


if __name__ == "__main__":
    unittest.main()
