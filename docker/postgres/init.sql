-- PostgreSQL initialization script for SHANDY
-- This script runs once when the postgres container is first created.
--
-- Creates two roles for the dual-engine pattern:
--   shandy_app   — non-superuser, subject to RLS (used by get_session)
--   shandy_admin — BYPASSRLS, for admin/background operations (used by get_admin_session)

-- Create app role (subject to RLS)
-- This is a NOLOGIN role; the main connection user (shandy) does SET ROLE shandy_app
-- to drop privileges and become subject to Row-Level Security policies.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'shandy_app') THEN
        CREATE ROLE shandy_app NOLOGIN;
        RAISE NOTICE 'Created shandy_app role (subject to RLS)';
    END IF;
END
$$;

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
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO shandy_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO shandy_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO shandy_admin;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO shandy_admin;

-- Grant default privileges for future tables created by migrations
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO shandy_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO shandy_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO shandy_admin;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO shandy_admin;
