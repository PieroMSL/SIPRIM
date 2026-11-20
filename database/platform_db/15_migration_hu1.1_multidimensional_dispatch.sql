-- =========================================================
-- PMV 1 - HU1.1: Carga única de expediente y distribución a los 5 especialistas
-- Migración idempotente para platform_db (sin modificar datos reales).
-- =========================================================

ALTER TABLE IF EXISTS projects
    ADD COLUMN IF NOT EXISTS creation_idempotency_key VARCHAR(120),
    ADD COLUMN IF NOT EXISTS content_fingerprint VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_creation_idempotency_key
    ON projects(creation_idempotency_key)
    WHERE creation_idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_projects_content_fingerprint
    ON projects(content_fingerprint)
    WHERE content_fingerprint IS NOT NULL;

CREATE TABLE IF NOT EXISTS evaluation_dimension_dispatches (
    id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL REFERENCES evaluations(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_version_id UUID NOT NULL REFERENCES project_versions(id) ON DELETE CASCADE,
    dimension_code VARCHAR(24) NOT NULL
        CHECK (dimension_code IN ('economic', 'social', 'environmental', 'technical', 'legal')),
    status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'insufficient_data', 'failed', 'pending_review', 'requires_review')),
    status_label VARCHAR(80) NOT NULL DEFAULT 'Pendiente',
    result_reference_id UUID,
    summary TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    dispatched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_eval_dispatch_version_dim UNIQUE (project_version_id, dimension_code)
);

CREATE INDEX IF NOT EXISTS idx_eval_dispatches_eval
    ON evaluation_dimension_dispatches(evaluation_id, dimension_code);

CREATE INDEX IF NOT EXISTS idx_eval_dispatches_version
    ON evaluation_dimension_dispatches(project_version_id, dimension_code);
