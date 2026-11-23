-- SPDX-License-Identifier: Apache-2.0
-- Deploy opendss_cim:hifld_staging
-- Raw HIFLD-schema features as fetched; loaders.hifld merges them into CIM.
BEGIN;
CREATE TABLE staging_hifld_substations (
    id TEXT PRIMARY KEY, name TEXT, owner TEXT, state TEXT, county TEXT, max_voltage DOUBLE PRECISION,
    status TEXT, props JSONB NOT NULL DEFAULT '{}', geom GEOMETRY(Point, 4326) NOT NULL
);
CREATE TABLE staging_hifld_lines (
    id TEXT PRIMARY KEY, owner TEXT, voltage DOUBLE PRECISION, volt_class TEXT, type TEXT, status TEXT,
    props JSONB NOT NULL DEFAULT '{}', geom GEOMETRY(MultiLineString, 4326) NOT NULL
);
CREATE INDEX idx_stg_sub_geog ON staging_hifld_substations USING GIST ((geom::geography));
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON staging_hifld_substations, staging_hifld_lines TO loader;
COMMIT;
