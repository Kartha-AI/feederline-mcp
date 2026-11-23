# SPDX-License-Identifier: Apache-2.0
"""The Python package, the npm package and (on a tag build) the git tag must carry the same version."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    py = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    npm = json.loads((ROOT / "npm" / "package.json").read_text())["version"]
    versions = {"pyproject.toml": py, "npm/package.json": npm}
    ref = os.environ.get("GITHUB_REF", "")
    if ref.startswith("refs/tags/v"):
        versions["git tag"] = ref.removeprefix("refs/tags/v")
    if len(set(versions.values())) != 1:
        sys.stderr.write(f"version mismatch: {versions}\n")
        return 1
    sys.stdout.write(f"version {py} consistent across {', '.join(versions)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
