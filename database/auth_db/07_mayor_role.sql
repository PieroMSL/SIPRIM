-- =========================================================
-- PMV 3 - HU4.1: Rol MAYOR (Alcalde) en auth_db
-- Solo crea el rol; no inserta cuentas de demostracion ni hashes.
-- Compatible con sesiones JWT existentes e idempotente.
-- =========================================================
INSERT INTO roles (id, code, name) VALUES
    ('00000000-0000-4000-a000-000000000005', 'MAYOR', 'Alcalde')
ON CONFLICT (code) DO NOTHING;
