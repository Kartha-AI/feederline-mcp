-- SPDX-License-Identifier: Apache-2.0
-- Deploy opendss_cim:roles_rls
BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'loader')       THEN CREATE ROLE loader NOLOGIN BYPASSRLS; END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'writer')       THEN CREATE ROLE writer NOLOGIN BYPASSRLS; END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'study_reader') THEN CREATE ROLE study_reader NOLOGIN; END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'study_writer') THEN CREATE ROLE study_writer NOLOGIN; END IF;
END $$;

GRANT USAGE ON SCHEMA public TO loader, writer, study_reader;
GRANT USAGE ON SCHEMA studies TO study_writer, study_reader;

-- loader: owns the model content
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO loader;
GRANT SELECT ON measurement_value_15m TO loader;
GRANT pg_maintain TO loader;                         -- ANALYZE after loads (PG17+)

-- writer: telemetry ingestion (samples, latest, switch state, partitions)
GRANT SELECT ON ALL TABLES IN SCHEMA public TO writer;
GRANT INSERT ON measurement_value TO writer;
GRANT INSERT, UPDATE ON measurement_value_latest TO writer;
GRANT UPDATE (open, open_ts) ON switch TO writer;
GRANT EXECUTE ON FUNCTION ensure_measurement_value_partition(DATE) TO writer;

-- study_reader: the MCP server's read access to the model and telemetry
GRANT SELECT ON ALL TABLES IN SCHEMA public TO study_reader;
GRANT SELECT ON measurement_value_15m TO study_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA studies TO study_reader;

-- study_writer: the MCP server writes only to studies.*
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA studies TO study_writer;

-- region_isolation: app.region_codes is a comma list set per transaction by DataSource.query(); '*' means all regions.
CREATE FUNCTION region_visible(code TEXT) RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('app.region_codes', true), '') = '*'
        OR code = ANY (string_to_array(coalesce(current_setting('app.region_codes', true), ''), ','))
$$;

ALTER TABLE substation           ENABLE ROW LEVEL SECURITY;
ALTER TABLE conducting_equipment ENABLE ROW LEVEL SECURITY;
ALTER TABLE connectivity_node    ENABLE ROW LEVEL SECURITY;
ALTER TABLE measurement          ENABLE ROW LEVEL SECURITY;
ALTER TABLE remote_unit          ENABLE ROW LEVEL SECURITY;

CREATE POLICY region_isolation ON substation           USING (region_visible(region_code));
CREATE POLICY region_isolation ON conducting_equipment USING (region_visible(region_code));
CREATE POLICY region_isolation ON connectivity_node    USING (region_visible(region_code));
CREATE POLICY region_isolation ON measurement          USING (region_visible(region_code));
CREATE POLICY region_isolation ON remote_unit          USING (region_visible(region_code));

COMMIT;
