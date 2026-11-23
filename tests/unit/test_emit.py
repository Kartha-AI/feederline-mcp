# SPDX-License-Identifier: Apache-2.0
"""One test per emitted row shape."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from feederline_mcp.compiler import emit
from feederline_mcp.compiler.report import Head, seed_loads

CN1 = UUID("11111111-0000-0000-0000-000000000001")
CN2 = UUID("22222222-0000-0000-0000-000000000002")
EQ = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def seg(**kw):
    row = {"mrid": EQ, "name": "L1", "attrs": {}, "length": 1000, "r": 0.3, "x": 0.6, "r0": 0.9, "x0": 1.8,
           "rated_current": 400, "conductor_type": None, "cn1": CN1, "cn2": CN2, "phases1": "ABC", "phases2": "ABC",
           "nominal_voltage": 12.47}
    row.update(kw)
    return row


def test_names_and_buses():
    assert emit.dss_name(EQ) == "aaaaaaaabbbbccccddddeeeeeeeeeeee"
    assert emit.bus(CN1, "ABC") == "cn_11111111.1.2.3"
    assert emit.bus(CN1, "C") == "cn_11111111.3"
    assert emit.bus(CN1, "AC") == "cn_11111111.1.3"


def test_three_phase_line_uses_sequence_ohms_per_km():
    out = emit.Emitted()
    emit.segment(seg(length=2000), out)
    cmd = out.commands[0]
    assert cmd.startswith("New Line.aaaaaaaabbbbccccddddeeeeeeeeeeee phases=3 bus1=cn_11111111.1.2.3")
    assert "length=2 units=km r1=0.15 x1=0.3 r0=0.45 x0=0.9" in cmd
    assert "normamps=400" in cmd
    assert out.names[emit.dss_name(EQ)] == (EQ, "L1")


def test_single_phase_lateral_with_phase_matrix():
    pz = {"r": [0.8], "x": [0.5], "c": [3.0], "length_km": 0.3}
    out = emit.Emitted()
    emit.segment(seg(attrs={"phase_impedance": pz}, phases1="B", phases2="B", length=300), out)
    cmd = out.commands[0]
    assert "phases=1 bus1=cn_11111111.2 bus2=cn_22222222.2" in cmd
    assert "rmatrix=[0.8] xmatrix=[0.5] cmatrix=[3]" in cmd and "r1=" not in cmd


def test_open_switch_is_emitted_then_opened():
    out = emit.Emitted()
    row = seg(cim_class="LoadBreakSwitch", rated_current=None)
    emit.switch(row, True, out)
    assert out.commands[0].endswith("switch=y")
    assert out.commands[1].startswith("Edit Line.") and "r1=0.0001" in out.commands[1] and "normamps=0" in out.commands[1]
    assert out.commands[2] == f"Open Line.{emit.dss_name(EQ)} 1"


def test_two_winding_transformer_percent_impedance():
    ends = [
        {"end_number": 1, "rated_u": 4.16, "rated_s": 0.5, "r": 0.1903616, "x": 0.692224, "connection_kind": "Y", "cn": CN1, "phases": "ABC"},
        {"end_number": 2, "rated_u": 0.48, "rated_s": 0.5, "r": 0.0025344, "x": 0, "connection_kind": "D", "cn": CN2, "phases": "ABC"},
    ]
    out = emit.Emitted()
    emit.transformer({"mrid": EQ, "name": "XFM1", "attrs": {}, "ends": ends}, out)
    cmd = out.commands[0]
    assert "windings=2 buses=[cn_11111111.1.2.3 cn_22222222.1.2.3] conns=[wye delta]" in cmd
    assert "kvs=[4.16 0.48] kvas=[500 500] %rs=[0.55 0.55]" in cmd
    assert "xhl=2" in cmd


def test_regulator_gets_regcontrol_and_measured_tap():
    ends = [
        {"end_number": n, "rated_u": 2.4, "rated_s": 1.666, "r": 0.0001, "x": 0.0003 if n == 1 else 0, "connection_kind": "Y",
         "cn": cn, "phases": "A"} for n, cn in ((1, CN1), (2, CN2))
    ]
    reg = {"winding": 2, "vreg": 122, "band": 2, "ptratio": 20, "ctprim": 700, "r": 3, "x": 9}
    out = emit.Emitted()
    emit.transformer({"mrid": EQ, "name": "Reg1", "attrs": {"phases": 1, "regulator": reg}, "ends": ends}, out, tap_position=10)
    assert "phases=1" in out.commands[0] and "taps=[1 1.0625]" in out.commands[0]
    assert out.commands[1].startswith("New RegControl.") and "vreg=122" in out.commands[1]


def load_row(**kw):
    row = {"mrid": EQ, "name": "671", "attrs": {"kv": 4.16, "model": 2}, "nominal_voltage": 4.16, "p_fixed": 100,
           "q_fixed": 50, "phase_connection": "D", "cn": CN1, "phases": "ABC", "p_value": None, "p_ts": None,
           "p_quality": None, "p_scan": 900, "q_value": None, "q_ts": None, "q_quality": None}
    row.update(kw)
    return row


def test_measured_load_is_constant_power_at_measured_value():
    r = load_row(p_value=120.0, p_ts=NOW - timedelta(minutes=5), p_quality=0, q_value=40.0, q_ts=NOW, q_quality=0)
    s = seed_loads([r], Head(), 0.0, "operational", NOW)
    assert (s.measured, s.allocated, s.defaulted) == (1, 0, 0)
    out = emit.Emitted()
    emit.load(s.loads[0], out)
    assert "model=1 kv=4.16 kw=120 kvar=40 vminpu=0.5" in out.commands[0] and "conn=delta" in out.commands[0]


def test_allocated_load_scales_by_head_ratio():
    stale = load_row(p_value=999.0, p_ts=NOW - timedelta(hours=2), p_quality=0)
    s = seed_loads([stale, load_row(mrid=CN2)], Head(p_kw=300.0), 0.0, "operational", NOW)
    assert s.allocated == 2 and abs(s.ratio - 1.5) < 1e-9
    assert all(ls.kw == 150 and ls.model == 1 for ls in s.loads)


def test_defaulted_load_keeps_nameplate_and_model():
    s = seed_loads([load_row(phases="A", phase_connection="Y", attrs={})], Head(), 0.0, "planning", NOW)
    out = emit.Emitted()
    emit.load(s.loads[0], out)
    assert s.defaulted == 1
    assert "phases=1 conn=wye model=1 kv=2.40177712" in out.commands[0] and "kw=100" in out.commands[0]


def test_defaulted_impedance_uses_conductor_table():
    out = emit.Emitted()
    emit.segment(seg(r=None, x=None, conductor_type="1/0_acsr"), out)
    assert out.defaulted_impedances == 1 and "r1=0.696" in out.commands[0]


def test_voltage_bases_dedupe_within_one_percent():
    assert emit.voltage_bases([4.16, 4.157, 0.48, 115, None]) == [115.0, 4.16, 0.48]
