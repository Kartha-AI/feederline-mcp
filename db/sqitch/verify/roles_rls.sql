-- SPDX-License-Identifier: Apache-2.0
SELECT 1/count(*) FROM pg_policies WHERE policyname = 'region_isolation';
SELECT region_visible('x');
