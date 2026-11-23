# SPDX-License-Identifier: Apache-2.0
"""Every SELECT of the SQL tool family, as named constants with %(name)s params.

Traces take their edge SQL from compiler.scope.render(): one constant per case ('operational', 'planning', 'all',
'section'), so the switch predicate here and in the compiler's scope is the same text. {rollup} is the adapter's
rollup_table name (never client input), substituted by rollup().
"""

from ...compiler.scope import render

# --- shared ---------------------------------------------------------------------------------------------------

# Terminal routing ids and container substation of equipment or connectivity nodes, for feeder attribution.
CONTEXT = """
SELECT x.mrid,
       ARRAY(SELECT cn.routing_id FROM terminal t JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
             WHERE t.conducting_equipment_mrid = x.mrid
             UNION SELECT cn.routing_id FROM connectivity_node cn WHERE cn.mrid = x.mrid) AS nodes,
       (SELECT sub.name FROM conducting_equipment ce
        JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
        JOIN substation sub ON sub.mrid = vl.substation_mrid
        WHERE ce.mrid = x.mrid) AS substation_name
FROM unnest(%(mrids)s::uuid[]) AS x(mrid)
"""

# Every node assigned to its nearest feeder head (equicost); heads' own edges are never crossed.
MEMBERSHIP = render("""
SELECT t.node, t.start_vid
FROM pgr_drivingDistance({edges}, %(roots)s::bigint[], 1e12::float8, directed => false, equicost => true) t
WHERE t.node = ANY(%(nodes)s::bigint[])
""")

# Equipment visible in network_edges (no RLS there) but hidden by RLS: the caller is outside its region.
EDGE_REGION = """
SELECT e.region_code FROM network_edges e WHERE e.equipment_mrid = %(mrid)s
"""

# --- resolve --------------------------------------------------------------------------------------------------

# Candidates by stage: 1 gis_oid, 2 tag (remote_source.name -> its equipment), 3 exact name, 4 trigram > 0.6.
RESOLVE = """
SELECT * FROM (
    SELECT ce.mrid, ce.name, ce.cim_class, ce.region_code, 1 AS stage, 1.0::float8 AS score
    FROM conducting_equipment ce WHERE ce.gis_oid = %(ref)s AND ce.valid_to IS NULL
    UNION ALL
    SELECT s.mrid, s.name, 'Substation', s.region_code, 1, 1.0 FROM substation s WHERE s.gis_oid = %(ref)s
    UNION ALL
    SELECT ce.mrid, ce.name, ce.cim_class, ce.region_code, 2, 1.0
    FROM remote_source rs
    JOIN measurement m ON m.mrid = rs.measurement_mrid
    JOIN conducting_equipment ce ON ce.mrid = m.power_system_resource_mrid
    WHERE rs.name = %(ref)s AND ce.valid_to IS NULL
    UNION ALL
    SELECT ce.mrid, ce.name, ce.cim_class, ce.region_code, 3, 0.9
    FROM conducting_equipment ce WHERE lower(ce.name) = lower(%(ref)s) AND ce.valid_to IS NULL
    UNION ALL
    SELECT s.mrid, s.name, 'Substation', s.region_code, 3, 0.9 FROM substation s WHERE lower(s.name) = lower(%(ref)s)
    UNION ALL
    SELECT vl.mrid, vl.name, 'VoltageLevel', s.region_code, 3, 0.9
    FROM voltage_level vl JOIN substation s ON s.mrid = vl.substation_mrid WHERE lower(vl.name) = lower(%(ref)s)
    UNION ALL
    SELECT ce.mrid, ce.name, ce.cim_class, ce.region_code, 4, similarity(ce.name, %(ref)s)
    FROM conducting_equipment ce
    WHERE ce.name %% %(ref)s AND similarity(ce.name, %(ref)s) > 0.6 AND ce.valid_to IS NULL
    UNION ALL
    SELECT s.mrid, s.name, 'Substation', s.region_code, 4, similarity(s.name, %(ref)s)
    FROM substation s WHERE s.name %% %(ref)s AND similarity(s.name, %(ref)s) > 0.6
) c
WHERE (%(cls)s::text IS NULL OR c.cim_class = %(cls)s) AND (%(region)s::text IS NULL OR c.region_code = %(region)s)
ORDER BY c.stage, c.score DESC, c.name
LIMIT 50
"""

