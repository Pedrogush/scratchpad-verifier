import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st

WORKTREE = Path(sys.argv[1])

tracked = subprocess.run(
    ["git", "-C", str(WORKTREE), "ls-files"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()
if len(tracked) < 5:
    sys.exit(f"need at least 5 tracked files, found {len(tracked)}")

changes = []


@st.composite
def plans(draw):
    files = draw(st.lists(st.sampled_from(tracked), min_size=5, max_size=15, unique=True))
    plan = []
    for f in files:
        action = draw(st.sampled_from(["delete", "append", "replace"]))
        text = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=5, max_size=40))
        pick = draw(st.integers(min_value=0, max_value=10_000))
        plan.append((f, action, text, pick))
    return plan


@settings(database=None, max_examples=1)
@given(plans())
def apply_plan(plan):
    for rel, action, text, pick in plan:
        path = WORKTREE / rel
        if action == "delete":
            path.unlink()
            changes.append(f"deleted {rel}")
            continue
        data = path.read_bytes().decode("utf-8", errors="replace")
        if action == "append":
            path.write_text(data + "\n" + text + "\n", encoding="utf-8")
            changes.append(f"appended to {rel}: {text!r}")
        else:
            lines = data.splitlines()
            if not lines:
                lines = [""]
            idx = pick % len(lines)
            lines[idx] = text
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            changes.append(f"replaced line {idx + 1} of {rel}: {text!r}")


apply_plan()
print(f"{len(changes)} changes in {WORKTREE}:")
for c in changes:
    print(c)
