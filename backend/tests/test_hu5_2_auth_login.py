import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import AuthService
from backend.application.services.validated_auth_service import ValidatedAuthService
from backend.domain.exceptions.pmv1_exceptions import CredencialesInvalidasError
from backend.infrastructure.config.dependencies import get_auth_service, get_platform_service
from backend.main import create_app


def _make_auth_service(db_url: str = "postgresql://fake/auth_db") -> ValidatedAuthService:
    return ValidatedAuthService(
        db_url=db_url,
        jwt_secret="test-secret-key-min-32-characters-hu52",
        issuer="siprim-auth-test",
        audience="siprim-api-test",
        expiration_minutes=30,
    )


def test_hu5_2_ca02_generic_rejection_and_equalized_hash_verify():
    """Correo inexistente, cuenta inactiva y clave incorrecta ejecutan Argon2 y dan el mismo error."""
    svc = _make_auth_service()
    active_hash = svc.password_hasher.hash("ClaveValida2026!")

    scenarios = [
        None,  # correo inexistente
        {"id": uuid4(), "full_name": "Inactivo", "password_hash": active_hash, "is_active": False, "roles": ["PLANNER"]},
        {"id": uuid4(), "full_name": "Activo", "password_hash": active_hash, "is_active": True, "roles": ["PLANNER"]},
    ]

    for row in scenarios:
        fake_conn = MagicMock()
        fake_conn.__enter__.return_value = fake_conn
        fake_conn.__exit__.return_value = False
        fake_conn.execute.return_value.fetchone.return_value = row

        verify_calls = 0
        real_hasher = svc.password_hasher

        class _SpyHasher:
            def verify(self, h: str, p: str) -> bool:
                nonlocal verify_calls
                verify_calls += 1
                return real_hasher.verify(h, p)

        svc.password_hasher = _SpyHasher()  # type: ignore[assignment]
        try:
            with (
                patch("backend.application.services.pmv1_services._is_db_available", return_value=True),
                patch("backend.application.services.pmv1_services.psycopg.connect", return_value=fake_conn),
            ):
                try:
                    svc.login("usuario@siprim.test", "ClaveErronea2026!")
                    raise AssertionError("Se esperaba CredencialesInvalidasError")
                except CredencialesInvalidasError as exc:
                    assert str(exc) == "Credenciales invalidas"
                    assert verify_calls == 1
        finally:
            svc.password_hasher = real_hasher


def test_hu5_2_ca03_expired_token_returns_401_and_blocks_protected_action():
    """Un token caducado devuelve 401 y nunca ejecuta la accion protegida en PlatformService."""
    auth_svc = _make_auth_service()
    app = create_app()
    fake_platform = MagicMock()
    app.dependency_overrides[get_auth_service] = lambda: auth_svc
    app.dependency_overrides[get_platform_service] = lambda: fake_platform

    expired_claims = {
        "sub": str(uuid4()),
        "roles": ["PLANNER"],
        "iss": auth_svc.issuer,
        "aud": auth_svc.audience,
        "iat": datetime.now(timezone.utc) - timedelta(hours=2),
        "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
    }
    expired_token = jwt.encode(expired_claims, auth_svc.jwt_secret, algorithm="HS256")

    with TestClient(app) as client:
        res = client.post(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {expired_token}"},
            json={
                "code": "EXP-NO-WRITE-01",
                "title": "No debe crearse",
                "location": "El Tambo",
                "department": "Junin",
                "province": "Huancayo",
                "district": "El Tambo",
                "estimated_budget_pen": 100000,
                "beneficiaries_count": 100,
            },
        )
        assert res.status_code == 401
        fake_platform.create_project.assert_not_called()


def test_hu5_2_ca01_postgres_valid_login_and_permitted_access():
    """CA-HU5.2-01 y CA-HU5.2-02 contra auth_db real: cuenta activa inicia sesion con roles vigentes y cuenta inactiva falla con 401."""
    auth_db_url = (os.getenv("AUTH_DB_URL") or "").strip()
    if not auth_db_url:
        pytest.skip("AUTH_DB_URL no configurada para prueba PostgreSQL aislada")
    try:
        with psycopg.connect(auth_db_url) as conn:
            conn.execute("SELECT 1")
    except Exception:
        pytest.skip("auth_db PostgreSQL de prueba no accesible")

    auth_svc = _make_auth_service(db_url=auth_db_url)
    user_id = uuid4()
    email = f"hu52.valid.{str(user_id)[:8]}@siprim.test"
    raw_pwd = "ClaveSeguraHU52!"
    pwd_hash = auth_svc.password_hasher.hash(raw_pwd)

    try:
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            auditor_role_id = conn.execute(
                "SELECT id FROM roles WHERE code = 'AUDITOR'"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, is_active)
                VALUES (%s, %s, 'Auditor Prueba HU52', %s, true)
                """,
                (user_id, email, pwd_hash),
            )
            conn.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                (user_id, auditor_role_id),
            )

        app = create_app()
        fake_platform = MagicMock()
        fake_platform.list_auditable_projects.return_value = {"projects": []}
        app.dependency_overrides[get_auth_service] = lambda: auth_svc
        app.dependency_overrides[get_platform_service] = lambda: fake_platform

        with TestClient(app) as client:
            # 1. Login valido con cuenta activa -> 200 + JWT con rol vigente
            res_ok = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": raw_pwd},
            )
            assert res_ok.status_code == 200
            body = res_ok.json()
            assert body["roles"] == ["AUDITOR"]
            assert body["user_id"] == str(user_id)
            assert "password" not in body
            token = body["access_token"]

            # 2. Acceso a funcion permitida para AUDITOR -> 200; accion no permitida (POST /projects) -> 403
            res_me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
            assert res_me.status_code == 200
            assert res_me.json()["roles"] == ["AUDITOR"]

            res_audit = client.get(
                "/api/v1/audit/projects", headers={"Authorization": f"Bearer {token}"}
            )
            assert res_audit.status_code == 200

            res_forbidden = client.post(
                "/api/v1/projects",
                headers={"Authorization": f"Bearer {token}"},
                json={"code": "NO-AUTH", "title": "Bloqueado", "estimated_budget_pen": 1000, "beneficiaries_count": 10},
            )
            assert res_forbidden.status_code == 403

            # 3. Contrasena erronea -> 401 generico
            res_bad = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "ClaveIncorrecta999!"},
            )
            assert res_bad.status_code == 401
            assert raw_pwd not in res_bad.text
            assert pwd_hash not in res_bad.text

            # 4. Cuenta inactiva -> 401 generico incluso con clave correcta
            with psycopg.connect(auth_db_url) as conn, conn.transaction():
                conn.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (user_id,))
            res_inactive = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": raw_pwd},
            )
            assert res_inactive.status_code == 401
    finally:
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            conn.execute("DELETE FROM user_roles WHERE user_id = %s", (user_id,))
            conn.execute("DELETE FROM users WHERE id = %s", (user_id,))
