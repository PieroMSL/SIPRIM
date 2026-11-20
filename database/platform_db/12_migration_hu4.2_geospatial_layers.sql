-- =========================================================
-- PMV 3 - HU4.2: Capas geoespaciales versionadas y verificables
-- Idempotente para bases de datos nuevas o existentes.
-- Estrictamente DDL (sin geometrias ficticias ni datos personales).
-- =========================================================

CREATE TABLE IF NOT EXISTS project_geospatial_layers (
    id UUID PRIMARY KEY,
    project_version_id UUID NOT NULL REFERENCES project_versions(id) ON DELETE RESTRICT,
    layer_type VARCHAR(40) NOT NULL
        CHECK (layer_type IN ('influence_area', 'beneficiary_coverage', 'territorial_zone')),
    layer_name VARCHAR(160) NOT NULL,
    meaning TEXT NOT NULL,
    source_name VARCHAR(240) NOT NULL,
    data_origin VARCHAR(24) NOT NULL DEFAULT 'declared'
        CHECK (data_origin IN ('declared', 'official_cadastre', 'field_survey', 'public', 'simulated')),
    availability_status VARCHAR(24) NOT NULL DEFAULT 'available'
        CHECK (availability_status IN ('available', 'unavailable')),
    geometry_type VARCHAR(24)
        CHECK (geometry_type IS NULL OR geometry_type IN ('Point', 'LineString', 'Polygon', 'MultiPolygon')),
    geojson_data JSONB,
    aggregate_beneficiaries_count INTEGER
        CHECK (aggregate_beneficiaries_count IS NULL OR aggregate_beneficiaries_count >= 0),
    created_by_user_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT geospatial_layer_availability_check CHECK (
        (availability_status = 'unavailable' AND geojson_data IS NULL)
        OR (availability_status = 'available' AND geojson_data IS NOT NULL AND geometry_type IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_geo_layers_version
    ON project_geospatial_layers(project_version_id, layer_type);
