-- Example target tables for the local compose setup and the tests.
-- The pipeline never creates or alters target tables. In a real deployment they
-- already exist and the mapping in config/sources/*.yaml points to them.

CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS example;

CREATE TABLE example.regions (
    id         serial PRIMARY KEY,
    region_key text NOT NULL,
    name       text,
    kind       text,
    updated_on date,
    source     text,
    geom       geometry(MULTIPOLYGON, 4326)
);

CREATE TABLE example.indicator_values (
    id         bigserial PRIMARY KEY,
    region_key text NOT NULL,
    indicator  text NOT NULL,
    period     text NOT NULL,
    value      double precision,
    level      text,
    source     text NOT NULL
);

CREATE TABLE example.dataset_metadata (
    dataset     text PRIMARY KEY,
    publisher   text,
    title       text,
    license     text,
    attribution text,
    source_url  text,
    modified    timestamptz
);