# Measurements by mRID, tag or exact name.
RESOLVE_MEASUREMENT = """
SELECT m.mrid, m.name, m.region_code,
       CASE WHEN m.mrid::text = %(ref)s THEN 1 WHEN rs.name = %(ref)s THEN 2 ELSE 3 END AS stage
FROM measurement m LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
WHERE (m.mrid::text = %(ref)s OR rs.name = %(ref)s OR lower(m.name) = lower(%(ref)s))
  AND (%(region)s::text IS NULL OR m.region_code = %(region)s)
ORDER BY stage, m.name
LIMIT 10
"""

# --- lookup ---------------------------------------------------------------------------------------------------

SEARCH_EQUIPMENT = """
SELECT * FROM (
    SELECT DISTINCT ON (h.mrid) h.* FROM (
        SELECT ce.mrid, ce.name, ce.cim_class, ce.gis_oid, NULL::text AS tag,
               CASE WHEN ce.gis_oid = %(q)s THEN 1.0 WHEN lower(ce.name) = lower(%(q)s) THEN 0.9
                    ELSE similarity(ce.name, %(q)s) END::float8 AS score
        FROM conducting_equipment ce
        WHERE ce.region_code = %(region)s AND ce.valid_to IS NULL
          AND (ce.gis_oid = %(q)s OR ce.name %% %(q)s OR lower(ce.name) LIKE %(pat)s)
        UNION ALL
        SELECT s.mrid, s.name, 'Substation', s.gis_oid, NULL,
               CASE WHEN s.gis_oid = %(q)s THEN 1.0 WHEN lower(s.name) = lower(%(q)s) THEN 0.9
                    ELSE similarity(s.name, %(q)s) END
        FROM substation s
        WHERE s.region_code = %(region)s AND (s.gis_oid = %(q)s OR s.name %% %(q)s OR lower(s.name) LIKE %(pat)s)
        UNION ALL
        SELECT cn.mrid, cn.name, 'ConnectivityNode', NULL, NULL,
               CASE WHEN lower(cn.name) = lower(%(q)s) THEN 0.9 ELSE similarity(cn.name, %(q)s) END
        FROM connectivity_node cn
        WHERE cn.region_code = %(region)s AND cn.name IS NOT NULL
          AND (cn.name %% %(q)s OR lower(cn.name) LIKE %(pat)s)
        UNION ALL
        SELECT ce.mrid, ce.name, ce.cim_class, ce.gis_oid, rs.name,
               CASE WHEN rs.name = %(q)s THEN 1.0 ELSE similarity(rs.name, %(q)s) END
        FROM remote_source rs
        JOIN measurement m ON m.mrid = rs.measurement_mrid
        JOIN conducting_equipment ce ON ce.mrid = m.power_system_resource_mrid
        WHERE ce.region_code = %(region)s AND ce.valid_to IS NULL AND (rs.name = %(q)s OR rs.name %% %(q)s)
    ) h
    WHERE %(cls)s::text IS NULL OR h.cim_class = %(cls)s
    ORDER BY h.mrid, h.score DESC
) d
ORDER BY d.score DESC, d.name
LIMIT %(limit)s
"""

DESCRIBE_HUB = """
SELECT ce.mrid, ce.name, ce.cim_class, ce.description, ce.gis_oid, ce.region_code, ce.in_service, ce.attrs,
       ce.valid_from, ce.valid_to, ce.updated_at, bv.nominal_voltage AS nominal_kv,
       ln.mrid AS line_mrid, ln.name AS line_name, vl.mrid AS voltage_level_mrid, vl.name AS voltage_level_name,
       sub.mrid AS substation_mrid, sub.name AS substation_name,
       l.mrid AS location_mrid, l.name AS location_name, ST_AsGeoJSON(l.geom, 7)::json AS geometry
FROM conducting_equipment ce
LEFT JOIN base_voltage bv ON bv.mrid = ce.base_voltage_mrid
LEFT JOIN line ln ON ln.mrid = ce.equipment_container_mrid
LEFT JOIN voltage_level vl ON vl.mrid = ce.equipment_container_mrid
LEFT JOIN substation sub ON sub.mrid = vl.substation_mrid
LEFT JOIN location l ON l.mrid = ce.location_mrid
WHERE ce.mrid = %(mrid)s
"""

