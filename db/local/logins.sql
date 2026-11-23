-- SPDX-License-Identifier: Apache-2.0
-- Local-only login users for the compose stack. Other deployments map their own identities onto these roles.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mcp')        THEN CREATE ROLE mcp LOGIN PASSWORD 'mcp'; END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grid_load')  THEN CREATE ROLE grid_load LOGIN BYPASSRLS PASSWORD 'grid_load'; END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grid_write') THEN CREATE ROLE grid_write LOGIN BYPASSRLS PASSWORD 'grid_write'; END IF;
END $$;
GRANT study_reader, study_writer TO mcp;
GRANT loader TO grid_load;
GRANT writer TO grid_write;
-- Role attributes are not inherited through membership; re-assert on existing roles.
ALTER ROLE grid_load BYPASSRLS;
ALTER ROLE grid_write BYPASSRLS;
-- /healthz and `feederline-mcp-admin status` report the deployed migration from the sqitch registry.
GRANT USAGE ON SCHEMA sqitch TO study_reader;
GRANT SELECT ON sqitch.changes TO study_reader;
