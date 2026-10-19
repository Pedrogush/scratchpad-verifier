# scratchpad-verifier

A small, dependency-free Python CLI that reads a Claude Code session transcript
(`~/.claude/projects/<project>/<session-id>.jsonl`) and pulls out everything
the agent wrote or ran during that session: scratchpad and `/tmp` files,
temporary files it created in the working directory and cleaned up, every
shell command, every inline script, and every tool call that failed.

Agents create most files through the shell rather than the `Write` tool
(`cat > f <<'EOF'`, `python - <<'EOF'`, `cp`, redirects), so the tool replays
both: `Write`/`Edit`/`MultiEdit` calls and a static analysis of each `Bash` and
`PowerShell` command. Every file gets a numbered copy per iteration, so you can
see how a script evolved between runs even after the temp directory is gone.

## Install

### With uv (recommended)

Install it once as a global command, in its own isolated environment:

```bash
uv tool install git+https://github.com/Pedrogush/scratchpad-verifier
```

If your shell then can't find `scratchpad-verifier`, run `uv tool update-shell`
once and open a new terminal (it adds uv's tool directory, usually
`~/.local/bin`, to your `PATH`).

```bash
uv tool upgrade scratchpad-verifier     # pull the latest version
uv tool uninstall scratchpad-verifier   # remove it
```

Or run it without installing:

```bash
uvx --from git+https://github.com/Pedrogush/scratchpad-verifier scratchpad-verifier list <session-id>
```

From a local clone, use `uv tool install .` (add `--editable` while hacking
on it).

### With pip

```bash
pip install git+https://github.com/Pedrogush/scratchpad-verifier
# or, from a clone
pip install .
```

Requires Python 3.9+. No runtime dependencies.

## Usage

The `TRANSCRIPT` argument is a path to a `.jsonl` file, or a bare session id
(looked up under `~/.claude/projects/*/`, or `$CLAUDE_CONFIG_DIR/projects`).
Subagent transcripts stored next to it (`<session-id>/subagents/*.jsonl`) are
included automatically.

```bash
# Overview: files (category, status, versions), files commands produced,
# command counts and failed tool calls
scratchpad-verifier list 1234abcd
scratchpad-verifier list 1234abcd --json

# Print one reconstructed file (or all of them, with headers)
scratchpad-verifier show 1234abcd /tmp/claude-0/.../scratchpad/plan.md

# Write everything under ./recovered/<session_id>/
scratchpad-verifier extract 1234abcd -o ./recovered

# Compare final versions with what's on disk;
# exits 1 if anything is MISSING / DIFFERS / UNKNOWN
scratchpad-verifier verify 1234abcd
```

`python -m scratchpad_verifier ...` works too.

### Output layout

```
recovered/
└── 1234abcd/                          # session id (transcript file name)
    ├── scratchpad/                    # files inside .../scratchpad/
    │   ├── plan_00.md                 # first write
    │   ├── plan_01.md                 # after the next edit / rewrite
    │   ├── log_02_branch_0.md         # a change that may not have run: one file
    │   ├── log_02_branch_1.md         #   per possible content (see Branches)
    │   └── old_00.md.patch            # edit to a file whose content was never seen
    ├── tmp/                           # /tmp and other temp dirs
    │   └── build/run_00.sh
    ├── ran_from_root/                 # temp files created in the working
    │   └── check_tmp_00.py            #   directory and deleted afterwards
    ├── commands/                      # every Bash / PowerShell call, in order
    │   ├── bash_0000.sh               #   (header: time, cwd, description, status)
    │   └── bash_0001.sh
    ├── inline_scripts/                # code run inline, named after its command
    │   └── python3_0001.py            #   (python - <<EOF, python -c, node -e, ...)
    ├── errored/                       # every tool call that returned an error
    │   ├── scratchpad/plan_00.md.json #   failed Write/Edit, kept with the file
    │   ├── bash/bash_00.json          #   failed commands, in order
    │   └── mcp__github__get_me/...    #   any other tool, one folder per tool
    ├── produced_without_content.tsv   # files commands wrote that can't be rebuilt
    └── manifest.json                  # index of all of the above
```

**Versions.** The first write of a file is `_00`; every later `Write`, `Edit`,
`MultiEdit`, heredoc, `echo >`/`>>`, `sed -i` or `cp` onto it adds the next number. If
the first change edits a file that existed before the session, its original
content is `_00`. When the transcript doesn't record that original (newer
Claude Code versions often don't), the last full `Read` of the file is used
instead, and a `Read` that shows content different from what the replay
expects (a script or formatter changed it) becomes a version of its own.
Edits to a file whose content never appears are written as `.patch` files.

**Branches.** When the transcript can't tell whether a change ran (a write or
`sed -i` inside `if`/`else` or a loop, after `||`, or after `cmd &&` when `cmd`
could have failed), the file forks: one branch where the change ran, one where
it didn't, and every later change applies to each branch. Versions before the
fork are written as usual; after it, each branch's final content is written as
`<name>_<NN>_branch_<k>`, where `NN` is the first uncertain version. Branch 0
assumes everything ran. `manifest.json` lists each branch's `choices` (e.g.
`"skipped: echo in command 4"`, numbered like `commands/`). A fork closes
again when a full `Read` or a later `cat` shows which branch is real, when an
`Edit` succeeds whose `old_string` only one branch contains, or when the file
is overwritten. `verify` accepts a file that matches any branch.