_SWITCH = "SELECT normal_open, open, open_ts, rated_current FROM switch WHERE mrid = %(mrid)s"
DESCRIBE_SUBCLASS = {
    "ACLineSegment": "SELECT length AS length_m, r AS r_ohm, x AS x_ohm, r0 AS r0_ohm, x0 AS x0_ohm, rated_current, "
                     "conductor_type FROM ac_line_segment WHERE mrid = %(mrid)s",
    "PowerTransformer": "SELECT vector_group FROM power_transformer WHERE mrid = %(mrid)s",
    "EnergyConsumer": "SELECT customer_count, p_fixed AS p_fixed_kw, q_fixed AS q_fixed_kvar, phase_connection "
                      "FROM energy_consumer WHERE mrid = %(mrid)s",
    "Breaker": _SWITCH, "Recloser": _SWITCH, "LoadBreakSwitch": _SWITCH, "Disconnector": _SWITCH, "Fuse": _SWITCH,
}

DESCRIBE_TERMINALS = """
SELECT t.mrid, t.sequence_number, t.phases, cn.mrid AS cn_mrid, cn.name AS cn_name,
       COALESCE((SELECT json_agg(json_build_object('mrid', o.mrid, 'name', o.name, 'cim_class', o.cim_class)
                                 ORDER BY o.name)
                 FROM terminal ot JOIN conducting_equipment o ON o.mrid = ot.conducting_equipment_mrid
                 WHERE ot.connectivity_node_mrid = t.connectivity_node_mrid
                   AND o.mrid <> t.conducting_equipment_mrid AND o.valid_to IS NULL), '[]'::json) AS others
FROM terminal t LEFT JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
WHERE t.conducting_equipment_mrid = %(mrid)s
ORDER BY t.sequence_number
"""

DESCRIBE_ENDS = """
SELECT e.mrid, e.end_number, e.rated_u AS rated_u_kv, e.rated_s AS rated_s_mva, e.r AS r_ohm, e.x AS x_ohm,
       e.connection_kind, e.terminal_mrid
FROM power_transformer_end e WHERE e.power_transformer_mrid = %(mrid)s ORDER BY e.end_number
"""

# --- telemetry ------------------------------------------------------------------------------------------------

LATEST_READINGS = """
SELECT m.mrid, m.name, m.cim_class, m.measurement_type, m.phases, m.unit_symbol, m.unit_multiplier,
       m.power_system_resource_mrid AS equipment_mrid, ce.name AS equipment_name, ce.cim_class AS equipment_class,
       rs.name AS tag, rs.scan_interval, l.value, l.quality, l.source, l.ts,
       EXTRACT(EPOCH FROM (now() - l.ts))::float8 AS age_s
FROM measurement m
JOIN conducting_equipment ce ON ce.mrid = m.power_system_resource_mrid
LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
LEFT JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
WHERE m.power_system_resource_mrid = ANY(%(psrs)s::uuid[])
  AND (%(mtype)s::text IS NULL OR m.measurement_type = %(mtype)s)
ORDER BY ce.name, m.measurement_type, m.phases, m.name
"""

MEASUREMENTS = """
SELECT m.mrid, m.name, m.measurement_type, m.unit_symbol, m.unit_multiplier, m.region_code,
       m.power_system_resource_mrid AS equipment_mrid, ce.name AS equipment_name
FROM measurement m JOIN conducting_equipment ce ON ce.mrid = m.power_system_resource_mrid
WHERE m.mrid = ANY(%(mrids)s::uuid[])
"""

MEASUREMENT_HISTORY = """
SELECT r.bucket, r.measurement_mrid, r.avg_value, r.min_value, r.max_value, r.last_value, r.samples, r.bad_samples
FROM {rollup} r
WHERE r.measurement_mrid = ANY(%(mrids)s::uuid[]) AND r.bucket >= %(start)s AND r.bucket < %(end)s
ORDER BY r.bucket
"""

