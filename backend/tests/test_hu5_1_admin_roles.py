import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.validated_auth_service import ValidatedAuthService
from backend.domain.exceptions.pmv1_exceptions import (
    AccesoDenegadoError,
    ConfiguracionPMV1Error,
    RecursoPMV1NoEncontradoError,
    ServicioNoDisponibleError,
)
from backend.infrastructure.config.dependencies import (
    get_auth_service,
    get_current_user,
    get_platform_service,
)
from backend.main import create_app


def _make_auth_service() -> ValidatedAuthService:
    return ValidatedAuthService(
        db_url="postgresql://fake/auth_db",
        jwt_secret="test-secret-key-min-32-characters-hu51",
        issuer="siprim-auth-test",
        audience="siprim-api-test",
        expiration_minutes=30,
    )


def test_hu5_1_admin_endpoints_auth_and_privilege_policy():
    app = create_app()
    fake_auth = MagicMock()
    target_id = uuid4()

    app.dependency_overrides[get_auth_service] = lambda: fake_auth

    # 1. Sin sesion -> 401
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/users").status_code == 401

    # 2. Rol no administrador (PLANNER / MAYOR) -> 403
    for role in ("PLANNER", "MAYOR", "LEGAL_ADVISOR"):
        app.dependency_overrides[get_current_user] = lambda r=role: {"sub": str(uuid4()), "roles": [r]}
        with TestClient(app) as client:
            assert client.get("/api/v1/admin/users").status_code == 403
            assert client.put(f"/api/v1/admin/users/{target_id}/roles", json={"roles": ["PLANNER"]}).status_code == 403

    # 3. ADMIN ordinario -> 200 en listado y actualizacion valida, pero 403 si intenta escalar a SUPERADMIN
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid4()), "roles": ["ADMIN"]}
    fake_auth.list_users_and_roles.return_value = {"users": [], "available_roles": []}
    fake_auth.update_user_roles.return_value = {
        "user": {
            "id": target_id,
            "email": "u@siprim.test",
            "full_name": "Usuario",
            "is_active": True,
            "roles": ["PLANNER"],
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        },
        "message": "Roles actualizados correctamente.",
    }
    with TestClient(app) as client:
        assert client.get("/api/v1/admin/users").status_code == 200
        res_ok = client.put(f"/api/v1/admin/users/{target_id}/roles", json={"roles": ["PLANNER"]})
        assert res_ok.status_code == 200
        assert res_ok.json()["user"]["roles"] == ["PLANNER"]

        # Duplicados -> 422 Pydantic
        assert client.put(f"/api/v1/admin/users/{target_id}/roles", json={"roles": ["PLANNER", "PLANNER"]}).status_code == 422

    # 4. Escalada a SUPERADMIN / usuario inexistente / rol invalido
    fake_auth.update_user_roles.side_effect = AccesoDenegadoError("Solo SUPERADMIN")
    with TestClient(app) as client:
        assert client.put(f"/api/v1/admin/users/{target_id}/roles", json={"roles": ["SUPERADMIN"]}).status_code == 403

    fake_auth.update_user_roles.side_effect = RecursoPMV1NoEncontradoError("No encontrado")
    with TestClient(app) as client:
        assert client.put(f"/api/v1/admin/users/{target_id}/roles", json={"roles": ["PLANNER"]}).status_code == 404

    fake_auth.update_user_roles.side_effect = ConfiguracionPMV1Error("Rol invalido")
    with TestClient(app) as client:
        assert client.put(f"/api/v1/admin/users/{target_id}/roles", json={"roles": ["INEXISTENTE"]}).status_code == 409


def test_hu5_1_revoked_role_in_old_jwt_is_denied_and_db_down_fails_closed():
    """Un JWT antiguo con PLANNER es rechazado con 403 si auth_db ya revoco PLANNER; y 503 si auth_db cae."""
    auth_svc = _make_auth_service()
    user_id = uuid4()
    old_token = jwt.encode(
        {
            "sub": str(user_id),
            "roles": ["PLANNER"],  # rol antiguo dentro del JWT
            "iss": auth_svc.issuer,
            "aud": auth_svc.audience,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=20),
        },
        auth_svc.jwt_secret,
        algorithm="HS256",
    )

    app = create_app()
    fake_platform = MagicMock()
    app.dependency_overrides[get_auth_service] = lambda: auth_svc
    app.dependency_overrides[get_platform_service] = lambda: fake_platform

    payload = {
        "code": "HU51-REVOKED-TEST",
        "title": "No debe crearse",
        "location": "El Tambo",
        "department": "Junin",
        "province": "Huancayo",
        "district": "El Tambo",
        "estimated_budget_pen": 100000,
        "beneficiaries_count": 100,
    }

    # 1. Roles vigentes en auth_db ya no incluyen PLANNER (ahora solo MAYOR) -> 403 y cero escrituras
    auth_svc.get_active_user_roles = MagicMock(return_value=["MAYOR"])  # type: ignore[method-assign]
    with TestClient(app) as client:
        res = client.post("/api/v1/projects", headers={"Authorization": f"Bearer {old_token}"}, json=payload)
        assert res.status_code == 403
        fake_platform.create_project.assert_not_called()

    # 2. Si auth_db no esta disponible -> 503 (fail-closed, nunca autoriza con roles antiguos del JWT)
    auth_svc.get_active_user_roles = MagicMock(  # type: ignore[method-assign]
        side_effect=ServicioNoDisponibleError("auth_db no disponible")
    )
    with TestClient(app) as client:
        res_down = client.post("/api/v1/projects", headers={"Authorization": f"Bearer {old_token}"}, json=payload)
        assert res_down.status_code == 503
        fake_platform.create_project.assert_not_called()


