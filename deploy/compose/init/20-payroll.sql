-- Bootstrap for schema payroll (PAY-606).
\set schema payroll
\set migrator payroll_migrator
\set runtime payroll_app
\set migrator_env PAYROLL_DB_MIGRATOR_PASSWORD
\set runtime_env PAYROLL_DB_PASSWORD
\ir lib/service_schema.psql
