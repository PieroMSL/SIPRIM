-- =========================================================
-- PMV 3 - HU3.3: Rol PLANNING_MANAGER (Gerente de Planeamiento) en auth_db
-- Solo crea el rol; no inserta cuentas de demostracion ni hashes.
-- Compatible con sesiones JWT existentes e idempotente.
-- =========================================================
INSERT INTO roles (id, code, name) VALUES
    ('00000000-0000-4000-a000-000000000006', 'PLANNING_MANAGER', 'Gerente de Planeamiento')
ON CONFLICT (code) DO NOTHING;
