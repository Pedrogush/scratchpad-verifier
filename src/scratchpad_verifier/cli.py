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
    SHELL_TOOLS,
    FileState,
    Operation,
    Session,
    analyze,
    output_dir_for,
    path_matches,
    select,
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


def _load(args: argparse.Namespace) -> tuple[Session, dict[str, FileState]]:
    transcript = resolve_transcript(args.transcript)
    session = analyze(transcript, include_subagents=not args.no_subagents)
    prefixes = list(DEFAULT_PREFIXES) + list(args.prefix or [])
    markers = list(DEFAULT_MARKERS) + list(args.marker or [])
    return session, select(session, prefixes, markers, match_all=args.all)


def _status(st: FileState) -> str:
    if st.content is None:
        return "unknown"
    return "partial" if st.problems else "ok"


def _error_record(op: Operation) -> dict:
    return {
        "tool": op.tool,
        "file_path": op.path or None,
        "timestamp": op.timestamp,
        "cwd": op.cwd,
        "tool_use_id": op.tool_use_id,
        "error": op.error,
        "input": op.input,
    }


def _one_line(text: str | None, limit: int = 100) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _other_errors(session: Session, files: dict[str, FileState]) -> list[Operation]:
    """Errored calls not attached to a selected file."""
    covered = {o.tool_use_id for st in files.values() for o in st.errored}
    return [o for o in session.errored_calls() if o.tool_use_id not in covered]


def _produced(session: Session, args: argparse.Namespace) -> list:
    prefixes = list(DEFAULT_PREFIXES) + list(args.prefix or [])
    markers = list(DEFAULT_MARKERS) + list(args.marker or [])
    return [
        pr for pr in session.produced.values()
        if pr.category != "other" or args.all or path_matches(pr.path, prefixes, markers)
    ]


def _file_summary(st: FileState) -> dict:
    return {
        "path": st.path,
        "category": st.category,
        "status": _status(st),
        "versions": [v.how for v in st.history],
        "deleted": st.deleted,
        "bytes": len(st.content.encode()) if st.content is not None else None,
        "errored": [{"tool": o.tool, "timestamp": o.timestamp, "error": o.error} for o in st.errored],
        "problems": st.problems,
    }


