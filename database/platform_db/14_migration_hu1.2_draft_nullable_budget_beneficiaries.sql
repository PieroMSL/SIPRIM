-- =========================================================
-- PMV 1 · HU1.2: Soporte de borradores sin presupuesto ni beneficiarios definitivos
-- Permite que project_versions.estimated_budget_pen y beneficiaries_count
-- sean NULL en borradores incompletos, conservando CHECK (> 0) cuando no son NULL.
-- Idempotente para bases de datos nuevas o existentes. NO aplicar automáticamente a Supabase.
-- =========================================================

ALTER TABLE IF EXISTS project_versions
    ALTER COLUMN estimated_budget_pen DROP NOT NULL,
    ALTER COLUMN beneficiaries_count DROP NOT NULL;

DO $$
DECLARE
    rec RECORD;
BEGIN
    -- Eliminar cualquier constraint CHECK heredado de una sola columna sobre estimated_budget_pen o beneficiaries_count
    -- para reemplazarlo por restricciones que permitan NULL en borradores y exijan > 0 si hay valor.
    FOR rec IN
        SELECT c.conname
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        WHERE t.relname = 'project_versions'
          AND n.nspname = current_schema()
          AND c.contype = 'c'
          AND (
              pg_get_constraintdef(c.oid) ILIKE '%estimated_budget_pen%'
              OR pg_get_constraintdef(c.oid) ILIKE '%beneficiaries_count%'
          )
    LOOP
        EXECUTE format('ALTER TABLE project_versions DROP CONSTRAINT IF EXISTS %I', rec.conname);
    END LOOP;

    ALTER TABLE project_versions
        ADD CONSTRAINT project_versions_estimated_budget_pen_check
        CHECK (estimated_budget_pen IS NULL OR estimated_budget_pen > 0);

    ALTER TABLE project_versions
        ADD CONSTRAINT project_versions_beneficiaries_count_check
        CHECK (beneficiaries_count IS NULL OR beneficiaries_count > 0);
END $$;