# Head P/Q of the feeder head, and the sum over its measured loads and DER, per rollup bucket.
LOAD_PROFILE = """
WITH m AS (
    SELECT m.mrid,
           CASE WHEN m.power_system_resource_mrid = %(root)s AND m.measurement_type = 'ThreePhaseActivePower' THEN 'head_p'
                WHEN m.power_system_resource_mrid = %(root)s AND m.measurement_type = 'ThreePhaseReactivePower' THEN 'head_q'
                WHEN ce.cim_class = 'EnergyConsumer' AND m.measurement_type = 'ThreePhaseActivePower' THEN 'load_p'
                WHEN ce.cim_class = 'SynchronousMachine' AND m.measurement_type = 'ThreePhaseActivePower' THEN 'der_p'
           END AS series
    FROM measurement m JOIN conducting_equipment ce ON ce.mrid = m.power_system_resource_mrid
    WHERE m.power_system_resource_mrid = ANY(%(scope)s::uuid[])
)
SELECT r.bucket, m.series, sum(r.avg_value)::float8 AS value, count(*) AS n
FROM m JOIN {rollup} r ON r.measurement_mrid = m.mrid
WHERE m.series IS NOT NULL AND r.bucket >= %(start)s AND r.bucket < %(end)s
GROUP BY r.bucket, m.series
ORDER BY r.bucket
"""

HEAD_P_MEASUREMENTS = """
SELECT m.mrid FROM measurement m
WHERE m.power_system_resource_mrid = %(root)s AND m.measurement_type = 'ThreePhaseActivePower'
"""

HEAD_P_BUCKETS = """
SELECT r.bucket, sum(r.avg_value)::float8 AS kw
FROM {rollup} r
WHERE r.measurement_mrid = ANY(%(mrids)s::uuid[]) AND r.bucket >= %(start)s AND r.bucket < %(end)s
GROUP BY r.bucket ORDER BY r.bucket
"""

LIMIT_EXCEEDANCES = """
SELECT m.mrid, m.name, m.cim_class, m.measurement_type, m.phases, m.unit_symbol, m.unit_multiplier,
       ce.mrid AS equipment_mrid, ce.name AS equipment_name, l.value, l.ts,
       EXTRACT(EPOCH FROM (now() - l.ts))::float8 AS age_s, a.min_value, a.max_value
FROM measurement m
JOIN analog a ON a.mrid = m.mrid
JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
JOIN conducting_equipment ce ON ce.mrid = m.power_system_resource_mrid
WHERE m.region_code = %(region)s
  AND (%(scope)s::uuid[] IS NULL OR m.power_system_resource_mrid = ANY(%(scope)s::uuid[]))
  AND (l.value < a.min_value OR l.value > a.max_value)
ORDER BY ce.name, m.name
"""

TELEMETRY_QUALITY = """
WITH m AS MATERIALIZED (
    SELECT m.mrid, m.name, m.cim_class, m.measurement_type, ce.mrid AS equipment_mrid, ce.name AS equipment_name,
           COALESCE(rs.scan_interval, %(default_scan)s)::float8 AS scan_interval, l.ts AS latest_ts
    FROM measurement m
    JOIN conducting_equipment ce ON ce.mrid = m.power_system_resource_mrid
    LEFT JOIN remote_source rs ON rs.measurement_mrid = m.mrid
    LEFT JOIN measurement_value_latest l ON l.measurement_mrid = m.mrid
    WHERE m.power_system_resource_mrid = ANY(%(scope)s::uuid[])
)
SELECT m.mrid, m.name, m.cim_class, m.measurement_type, m.equipment_mrid, m.equipment_name, m.scan_interval,
       m.latest_ts, COALESCE(sum(r.samples), 0)::bigint AS received, COALESCE(sum(r.bad_samples), 0)::bigint AS bad,
       COALESCE(array_agg(EXTRACT(EPOCH FROM r.bucket)::float8 ORDER BY r.bucket)
                FILTER (WHERE r.bucket IS NOT NULL), '{}') AS buckets
FROM m
LEFT JOIN {rollup} r ON r.measurement_mrid = m.mrid AND r.bucket >= %(start)s AND r.bucket < %(end)s
GROUP BY m.mrid, m.name, m.cim_class, m.measurement_type, m.equipment_mrid, m.equipment_name, m.scan_interval,
         m.latest_ts
ORDER BY m.equipment_name, m.name
"""

# --- topology -------------------------------------------------------------------------------------------------

