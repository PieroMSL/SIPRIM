import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import PlatformService
from backend.application.services.validated_auth_service import ValidatedAuthService
from backend.domain.exceptions.pmv1_exceptions import (
    RecursoPMV1NoEncontradoError,
    ServicioNoDisponibleError,
)
from backend.infrastructure.config.dependencies import (
    get_auth_service,
    get_current_user,
    get_platform_service,
)
from backend.main import create_app


def _make_auth_service(db_url: str = "postgresql://fake/auth_db") -> ValidatedAuthService:
    return ValidatedAuthService(
        db_url=db_url,
        jwt_secret="test-secret-key-min-32-characters-hu54",
        issuer="siprim-auth-test",
        audience="siprim-api-test",
        expiration_minutes=30,
    )


def test_hu5_4_verify_integrity_endpoint_auth_revocation_and_not_found():
    """Solo AUDITOR, ADMIN y SUPERADMIN verifican integridad; otros roles y JWT revocados reciben 403."""
    app = create_app()
    fake_auth = MagicMock()
    fake_platform = MagicMock()
    proj_id = uuid4()
    ver_id = uuid4()
    dec_id = uuid4()

    app.dependency_overrides[get_auth_service] = lambda: fake_auth
    app.dependency_overrides[get_platform_service] = lambda: fake_platform

    endpoint = f"/api/v1/projects/{proj_id}/versions/{ver_id}/decisions/{dec_id}/verify-integrity"

    # 1. Sin autenticacion -> 401
    with TestClient(app) as client:
        assert client.get(endpoint).status_code == 401

    # 2. Roles sin permiso de auditoria -> 403 sin exponer datos de decision
    for role in ("PLANNER", "MAYOR", "LEGAL_ADVISOR", "PLANNING_MANAGER", "CITIZEN"):
        app.dependency_overrides[get_current_user] = lambda r=role: {
            "sub": str(uuid4()),
            "roles": [r],
        }
        with TestClient(app) as client:
            res = client.get(endpoint)
            assert res.status_code == 403
            body = res.json()
            assert "justification" not in body
            assert "stored_hash_sha256" not in body
    fake_platform.verify_decision_integrity.assert_not_called()

    # 3. Roles autorizados: AUDITOR, ADMIN, SUPERADMIN -> 200
    fake_platform.verify_decision_integrity.return_value = {
        "project_id": proj_id,
        "project_code": "EXP-HU54",
        "project_title": "Proyecto Auditado",
        "project_version_id": ver_id,
        "version_number": 1,
        "decision_id": dec_id,
        "decision_kind": "checkpoint_decision",
        "checkpoint_id": uuid4(),
        "checkpoint_status": "resolved",
        "decision": "approve_continue",
        "decision_label": "Aprobación para continuar evaluación",
        "justification": "Conforme en revision tecnica.",
        "conditions": None,
        "decided_by_user_id": uuid4(),
        "decided_by_name": "Lucia Paredes",
        "actor_in_directory": True,
        "decided_at": datetime.now(timezone.utc),
        "has_integrity_hash": True,
        "integrity_status": "intact",
        "integrity_label": "Íntegra",
        "stored_hash_sha256": "a" * 64,
        "recomputed_hash_sha256": "a" * 64,
        "canonical_payload_matches": True,
        "checked_at": datetime.now(timezone.utc),
        "prototype_notice": "No constituye firma digital certificada con validez juridica.",
        "read_only": True,
    }
    for allowed_role in ("AUDITOR", "ADMIN", "SUPERADMIN"):
        app.dependency_overrides[get_current_user] = lambda r=allowed_role: {
            "sub": str(uuid4()),
            "roles": [r],
        }
        with TestClient(app) as client:
            res_ok = client.get(endpoint)
            assert res_ok.status_code == 200
            assert res_ok.json()["integrity_label"] == "Íntegra"
            assert res_ok.json()["read_only"] is True

    # 4. Decision inexistente o de otra version -> 404 generico sin filtrar datos protegidos
    fake_platform.verify_decision_integrity.side_effect = RecursoPMV1NoEncontradoError(
        "No se encontró la decisión solicitada para el expediente y versión indicados."
    )
    with TestClient(app) as client:
        res_404 = client.get(endpoint)
        assert res_404.status_code == 404
        assert "stored_hash_sha256" not in res_404.text
        assert "justification" not in res_404.text

    # 5. Revocacion inmediata de rol AUDITOR con JWT anterior -> 403 + 503 fail-closed
    auth_svc = _make_auth_service()
    user_id = uuid4()
    prior_jwt = jwt.encode(
        {
            "sub": str(user_id),
            "roles": ["AUDITOR"],
            "iss": auth_svc.issuer,
            "aud": auth_svc.audience,
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(minutes=20),
        },
        auth_svc.jwt_secret,
        algorithm="HS256",
    )
    app_rev = create_app()
    fake_platform_rev = MagicMock()
    app_rev.dependency_overrides[get_auth_service] = lambda: auth_svc
    app_rev.dependency_overrides[get_platform_service] = lambda: fake_platform_rev

    auth_svc.get_active_user_roles = MagicMock(return_value=["PLANNER"])  # type: ignore[method-assign]
    with TestClient(app_rev) as client:
        assert (
            client.get(endpoint, headers={"Authorization": f"Bearer {prior_jwt}"}).status_code
            == 403
        )
        fake_platform_rev.verify_decision_integrity.assert_not_called()

    auth_svc.get_active_user_roles = MagicMock(  # type: ignore[method-assign]
        side_effect=ServicioNoDisponibleError("auth_db caida")
    )
    with TestClient(app_rev) as client:
        assert (
            client.get(endpoint, headers={"Authorization": f"Bearer {prior_jwt}"}).status_code
            == 503
        )
        fake_platform_rev.verify_decision_integrity.assert_not_called()


