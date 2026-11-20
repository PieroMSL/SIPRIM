-- =========================================================
-- PMV 3 - HU3.3: Registro de decisiones HITL en checkpoints con huella SHA-256
-- Idempotente para bases de datos nuevas o existentes.
-- Estrictamente DDL (sin datos semilla de demostracion).
-- =========================================================

CREATE TABLE IF NOT EXISTS checkpoint_review_decisions (
    id UUID PRIMARY KEY,
    checkpoint_id UUID NOT NULL REFERENCES evaluation_checkpoints(id) ON DELETE CASCADE,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_version_id UUID NOT NULL REFERENCES project_versions(id) ON DELETE CASCADE,
    decision VARCHAR(32) NOT NULL
        CHECK (decision IN ('approve_continue', 'request_correction')),
    justification TEXT NOT NULL,
    conditions TEXT,
    decided_by_user_id UUID NOT NULL,
    decided_by_name VARCHAR(160),
    decided_by_email VARCHAR(254),
    decided_at TIMESTAMPTZ NOT NULL,
    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
    canonical_payload JSONB NOT NULL,
    integrity_hash_sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cp_decisions_checkpoint
    ON checkpoint_review_decisions(checkpoint_id, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_cp_decisions_version
    ON checkpoint_review_decisions(project_version_id, decided_at DESC);

ALTER TABLE evaluation_checkpoints
    ADD COLUMN IF NOT EXISTS decision_id UUID REFERENCES checkpoint_review_decisions(id) ON DELETE SET NULL;
