# SPDX-License-Identifier: Apache-2.0
"""The CIM SELECTs. Every query after the scope is `WHERE ce.mrid = ANY(%(scope)s)`."""

ROOT = """
SELECT ce.mrid, ce.name, ce.cim_class, ce.region_code, ce.attrs, ce.updated_at, bv.nominal_voltage, ce.routing_id AS root_edge,
       t1.connectivity_node_mrid AS source_cn, cn1.name AS source_cn_name,
       t2.connectivity_node_mrid AS load_cn, cn2.routing_id AS load_routing_id, cn1.routing_id AS source_routing_id,
       t2.mrid AS load_terminal
FROM conducting_equipment ce
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
JOIN terminal t1 ON t1.conducting_equipment_mrid = ce.mrid AND t1.sequence_number = 1
JOIN terminal t2 ON t2.conducting_equipment_mrid = ce.mrid AND t2.sequence_number = 2
JOIN connectivity_node cn1 ON cn1.mrid = t1.connectivity_node_mrid
JOIN connectivity_node cn2 ON cn2.mrid = t2.connectivity_node_mrid
WHERE ce.mrid = %(root)s AND ce.cim_class IN ('Breaker', 'Recloser') AND ce.valid_to IS NULL
"""

# A VoltageLevel root resolves to the breaker it contains.
VOLTAGE_LEVEL_HEAD = """
SELECT ce.mrid FROM conducting_equipment ce
WHERE ce.equipment_container_mrid = %(root)s AND ce.cim_class IN ('Breaker', 'Recloser') AND ce.valid_to IS NULL
ORDER BY ce.name LIMIT 1
"""

# Load-side trace through closed switches (planning case: normally closed). {edges} is scope.edges_expr(case): the
# shared edge SQL, built server-side with format(%%L) so region and excluded edges are literals inside pgRouting's
# text argument, never Python-interpolated. scope.py renders one constant per case at import time.
SCOPE_TEMPLATE = """
WITH reach AS (
    SELECT node FROM pgr_drivingDistance({edges}, %(start)s::bigint, 1e12::float8, directed => false)
    UNION SELECT %(start)s::bigint
),
terms AS (
    SELECT ce.mrid, ce.cim_class, bool_and(cn.routing_id IN (SELECT node FROM reach)) AS all_in,
           bool_or(cn.routing_id IN (SELECT node FROM reach)) AS any_in
    FROM conducting_equipment ce
    JOIN terminal t ON t.conducting_equipment_mrid = ce.mrid
    JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
    WHERE ce.region_code = %(region)s AND ce.valid_to IS NULL AND ce.in_service
    GROUP BY ce.mrid, ce.cim_class
)
SELECT mrid FROM terms
WHERE all_in OR (any_in AND cim_class IN ('Breaker', 'Recloser', 'LoadBreakSwitch', 'Disconnector', 'Fuse'))
UNION SELECT %(root)s::uuid
"""

# Any equipment as a trace anchor: its terminals' routing ids in sequence order; is_root when it heads a feeder.
ANCHOR = """
SELECT ce.mrid, ce.name, ce.cim_class, ce.region_code, ce.routing_id AS own_edge,
       (ce.cim_class IN ('Breaker', 'Recloser') AND vl.mrid IS NOT NULL) AS is_root,
       array_agg(cn.routing_id ORDER BY t.sequence_number) AS nodes,
       array_agg(cn.mrid ORDER BY t.sequence_number) AS cns
FROM conducting_equipment ce
LEFT JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
JOIN terminal t ON t.conducting_equipment_mrid = ce.mrid
JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
WHERE ce.mrid = %(mrid)s AND ce.valid_to IS NULL
GROUP BY ce.mrid, ce.name, ce.cim_class, ce.region_code, ce.routing_id, vl.mrid
"""

