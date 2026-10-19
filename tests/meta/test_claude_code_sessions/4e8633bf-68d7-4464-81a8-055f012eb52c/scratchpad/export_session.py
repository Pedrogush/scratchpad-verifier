"""Copy the scratchpad (minus clone/worktree/.venv) and the session transcript
into tests/meta/test_claude_code_sessions/ under the current directory."""
import os
import shutil
import sys
from pathlib import Path

SP = Path("C:/Users/Pedro/AppData/Local/Temp/scratchpad-verifier-meta/4e8633bf-68d7-4464-81a8-055f012eb52c/scratchpad")
SID = "4e8633bf-68d7-4464-81a8-055f012eb52c"
EXCLUDE = {"clone", "worktree", ".venv"}
DEST = Path.cwd() / "tests" / "meta" / "test_claude_code_sessions"


def skip_top_level(directory, names):
    # Only exclude the three directories at the top of SP, not same-named entries deeper down.
    return [n for n in names if n in EXCLUDE] if Path(directory) == SP else []


def find_transcript():
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(config_dir) if config_dir else Path.home() / ".claude"
    matches = list((root / "projects").rglob(f"{SID}.jsonl"))
    if not matches:
        sys.exit(f"transcript {SID}.jsonl not found under {root / 'projects'}")
    return matches[0]


scratch_dest = DEST / SID / "scratchpad"
shutil.copytree(SP, scratch_dest, ignore=skip_top_level, dirs_exist_ok=True)
for p in sorted(scratch_dest.rglob("*")):
    print("copied", p.relative_to(DEST))

transcript = find_transcript()
shutil.copy2(transcript, DEST / f"{SID}.jsonl")
print("copied transcript to", (DEST / f"{SID}.jsonl").relative_to(Path.cwd()))
