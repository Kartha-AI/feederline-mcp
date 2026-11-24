# Changelog

## 0.1.1 (2026-11-23)

- Server image includes the `kafka` extra, so the optional `kafka-writer` service starts.
- CLI accepts Docker Compose v2 and later (v5 was rejected).
- README: architecture and tools diagrams, Claude Desktop screenshots.

## 0.1.0 (2026-11-23)

First public release.

- CIM operational data store (Postgres + PostGIS + pgRouting) and 34 MCP tools (engine and SQL families).
- npm CLI `@kartha-energy/feederline-mcp`: `up`, `down`, `reset`, `status`, `logs`, `load`, `config`, `connect`,
  `inspect`, `doctor`.
- `feederline-mcp-admin` in the server image: `migrate`, `load`, `simulate`, `refresh-rollups`, `status`.
- Adapters discovered through the `feederline_mcp.adapters` entry-point group (`FEEDERLINE_MCP_ADAPTER`).