# Feeder heads in a region: load-side node (the trace start) and the head's own edge (never traversed).
ROOTS = """
SELECT ce.mrid, ce.name, ce.routing_id AS own_edge, cn2.routing_id AS load_node, cn2.mrid AS load_cn,
       s.name AS substation_name, s.mrid AS substation_mrid
FROM conducting_equipment ce
JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
LEFT JOIN substation s ON s.mrid = vl.substation_mrid
JOIN terminal t2 ON t2.conducting_equipment_mrid = ce.mrid AND t2.sequence_number = 2
JOIN connectivity_node cn2 ON cn2.mrid = t2.connectivity_node_mrid
WHERE ce.cim_class IN ('Breaker', 'Recloser') AND ce.valid_to IS NULL AND ce.region_code = %(region)s
ORDER BY ce.name
"""

# Which of a device's terminal nodes is farther from every feeder head (its load side). {edges} per case.
ORIENT_TEMPLATE = """
SELECT d.start_vid, min(d.agg_cost) AS dist
FROM pgr_dijkstra({edges}, %(starts)s::bigint[], %(roots)s::bigint[], directed => false) d
WHERE d.edge = -1
GROUP BY d.start_vid
"""

NODES = """
SELECT DISTINCT cn.mrid, cn.name
FROM terminal t JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
WHERE t.conducting_equipment_mrid = ANY(%(scope)s)
"""

SEGMENTS = """
SELECT ce.mrid, ce.name, ce.attrs, ce.updated_at, bv.nominal_voltage,
       seg.length, seg.r, seg.x, seg.r0, seg.x0, seg.rated_current, seg.conductor_type,
       t1.connectivity_node_mrid AS cn1, t1.phases AS phases1, t2.connectivity_node_mrid AS cn2, t2.phases AS phases2
FROM conducting_equipment ce
JOIN ac_line_segment seg ON seg.mrid = ce.mrid
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
JOIN terminal t1 ON t1.conducting_equipment_mrid = ce.mrid AND t1.sequence_number = 1
JOIN terminal t2 ON t2.conducting_equipment_mrid = ce.mrid AND t2.sequence_number = 2
WHERE ce.mrid = ANY(%(scope)s)
ORDER BY ce.name
"""

SWITCHES = """
SELECT ce.mrid, ce.name, ce.cim_class, ce.attrs, ce.updated_at, bv.nominal_voltage,
       s.open, s.normal_open, s.open_ts, s.rated_current, rs.scan_interval,
       t1.connectivity_node_mrid AS cn1, t1.phases AS phases1, t2.connectivity_node_mrid AS cn2, t2.phases AS phases2
FROM conducting_equipment ce
JOIN switch s ON s.mrid = ce.mrid
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
JOIN terminal t1 ON t1.conducting_equipment_mrid = ce.mrid AND t1.sequence_number = 1
JOIN terminal t2 ON t2.conducting_equipment_mrid = ce.mrid AND t2.sequence_number = 2
LEFT JOIN measurement m ON m.power_system_resource_mrid = ce.mrid AND m.measurement_type = 'SwitchPosition'
LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
WHERE ce.mrid = ANY(%(scope)s)
ORDER BY ce.name
"""

TRANSFORMERS = """
SELECT ce.mrid, ce.name, ce.attrs, ce.updated_at, pt.vector_group,
       tap.value AS tap_value, tap.ts AS tap_ts, tap.quality AS tap_quality, tap.scan_interval AS tap_scan,
       json_agg(json_build_object(
           'end_number', e.end_number, 'rated_u', e.rated_u, 'rated_s', e.rated_s, 'r', e.r, 'x', e.x,
           'connection_kind', e.connection_kind, 'cn', t.connectivity_node_mrid, 'phases', t.phases
       ) ORDER BY e.end_number) AS ends
FROM conducting_equipment ce
JOIN power_transformer pt ON pt.mrid = ce.mrid
JOIN power_transformer_end e ON e.power_transformer_mrid = ce.mrid
JOIN terminal t ON t.conducting_equipment_mrid = ce.mrid AND t.sequence_number = e.end_number
LEFT JOIN LATERAL (
    SELECT l.value, l.ts, l.quality, rs.scan_interval
    FROM measurement m JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
    LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
    WHERE m.power_system_resource_mrid = ce.mrid AND m.measurement_type = 'TapPosition'
    ORDER BY l.ts DESC LIMIT 1
) tap ON true
WHERE ce.mrid = ANY(%(scope)s)
GROUP BY ce.mrid, ce.name, ce.attrs, ce.updated_at, pt.vector_group, tap.value, tap.ts, tap.quality, tap.scan_interval
ORDER BY ce.name
"""

