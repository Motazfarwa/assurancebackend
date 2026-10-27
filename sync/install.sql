-- Offline sync: change capture and replay, installed on BOTH databases (agency PC and Render).
-- Re-running it is safe. Nothing in the dbo tables is altered: the legacy software keeps working unchanged.
--
-- The dbo tables have no primary keys, so a change is replayed on the other side by matching the whole row:
-- every captured change keeps the row before (old_row) and after (new_row) as jsonb.

CREATE SCHEMA IF NOT EXISTS sync;

-- Every insert / update / delete / truncate made on a synced table, in commit order of capture.
CREATE TABLE IF NOT EXISTS sync.changes (
    id bigserial PRIMARY KEY,
    tbl text NOT NULL,
    op char(1) NOT NULL CHECK (op IN ('I', 'U', 'D', 'T')),
    old_row jsonb,
    new_row jsonb,
    at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS changes_at_idx ON sync.changes (at);

-- Key / value state: "cursor:<source>" = last change of that source applied here, worker status...
CREATE TABLE IF NOT EXISTS sync.state (
    key text PRIMARY KEY,
    value text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Changes that could not be replayed (the row was changed on both sides), kept for review.
CREATE TABLE IF NOT EXISTS sync.conflicts (
    id bigserial PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    direction text NOT NULL,           -- 'push' (agency -> Render) or 'pull' (Render -> agency)
    source_change_id bigint NOT NULL,
    tbl text NOT NULL,
    op char(1) NOT NULL,
    old_row jsonb,
    new_row jsonb,
    error text,                        -- set when the change could not be replayed at all
    resolved_at timestamptz,
    resolved_by text
);

-- Tables never synced: scratch tables the legacy software fills and empties, and its audit log.
CREATE TABLE IF NOT EXISTS sync.excluded (pattern text PRIMARY KEY);
INSERT INTO sync.excluded (pattern) VALUES ('TTemp%'), ('TMouchard')
ON CONFLICT (pattern) DO NOTHING;


CREATE OR REPLACE FUNCTION sync.capture() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    -- A change replayed by the sync worker is not captured again (no echo).
    IF current_setting('sync.applying', true) = 'on' THEN
        RETURN NULL;
    END IF;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO sync.changes (tbl, op, new_row) VALUES (TG_TABLE_NAME, 'I', to_jsonb(NEW));
    ELSIF TG_OP = 'UPDATE' THEN
        IF to_jsonb(OLD) IS DISTINCT FROM to_jsonb(NEW) THEN
            INSERT INTO sync.changes (tbl, op, old_row, new_row) VALUES (TG_TABLE_NAME, 'U', to_jsonb(OLD), to_jsonb(NEW));
        END IF;
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO sync.changes (tbl, op, old_row) VALUES (TG_TABLE_NAME, 'D', to_jsonb(OLD));
    ELSE -- TRUNCATE
        INSERT INTO sync.changes (tbl, op) VALUES (TG_TABLE_NAME, 'T');
    END IF;
    RETURN NULL;
END
$$;


-- Attaches the capture triggers to every dbo table that is not excluded (run again after creating tables).
CREATE OR REPLACE FUNCTION sync.attach_all() RETURNS integer LANGUAGE plpgsql AS $$
DECLARE
    t record;
    attached integer := 0;
BEGIN
    FOR t IN
        SELECT c.relname
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'dbo' AND c.relkind IN ('r', 'p')
          AND NOT EXISTS (SELECT 1 FROM sync.excluded e WHERE c.relname LIKE e.pattern)
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS sync_capture_row ON dbo.%I', t.relname);
        EXECUTE format('DROP TRIGGER IF EXISTS sync_capture_truncate ON dbo.%I', t.relname);
        EXECUTE format('CREATE TRIGGER sync_capture_row AFTER INSERT OR UPDATE OR DELETE ON dbo.%I '
                       'FOR EACH ROW EXECUTE FUNCTION sync.capture()', t.relname);
        EXECUTE format('CREATE TRIGGER sync_capture_truncate AFTER TRUNCATE ON dbo.%I '
                       'FOR EACH STATEMENT EXECUTE FUNCTION sync.capture()', t.relname);
        attached := attached + 1;
    END LOOP;
    RETURN attached;
END
$$;


-- Replays one change captured on the other database. Returns false when the row to update or delete
-- is not found here (it was changed here too): the caller records a conflict.
CREATE OR REPLACE FUNCTION sync.apply_change(p_tbl text, p_op text, p_old jsonb, p_new jsonb)
RETURNS boolean LANGUAGE plpgsql AS $$
DECLARE
    rel regclass := format('dbo.%I', p_tbl)::regclass;
    insert_cols text;
    update_cols text;
    target tid;
BEGIN
    PERFORM set_config('sync.applying', 'on', true); -- for this transaction only

    IF p_op = 'T' THEN
        EXECUTE format('TRUNCATE %s', rel);
        RETURN true;
    END IF;

    -- Generated columns are computed, identity "always" columns cannot be updated.
    SELECT string_agg(format('%I', attname), ', ' ORDER BY attnum) FILTER (WHERE attgenerated = ''),
           string_agg(format('%I', attname), ', ' ORDER BY attnum) FILTER (WHERE attgenerated = '' AND attidentity <> 'a')
      INTO insert_cols, update_cols
      FROM pg_attribute
     WHERE attrelid = rel AND attnum > 0 AND NOT attisdropped;

    IF p_op = 'I' THEN
        EXECUTE format('INSERT INTO %s (%s) OVERRIDING SYSTEM VALUE SELECT %s FROM jsonb_populate_record(NULL::%s, $1)',
                       rel, insert_cols, insert_cols, rel)
          USING p_new;
        RETURN true;
    END IF;

    -- The row as it was on the source; identical duplicates are interchangeable, so any one of them will do.
    EXECUTE format('SELECT t.ctid FROM %s t WHERE to_jsonb(t) = $1 LIMIT 1 FOR UPDATE', rel)
       INTO target
      USING p_old;
    IF target IS NULL THEN
        RETURN false;
    END IF;

    IF p_op = 'D' THEN
        EXECUTE format('DELETE FROM %s WHERE ctid = $1', rel) USING target;
    ELSE
        EXECUTE format('UPDATE %s SET (%s) = (SELECT %s FROM jsonb_populate_record(NULL::%s, $1)) WHERE ctid = $2',
                       rel, update_cols, update_cols, rel)
          USING p_new, target;
    END IF;
    RETURN true;
END
$$;


-- After replayed inserts, serial / identity sequences must not hand out numbers already used.
CREATE OR REPLACE FUNCTION sync.bump_sequences() RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    s record;
    highest bigint;
BEGIN
    FOR s IN
        SELECT seq.oid::regclass AS seq, tab.oid::regclass AS tab, att.attname AS col
        FROM pg_depend d
        JOIN pg_class seq ON seq.oid = d.objid AND seq.relkind = 'S'
        JOIN pg_class tab ON tab.oid = d.refobjid
        JOIN pg_namespace n ON n.oid = tab.relnamespace AND n.nspname = 'dbo'
        JOIN pg_attribute att ON att.attrelid = tab.oid AND att.attnum = d.refobjsubid
        WHERE d.deptype IN ('a', 'i')
    LOOP
        EXECUTE format('SELECT max(%I)::bigint FROM %s', s.col, s.tab) INTO highest;
        IF highest IS NOT NULL THEN
            EXECUTE format('SELECT setval(%L, GREATEST(%s, (SELECT last_value FROM %s)))', s.seq, highest, s.seq);
        END IF;
    END LOOP;
END
$$;


SELECT sync.attach_all();
