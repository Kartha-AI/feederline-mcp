-- SPDX-License-Identifier: Apache-2.0
BEGIN;
DROP POLICY region_isolation ON substation;
DROP POLICY region_isolation ON conducting_equipment;
DROP POLICY region_isolation ON connectivity_node;
DROP POLICY region_isolation ON measurement;
DROP POLICY region_isolation ON remote_unit;
ALTER TABLE substation DISABLE ROW LEVEL SECURITY;
ALTER TABLE conducting_equipment DISABLE ROW LEVEL SECURITY;
ALTER TABLE connectivity_node DISABLE ROW LEVEL SECURITY;
ALTER TABLE measurement DISABLE ROW LEVEL SECURITY;
ALTER TABLE remote_unit DISABLE ROW LEVEL SECURITY;
DROP FUNCTION region_visible(TEXT);
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM loader, writer, study_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA studies FROM study_reader, study_writer;
REVOKE ALL ON SCHEMA studies FROM study_reader, study_writer;
COMMIT;
