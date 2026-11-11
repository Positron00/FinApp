-- PayRun DB bootstrap (PAY-606; platform LLD §2.1). Runs as the bootstrap admin
-- (POSTGRES_USER in Compose, the managed admin in uat/prod). Idempotent.
REVOKE ALL ON DATABASE payrun FROM PUBLIC;            -- no CONNECT/TEMP for unknown roles
REVOKE ALL ON SCHEMA public FROM PUBLIC;              -- nothing lives in public
