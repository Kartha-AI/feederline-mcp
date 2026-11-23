# Changelog

## 0.1.0 (unreleased)

First public release.

- CIM operational data store (Postgres + PostGIS + pgRouting) and 34 MCP tools (engine and SQL families).
- npm CLI `@kartha-energy/feederline-mcp`: `up`, `down`, `reset`, `status`, `logs`, `load`, `config`, `connect`,
  `inspect`, `doctor`.
- `feederline-mcp-admin` in the server image: `migrate`, `load`, `simulate`, `refresh-rollups`, `status`.
- Adapters discovered through the `feederline_mcp.adapters` entry-point group (`FEEDERLINE_MCP_ADAPTER`).
