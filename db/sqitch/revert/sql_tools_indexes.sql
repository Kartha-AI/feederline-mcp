-- SPDX-License-Identifier: Apache-2.0
-- Revert opendss_cim:sql_tools_indexes (pg_trgm stays: other objects may use it)
BEGIN;
DROP INDEX IF EXISTS idx_ce_name_trgm;
DROP INDEX IF EXISTS idx_rs_name_trgm;
DROP INDEX IF EXISTS idx_ce_updated;
COMMIT;
