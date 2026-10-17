"""Command-line interface for scratchpad-verifier."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path, PurePosixPath

from . import __version__
from .transcript import (
    DEFAULT_MARKERS,
    DEFAULT_PREFIXES,
    FILE_TOOLS,
    FileState,
    Operation,
    errored_calls,
    output_dir_for,
    reconstruct,
    tool_slug,
    versioned_names,
)


def resolve_transcript(arg: str) -> Path:
    """Accept a .jsonl path or a bare session id found under ~/.claude/projects."""
    p = Path(arg).expanduser()
    if p.is_file():
        return p
    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
    matches = sorted((config_dir / "projects").glob(f"*/{arg}.jsonl"))
    if matches:
        return matches[0]
    raise SystemExit(f"error: transcript not found: {arg}")


def _load(args: argparse.Namespace) -> dict[str, FileState]:
    prefixes = list(DEFAULT_PREFIXES) + list(args.prefix or [])
    markers = list(DEFAULT_MARKERS) + list(args.marker or [])
    transcript = resolve_transcript(args.transcript)
    args.session_id = transcript.stem
    args.transcript_path = transcript
    return reconstruct(
        transcript,
        prefixes=prefixes,
        markers=markers,
        match_all=args.all,
        include_subagents=not args.no_subagents,
    )


def _status(st: FileState) -> str:
    if st.content is None:
        return "unknown"
    return "partial" if st.problems else "ok"


def _other_errors(args: argparse.Namespace, states: dict[str, FileState]) -> list[Operation]:
    return errored_calls(args.transcript_path, states, include_subagents=not args.no_subagents)


def _error_record(op: Operation) -> dict:
    return {
        "tool": op.tool,
        "file_path": op.path or None,
        "timestamp": op.timestamp,
        "tool_use_id": op.tool_use_id,
        "error": op.error,
        "input": op.input,
    }


def _one_line(text: str | None, limit: int = 100) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def cmd_list(args: argparse.Namespace) -> int:
    states = _load(args)
    others = _other_errors(args, states)
    if args.json:
        out = [
            {
                "path": st.path,
                "status": _status(st),
                "operations": [o.tool for o in st.operations],
                "versions": len(st.versions),
                "errored": [
                    {"tool": o.tool, "timestamp": o.timestamp, "error": o.error}
                    for o in st.errored
                ],
                "bytes": len(st.content.encode()) if st.content is not None else None,
                "problems": st.problems,
            }
            for st in states.values()
        ]
        print(json.dumps({"files": out, "errored_calls": [_error_record(o) for o in others]}, indent=2))
        return 0
    for st in states.values():
        size = f"{len(st.content.encode()):>8}B" if st.content is not None else "       ?B"
        ops = ",".join(o.tool for o in st.operations)
        ops = ",".join(o.tool + ("!" if o.is_error else "") for o in st.operations)
        print(f"{_status(st):<8} {size}  v{len(st.versions):<3} {st.path}  [{ops}]")
    if not states:
        print("no matching files", file=sys.stderr)
    if others:
        print(f"\n{len(others)} other errored tool call(s):")
        for o in others:
            what = o.path or o.input.get("command") or json.dumps(o.input, ensure_ascii=False)
            print(f"ERROR    {o.tool:<10} {_one_line(what, 70)}  ->  {_one_line(o.error, 60)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    states = _load(args)
    targets = args.paths or list(states)
    rc = 0
    for path in targets:
        st = states.get(path)
        if st is None:
            print(f"error: no writes to {path} in transcript", file=sys.stderr)
            rc = 1
            continue
        if len(targets) > 1:
            print(f"===== {path} ({_status(st)}) =====")
        if st.content is None:
            print(f"error: content of {path} could not be reconstructed", file=sys.stderr)
            rc = 1
            continue
        sys.stdout.write(st.content)
        if len(targets) > 1 and not st.content.endswith("\n"):
            sys.stdout.write("\n")
    return rc


def _write_error(dest: Path, op: Operation, verbose: bool) -> None:
    text = json.dumps(_error_record(op), indent=2, ensure_ascii=False) + "\n"
    dest.write_text(text, encoding="utf-8")
    if verbose:
        print(dest)


def cmd_extract(args: argparse.Namespace) -> int:
    states = _load(args)
    session_dir = Path(args.output) / args.session_id
    count = 0
    errored = 0
    for st in states.values():
        rel_dir = output_dir_for(st.path)
        if st.versions:
            dest_dir = session_dir / rel_dir
            dest_dir.mkdir(parents=True, exist_ok=True)
            names = versioned_names(st.path, len(st.versions))
            for name, content in zip(names, st.versions):
                dest = dest_dir / name
                dest.write_text(content, encoding="utf-8")
                count += 1
                if args.verbose:
                    print(dest)
        elif not st.errored:
            print(f"skip (unknown content): {st.path}", file=sys.stderr)

        failed = st.errored
        if failed:
            err_dir = session_dir / "errored" / rel_dir
            err_dir.mkdir(parents=True, exist_ok=True)
            names = versioned_names(st.path, len(failed))
            for name, op in zip(names, failed):
                _write_error(err_dir / f"{name}.json", op, args.verbose)
                errored += 1

    # Every other failed call: Bash and other tools grouped by tool name,
    # file tools outside the path filter by their original path.
    groups: dict[PurePosixPath, list[Operation]] = {}
    for op in _other_errors(args, states):
        if op.tool in FILE_TOOLS and op.path:
            key = output_dir_for(op.path) / PurePosixPath(op.path.replace("\\", "/")).name
        else:
            key = PurePosixPath(tool_slug(op.tool), tool_slug(op.tool))
        groups.setdefault(key, []).append(op)
    for key, ops in groups.items():
        err_dir = session_dir / "errored" / key.parent
        err_dir.mkdir(parents=True, exist_ok=True)
        for name, op in zip(versioned_names(key.name, len(ops)), ops):
            _write_error(err_dir / f"{name}.json", op, args.verbose)
            errored += 1
    print(
        f"wrote {count} file version(s) and {errored} errored call(s) to {session_dir}",
        file=sys.stderr,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    states = _load(args)
    root = Path(args.root) if args.root else None
    mismatches = 0
    for st in states.values():
        disk = Path(st.path) if root is None else root / st.path.lstrip("/\\")
        if st.content is None:
            result = "UNKNOWN"
        elif not disk.is_file():
            result = "MISSING"
        else:
            try:
                same = disk.read_text(encoding="utf-8") == st.content
            except UnicodeDecodeError:
                same = False
            result = "MATCH" if same else "DIFFERS"
        if result != "MATCH":
            mismatches += 1
        print(f"{result:<8} {st.path}")
    return 1 if mismatches else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scratchpad-verifier",
        description=(
            "Reconstruct files written to /tmp and scratchpad paths during a "
            "Claude Code session from its .jsonl transcript."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("transcript", help="path to a session .jsonl, or a session id")
    common.add_argument(
        "--prefix", action="append", metavar="PATH",
        help="also include files under this path prefix (repeatable)",
    )
    common.add_argument(
        "--marker", action="append", metavar="TEXT",
        help="also include paths containing this text (repeatable)",
    )
    common.add_argument("--all", action="store_true", help="include every written file")
    common.add_argument(
        "--no-subagents", action="store_true",
        help="ignore subagent transcripts stored next to the session",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", parents=[common], help="list reconstructed files")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", parents=[common], help="print reconstructed file contents")
    p.add_argument("paths", nargs="*", help="file paths to print (default: all)")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser(
        "extract", parents=[common],
        help="write every version of each file to OUTPUT/<session_id>/{scratchpad,tmp}/",
    )
    p.add_argument("-o", "--output", default=".", help="destination directory (default: .)")
    p.add_argument("-v", "--verbose", action="store_true", help="print each written path")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser(
        "verify", parents=[common],
        help="compare reconstructed files with what is on disk (exit 1 on mismatch)",
    )
    p.add_argument("--root", help="look for files under this directory instead of /")
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
