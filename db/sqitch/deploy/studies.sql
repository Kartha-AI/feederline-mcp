-- SPDX-License-Identifier: Apache-2.0
-- Deploy opendss_cim:studies
BEGIN;
CREATE SCHEMA studies;
CREATE TABLE studies.models (
    model_id UUID PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), principal TEXT NOT NULL,
    root_mrid UUID NOT NULL, root_name TEXT, case_kind TEXT NOT NULL, region_code TEXT NOT NULL,
    cim_updated_at TIMESTAMPTZ NOT NULL, telemetry_ts TIMESTAMPTZ, compile_report JSONB NOT NULL
);
CREATE INDEX idx_models_root ON studies.models (root_mrid, created_at DESC);
CREATE TABLE studies.results (
    study_id UUID PRIMARY KEY, model_id UUID NOT NULL REFERENCES studies.models(model_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), tool TEXT NOT NULL, inputs JSONB NOT NULL,
    data JSONB NOT NULL, warnings JSONB NOT NULL DEFAULT '[]', duration_ms INT
);
CREATE TABLE studies.sessions (
    session_id TEXT PRIMARY KEY, principal TEXT NOT NULL, started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ,
    tool_log JSONB NOT NULL DEFAULT '[]'
);
COMMIT;