def test_hu5_4_postgres_decision_audit_integrity_tamper_and_readonly():
    """Verifica detalle completo por version, comprobacion SHA-256 en vivo (intacta y alterada), dictamen sin huella, actor eliminado y solo lectura."""
    platform_db_url = (os.getenv("PLATFORM_DB_URL") or "").strip()
    auth_db_url = (os.getenv("AUTH_DB_URL") or "").strip()
    if not platform_db_url or not auth_db_url:
        pytest.skip("PLATFORM_DB_URL / AUTH_DB_URL no configuradas para prueba PostgreSQL aislada")

    try:
        with psycopg.connect(platform_db_url) as conn:
            conn.execute("SELECT 1")
        with psycopg.connect(auth_db_url) as conn:
            conn.execute("SELECT 1")
    except Exception:
        pytest.skip("Base de datos PostgreSQL de prueba no accesible")

    svc = PlatformService(db_url=platform_db_url, dispatcher=MagicMock(), auth_db_url=auth_db_url)
    planner_id = uuid4()
    manager_id = uuid4()
    proj_id = uuid4()
    v1_id = uuid4()
    v2_id = uuid4()
    eval_v1_id = uuid4()
    eval_v2_id = uuid4()
    cp1_id = uuid4()
    approval_v2_id = uuid4()

    t0 = datetime(2026, 11, 18, 8, 30, 0, tzinfo=timezone.utc)
    unique_code = f"HU54-PG-{str(proj_id)[:6].upper()}"

    try:
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            planner_role = conn.execute("SELECT id FROM roles WHERE code = 'PLANNER'").fetchone()[0]
            manager_role = conn.execute("SELECT id FROM roles WHERE code = 'PLANNING_MANAGER'").fetchone()[0]
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, is_active)
                VALUES
                  (%s, %s, 'Carlos Planificador HU54', 'hash-no-expuesto', true),
                  (%s, %s, 'Lucia Gerente HU54', 'hash-no-expuesto', true)
                """,
                (
                    planner_id,
                    f"planner-{str(planner_id)[:8]}@siprim.test",
                    manager_id,
                    f"manager-{str(manager_id)[:8]}@siprim.test",
                ),
            )
            conn.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s), (%s, %s)",
                (planner_id, planner_role, manager_id, manager_role),
            )

        with psycopg.connect(platform_db_url) as conn, conn.transaction():
            criteria_id = conn.execute(
                "SELECT id FROM criteria_versions WHERE status = 'active' ORDER BY version_number DESC LIMIT 1"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO projects (id, code, status, created_by_user_id, created_at, updated_at)
                VALUES (%s, %s, 'pending_review', %s, %s, %s)
                """,
                (proj_id, unique_code, planner_id, t0, t0 + timedelta(hours=2)),
            )
            conn.execute(
                """
                INSERT INTO project_versions
                (id, project_id, version_number, title, description, location_description, proposed_land_use,
                 territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id, created_at)
                VALUES (%s, %s, 1, 'Proyecto Auditado v1', 'Desc v1', 'El Tambo', 'Vial', 'public', 300000, 800, %s, %s)
                """,
                (v1_id, proj_id, planner_id, t0 + timedelta(minutes=1)),
            )
            conn.execute(
                """
                INSERT INTO evaluations
                (id, project_version_id, criteria_version_id, requested_by_user_id, status, idempotency_key, requested_at)
                VALUES (%s, %s, %s, %s, 'pending_review', %s, %s)
                """,
                (eval_v1_id, v1_id, criteria_id, planner_id, f"hu54-e1-{uuid4()}", t0 + timedelta(minutes=5)),
            )
            conn.execute(
                """
                INSERT INTO evaluation_checkpoints
                (id, project_id, project_version_id, evaluation_id, checkpoint_type, status, reason, risk_source, risk_level, created_at)
                VALUES (%s, %s, %s, %s, 'high_risk_pause', 'pending_review', 'Pausa de riesgo alto v1', 'environmental_risk', 'high', %s)
                """,
                (cp1_id, proj_id, v1_id, eval_v1_id, t0 + timedelta(minutes=8)),
            )

        # Decision 1: solicitar correccion + Decision 2: aprobar y continuar + Reanudacion HU3.4
        d1 = svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=manager_id,
            actor_name="Lucia Gerente HU54",
            actor_email="lucia@siprim.test",
            decision="request_correction",
            justification="Falta estudio complementario de drenaje.",
            conditions="Adjuntar memoria hidrologica",
            idempotency_key=f"hu54-d1-{uuid4()}",
        )
        d2 = svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=manager_id,
            actor_name="Lucia Gerente HU54",
            actor_email="lucia@siprim.test",
            decision="approve_continue",
            justification="Memoria hidrologica verificada conforme.",
            conditions="Control en etapa de ejecucion",
            idempotency_key=f"hu54-d2-{uuid4()}",
        )
        svc.resume_project_checkpoint(
            project_id=proj_id,
            actor_id=manager_id,
            justification="Reanudacion tras subsanacion y aprobacion.",
            conditions="Mantener monitoreo",
            idempotency_key=f"hu54-res-{uuid4()}",
        )

        # Version 2 + dictamen final (sin huella SHA-256)
        with psycopg.connect(platform_db_url) as conn, conn.transaction():
            t_v2 = datetime.now(timezone.utc) + timedelta(seconds=10)
            conn.execute(
                """
                INSERT INTO project_versions
                (id, project_id, version_number, title, description, location_description, proposed_land_use,
                 territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id, created_at)
                VALUES (%s, %s, 2, 'Proyecto Auditado v2', 'Desc v2', 'El Tambo', 'Vial', 'public', 340000, 950, %s, %s)
                """,
                (v2_id, proj_id, planner_id, t_v2),
            )
            conn.execute(
                """
                INSERT INTO evaluations
                (id, project_version_id, criteria_version_id, requested_by_user_id, status, idempotency_key, requested_at)
                VALUES (%s, %s, %s, %s, 'completed', %s, %s)
                """,
                (eval_v2_id, v2_id, criteria_id, planner_id, f"hu54-e2-{uuid4()}", t_v2 + timedelta(seconds=5)),
            )
            conn.execute(
                """
                INSERT INTO human_approvals
                (id, project_id, project_version_id, decision, justification, conditions,
                 decided_by_user_id, idempotency_key, decided_at)
                VALUES (%s, %s, %s, 'approved', 'Dictamen final aprobado para v2.', 'Ejecucion priorizada', %s, %s, %s)
                """,
                (approval_v2_id, proj_id, v2_id, manager_id, f"hu54-ap-{uuid4()}", t_v2 + timedelta(seconds=10)),
            )
            conn.execute(
                "UPDATE projects SET status = 'approved', updated_at = %s WHERE id = %s",
                (t_v2 + timedelta(seconds=12), proj_id),
            )
            updated_before = conn.execute(
                "SELECT updated_at FROM projects WHERE id = %s", (proj_id,)
            ).fetchone()[0]

        # 1) Historial expone detalle estructurado por decision y version
        hist = svc.get_project_history(proj_id)
        cp_events = [e for e in hist["events"] if e.get("decision_kind") == "checkpoint_decision"]
        ap_events = [e for e in hist["events"] if e.get("decision_kind") == "final_approval"]
        assert len(cp_events) == 2
        assert len(ap_events) == 1
        assert all(e["version_number"] == 1 and e["project_version_id"] == v1_id for e in cp_events)
        assert all(e["actor_user_id"] == manager_id and e["actor_name"] == "Lucia Gerente HU54" for e in cp_events)
        assert all(e["checkpoint_status"] == "resolved" for e in cp_events)
        assert ap_events[0]["version_number"] == 2
        assert ap_events[0]["has_integrity_hash"] is False

        # 2) Verificacion en vivo retorna Integra cuando no hay alteracion
        v_ok = svc.verify_decision_integrity(
            project_id=proj_id,
            project_version_id=v1_id,
            decision_id=d2["decision_id"],
        )
        assert v_ok["read_only"] is True
        assert v_ok["has_integrity_hash"] is True
        assert v_ok["integrity_status"] == "intact"
        assert v_ok["integrity_label"] == "Íntegra"
        assert v_ok["stored_hash_sha256"] == d2["integrity_hash_sha256"]
        assert v_ok["recomputed_hash_sha256"] == d2["integrity_hash_sha256"]
        assert v_ok["decided_by_user_id"] == manager_id
        assert v_ok["decided_by_name"] == "Lucia Gerente HU54"
        assert v_ok["actor_in_directory"] is True
        assert v_ok["version_number"] == 1

        # 3) Verificar pertenencia a version/expediente (cruce a v2 o proyecto ajeno -> NotFound)
        with pytest.raises(RecursoPMV1NoEncontradoError):
            svc.verify_decision_integrity(
                project_id=proj_id,
                project_version_id=v2_id,
                decision_id=d2["decision_id"],
            )
        with pytest.raises(RecursoPMV1NoEncontradoError):
            svc.verify_decision_integrity(
                project_id=uuid4(),
                project_version_id=v1_id,
                decision_id=d2["decision_id"],
            )

        # 4) Dictamen final sin huella guardada -> Verificacion no disponible (nunca Integra)
        v_ap = svc.verify_decision_integrity(
            project_id=proj_id,
            project_version_id=v2_id,
            decision_id=approval_v2_id,
        )
        assert v_ap["has_integrity_hash"] is False
        assert v_ap["integrity_status"] == "unavailable"
        assert v_ap["integrity_label"] == "Verificación de integridad no disponible para este registro"

        # 5) Alteracion controlada de un campo protegido solo en la DB aislada -> Alteracion detectada
        with psycopg.connect(platform_db_url) as conn, conn.transaction():
            conn.execute(
                "UPDATE checkpoint_review_decisions SET justification = %s WHERE id = %s",
                ("Justificacion alterada externamente en la base aislada.", d2["decision_id"]),
            )
        v_tampered = svc.verify_decision_integrity(
            project_id=proj_id,
            project_version_id=v1_id,
            decision_id=d2["decision_id"],
        )
        assert v_tampered["integrity_status"] == "tampered"
        assert v_tampered["integrity_label"] == "Alteración detectada"
        assert v_tampered["recomputed_hash_sha256"] != v_tampered["stored_hash_sha256"]

        # 6) Actor eliminado del directorio -> conserva UUID y nombre = None (no inventa)
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            conn.execute("DELETE FROM user_roles WHERE user_id = %s", (manager_id,))
            conn.execute("DELETE FROM users WHERE id = %s", (manager_id,))
        v_missing_actor = svc.verify_decision_integrity(
            project_id=proj_id,
            project_version_id=v1_id,
            decision_id=d1["decision_id"],
        )
        assert v_missing_actor["decided_by_user_id"] == manager_id
        assert v_missing_actor["actor_in_directory"] is False
        assert v_missing_actor["decided_by_name"] is None
        assert v_missing_actor["integrity_status"] == "intact"

        # Verificar estricta solo-lectura de las verificaciones
        with psycopg.connect(platform_db_url) as conn:
            updated_after = conn.execute(
                "SELECT updated_at FROM projects WHERE id = %s", (proj_id,)
            ).fetchone()[0]
        assert updated_after == updated_before
    finally:
        with psycopg.connect(platform_db_url) as conn, conn.transaction():
            conn.execute("DELETE FROM human_approvals WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM checkpoint_review_decisions WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM evaluation_checkpoints WHERE project_id = %s", (proj_id,))
            conn.execute(
                "DELETE FROM economic_result_projections WHERE evaluation_id IN (%s, %s)",
                (eval_v1_id, eval_v2_id),
            )
            conn.execute("DELETE FROM evaluations WHERE project_version_id IN (%s, %s)", (v1_id, v2_id))
            conn.execute("DELETE FROM project_versions WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM projects WHERE id = %s", (proj_id,))
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            conn.execute("DELETE FROM user_roles WHERE user_id IN (%s, %s)", (planner_id, manager_id))
            conn.execute("DELETE FROM users WHERE id IN (%s, %s)", (planner_id, manager_id))
