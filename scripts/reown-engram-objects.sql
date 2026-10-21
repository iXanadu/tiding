-- Usage (ENGRAM-DB-ROLE-1): psql -d engram -f scripts/reown-engram-objects.sql, as the superuser,
-- AFTER `ALTER DATABASE engram OWNER TO engram`. Rehearsed 2026-10-15 on the test instance;
-- run on prod 2026-10-21 (the column-owned sequence skip below was found there).
-- ENGRAM-DB-ROLE-1: hand every engram-schema object in THIS database to role engram.
-- Per-object, never REASSIGN OWNED (that would also move other databases the superuser owns).
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f')
             AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=c.oid AND d.deptype='e')
             -- serial/identity sequences follow their table; ALTER on them errors
             AND NOT (c.relkind='S' AND EXISTS (SELECT 1 FROM pg_depend d
                      WHERE d.objid=c.oid AND d.classid='pg_class'::regclass
                        AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i')))
  LOOP
    EXECUTE format('ALTER %s public.%I OWNER TO engram',
      CASE r.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                     WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
      r.relname);
  END LOOP;
  FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
           WHERE n.nspname='public'
             AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid=p.oid AND d.deptype='e')
  LOOP
    EXECUTE format('ALTER ROUTINE %s OWNER TO engram', r.sig);
  END LOOP;
END $$;
