-- Bootstrap for schema payouts (PAY-606).
\set schema payouts
\set migrator payouts_migrator
\set runtime payouts_app
\set migrator_env PAYOUTS_DB_MIGRATOR_PASSWORD
\set runtime_env PAYOUTS_DB_PASSWORD
\ir lib/service_schema.psql
