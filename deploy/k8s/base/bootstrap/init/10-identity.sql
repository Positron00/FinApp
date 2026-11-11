-- Bootstrap for schema identity (PAY-606).
\set schema identity
\set migrator identity_migrator
\set runtime identity_app
\set migrator_env IDENTITY_DB_MIGRATOR_PASSWORD
\set runtime_env IDENTITY_DB_PASSWORD
\ir lib/service_schema.psql
-- C50: the audit log is append-only for the runtime role. The template's re-grant above would restore
-- UPDATE/DELETE on a re-run (e.g. a password rotation), so revoke again whenever the table exists.
SELECT 'REVOKE UPDATE, DELETE ON TABLE identity.audit_log FROM identity_app'
 WHERE to_regclass('identity.audit_log') IS NOT NULL \gexec