TRACE_DOWNSTREAM = render("""
SELECT t.seq, t.depth, t.pred, t.node, t.edge, t.agg_cost,
       e.equipment_mrid, e.source, e.target, ce.name, e.cim_class, sw.open, sw.normal_open, sw.open_ts,
       seg.length, cn.mrid AS cn_mrid
FROM pgr_drivingDistance({edges}, %(start)s::bigint, 1e12::float8, directed => false) t
LEFT JOIN network_edges e ON e.id = t.edge
LEFT JOIN conducting_equipment ce ON ce.mrid = e.equipment_mrid
LEFT JOIN switch sw ON sw.mrid = e.equipment_mrid
LEFT JOIN ac_line_segment seg ON seg.mrid = e.equipment_mrid
LEFT JOIN connectivity_node cn ON cn.routing_id = t.node
ORDER BY t.seq
""")

# Equipment with a terminal on a reached node that the tree did not traverse: one-terminal devices (loads,
# capacitors, DER), boundary switches, and edges that close loops.
TRACE_EXTRA = """
WITH r AS (SELECT DISTINCT unnest(%(nodes)s::bigint[]) AS node),
terms AS (
    SELECT t.conducting_equipment_mrid AS mrid, t.sequence_number, cn.routing_id, t.phases,
           cn.routing_id IN (SELECT node FROM r) AS hit
    FROM terminal t JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
    WHERE cn.region_code = %(region)s
)
SELECT ce.mrid, ce.name, ce.cim_class, sw.open, sw.normal_open, sw.open_ts, seg.length,
       ec.customer_count, ec.p_fixed,
       array_agg(terms.routing_id ORDER BY terms.sequence_number) AS nodes,
       array_agg(terms.hit ORDER BY terms.sequence_number) AS hits,
       min(terms.phases) AS phases
FROM terms
JOIN conducting_equipment ce ON ce.mrid = terms.mrid
LEFT JOIN switch sw ON sw.mrid = ce.mrid
LEFT JOIN ac_line_segment seg ON seg.mrid = ce.mrid
LEFT JOIN energy_consumer ec ON ec.mrid = ce.mrid
WHERE ce.region_code = %(region)s AND ce.valid_to IS NULL AND ce.in_service
  AND NOT (ce.mrid = ANY(%(skip)s::uuid[]))
GROUP BY ce.mrid, ce.name, ce.cim_class, sw.open, sw.normal_open, sw.open_ts, seg.length, ec.customer_count, ec.p_fixed
HAVING bool_or(terms.hit)
"""

# Shortest paths from any start node to any end node (trace_upstream: ends = feeder heads' load-side nodes).
PATH = render("""
SELECT d.seq, d.path_seq, d.start_vid, d.end_vid, d.node, d.edge, d.cost, d.agg_cost,
       e.equipment_mrid, e.cim_class, ce.name, sw.open, sw.normal_open, seg.r, seg.x, seg.length
FROM pgr_dijkstra({edges}, %(starts)s::bigint[], %(ends)s::bigint[], directed => false) d
LEFT JOIN network_edges e ON e.id = d.edge
LEFT JOIN conducting_equipment ce ON ce.mrid = e.equipment_mrid
LEFT JOIN switch sw ON sw.mrid = e.equipment_mrid
LEFT JOIN ac_line_segment seg ON seg.mrid = e.equipment_mrid
ORDER BY d.start_vid, d.end_vid, d.path_seq
""")
TRACE_UPSTREAM = PATH
FIND_PATH = PATH
FIND_PATH_IGNORE_SWITCHES = PATH["all"]

# All region edges ignoring switch state, by connected component.
COMPONENTS = render("""
SELECT c.component, c.node FROM pgr_connectedComponents({edges}) c
""")

COMPONENT_EQUIPMENT = """
SELECT e.equipment_mrid AS mrid, ce.name, e.cim_class
FROM network_edges e LEFT JOIN conducting_equipment ce ON ce.mrid = e.equipment_mrid
WHERE e.region_code = %(region)s AND e.grid_tier = 'Distribution'
  AND (e.source = ANY(%(nodes)s::bigint[]) OR e.target = ANY(%(nodes)s::bigint[]))
ORDER BY ce.name
"""

ORPHAN_TERMINALS = """
SELECT ce.mrid, ce.name, ce.cim_class
FROM terminal t JOIN conducting_equipment ce ON ce.mrid = t.conducting_equipment_mrid
WHERE ce.region_code = %(region)s AND t.connectivity_node_mrid IS NULL AND ce.valid_to IS NULL
ORDER BY ce.name
"""

