import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st, HealthCheck

WT = Path(sys.argv[1])
if not (WT / ".git").exists():
    sys.exit(f"not a git worktree: {WT}")
TRACKED = subprocess.run(
    ["git", "-C", str(WT), "ls-files"], capture_output=True, text=True, check=True
).stdout.splitlines()

text_st = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=1, max_size=40)
action_st = st.sampled_from(["delete", "append", "replace"])
plan_st = st.lists(
    st.tuples(st.sampled_from(TRACKED), action_st, text_st, st.integers(min_value=0, max_value=10**6)),
    min_size=5,
    max_size=15,
    unique_by=lambda t: t[0],
)


@settings(database=None, max_examples=1, suppress_health_check=list(HealthCheck), derandomize=False)
@given(plan_st)
def mutate(plan):
    print(f"planned changes: {len(plan)}")
    for rel, action, text, n in plan:
        path = WT / rel
        if not path.exists():
            continue
        if action == "delete":
            path.unlink()
        elif action == "append":
            with path.open("a", encoding="utf-8", newline="") as f:
                f.write("\n" + text + "\n")
        else:
            try:
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                continue
            if not lines:
                lines = [text + "\n"]
            else:
                lines[n % len(lines)] = text + "\n"
            path.write_text("".join(lines), encoding="utf-8", newline="")
        print(f"{action}: {rel}")


mutate()