LOADS = """
SELECT ce.mrid, ce.name, ce.attrs, ce.updated_at, bv.nominal_voltage,
       ec.customer_count, ec.p_fixed, ec.q_fixed, ec.phase_connection,
       t.connectivity_node_mrid AS cn, t.phases,
       p.value AS p_value, p.ts AS p_ts, p.quality AS p_quality, p.scan_interval AS p_scan,
       q.value AS q_value, q.ts AS q_ts, q.quality AS q_quality
FROM conducting_equipment ce
JOIN energy_consumer ec ON ec.mrid = ce.mrid
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
JOIN terminal t ON t.conducting_equipment_mrid = ce.mrid AND t.sequence_number = 1
LEFT JOIN LATERAL (
    SELECT l.value, l.ts, l.quality, rs.scan_interval
    FROM measurement m JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
    LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
    WHERE m.power_system_resource_mrid = ce.mrid AND m.measurement_type = 'ThreePhaseActivePower'
    ORDER BY l.ts DESC LIMIT 1
) p ON true
LEFT JOIN LATERAL (
    SELECT l.value, l.ts, l.quality
    FROM measurement m JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
    WHERE m.power_system_resource_mrid = ce.mrid AND m.measurement_type = 'ThreePhaseReactivePower'
    ORDER BY l.ts DESC LIMIT 1
) q ON true
WHERE ce.mrid = ANY(%(scope)s)
ORDER BY ce.name
"""

CAPACITORS = """
SELECT ce.mrid, ce.name, ce.attrs, ce.updated_at, bv.nominal_voltage, t.connectivity_node_mrid AS cn, t.phases
FROM conducting_equipment ce
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
JOIN terminal t ON t.conducting_equipment_mrid = ce.mrid AND t.sequence_number = 1
WHERE ce.mrid = ANY(%(scope)s) AND ce.cim_class = 'LinearShuntCompensator'
ORDER BY ce.name
"""

DER = """
SELECT ce.mrid, ce.name, ce.attrs, ce.updated_at, bv.nominal_voltage, t.connectivity_node_mrid AS cn, t.phases,
       p.value AS p_value, p.ts AS p_ts, p.quality AS p_quality, p.scan_interval AS p_scan
FROM conducting_equipment ce
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
JOIN terminal t ON t.conducting_equipment_mrid = ce.mrid AND t.sequence_number = 1
LEFT JOIN LATERAL (
    SELECT l.value, l.ts, l.quality, rs.scan_interval
    FROM measurement m JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
    LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
    WHERE m.power_system_resource_mrid = ce.mrid AND m.measurement_type = 'ThreePhaseActivePower'
    ORDER BY l.ts DESC LIMIT 1
) p ON true
WHERE ce.mrid = ANY(%(scope)s) AND ce.cim_class = 'SynchronousMachine'
ORDER BY ce.name
"""

HEAD_MEASUREMENTS = """
SELECT m.measurement_type, l.value, l.ts, l.quality, rs.scan_interval
FROM measurement m
JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
WHERE m.power_system_resource_mrid = %(root)s
  AND m.measurement_type IN ('ThreePhaseActivePower', 'ThreePhaseReactivePower', 'LineToLineVoltage')
"""

FEEDERS = """
SELECT ce.mrid, ce.name, ce.region_code, ce.cim_class, s.name AS substation, bv.nominal_voltage
FROM conducting_equipment ce
LEFT JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
LEFT JOIN substation s ON s.mrid = vl.substation_mrid
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
WHERE ce.cim_class IN ('Breaker', 'Recloser') AND ce.valid_to IS NULL
  AND vl.mrid IS NOT NULL
  AND (%(region)s::text IS NULL OR ce.region_code = %(region)s)
ORDER BY ce.region_code, ce.name
"""

