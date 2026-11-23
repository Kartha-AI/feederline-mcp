#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Fails if the tree names a specific commercial data platform. The core stays platform-neutral; adapters for other
# platforms ship as separate packages through the `feederline_mcp.adapters` entry-point group.
#   scripts/check-platform-refs.sh [dir]   (default: files git would publish in this repo)
set -euo pipefail
pattern='databricks|lakebase|unity catalog|uc volume|/Volumes/|delta lake|lakeflow|mosaic ai|mlflow|synced table|warehouse_id|bundle-deploy|test-lakebase'
self="scripts/check-platform-refs.sh"
if [ $# -gt 0 ]; then
  cd "$1"
  files=$(find . -type f -not -path './.git/*' -not -path '*/node_modules/*' | sed 's|^\./||')
else
  files=$(git ls-files --cached --others --exclude-standard)
fi
hits=$(printf '%s\n' "$files" | grep -v -E "^(CHANGELOG\.md|NOTICE|$self|uv\.lock|npm/package-lock\.json)$" \
  | while IFS= read -r f; do [ -f "$f" ] && grep -n -i -E "$pattern" "$f" /dev/null || true; done)
if [ -n "$hits" ]; then
  printf '%s\n' "$hits"
  echo "platform-specific references found (above)" >&2
  exit 1
fi
echo "no platform-specific references"
