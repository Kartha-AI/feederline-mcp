-- SPDX-License-Identifier: Apache-2.0
-- Deploy opendss_cim:sql_tools_indexes
-- Name/tag search (search_equipment, resolve_mrid) and model_changes. No columns, roles, grants or policies.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_ce_name_trgm ON conducting_equipment USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_rs_name_trgm ON remote_source USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_ce_updated ON conducting_equipment (updated_at) WHERE valid_to IS NULL;
COMMIT;
