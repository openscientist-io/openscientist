-- PostgreSQL initialization script for SHANDY
-- This script runs once when the postgres container is first created.
--
-- Creates the admin role for elevated database operations.
-- The admin role bypasses Row-Level Security (RLS) policies.

-- Create admin role with BYPASSRLS privilege
-- Uses the same password as the main user for simplicity in development.
-- In production, use a different secure password.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'shandy_admin') THEN
        CREATE ROLE shandy_admin WITH LOGIN PASSWORD 'shandy_dev_password' BYPASSRLS;
        RAISE NOTICE 'Created shandy_admin role with BYPASSRLS privilege';
    END IF;
END
$$;

-- Grant database access
GRANT ALL PRIVILEGES ON DATABASE shandy TO shandy_admin;

-- Grant schema access (run after tables are created by migrations)
-- These will be applied to existing tables
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO shandy_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO shandy_admin;

-- Grant default privileges for future tables created by migrations
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO shandy_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO shandy_admin;