SCOPE_COUNTS = """
SELECT ce.cim_class, count(*) AS n FROM conducting_equipment ce WHERE ce.mrid = ANY(%(scope)s) GROUP BY ce.cim_class
"""

# Rollup values for time-series studies: one row per measurement per bucket for the requested day.
ROLLUP_DAY = """
SELECT r.bucket, r.measurement_mrid, m.power_system_resource_mrid, m.measurement_type, r.avg_value, r.samples, r.bad_samples
FROM {rollup} r
JOIN measurement m ON m.mrid = r.measurement_mrid
WHERE m.power_system_resource_mrid = ANY(%(psrs)s)
  AND m.measurement_type IN ('ThreePhaseActivePower', 'ThreePhaseReactivePower')
  AND r.bucket >= %(start)s AND r.bucket < %(end)s
ORDER BY r.bucket
"""

# AMI/raw samples bucketed on the fly (source='ami').
RAW_DAY = """
SELECT date_bin(%(step)s::interval, v.ts, TIMESTAMPTZ '2000-01-01') AS bucket, v.measurement_mrid,
       m.power_system_resource_mrid, m.measurement_type, avg(v.value) AS avg_value, count(*) AS samples,
       count(*) FILTER (WHERE v.quality <> 0) AS bad_samples
FROM measurement_value v
JOIN measurement m ON m.mrid = v.measurement_mrid
WHERE m.power_system_resource_mrid = ANY(%(psrs)s)
  AND m.measurement_type IN ('ThreePhaseActivePower', 'ThreePhaseReactivePower')
  AND v.ts >= %(start)s AND v.ts < %(end)s
GROUP BY 1, 2, 3, 4
ORDER BY 1
"""

# Radius rolldown: substations (nearest first) and transmission/distribution segments within a radius of a point.
SUBSTATIONS_NEAR = """
WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography AS g)
SELECT s.mrid, s.name, s.region_code,
       round((ST_Distance(l.geom::geography, p.g) / 1000)::numeric, 2) AS km,
       (SELECT max(bv.nominal_voltage) FROM voltage_level vl JOIN base_voltage bv ON bv.mrid = vl.base_voltage_mrid
        WHERE vl.substation_mrid = s.mrid) AS max_kv
FROM p
JOIN location l ON ST_DWithin(l.geom::geography, p.g, %(radius_m)s)
JOIN substation s ON s.location_mrid = l.mrid
ORDER BY km
LIMIT %(limit)s
"""

SEGMENTS_NEAR = """
WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography AS g)
SELECT ce.region_code, bv.nominal_voltage, count(*) AS segments, round(sum(seg.length) / 1000, 1) AS km
FROM p
JOIN location l ON ST_DWithin(l.geom::geography, p.g, %(radius_m)s)
JOIN conducting_equipment ce ON ce.location_mrid = l.mrid AND ce.cim_class = 'ACLineSegment'
JOIN ac_line_segment seg ON seg.mrid = ce.mrid
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
GROUP BY 1, 2
ORDER BY 2 DESC NULLS LAST
"""

# Data-quality predicates (compiler/validate.py) over a compiled scope.
LOADS_WITHOUT_BASE_VOLTAGE = """
SELECT ce.mrid, ce.name, ce.cim_class FROM conducting_equipment ce
WHERE ce.mrid = ANY(%(scope)s) AND ce.cim_class = 'EnergyConsumer' AND ce.base_voltage_mrid IS NULL
ORDER BY ce.name
"""

ENDS_WITHOUT_TERMINAL = """
SELECT DISTINCT ce.mrid, ce.name, ce.cim_class FROM conducting_equipment ce
JOIN power_transformer_end e ON e.power_transformer_mrid = ce.mrid
WHERE ce.mrid = ANY(%(scope)s) AND e.terminal_mrid IS NULL
ORDER BY ce.name
"""

WITHOUT_LOCATION = """
SELECT ce.mrid, ce.name, ce.cim_class FROM conducting_equipment ce
WHERE ce.mrid = ANY(%(scope)s) AND ce.location_mrid IS NULL
ORDER BY ce.name
"""
