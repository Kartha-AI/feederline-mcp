-- SPDX-License-Identifier: Apache-2.0
BEGIN;
ALTER TABLE terminal DROP CONSTRAINT terminal_phases_check;
ALTER TABLE terminal ADD CONSTRAINT terminal_phases_check CHECK (phases IN ('ABC', 'AB', 'BC', 'AC', 'A', 'B', 'C'));
COMMIT;