def cmd_list(args: argparse.Namespace) -> int:
    session, files = _load(args)
    others = _other_errors(session, files)
    if args.json:
        out = {
            "session_id": session.session_id,
            "root": session.root,
            "files": [_file_summary(st) for st in files.values()],
            "commands": len(session.commands),
            "inline_scripts": len(session.scripts),
            "produced_without_content": [
                {"path": pr.path, "category": pr.category, "how": pr.how, "deleted": pr.deleted}
                for pr in _produced(session, args)
            ],
            "errored_calls": [_error_record(o) for o in others],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    for st in files.values():
        size = f"{len(st.content.encode()):>8}B" if st.content is not None else "       ?B"
        hist = ",".join(v.how for v in st.history) or "-"
        if st.errored:
            hist += "," + ",".join(o.tool + "!" for o in st.errored)
        flag = " (deleted)" if st.deleted else ""
        print(f"{_status(st):<8} {st.category:<13} {size}  v{len(st.history):<3} {st.path}{flag}  [{hist}]")
    if not files:
        print("no matching files", file=sys.stderr)
    produced = _produced(session, args)
    if produced:
        print(f"\n{len(produced)} file(s) known only by name (written by programs, or mentioned), "
              "content not in the transcript:")
        for pr in produced:
            flag = " (deleted)" if pr.deleted else ""
            print(f"         {pr.category:<13} {pr.path}{flag}  [{_one_line('; '.join(dict.fromkeys(pr.how)), 60)}]")
    print(f"\n{len(session.commands)} shell command(s), {len(session.scripts)} inline script(s) ran")
    hidden = sum(1 for p in session.files if p not in files)
    if hidden:
        print(f"{hidden} other file(s) written outside scratchpad/tmp (show them with --all)")
    if others:
        print(f"\n{len(others)} other errored tool call(s):")
        for o in others:
            what = o.path or o.input.get("command") or json.dumps(o.input, ensure_ascii=False)
            print(f"ERROR    {o.tool:<10} {_one_line(what, 70)}  ->  {_one_line(o.error, 60)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    _, files = _load(args)
    targets = args.paths or list(files)
    rc = 0
    for path in targets:
        st = files.get(path)
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


class _Writer:
    def __init__(self, root: Path, verbose: bool):
        self.root = root
        self.verbose = verbose
        self.count = 0

    def write(self, rel: PurePosixPath, text: str) -> str:
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        self.count += 1
        if self.verbose:
            print(dest)
        return rel.as_posix()


def _patch_text(op: Operation) -> str:
    """A unified diff for an Edit, from its structuredPatch (or old/new strings)."""
    path = op.path.replace("\\", "/")
    lines = [f"--- a/{path}", f"+++ b/{path}"]
    hunks = (op.result or {}).get("structuredPatch") or []
    if hunks:
        for h in hunks:
            lines.append(f"@@ -{h.get('oldStart')},{h.get('oldLines')} +{h.get('newStart')},{h.get('newLines')} @@")
            lines.extend(h.get("lines") or [])
    else:
        edits = [op.input] if op.tool == "Edit" else (op.input.get("edits") or [])
        for e in edits:
            lines.append("@@ edit @@")
            lines.extend("-" + x for x in str(e.get("old_string", "")).splitlines())
            lines.extend("+" + x for x in str(e.get("new_string", "")).splitlines())
    return "\n".join(lines) + "\n"


def _command_text(op: Operation, index: int, ext: str) -> str:
    c = "#"
    lines = [
        f"{c} tool: {op.tool}",
        f"{c} timestamp: {op.timestamp}",
        f"{c} cwd: {op.cwd}",
    ]
    if op.input.get("description"):
        lines.append(f"{c} description: {_one_line(op.input['description'], 200)}")
    if op.is_error:
        lines.append(f"{c} status: ERROR: {_one_line(op.error, 200)}")
    else:
        lines.append(f"{c} status: ok")
    command = op.input.get("command")
    if not isinstance(command, str):
        command = json.dumps(op.input, indent=2, ensure_ascii=False)
    return "\n".join(lines) + "\n\n" + command.rstrip("\n") + "\n"


def cmd_extract(args: argparse.Namespace) -> int:
    session, files = _load(args)
    out = _Writer(Path(args.output) / session.session_id, args.verbose)
    manifest: dict = {
        "session_id": session.session_id,
        "root": session.root,
        "files": [],
        "commands": [],
        "inline_scripts": [],
        "produced_without_content": [],
        "errored": [],
    }

    for st in files.values():
        rel_dir = output_dir_for(st.path, st.category, session.root)
        entry = _file_summary(st)
        entry["versions"] = []
        names = versioned_names(st.path, len(st.history))
        for name, v in zip(names, st.history):
            rel = out.write(rel_dir / name, v.content)
            entry["versions"].append(
                {"file": rel, "how": v.how, "timestamp": v.op.timestamp, "note": v.note or None}
            )
        # Edits on a file whose content we never saw: keep them as patches.
        if not st.history:
            edits = [o for o in st.operations if o.tool in ("Edit", "MultiEdit") and not o.is_error]
            entry["patches"] = []
            for name, op in zip(versioned_names(st.path, len(edits)), edits):
                rel = out.write(rel_dir / f"{name}.patch", _patch_text(op))
                entry["patches"].append(rel)
        failed = st.errored
        for name, op in zip(versioned_names(st.path, len(failed)), failed):
            rel = out.write(
                PurePosixPath("errored") / rel_dir / f"{name}.json",
                json.dumps(_error_record(op), indent=2, ensure_ascii=False) + "\n",
            )
            manifest["errored"].append({"file": rel, "tool": op.tool, "error": op.error})
        manifest["files"].append(entry)

    # Every Bash / PowerShell command, numbered in the order they ran.
    width = max(4, len(str(len(session.commands))))
    command_files: dict[int, str] = {}
    for i, op in enumerate(session.commands):
        ext = SHELL_TOOLS.get(op.tool, "txt")
        rel = out.write(
            PurePosixPath("commands") / f"{tool_slug(op.tool)}_{i:0{width}d}.{ext}",
            _command_text(op, i, ext),
        )
        command_files[i] = rel
        manifest["commands"].append(
            {"file": rel, "tool": op.tool, "timestamp": op.timestamp, "status": "error" if op.is_error else "ok"}
        )

    # Code passed inline to interpreters, named after the command that ran it.
    seen: dict[int, int] = {}
    for run in session.scripts:
        n = seen.get(run.command_index, 0)
        seen[run.command_index] = n + 1
        suffix = f"_{n}" if n else ""
        interp = tool_slug(run.script.interpreter)
        rel = out.write(
            PurePosixPath("inline_scripts")
            / f"{interp}_{run.command_index:0{width}d}{suffix}.{run.script.lang}",
            run.script.code,
        )
        manifest["inline_scripts"].append(
            {
                "file": rel,
                "interpreter": run.script.interpreter,
                "command": command_files.get(run.command_index),
                "status": "error" if run.op.is_error else "ok",
            }
        )

    # Every other failed call: Bash and other tools grouped by tool name,
    # file tools on unselected paths by their original path.
    groups: dict[PurePosixPath, list[Operation]] = {}
    for op in _other_errors(session, files):
        if op.tool in FILE_TOOLS and op.path:
            st = session.files.get(op.path)
            category = st.category if st else None
            key = output_dir_for(op.path, category, session.root) / PurePosixPath(op.path.replace("\\", "/")).name
        else:
            key = PurePosixPath(tool_slug(op.tool), tool_slug(op.tool))
        groups.setdefault(key, []).append(op)
    for key, ops in groups.items():
        for name, op in zip(versioned_names(key.name, len(ops)), ops):
            rel = out.write(
                PurePosixPath("errored") / key.parent / f"{name}.json",
                json.dumps(_error_record(op), indent=2, ensure_ascii=False) + "\n",
            )
            manifest["errored"].append({"file": rel, "tool": op.tool, "error": op.error})

    # Files commands wrote that we can't reconstruct: listed, pointing at the command.
    index_of = {id(op): i for i, op in enumerate(session.commands)}
    lines = []
    for pr in _produced(session, args):
        cmds = [command_files[index_of[id(o)]] for o in pr.ops if id(o) in index_of]
        manifest["produced_without_content"].append(
            {"path": pr.path, "category": pr.category, "how": pr.how, "deleted": pr.deleted, "commands": cmds}
        )
        lines.append(f"{pr.path}\t{'; '.join(dict.fromkeys(pr.how))}\t{', '.join(dict.fromkeys(cmds))}")
    if lines:
        out.write(
            PurePosixPath("produced_without_content.tsv"),
            "path\thow\tcommands\n" + "\n".join(lines) + "\n",
        )

    out.write(PurePosixPath("manifest.json"), json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(
        f"wrote {sum(len(f['versions']) for f in manifest['files'])} file version(s), "
        f"{len(manifest['commands'])} command(s), {len(manifest['inline_scripts'])} inline script(s) "
        f"and {len(manifest['errored'])} errored call(s) to {out.root}",
        file=sys.stderr,
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    _, files = _load(args)
    root = Path(args.root) if args.root else None
    mismatches = 0
    for st in files.values():
        if st.deleted:
            continue  # cleaned up on purpose; nothing to compare
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
            "Reconstruct everything a Claude Code session wrote or ran (scratchpad, "
            "/tmp and temporary files in the working directory, shell commands, "
            "inline scripts and failed tool calls) from its .jsonl transcript."
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
        help="write files, commands, inline scripts and errors to OUTPUT/<session_id>/",
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