**ran_from_root.** A file lands here when the session created it under the
directory it started in (the transcript's `cwd`) and later removed it with
`rm`, `del` or `Remove-Item`.

**Errors.** Each file in `errored/` is a JSON record with the tool name, file
path (if any), timestamp, cwd, the error message (for Bash: exit code and
output) and the full tool input. Failed calls never create a version. Commands
that failed after writing a file (e.g. `cat > t.py <<EOF ... && python t.py`)
still count as writing it; commands the user denied, interrupted, or that were too long to start
(`ENAMETOOLONG`) don't.

**Produced without content.** Some files have no content in the transcript:
what a `python - <<EOF` script wrote with `open(..., 'w')`, `.to_csv()` or
`.save()`, output redirected from a program (`python x.py > out.log`), files a
script on disk wrote (`python build.py`), `sed -i` scripts that can't be
replayed, copies of unknown files.
These are listed with the command that produced them, so they stay observable.

The list also includes files known only by name: paths in a script's output
flag (`--output`, `-o`, `--path` on screenshot/export commands), outputs of a
script run with arguments (`python gen.py /tmp/out.csv` where `gen.py` writes
`sys.argv[1]`), and scratchpad paths that appear in command output, in the
model's own messages or in background-task notifications.

### What's recognized in shell commands

- heredocs: `cat > f <<'EOF'`, `cat <<EOF > f`, `cat <<EOF | tee [-a] f`
- `echo` (`-n`, `-e`) / `printf` (`%s`, `%b`, `%d`) with `>` or `>>`, `touch`
- `sed -i` edits, replayed on the known content: `s` (flags `g`, `N`, `I`, `p`),
  `d`, `p`, `a`/`i`/`c`, with line, `$`, `/regex/` and range addresses; basic and
  extended (`-E`) regexes, `-e`, `-i.bak` backups
- `cp` / `mv` (content follows the file when the source is known), `mkdir -p`
- `rm`, `unlink`, `rmdir`, globs included
- `cd` and `VAR=value` / `export VAR=value` earlier in the same command, so
  `S=/tmp/x; cat > $S/a.py` and `cd dir && cat > a.py` resolve correctly
- inline code: `python - <<EOF`, `python -c`, `node -e`, `bash -c`, `uv run python -`, ...
- `git rm`, `git worktree remove`
- scripts run from a file (`python x.py`, `node x.js`, `bash x.sh`, `$PY x.py`):
  the outputs that script writes are listed, with `sys.argv` filled in
- PowerShell: `$var = "..."` and `$env:VAR`, `Set-Content` / `Add-Content` /
  `Out-File` / `Tee-Object` with here-strings, `>` / `>>` redirects,
  `Copy-Item` / `Move-Item`, `Remove-Item`

Windows paths are normalized, so `C:\Users\...\scratchpad\a.md` (from `Write`)
and `/c/Users/.../scratchpad/a.md` (from git-bash) are the same file.

### Which files are included

By default: scratchpad files, temp-dir files (`/tmp`, `/var/tmp`, macOS
`/var/folders/.../T`, Windows `%TEMP%`) and `ran_from_root` files. Files
written anywhere else (your repo, deliverables) are counted in `list` but only
extracted when asked for:

| Option | Effect |
| --- | --- |
| `--prefix PATH` | also include paths under `PATH` (repeatable) |
| `--marker TEXT` | also include paths containing `TEXT` (repeatable) |
| `--all` | include every file written in the session, under `other/` |
| `--no-subagents` | skip subagent transcripts |

### Status values

- `ok`: content fully reconstructed.
- `branched`: some changes may not have run, so the file has several possible
  contents (see Branches above).
- `partial`: something changed the file in a way the transcript doesn't show
  (a script, a `sed -i` script that can't be replayed, an edit whose
  `old_string` didn't match), or its content has a `$var` / `$(...)` whose
  value is unknown, so the last version is the last known content. A later
  `cat` of the file (or a full `Read`) that fits only one possibility makes it
  `ok` again.
- `unknown`: the file was only edited, never written, and the transcript did
  not record its original content.

### Limitations

This is static analysis: nothing is executed. Shell expansion in unquoted
heredocs (`<<EOF`) is not applied (the version's `note` in `manifest.json`
says so), loops and functions aren't followed, and binary files produced by
programs (images, spreadsheets) are listed, not recovered.

## Development

```bash
python -m unittest discover -s tests
```

### Checking against real sessions

`tests/meta/record_test_session.sh` starts headless Claude Code sessions
(`claude -p`) that clone this repo into an empty scratchpad, edit it at random
with hypothesis, and save the scratchpad and transcript under
`tests/meta/test_claude_code_sessions/`. One session writes and revises its
scripts as files (heredoc, Write, Edit, `sed -i`, `cp`, `rm`); the other runs
everything inline. `tests/meta/test_claude_code_sessions.py` then checks
every file left in each scratchpad: the tool must rebuild the same content
(line endings aside) or at least list the file by name. A file it doesn't know
about, or rebuilds wrongly, fails the test.

```bash
tests/meta/record_test_session.sh            # both modes; or: scripts / inline
python tests/meta/test_claude_code_sessions.py -v
```

The sessions run Bash without asking, on your machine. Secrets in the saved
transcripts are redacted, but review a recording before committing it.

## License

MIT
