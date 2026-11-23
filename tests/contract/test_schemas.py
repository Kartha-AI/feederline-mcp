# SPDX-License-Identifier: Apache-2.0
"""Pydantic schemas for all tool inputs and the envelope; JSON-schema snapshot (UPDATE_SNAPSHOTS=1 to refresh)."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from feederline_mcp.studies import Envelope
from feederline_mcp.tools.engine.capacity import AnalyzeCapacityInput
from feederline_mcp.tools.engine.der import OptimizeDerInput
from feederline_mcp.tools.engine.load_feeder import LoadFeederInput
from feederline_mcp.tools.engine.power_flow import PowerFlowInput
from feederline_mcp.tools.engine.timeseries import TimeseriesInput
from feederline_mcp.tools.engine.viz import VisualizationInput
from feederline_mcp.tools.engine.voltages import CheckVoltagesInput

SCHEMAS = {
    "load_feeder": LoadFeederInput, "run_power_flow": PowerFlowInput, "check_voltages": CheckVoltagesInput,
    "analyze_capacity": AnalyzeCapacityInput, "optimize_der": OptimizeDerInput, "run_timeseries": TimeseriesInput,
    "create_visualization": VisualizationInput, "envelope": Envelope,
}
SNAPSHOT = Path(__file__).parent / "schemas.json"


def test_json_schema_snapshot():
    current = {k: v.model_json_schema() for k, v in SCHEMAS.items()}
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not SNAPSHOT.exists():
        SNAPSHOT.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n")
    assert json.loads(SNAPSHOT.read_text()) == json.loads(json.dumps(current))


@pytest.mark.parametrize("raw", [
    {"root_mrid": "not-a-uuid", "region_code": "X"},
    {"root_mrid": "c990acfa-4c84-5788-be92-53b20464fbd8", "region_code": "X; DROP TABLE"},
    {"root_mrid": "c990acfa-4c84-5788-be92-53b20464fbd8", "region_code": "X", "case": "whatif"},
])
def test_load_feeder_rejects_untyped_input(raw):
    with pytest.raises(ValidationError):
        LoadFeederInput.model_validate(raw)


def test_capacity_solve_limit():
    with pytest.raises(ValidationError):
        AnalyzeCapacityInput(bus_mrid="b", step_kw=10, max_kw=5000)
    assert AnalyzeCapacityInput(bus_mrid="b").max_kw / AnalyzeCapacityInput(bus_mrid="b").step_kw == 20


def test_limits():
    with pytest.raises(ValidationError):
        OptimizeDerInput(capacity_kw=100, candidate_buses=[str(i) for i in range(201)])
    with pytest.raises(ValidationError):
        CheckVoltagesInput(min_pu=1.1, max_pu=1.0)
    with pytest.raises(ValidationError):
        PowerFlowInput(control_mode="time")


def test_envelope_failure_carries_no_numbers():
    from feederline_mcp.studies import ErrorInfo, Meta, envelope

    e = envelope(success=False, data={"min_v_pu": 0.9}, errors=[ErrorInfo(code="not_converged", message="x", iterations=100)],
                 meta=Meta(session_id="s", engine="e", duration_ms=1, source="local"))
    d = e.model_dump(mode="json")
    assert d["data"] is None and d["errors"][0]["iterations"] == 100
