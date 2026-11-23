-- SPDX-License-Identifier: Apache-2.0
-- Deploy opendss_cim:split_phase_codes
-- 120/240 V center-tap secondaries carry the CIM PhaseCode values s1, s2 and s12.
BEGIN;
ALTER TABLE terminal DROP CONSTRAINT terminal_phases_check;
ALTER TABLE terminal ADD CONSTRAINT terminal_phases_check
    CHECK (phases IN ('ABC', 'AB', 'BC', 'AC', 'A', 'B', 'C', 's1', 's2', 's12'));
COMMIT;
