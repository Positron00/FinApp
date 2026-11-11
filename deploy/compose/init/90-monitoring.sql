-- Read-only monitoring role for postgres-exporter (pg_monitor: stats views, no table data).
\getenv exporter_pw POSTGRES_EXPORTER_PASSWORD
\if :{?exporter_pw}
\else
  DO $$ BEGIN RAISE EXCEPTION 'bootstrap: POSTGRES_EXPORTER_PASSWORD is not set'; END $$;
\endif
SELECT 'CREATE ROLE postgres_exporter LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE CONNECTION LIMIT 3'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres_exporter') \gexec
SELECT format('ALTER ROLE postgres_exporter PASSWORD %L', :'exporter_pw') \gexec
GRANT pg_monitor TO postgres_exporter;
GRANT CONNECT ON DATABASE payrun TO postgres_exporter;
ALTER ROLE postgres_exporter SET search_path = pg_catalog;
