-- SPDX-License-Identifier: Apache-2.0
-- Deploy opendss_cim:scada_telemetry
BEGIN;

CREATE TABLE measurement (
    mrid UUID PRIMARY KEY, name TEXT NOT NULL, measurement_type TEXT NOT NULL,
    phases TEXT NOT NULL DEFAULT 'ABC', unit_symbol TEXT NOT NULL, unit_multiplier TEXT NOT NULL DEFAULT 'none',
    power_system_resource_mrid UUID NOT NULL REFERENCES conducting_equipment(mrid), terminal_mrid UUID REFERENCES terminal(mrid),
    cim_class TEXT NOT NULL CHECK (cim_class IN ('Analog','Discrete','Accumulator')), region_code TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_measurement_psr ON measurement (power_system_resource_mrid, measurement_type);
CREATE TABLE analog   (mrid UUID PRIMARY KEY REFERENCES measurement(mrid), min_value DOUBLE PRECISION, max_value DOUBLE PRECISION,
                       normal_value DOUBLE PRECISION, positive_flow_in BOOLEAN NOT NULL DEFAULT true);
CREATE TABLE discrete (mrid UUID PRIMARY KEY REFERENCES measurement(mrid), min_value INT, max_value INT, normal_value INT, value_aliases JSONB);
CREATE TABLE remote_unit (mrid UUID PRIMARY KEY, name TEXT NOT NULL, remote_unit_type TEXT NOT NULL,
                          substation_mrid UUID REFERENCES substation(mrid), region_code TEXT NOT NULL);
CREATE TABLE remote_source (
    mrid UUID PRIMARY KEY, name TEXT UNIQUE NOT NULL,                      -- the feed's tag
    measurement_mrid UUID UNIQUE NOT NULL REFERENCES measurement(mrid),
    remote_unit_mrid UUID NOT NULL REFERENCES remote_unit(mrid),
    scan_interval NUMERIC(8,3), deadband DOUBLE PRECISION, sensor_accuracy DOUBLE PRECISION
);

-- quality: 0 good; bit0 questionable, bit1 invalid, bit2 substituted, bit3 old, bit4 out of range, bit5 blocked, bit6 test
-- source: 1 SCADA, 2 AMI, 3 PMU, 4 Estimator, 5 Manual, 6 Simulator
CREATE TABLE measurement_value (
    ts TIMESTAMPTZ NOT NULL, measurement_mrid UUID NOT NULL,
    value DOUBLE PRECISION NOT NULL, quality SMALLINT NOT NULL DEFAULT 0, source SMALLINT NOT NULL DEFAULT 1,
    PRIMARY KEY (measurement_mrid, ts)
) PARTITION BY RANGE (ts);
CREATE TABLE measurement_value_latest (
    measurement_mrid UUID PRIMARY KEY REFERENCES measurement(mrid),
    ts TIMESTAMPTZ NOT NULL, value DOUBLE PRECISION NOT NULL, quality SMALLINT NOT NULL, source SMALLINT NOT NULL
);

-- Daily partitions measurement_value_YYYY_MM_DD; ingest.rotate keeps D+1, D+2 ahead.
CREATE FUNCTION ensure_measurement_value_partition(d DATE) RETURNS TEXT LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE part TEXT := format('measurement_value_%s', to_char(d, 'YYYY_MM_DD'));
BEGIN
    EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF measurement_value FOR VALUES FROM (%L) TO (%L)',
                   part, d::timestamptz, (d + 1)::timestamptz);
    RETURN part;
END $$;
DO $$ BEGIN PERFORM ensure_measurement_value_partition((current_date + i)::date) FROM generate_series(-1, 2) AS i; END $$;

-- Rollup, local definition (another deployment may provide it as a plain table; the name and columns are the contract)
CREATE MATERIALIZED VIEW measurement_value_15m AS
SELECT date_bin('15 minutes', ts, TIMESTAMPTZ '2000-01-01') AS bucket, measurement_mrid,
       avg(value) AS avg_value, min(value) AS min_value, max(value) AS max_value,
       (array_agg(value ORDER BY ts DESC))[1] AS last_value,
       count(*) AS samples, count(*) FILTER (WHERE quality <> 0) AS bad_samples
FROM measurement_value
WHERE ts >= now() - INTERVAL '90 days'
GROUP BY 1, 2;
CREATE UNIQUE INDEX measurement_value_15m_key ON measurement_value_15m (measurement_mrid, bucket DESC);

COMMIT;
