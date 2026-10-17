# scratchpad-verifier

A small, dependency-free Python CLI that reads a Claude Code session transcript
(`~/.claude/projects/<project>/<session-id>.jsonl`) and reconstructs the files
the agent wrote to `/tmp` and scratchpad directories during that session.

It replays every `Write`, `Edit` and `MultiEdit` tool call in order and keeps a
numbered copy of each file after every iteration, so you can see exactly how a
script evolved between runs, even after the container or temp directory is
gone. It can also check whether what's on disk still matches what the agent
wrote.

## Install

```bash
pip install git+https://github.com/Pedrogush/scratchpad-verifier
# or, from a clone
pip install .
```

Requires Python 3.9+. No runtime dependencies.

## Usage

The `TRANSCRIPT` argument is a path to a `.jsonl` file, or a bare session id
(looked up under `~/.claude/projects/*/`, or `$CLAUDE_CONFIG_DIR/projects`).

```bash
# List reconstructed files with status, size and the tool calls that touched them
scratchpad-verifier list ~/.claude/projects/-home-me-app/1234abcd.jsonl
scratchpad-verifier list 1234abcd --json

# Print one file (or all of them, with headers)
scratchpad-verifier show 1234abcd /tmp/claude-0/.../scratchpad/plan.md
scratchpad-verifier show 1234abcd

# Write every version of every file under ./recovered/<session_id>/
scratchpad-verifier extract 1234abcd -o ./recovered

# Compare the final versions with what's on disk;
# exits 1 if anything is MISSING / DIFFERS / UNKNOWN
scratchpad-verifier verify 1234abcd
```

### Output layout

`extract` writes one file per iteration: the first `Write` is `_00`, and every
later `Edit`, `MultiEdit` or overwriting `Write` adds the next number. Files are
grouped by where they lived:

```
recovered/
└── 1234abcd/                      # session id (transcript file name)
    ├── scratchpad/                # paths inside .../scratchpad/
    │   ├── plan_00.md
    │   └── plan_01.md
    ├── tmp/                       # other paths under /tmp/
    │   └── build/
    │       ├── run_00.sh          # first Write
    │       ├── run_01.sh          # after the first Edit
    │       └── run_02.sh          # after the second Edit
    └── errored/                   # tool calls that returned an error
        └── tmp/build/
            └── run_00.sh.json     # first failed call on run.sh
```

Each file in `errored/` is a JSON record of one failed `Write`, `Edit` or
`MultiEdit` call: the tool name, file path, timestamp, the error message and
the full tool input (`content`, or `old_string`/`new_string`/`edits`). Failed
calls are numbered per file in the same way and don't create a version.

Subdirectories below the scratchpad or `/tmp` are kept. Files matched only via
`--prefix`, `--marker` or `--all` go under `other/<original path>`. If an edit
targets a file that existed before the session, its original content is `_00`.

`python -m scratchpad_verifier ...` works too.

### Which files are included

By default, any path that starts with `/tmp/` or contains `/scratchpad/`.
Widen the filter with:

| Option | Effect |
| --- | --- |
| `--prefix PATH` | also include paths under `PATH` (repeatable) |
| `--marker TEXT` | also include paths containing `TEXT` (repeatable) |
| `--all` | include every file written in the session |
| `--no-subagents` | skip subagent transcripts in `<session-id>/subagents/*.jsonl` |

### Status values

- `ok`: content fully reconstructed.
- `partial`: an `Edit` could not be applied (its `old_string` wasn't found), so
  the content reflects the state before that edit.
- `unknown`: the file was only edited, never written, and the transcript did
  not record its original content.

Tool calls whose result was an error are skipped. When an `Edit` targets a
file that was never written in the session, the original content recorded in
the tool result (`toolUseResult.originalFile`) is used as the starting point.

### Limitations

Only the file tools are replayed. Files created through `Bash` (`echo >`,
heredocs, scripts) or by programs the agent ran can't be reconstructed from the
transcript, and won't appear in the output.

## Development

```bash
python -m unittest discover -s tests
```

## License

MIT
