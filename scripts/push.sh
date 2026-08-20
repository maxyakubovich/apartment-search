#!/usr/bin/env bash
# Push local work past the watcher's own state commits.
#
# The live loop commits state/seen.json from CI, so a plain `git push` from a
# laptop is rejected whenever the bot got there first. Rebasing local commits
# on top of it is always the right move — the two never touch the same files.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "Fetching..."
git pull --rebase --autostash origin main
echo "Pushing..."
git push origin main
echo "✓ Pushed. $(git log --oneline -1)"
