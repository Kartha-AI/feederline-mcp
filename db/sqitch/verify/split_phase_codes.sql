-- SPDX-License-Identifier: Apache-2.0
SELECT 1/count(*) FROM pg_constraint WHERE conname = 'terminal_phases_check' AND pg_get_constraintdef(oid) LIKE '%s12%';
