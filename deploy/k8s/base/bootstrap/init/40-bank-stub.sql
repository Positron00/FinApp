-- Bootstrap for schema bank_stub (PAY-606).
\set schema bank_stub
\set migrator bank_stub_migrator
\set runtime bank_stub_app
\set migrator_env BANK_STUB_DB_MIGRATOR_PASSWORD
\set runtime_env BANK_STUB_DB_PASSWORD
\ir lib/service_schema.psql
