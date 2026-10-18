#!/usr/bin/env bash
set -euo pipefail
SP="$(cd "$(dirname "$0")" && pwd)"
git clone https://github.com/Pedrogush/scratchpad-verifier "$SP/clone"
git -C "$SP/clone" worktree add -b meta-test "$SP/worktree"
echo "clone and worktree ready at $SP"
