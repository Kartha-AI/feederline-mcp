-- SPDX-License-Identifier: Apache-2.0
SELECT 1/count(*) FROM pg_extension WHERE extname = 'pg_trgm';
SELECT 1/count(*) FROM pg_indexes WHERE indexname IN ('idx_ce_name_trgm', 'idx_rs_name_trgm', 'idx_ce_updated') HAVING count(*) = 3;
