-- SPDX-License-Identifier: Apache-2.0
BEGIN;
DROP MATERIALIZED VIEW measurement_value_15m;
DROP FUNCTION ensure_measurement_value_partition(DATE);
DROP TABLE measurement_value_latest, measurement_value, remote_source, remote_unit, discrete, analog, measurement;
COMMIT;
