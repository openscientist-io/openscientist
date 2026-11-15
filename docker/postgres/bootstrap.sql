-- Database logins and ownership, run by the image superuser before every migration.
-- Every statement is idempotent. The passwords come from the environment:
-- POSTGRES_OWNER_PASSWORD, POSTGRES_WEB_PASSWORD, POSTGRES_ADMIN_PASSWORD.

\set ON_ERROR_STOP on

\getenv owner_password POSTGRES_OWNER_PASSWORD
\getenv web_password POSTGRES_WEB_PASSWORD
\getenv admin_password POSTGRES_ADMIN_PASSWORD
\if :{?owner_password} \else \set owner_password '' \endif
\if :{?web_password} \else \set web_password '' \endif
\if :{?admin_password} \else \set admin_password '' \endif

SELECT :'owner_password' = '' AS owner_missing,
       :'web_password' = '' AS web_missing,
       :'admin_password' = '' AS admin_missing \gset
\if :owner_missing
DO $$ BEGIN RAISE EXCEPTION 'POSTGRES_OWNER_PASSWORD is not set'; END $$;
\endif
\if :web_missing
DO $$ BEGIN RAISE EXCEPTION 'POSTGRES_WEB_PASSWORD is not set'; END $$;
\endif
\if :admin_missing
DO $$ BEGIN RAISE EXCEPTION 'POSTGRES_ADMIN_PASSWORD is not set'; END $$;
\endif

BEGIN;

SELECT format('CREATE ROLE %I', name)
FROM unnest(ARRAY['openscientist_app', 'openscientist_agent', 'openscientist_admin',
                  'openscientist_owner', 'openscientist_web']) AS name
WHERE NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = name) \gexec

-- Groups that only the logins below switch to or grant.
ALTER ROLE openscientist_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
ALTER ROLE openscientist_agent NOLOGIN NOSUPERUSER NOBYPASSRLS;
-- The web's admin sessions.
ALTER ROLE openscientist_admin LOGIN NOSUPERUSER BYPASSRLS PASSWORD :'admin_password';
-- Owns the schema and runs migrations. The SECURITY DEFINER functions it owns read
-- tables with forced RLS, so it needs BYPASSRLS.
ALTER ROLE openscientist_owner LOGIN NOSUPERUSER BYPASSRLS CREATEROLE PASSWORD :'owner_password';
-- The web's own login. CREATEROLE manages only the roles it holds ADMIN on.
ALTER ROLE openscientist_web LOGIN NOSUPERUSER NOBYPASSRLS CREATEROLE PASSWORD :'web_password';

GRANT openscientist_app, openscientist_agent, openscientist_admin TO openscientist_owner
    WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
GRANT openscientist_app TO openscientist_web WITH INHERIT FALSE, SET TRUE;
GRANT openscientist_agent TO openscientist_web WITH ADMIN TRUE, INHERIT FALSE, SET FALSE;
-- Ends an agent login's sessions before it is dropped.
GRANT pg_signal_backend TO openscientist_web;

ALTER DATABASE :"DBNAME" OWNER TO openscientist_owner;

-- Objects the superuser created before the owner existed. REASSIGN OWNED is refused
-- for the bootstrap superuser, so each object moves on its own. Indexes and owned
-- sequences follow their table, and extension members stay with the extension.
DO $$
DECLARE
    obj record;
BEGIN
    FOR obj IN
        SELECT c.oid::regclass AS name,
               CASE c.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                              WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END AS kind
        FROM pg_catalog.pg_class c
        WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
          AND c.relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend d
              WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid
                AND (d.deptype = 'e' OR (c.relkind = 'S' AND d.deptype IN ('a', 'i')))
          )
    LOOP
        EXECUTE format('ALTER %s %s OWNER TO openscientist_owner', obj.kind, obj.name);
    END LOOP;

    FOR obj IN
        SELECT p.oid::regprocedure AS name
        FROM pg_catalog.pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace
          AND p.proowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend d
              WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid AND d.deptype = 'e'
          )
    LOOP
        EXECUTE format('ALTER ROUTINE %s OWNER TO openscientist_owner', obj.name);
    END LOOP;

    FOR obj IN
        SELECT t.oid::regtype AS name
        FROM pg_catalog.pg_type t
        WHERE t.typnamespace = 'public'::regnamespace
          AND t.typtype IN ('e', 'd')
          AND t.typowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname = current_user)
          AND NOT EXISTS (
              SELECT FROM pg_catalog.pg_depend d
              WHERE d.classid = 'pg_type'::regclass AND d.objid = t.oid AND d.deptype = 'e'
          )
    LOOP
        EXECUTE format('ALTER TYPE %s OWNER TO openscientist_owner', obj.name);
    END LOOP;
END
$$;

-- Tables and sequences the owner creates reach the app and admin roles. The app
-- role gets no TRUNCATE, which row-level security does not cover.
ALTER DEFAULT PRIVILEGES FOR ROLE openscientist_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO openscientist_app;
ALTER DEFAULT PRIVILEGES FOR ROLE openscientist_owner IN SCHEMA public
    GRANT ALL PRIVILEGES ON TABLES TO openscientist_admin;
ALTER DEFAULT PRIVILEGES FOR ROLE openscientist_owner IN SCHEMA public
    GRANT ALL PRIVILEGES ON SEQUENCES TO openscientist_app, openscientist_admin;

COMMIT;
