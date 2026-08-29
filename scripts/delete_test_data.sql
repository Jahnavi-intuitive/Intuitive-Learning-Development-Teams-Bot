-- One-shot removal of everything tagged is_test_data = true, plus rows that
-- only exist because they reference test data (verification results,
-- audit log entries, escalations tied to a test employee).
--
-- Run against the same database as the backend, e.g.:
--   docker compose exec -T db psql -U lnd_app -d lnd_certification -f /dev/stdin < scripts/delete_test_data.sql
-- or from a local psql pointed at DATABASE_URL.

BEGIN;

-- Drive enrollments/drives (2026-08-25 drives work) — delete before
-- certificate_records/employees so no FK/audit reference is left dangling.
-- Covers both directions: a test employee's enrollment in an otherwise-real
-- drive, and any enrollment (test or not) in a drive itself flagged
-- is_test_data.
DELETE FROM audit_log
WHERE entity_type = 'drive_enrollments'
  AND entity_id IN (
      SELECT de.id FROM drive_enrollments de
      JOIN employees e ON e.id = de.employee_id
      JOIN certification_drives cd ON cd.id = de.drive_id
      WHERE e.is_test_data = true OR cd.is_test_data = true
  );

DELETE FROM drive_enrollments de
USING employees e, certification_drives cd
WHERE de.employee_id = e.id AND de.drive_id = cd.id
  AND (e.is_test_data = true OR cd.is_test_data = true);

DELETE FROM audit_log
WHERE entity_type = 'drive_certifications'
  AND entity_id IN (
      SELECT id FROM drive_certifications WHERE drive_id IN (
          SELECT id FROM certification_drives WHERE is_test_data = true
      )
  );

DELETE FROM drive_certifications
WHERE drive_id IN (SELECT id FROM certification_drives WHERE is_test_data = true);

DELETE FROM audit_log
WHERE entity_type = 'certification_drives'
  AND entity_id IN (SELECT id FROM certification_drives WHERE is_test_data = true);

DELETE FROM certification_drives WHERE is_test_data = true;

DELETE FROM verification_results
WHERE record_id IN (SELECT id FROM certificate_records WHERE is_test_data = true);

DELETE FROM audit_log
WHERE entity_type = 'certificate_record'
  AND entity_id IN (SELECT id FROM certificate_records WHERE is_test_data = true);

DELETE FROM escalations
WHERE employee_id IN (SELECT id FROM employees WHERE is_test_data = true);

DELETE FROM certificate_records WHERE is_test_data = true;

DELETE FROM employees WHERE is_test_data = true;

COMMIT;