def test_hu5_1_postgres_role_assignment_and_immediate_revocation():
    """HU5.1 contra auth_db real: asignacion/cambio de roles (incluidos PLANNING_MANAGER y AUDITOR), bloqueo de escalada y revocacion con JWT previo."""
    auth_db_url = (os.getenv("AUTH_DB_URL") or "").strip()
    if not auth_db_url:
        pytest.skip("AUTH_DB_URL no configurada para prueba PostgreSQL aislada")
    try:
        with psycopg.connect(auth_db_url) as conn:
            conn.execute("SELECT 1")
    except Exception:
        pytest.skip("auth_db PostgreSQL de prueba no accesible")

    auth_svc = ValidatedAuthService(
        db_url=auth_db_url,
        jwt_secret="test-secret-key-min-32-characters-hu51",
        issuer="siprim-auth-test",
        audience="siprim-api-test",
        expiration_minutes=30,
    )
    admin_id = uuid4()
    target_id = uuid4()
    inactive_id = uuid4()
    raw_pwd = "PruebaHU51!"
    pwd_hash = auth_svc.password_hasher.hash(raw_pwd)

    try:
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            roles = dict(conn.execute("SELECT code, id FROM roles").fetchall())
            for rcode in ("ADMIN", "MAYOR", "PLANNER", "PLANNING_MANAGER", "AUDITOR", "SUPERADMIN"):
                assert rcode in roles, f"Falta rol {rcode} en auth_db"

            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, is_active)
                VALUES
                  (%s, %s, 'Admin Prueba HU51', %s, true),
                  (%s, %s, 'Usuario Objetivo HU51', %s, true),
                  (%s, %s, 'Usuario Inactivo HU51', %s, false)
                """,
                (
                    admin_id, f"admin.{str(admin_id)[:8]}@siprim.test", pwd_hash,
                    target_id, f"target.{str(target_id)[:8]}@siprim.test", pwd_hash,
                    inactive_id, f"inactive.{str(inactive_id)[:8]}@siprim.test", pwd_hash,
                ),
            )
            conn.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s), (%s, %s)",
                (admin_id, roles["ADMIN"], target_id, roles["MAYOR"]),
            )

        app = create_app()
        fake_platform = MagicMock()
        fake_platform.list_auditable_projects.return_value = {"projects": []}
        app.dependency_overrides[get_auth_service] = lambda: auth_svc
        app.dependency_overrides[get_platform_service] = lambda: fake_platform

        admin_token = auth_svc.login(
            f"admin.{str(admin_id)[:8]}@siprim.test", raw_pwd
        )["access_token"]

        with TestClient(app) as client:
            # 1. Admin lista usuarios y catalogo completo de roles sin hashes
            res_list = client.get(
                "/api/v1/admin/users",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert res_list.status_code == 200
            list_body = res_list.json()
            codes = {r["code"] for r in list_body["available_roles"]}
            assert {"ADMIN", "PLANNER", "PLANNING_MANAGER", "LEGAL_ADVISOR", "MAYOR", "AUDITOR", "SUPERADMIN"} <= codes
            assert pwd_hash not in res_list.text

            # 2. Admin asigna PLANNING_MANAGER y AUDITOR al usuario objetivo -> persiste en auth_db
            res_assign = client.put(
                f"/api/v1/admin/users/{target_id}/roles",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"roles": ["AUDITOR", "PLANNING_MANAGER"]},
            )
            assert res_assign.status_code == 200
            assert sorted(res_assign.json()["user"]["roles"]) == ["AUDITOR", "PLANNING_MANAGER"]

            # Usuario objetivo inicia sesion y obtiene JWT con AUDITOR
            target_login = client.post(
                "/api/v1/auth/login",
                json={"email": f"target.{str(target_id)[:8]}@siprim.test", "password": raw_pwd},
            )
            assert target_login.status_code == 200
            target_token = target_login.json()["access_token"]
            assert client.get(
                "/api/v1/audit/projects", headers={"Authorization": f"Bearer {target_token}"}
            ).status_code == 200

            # 3. Admin ordinario no puede escalar a SUPERADMIN ni asignar roles a cuenta inactiva ni rol invalido
            assert (
                client.put(
                    f"/api/v1/admin/users/{target_id}/roles",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"roles": ["SUPERADMIN"]},
                ).status_code
                == 403
            )
            assert (
                client.put(
                    f"/api/v1/admin/users/{inactive_id}/roles",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"roles": ["PLANNER"]},
                ).status_code
                == 409
            )
            assert (
                client.put(
                    f"/api/v1/admin/users/{target_id}/roles",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    json={"roles": ["ROL_INEXISTENTE"]},
                ).status_code
                == 409
            )

            # 4. Revocacion de AUDITOR dejando solo MAYOR -> JWT previo es rechazado de inmediato con 403
            res_revoke = client.put(
                f"/api/v1/admin/users/{target_id}/roles",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"roles": ["MAYOR"]},
            )
            assert res_revoke.status_code == 200
            assert res_revoke.json()["user"]["roles"] == ["MAYOR"]

            res_after_revoke = client.get(
                "/api/v1/audit/projects", headers={"Authorization": f"Bearer {target_token}"}
            )
            assert res_after_revoke.status_code == 403
            fake_platform.list_auditable_projects.assert_called_once()
    finally:
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            conn.execute(
                "DELETE FROM user_roles WHERE user_id IN (%s, %s, %s)",
                (admin_id, target_id, inactive_id),
            )
            conn.execute(
                "DELETE FROM users WHERE id IN (%s, %s, %s)",
                (admin_id, target_id, inactive_id),
            )
