#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copy the cleaned working tree (tracked + unignored files only) into a new directory with a single initial commit,
# so the public repository starts from a fresh history. Pushes nothing; publishing is a human decision.
#
#   scripts/export-public.sh ../feederline-mcp-public
set -euo pipefail

dest="${1:?usage: scripts/export-public.sh <empty-destination-dir>}"
src="$(git rev-parse --show-toplevel)"

if [ -e "$dest" ] && [ -n "$(ls -A "$dest" 2>/dev/null)" ]; then
  echo "destination $dest exists and is not empty" >&2
  exit 1
fi
mkdir -p "$dest"

# Files git would publish: tracked plus untracked-but-not-ignored. Ignored files (CLAUDE.md, docs/, data/...) stay out.
(cd "$src" && git ls-files -z --cached --others --exclude-standard) | (cd "$src" && xargs -0 tar -cf - --) | (cd "$dest" && tar -xf -)

"$src/scripts/check-platform-refs.sh" "$dest"

cd "$dest"
git init -q -b main
git add -A
git -c user.name="${GIT_AUTHOR_NAME:-Kartha Energy}" -c user.email="${GIT_AUTHOR_EMAIL:-noreply@kartha.energy}" \
  commit -q -s -m "feederline-mcp: initial public release"
echo "exported to $dest ($(git ls-files | wc -l | tr -d ' ') files, 1 commit). Nothing was pushed."
if command -v gitleaks >/dev/null; then gitleaks detect --source . --redact; else echo "gitleaks not installed; run it before publishing" >&2; fi
