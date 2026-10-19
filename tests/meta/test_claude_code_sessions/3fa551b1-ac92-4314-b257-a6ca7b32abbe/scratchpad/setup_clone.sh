#!/usr/bin/env bash
set -euo pipefail
SP="$1"
git clone https://github.com/Pedrogush/scratchpad-verifier "$SP/clone"
git -C "$SP/clone" worktree add -b meta-test "$SP/worktree"
echo "clone and worktree ready"
