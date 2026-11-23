-- SPDX-License-Identifier: Apache-2.0
SELECT mrid, measurement_type FROM measurement WHERE false;
SELECT ts, measurement_mrid, value, quality, source FROM measurement_value WHERE false;
SELECT bucket, measurement_mrid, avg_value FROM measurement_value_15m WHERE false;