# Nodes of in-scope equipment with exactly one terminal, unless that terminal belongs to a one-terminal device.
DANGLING_NODES = """
WITH n AS (
    SELECT t.connectivity_node_mrid AS cn
    FROM terminal t WHERE t.conducting_equipment_mrid = ANY(%(scope)s::uuid[]) AND t.connectivity_node_mrid IS NOT NULL
)
SELECT cn.mrid, cn.name, 'ConnectivityNode' AS cim_class
FROM connectivity_node cn
WHERE cn.mrid IN (SELECT cn FROM n)
  AND (SELECT count(*) FROM terminal t WHERE t.connectivity_node_mrid = cn.mrid) = 1
  AND NOT EXISTS (SELECT 1 FROM terminal t JOIN conducting_equipment ce ON ce.mrid = t.conducting_equipment_mrid
                  WHERE t.connectivity_node_mrid = cn.mrid
                    AND ce.cim_class IN ('EnergyConsumer', 'LinearShuntCompensator', 'SynchronousMachine'))
ORDER BY cn.name
"""

SWITCHES_NO_TS = """
SELECT ce.mrid, ce.name, ce.cim_class FROM conducting_equipment ce JOIN switch s ON s.mrid = ce.mrid
WHERE ce.mrid = ANY(%(scope)s::uuid[]) AND s.open_ts IS NULL ORDER BY ce.name
"""

SEGMENTS_NO_LENGTH = """
SELECT ce.mrid, ce.name, ce.cim_class FROM conducting_equipment ce JOIN ac_line_segment seg ON seg.mrid = ce.mrid
WHERE ce.mrid = ANY(%(scope)s::uuid[]) AND (seg.length IS NULL OR seg.length = 0) ORDER BY ce.name
"""

SWITCH_STATE = """
SELECT ce.mrid, ce.name, ce.cim_class, s.open, s.normal_open, s.open_ts
FROM conducting_equipment ce JOIN switch s ON s.mrid = ce.mrid WHERE ce.mrid = ANY(%(mrids)s::uuid[])
"""

# --- spatial --------------------------------------------------------------------------------------------------

EQUIPMENT_NEAR = """
WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS g)
SELECT ce.mrid, ce.name, ce.cim_class, ST_AsGeoJSON(l.geom, 6)::json AS geometry,
       ST_Distance(l.geom::geography, p.g::geography)::float8 AS distance_m,
       ARRAY(SELECT cn.routing_id FROM terminal t JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
             WHERE t.conducting_equipment_mrid = ce.mrid) AS nodes
FROM p
JOIN location l ON ST_DWithin(l.geom::geography, p.g::geography, %(radius_m)s)
JOIN conducting_equipment ce ON ce.location_mrid = l.mrid
WHERE ce.region_code = %(region)s AND ce.valid_to IS NULL AND (%(cls)s::text IS NULL OR ce.cim_class = %(cls)s)
ORDER BY distance_m, ce.name
LIMIT %(limit)s
"""

NEAREST_SUBSTATION = """
WITH p AS (SELECT ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) AS g)
SELECT s.mrid, s.name, s.region_code, ST_AsGeoJSON(l.geom, 6)::json AS geometry,
       ST_Distance(l.geom::geography, p.g::geography)::float8 AS distance_m,
       ARRAY(SELECT DISTINCT bv.nominal_voltage::float8 FROM voltage_level vl
             JOIN base_voltage bv ON bv.mrid = vl.base_voltage_mrid
             WHERE vl.substation_mrid = s.mrid ORDER BY 1 DESC) AS voltages_kv,
       (SELECT count(*) FROM voltage_level vl
        JOIN conducting_equipment ce ON ce.equipment_container_mrid = vl.mrid
         AND ce.cim_class IN ('Breaker', 'Recloser') AND ce.valid_to IS NULL
        WHERE vl.substation_mrid = s.mrid) AS feeders
FROM p, substation s JOIN location l ON l.mrid = s.location_mrid
WHERE s.region_code = %(region)s
ORDER BY l.geom <-> p.g
LIMIT %(k)s
"""

