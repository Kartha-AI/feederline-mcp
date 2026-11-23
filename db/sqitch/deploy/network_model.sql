-- SPDX-License-Identifier: Apache-2.0
-- Deploy opendss_cim:network_model
BEGIN;

CREATE TABLE location (
    mrid      UUID PRIMARY KEY,
    name      TEXT,
    geom      GEOMETRY(Geometry, 4326) NOT NULL,
    geom_proj GEOMETRY(Geometry, 26911) GENERATED ALWAYS AS (ST_Transform(geom, 26911)) STORED,
    CONSTRAINT chk_geom_type CHECK (GeometryType(geom) IN ('POINT', 'LINESTRING'))
);
CREATE TABLE base_voltage (mrid UUID PRIMARY KEY, nominal_voltage NUMERIC(8,3) NOT NULL UNIQUE);   -- kV
CREATE TABLE substation (
    mrid UUID PRIMARY KEY, name TEXT NOT NULL, description TEXT, gis_oid TEXT UNIQUE,
    region_code TEXT NOT NULL, location_mrid UUID REFERENCES location(mrid),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(), valid_to TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE voltage_level (
    mrid UUID PRIMARY KEY, name TEXT NOT NULL,
    substation_mrid UUID NOT NULL REFERENCES substation(mrid),
    base_voltage_mrid UUID NOT NULL REFERENCES base_voltage(mrid)
);
CREATE TABLE line (mrid UUID PRIMARY KEY, name TEXT NOT NULL, region_code TEXT NOT NULL);
CREATE TABLE connectivity_node (
    mrid UUID PRIMARY KEY, name TEXT, connectivity_node_container_mrid UUID, region_code TEXT NOT NULL,
    routing_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
);
CREATE TABLE conducting_equipment (
    mrid UUID PRIMARY KEY,
    cim_class TEXT NOT NULL CHECK (cim_class IN ('ACLineSegment','PowerTransformer','Breaker','Recloser','LoadBreakSwitch',
                                                 'Disconnector','Fuse','EnergyConsumer','SynchronousMachine','LinearShuntCompensator')),
    name TEXT NOT NULL, description TEXT, gis_oid TEXT UNIQUE, region_code TEXT NOT NULL,
    equipment_container_mrid UUID, base_voltage_mrid UUID REFERENCES base_voltage(mrid),
    location_mrid UUID REFERENCES location(mrid), in_service BOOLEAN NOT NULL DEFAULT true,
    attrs JSONB NOT NULL DEFAULT '{}',
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(), valid_to TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    routing_id BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE
);
-- r, x, r0, x0 are total ohms; unconstrained NUMERIC so regulator-scale impedances survive the round trip.
CREATE TABLE ac_line_segment (
    mrid UUID PRIMARY KEY REFERENCES conducting_equipment(mrid),
    length NUMERIC(12,2), r NUMERIC, x NUMERIC, r0 NUMERIC, x0 NUMERIC,
    rated_current NUMERIC(8,2), conductor_type TEXT
);
CREATE TABLE switch (
    mrid UUID PRIMARY KEY REFERENCES conducting_equipment(mrid),
    normal_open BOOLEAN NOT NULL DEFAULT false, open BOOLEAN NOT NULL DEFAULT false, open_ts TIMESTAMPTZ,
    rated_current NUMERIC(8,2)
);
CREATE TABLE power_transformer (mrid UUID PRIMARY KEY REFERENCES conducting_equipment(mrid), vector_group TEXT);
CREATE TABLE power_transformer_end (
    mrid UUID PRIMARY KEY, power_transformer_mrid UUID NOT NULL REFERENCES power_transformer(mrid),
    end_number SMALLINT NOT NULL, rated_u NUMERIC(8,3), rated_s NUMERIC(10,3), r NUMERIC, x NUMERIC,
    connection_kind TEXT, terminal_mrid UUID, UNIQUE (power_transformer_mrid, end_number)
);
CREATE TABLE energy_consumer (
    mrid UUID PRIMARY KEY REFERENCES conducting_equipment(mrid),
    customer_count INT NOT NULL DEFAULT 1, p_fixed NUMERIC(10,4), q_fixed NUMERIC(10,4), phase_connection TEXT
);
CREATE TABLE terminal (
    mrid UUID PRIMARY KEY, conducting_equipment_mrid UUID NOT NULL REFERENCES conducting_equipment(mrid),
    connectivity_node_mrid UUID REFERENCES connectivity_node(mrid),
    sequence_number SMALLINT NOT NULL, phases TEXT NOT NULL DEFAULT 'ABC'
        CHECK (phases IN ('ABC','AB','BC','AC','A','B','C')),
    UNIQUE (conducting_equipment_mrid, sequence_number)
);

CREATE INDEX idx_location_geom ON location USING GIST (geom);
CREATE INDEX idx_location_geog ON location USING GIST ((geom::geography));
CREATE INDEX idx_ce_region     ON conducting_equipment (region_code, cim_class) WHERE valid_to IS NULL;
CREATE INDEX idx_terminal_cn   ON terminal (connectivity_node_mrid);
CREATE INDEX idx_terminal_ce   ON terminal (conducting_equipment_mrid);

-- Derived by the loader after every model change
CREATE TABLE network_edges (
    id BIGINT PRIMARY KEY, equipment_mrid UUID NOT NULL, cim_class TEXT NOT NULL,
    source BIGINT NOT NULL, target BIGINT NOT NULL, cost DOUBLE PRECISION NOT NULL, reverse_cost DOUBLE PRECISION NOT NULL,
    region_code TEXT NOT NULL, grid_tier TEXT NOT NULL
);
CREATE INDEX idx_edges_region ON network_edges (region_code, grid_tier) INCLUDE (id, equipment_mrid, source, target, cost, reverse_cost);

COMMIT;
