-- Bootstrap for schema payroll (PAY-606).
\set schema payroll
\set migrator payroll_migrator
\set runtime payroll_app
\set migrator_env PAYROLL_DB_MIGRATOR_PASSWORD
\set runtime_env PAYROLL_DB_PASSWORD
\ir lib/service_schema.psql
-- C73 (as C50 for identity): the audit log is append-only for the runtime role. The template's
-- re-grant above would restore UPDATE/DELETE on a re-run (e.g. a password rotation), so revoke
-- again whenever the table exists.
SELECT 'REVOKE UPDATE, DELETE, TRUNCATE ON TABLE payroll.audit_events FROM payroll_app'
 WHERE to_regclass('payroll.audit_events') IS NOT NULL \gexec
