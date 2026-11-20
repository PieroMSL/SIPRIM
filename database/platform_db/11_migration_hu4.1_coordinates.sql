-- =========================================================
-- PMV 3 - HU4.1: Coordenadas geográficas versionadas y opcionales
-- Idempotente para bases de datos nuevas o existentes.
-- Estrictamente DDL (sin datos semilla ni conversión de direcciones).
-- =========================================================

ALTER TABLE IF EXISTS project_versions
    ADD COLUMN IF NOT EXISTS latitude NUMERIC(9, 6),
    ADD COLUMN IF NOT EXISTS longitude NUMERIC(9, 6),
    ADD COLUMN IF NOT EXISTS coordinates_origin VARCHAR(24),
    ADD COLUMN IF NOT EXISTS coordinates_source VARCHAR(240);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_versions_latitude_range_check'
    ) THEN
        ALTER TABLE project_versions
        ADD CONSTRAINT project_versions_latitude_range_check
        CHECK (latitude IS NULL OR (latitude >= -90 AND latitude <= 90));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_versions_longitude_range_check'
    ) THEN
        ALTER TABLE project_versions
        ADD CONSTRAINT project_versions_longitude_range_check
        CHECK (longitude IS NULL OR (longitude >= -180 AND longitude <= 180));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_versions_coordinates_pair_check'
    ) THEN
        ALTER TABLE project_versions
        ADD CONSTRAINT project_versions_coordinates_pair_check
        CHECK (
            (latitude IS NULL AND longitude IS NULL)
            OR (latitude IS NOT NULL AND longitude IS NOT NULL)
        );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'project_versions_coordinates_origin_check'
    ) THEN
        ALTER TABLE project_versions
        ADD CONSTRAINT project_versions_coordinates_origin_check
        CHECK (
            coordinates_origin IS NULL
            OR coordinates_origin IN ('declared', 'official_cadastre', 'field_survey', 'public', 'simulated')
        );
    END IF;
END $$;
