-- =========================================================
-- PMV 3 - HU5.3: Rol AUDITOR (Auditor / Contraloria) en auth_db
-- Solo crea el rol; no inserta cuentas de demostracion ni hashes.
-- Compatible con sesiones JWT existentes e idempotente.
-- =========================================================
INSERT INTO roles (id, code, name) VALUES
    ('00000000-0000-4000-a000-000000000007', 'AUDITOR', 'Auditor / Contraloría')
ON CONFLICT (code) DO NOTHING;