SEGMENTS_IN_AREA = """
WITH a AS (SELECT ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(%(polygon)s), 4326)) AS g)
SELECT ce.mrid, ce.name, ce.cim_class, seg.length::float8 AS length_m,
       ST_AsGeoJSON(ST_Simplify(l.geom, %(tol)s), 6)::json AS geometry,
       ARRAY(SELECT cn.routing_id FROM terminal t JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
             WHERE t.conducting_equipment_mrid = ce.mrid ORDER BY t.sequence_number) AS nodes
FROM a
JOIN location l ON ST_Intersects(l.geom, a.g)
JOIN conducting_equipment ce ON ce.location_mrid = l.mrid AND ce.cim_class = 'ACLineSegment' AND ce.valid_to IS NULL
LEFT JOIN ac_line_segment seg ON seg.mrid = ce.mrid
WHERE ce.region_code = %(region)s
ORDER BY ce.name
"""

FEEDER_GEOMETRY = """
SELECT ce.mrid, ce.name, ce.cim_class, seg.length::float8 AS length_m, COALESCE(seg.rated_current, sw.rated_current)::float8 AS rated_a,
       sw.open, sw.normal_open, t1.phases,
       ST_AsGeoJSON(CASE WHEN GeometryType(l.geom) = 'LINESTRING' THEN ST_Simplify(l.geom, %(tol)s) ELSE l.geom END,
                    6)::json AS geometry
FROM conducting_equipment ce
JOIN location l ON l.mrid = ce.location_mrid
LEFT JOIN ac_line_segment seg ON seg.mrid = ce.mrid
LEFT JOIN switch sw ON sw.mrid = ce.mrid
LEFT JOIN terminal t1 ON t1.conducting_equipment_mrid = ce.mrid AND t1.sequence_number = 1
WHERE ce.mrid = ANY(%(scope)s::uuid[])
ORDER BY CASE ce.cim_class WHEN 'ACLineSegment' THEN 0 WHEN 'EnergyConsumer' THEN 2 ELSE 1 END, ce.name
"""

FEEDER_BBOX = """
SELECT ST_XMin(x.e) AS xmin, ST_YMin(x.e) AS ymin, ST_XMax(x.e) AS xmax, ST_YMax(x.e) AS ymax
FROM (SELECT ST_Extent(l.geom) AS e FROM conducting_equipment ce JOIN location l ON l.mrid = ce.location_mrid
      WHERE ce.mrid = ANY(%(scope)s::uuid[])) x
"""

# Content version of a region's model and switch states, for resource caches.
REGION_VERSION = """
SELECT (SELECT max(updated_at) FROM conducting_equipment WHERE region_code = %(region)s) AS cim,
       (SELECT max(s.open_ts) FROM switch s JOIN conducting_equipment ce ON ce.mrid = s.mrid
        WHERE ce.region_code = %(region)s) AS switches
"""

# --- governance -----------------------------------------------------------------------------------------------

MODEL_CHANGES = """
SELECT ce.mrid, ce.name, ce.cim_class, ce.updated_at, ce.valid_to,
       CASE WHEN ce.valid_to IS NOT NULL THEN 'retired' ELSE 'updated' END AS change
FROM conducting_equipment ce
WHERE ce.region_code = %(region)s
  AND ((ce.mrid = ANY(%(scope)s::uuid[]) AND ce.valid_to IS NULL AND ce.updated_at > %(since)s)
       OR (ce.valid_to IS NOT NULL AND ce.valid_to > %(since)s
           AND EXISTS (SELECT 1 FROM terminal t JOIN connectivity_node cn ON cn.mrid = t.connectivity_node_mrid
                       WHERE t.conducting_equipment_mrid = ce.mrid AND cn.routing_id = ANY(%(nodes)s::bigint[]))))
ORDER BY ce.updated_at DESC, ce.name
"""


def rollup(template: str, table: str) -> str:
    """A rollup query on the adapter's rollup table name (configuration, never client input)."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"bad rollup table name {table!r}")
    return template.replace("{rollup}", table)

# CIM class and terminal-1 phases of equipment named in an engine result.
EQUIPMENT_CLASSES = """
SELECT ce.mrid, ce.name, ce.cim_class FROM conducting_equipment ce WHERE ce.mrid = ANY(%(mrids)s::uuid[])
"""

# Region of a visible equipment or substation (RLS applies).
VISIBLE_REGION = """
SELECT region_code FROM conducting_equipment WHERE mrid = %(mrid)s
UNION ALL SELECT region_code FROM substation WHERE mrid = %(mrid)s
UNION ALL SELECT s.region_code FROM voltage_level vl JOIN substation s ON s.mrid = vl.substation_mrid WHERE vl.mrid = %(mrid)s
"""
