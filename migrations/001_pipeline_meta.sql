-- Control schema of the pipeline. This is the only schema the pipeline ever
-- creates or alters; target tables are never changed structurally.

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE pipeline_meta.runs (
    run_id       uuid PRIMARY KEY,
    trigger      text NOT NULL,
    started_at   timestamptz NOT NULL,
    finished_at  timestamptz,
    status       text NOT NULL,
    report_path  text
);

CREATE TABLE pipeline_meta.source_runs (
    run_id         uuid NOT NULL REFERENCES pipeline_meta.runs (run_id) ON DELETE CASCADE,
    source_id      text NOT NULL,
    started_at     timestamptz NOT NULL,
    finished_at    timestamptz,
    status         text NOT NULL,
    source_version text,
    fetched_rows   bigint NOT NULL DEFAULT 0,
    valid_rows     bigint NOT NULL DEFAULT 0,
    skipped_rows   bigint NOT NULL DEFAULT 0,
    new_rows       bigint NOT NULL DEFAULT 0,
    changed_rows   bigint NOT NULL DEFAULT 0,
    deleted_rows   bigint NOT NULL DEFAULT 0,
    baseline       boolean NOT NULL DEFAULT false,
    error          text,
    warnings       jsonb NOT NULL DEFAULT '[]'::jsonb,
    PRIMARY KEY (run_id, source_id)
);

-- Last imported state per source. The comparison runs against this table because
-- parts of a business key may not exist as columns in the target tables.
CREATE TABLE pipeline_meta.source_state (
    source_id    text  NOT NULL,
    key_hash     bytea NOT NULL,
    key_text     text  NOT NULL,
    content_hash bytea NOT NULL,
    last_run_id  uuid  NOT NULL,
    PRIMARY KEY (source_id, key_hash)
);

-- Parses GML, reprojects to the target CRS, forces 2D, repairs invalid geometries
-- and fits the result to the target geometry type. Never raises for bad input: one
-- broken feature must not abort the statement for all others. The status tells the
-- caller what happened. The hash is taken from the source coordinates, so a PROJ
-- upgrade cannot make unchanged data look changed.
CREATE FUNCTION pipeline_meta.process_gml(
    p_gml          text,
    p_fallback_srid integer,
    p_target_type  text,
    p_target_srid  integer,
    OUT out_geom   geometry,
    OUT out_hash   bytea,
    OUT out_status text
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_raw      geometry;
    v_geom     geometry;
    v_family   integer;
    v_multi    boolean := p_target_type LIKE 'MULTI%';
    v_repaired boolean := false;
BEGIN
    v_family := CASE
        WHEN p_target_type IN ('POINT', 'MULTIPOINT') THEN 1
        WHEN p_target_type IN ('LINESTRING', 'MULTILINESTRING') THEN 2
        WHEN p_target_type IN ('POLYGON', 'MULTIPOLYGON') THEN 3
    END;

    BEGIN
        v_raw := ST_GeomFromGML(p_gml, p_fallback_srid);
    EXCEPTION WHEN OTHERS THEN
        out_status := 'parse_error';
        RETURN;
    END;

    IF v_raw IS NULL OR ST_IsEmpty(v_raw) THEN
        out_status := 'empty';
        RETURN;
    END IF;

    v_raw := ST_Force2D(v_raw);
    out_hash := sha256(ST_AsEWKB(v_raw));

    BEGIN
        v_geom := ST_Transform(v_raw, p_target_srid);
    EXCEPTION WHEN OTHERS THEN
        out_status := 'transform_error';
        RETURN;
    END;

    IF NOT ST_IsValid(v_geom) THEN
        BEGIN
            v_geom := ST_MakeValid(v_geom);
            v_repaired := true;
        EXCEPTION WHEN OTHERS THEN
            out_status := 'invalid';
            RETURN;
        END;
    END IF;

    -- Repairing can yield a collection; keep only the parts of the target family.
    v_geom := ST_CollectionExtract(v_geom, v_family);
    IF v_geom IS NULL OR ST_IsEmpty(v_geom) THEN
        out_status := 'type_mismatch';
        RETURN;
    END IF;

    IF v_multi THEN
        v_geom := ST_Multi(v_geom);
    ELSIF ST_NumGeometries(v_geom) > 1 THEN
        out_status := 'type_mismatch';
        RETURN;
    ELSE
        v_geom := ST_GeometryN(v_geom, 1);
    END IF;

    out_geom := v_geom;
    out_status := CASE WHEN v_repaired THEN 'repaired' ELSE 'ok' END;
END;
$$;
