-- Sutradhar local database bootstrap.
-- Run as a PostgreSQL superuser:
--   psql -U postgres -h localhost -f scripts/bootstrap_db.sql
--
-- Creates the sutradhar role plus the dev and test databases it owns,
-- then enables pgcrypto and citext in each. Safe to re-run.

\set ON_ERROR_STOP on

-- --- Role -------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sutradhar') THEN
        CREATE ROLE sutradhar LOGIN PASSWORD 'sutradhar_dev';
    ELSE
        ALTER ROLE sutradhar LOGIN PASSWORD 'sutradhar_dev';
    END IF;
END
$$;

-- --- Databases (CREATE DATABASE cannot run inside a DO block) ---------
SELECT 'CREATE DATABASE sutradhar_dev OWNER sutradhar ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sutradhar_dev')
\gexec

SELECT 'CREATE DATABASE sutradhar_test OWNER sutradhar ENCODING ''UTF8'''
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'sutradhar_test')
\gexec

-- --- Extensions, per database ----------------------------------------
\connect sutradhar_dev
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
GRANT ALL ON SCHEMA public TO sutradhar;
ALTER SCHEMA public OWNER TO sutradhar;

\connect sutradhar_test
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
GRANT ALL ON SCHEMA public TO sutradhar;
ALTER SCHEMA public OWNER TO sutradhar;

\echo 'bootstrap complete'
