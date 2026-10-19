"""Randomly delete/edit tracked files in a git worktree using hypothesis."""
import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st, HealthCheck

WORKTREE = Path(sys.argv[1])
if not (WORKTREE / ".git").exists():
    sys.exit(f"not a git worktree: {WORKTREE}")

tracked = subprocess.run(
    ["git", "-C", str(WORKTREE), "ls-files"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()

text_st = st.text(alphabet="abcdefghijklmnopqrstuvwxyz ", min_size=5, max_size=40)
action_st = st.sampled_from(["delete", "append", "replace"])
plan_st = st.lists(
    st.tuples(st.sampled_from(tracked), action_st, text_st, st.integers(min_value=0)),
    min_size=5, max_size=15, unique_by=lambda t: t[0],
)


@settings(database=None, max_examples=1, suppress_health_check=list(HealthCheck))
@given(plan_st)
def mutate(plan):
    print(f"changing {len(plan)} of {len(tracked)} tracked files")
    for rel, action, text, n in plan:
        path = WORKTREE / rel
        if action == "delete":
            path.unlink()
        elif action == "append":
            with open(path, "a", encoding="utf-8", newline="\n") as f:
                f.write("\n" + text + "\n")
        else:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if not lines:
                lines = [text]
            else:
                lines[n % len(lines)] = text
            path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        print(f"{action}: {rel}")


mutate()
