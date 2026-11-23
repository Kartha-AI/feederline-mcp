# SPDX-License-Identifier: Apache-2.0
"""feederline-mcp: a CIM operational data store on Postgres plus an OpenDSS study engine served over MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("feederline-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+source"
