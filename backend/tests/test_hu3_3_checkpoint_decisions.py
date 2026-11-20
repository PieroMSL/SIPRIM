from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import _compute_checkpoint_decision_sha256
from backend.domain.exceptions.pmv1_exceptions import ConfiguracionPMV1Error
from backend.infrastructure.config.dependencies import (
    get_auth_service,
    get_current_user,
    get_platform_service,
)
from backend.main import create_app


def test_hu3_3_sha256_canonical_calculation_detects_tampering():
    """La huella SHA-256 canonica es determinista y detecta cualquier alteracion posterior."""
    did = uuid4()
    cid = uuid4()
    pid = uuid4()
    vid = uuid4()
    uid = uuid4()
    now = datetime(2026, 11, 19, 17, 45, 0, 123456, tzinfo=timezone.utc)

    payload_a, hash_a = _compute_checkpoint_decision_sha256(
        decision_id=did,
        checkpoint_id=cid,
        project_id=pid,
        project_version_id=vid,
        decision="approve_continue",
        justification="Sustento tecnico verificado en campo.",
        conditions="Monitoreo trimestral",
        decided_by_user_id=uid,
        decided_by_name="Gerente Planeamiento",
        decided_by_email="gerente@siprim.test",
        decided_at=now,
    )
    _, hash_same = _compute_checkpoint_decision_sha256(
        decision_id=did,
        checkpoint_id=cid,
        project_id=pid,
        project_version_id=vid,
        decision="approve_continue",
        justification="Sustento tecnico verificado en campo.",
        conditions="Monitoreo trimestral",
        decided_by_user_id=uid,
        decided_by_name="Gerente Planeamiento",
        decided_by_email="gerente@siprim.test",
        decided_at=now,
    )
    _, hash_tampered = _compute_checkpoint_decision_sha256(
        decision_id=did,
        checkpoint_id=cid,
        project_id=pid,
        project_version_id=vid,
        decision="approve_continue",
        justification="Sustento alterado manualmente.",
        conditions="Monitoreo trimestral",
        decided_by_user_id=uid,
        decided_by_name="Gerente Planeamiento",
        decided_by_email="gerente@siprim.test",
        decided_at=now,
    )

    assert len(hash_a) == 64
    assert payload_a["schema_version"] == "hu3.3-v2"
    assert hash_a == hash_same
    assert hash_a != hash_tampered


def test_hu3_3_endpoint_permissions_401_403_and_resume_gate():
    """CA-HU3.3-03: exige sesion (401) y rol PLANNING_MANAGER o ADMIN (403 a PLANNER/MAYOR/CITIZEN)."""
    app = create_app()
    fake_platform = MagicMock()
    fake_auth = MagicMock()
    pid = uuid4()

    app.dependency_overrides[get_platform_service] = lambda: fake_platform
    app.dependency_overrides[get_auth_service] = lambda: fake_auth

    body = {
        "decision": "approve_continue",
        "justification": "Aprobado por Gerencia de Planeamiento.",
        "conditions": "Plan de contingencia",
    }

    # 1. Sin sesion -> 401
    with TestClient(app) as client:
        assert (
            client.post(
                f"/api/v1/projects/{pid}/checkpoint/decision",
                headers={"Idempotency-Key": "k1"},
                json=body,
            ).status_code
            == 401
        )
        fake_platform.register_checkpoint_decision.assert_not_called()

    # 2. Roles insuficientes (incluso PLANNER ordinario, MAYOR, LEGAL_ADVISOR, CITIZEN) -> 403
    for role in ("PLANNER", "MAYOR", "LEGAL_ADVISOR", "CITIZEN"):
        app.dependency_overrides[get_current_user] = lambda r=role: {"sub": str(uuid4()), "roles": [r]}
        with TestClient(app) as client:
            res = client.post(
                f"/api/v1/projects/{pid}/checkpoint/decision",
                headers={"Idempotency-Key": "k2"},
                json=body,
            )
            assert res.status_code == 403, (role, res.status_code)
            fake_platform.register_checkpoint_decision.assert_not_called()

    # 3. PLANNING_MANAGER -> 201
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid4()), "roles": ["PLANNING_MANAGER"]}
    fake_platform.register_checkpoint_decision.return_value = {
        "decision_id": uuid4(),
        "checkpoint_id": uuid4(),
        "project_id": pid,
        "project_version_id": uuid4(),
        "version_number": 1,
        "decision": "approve_continue",
        "decision_label": "Aprobación para continuar evaluación",
        "justification": body["justification"],
        "conditions": body["conditions"],
        "decided_by_user_id": uuid4(),
        "decided_by_name": "Gerente Temporal",
        "decided_by_email": "gerente@siprim.test",
        "decided_at": datetime.now(timezone.utc),
        "idempotency_key": "k3",
        "integrity_hash_sha256": "a" * 64,
        "recomputed_hash_sha256": "a" * 64,
        "verification_status": "valid",
        "verification_label": "Válida (integridad verificada)",
        "prototype_notice": "Constancia de integridad SHA-256 del prototipo académico SIPRIM",
        "checkpoint_status": "pending_review",
        "duplicated": False,
    }
    with TestClient(app) as client:
        res_ok = client.post(
            f"/api/v1/projects/{pid}/checkpoint/decision",
            headers={"Idempotency-Key": "k3"},
            json=body,
        )
        assert res_ok.status_code == 201
        assert res_ok.json()["verification_status"] == "valid"

    # 4. Intentar /checkpoint/resume sin decision previa aprobada -> 409
    fake_platform.resume_project_checkpoint.side_effect = ConfiguracionPMV1Error(
        "Debe registrar previamente una aprobación de continuación firmada (HU3.3)"
    )
    with TestClient(app) as client:
        res_gate = client.post(
            f"/api/v1/projects/{pid}/checkpoint/resume",
            headers={"Idempotency-Key": "k-res"},
            json={"justification": "Intento de salto"},
        )
        assert res_gate.status_code == 409


def test_hu3_3_postgres_sequence_tampering_version_isolation_and_idempotency():
    """Prueba en PostgreSQL aislado:
    - aprobacion -> correccion posterior -> reanudacion rechazada sin cambios -> nueva aprobacion -> reanudacion permitida
    - aprobacion alterada (columna o canonical_payload) -> reanudacion rechazada
    - version distinta -> rechazo
    - reintento idempotente
    """
    import psycopg
    import pytest
    from psycopg.rows import dict_row
    from backend.application.services.pmv1_services import PlatformService
    from backend.infrastructure.config.settings import get_settings

    settings = get_settings()
    db_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    svc = PlatformService(db_url=settings.platform_db_url, dispatcher=None, enable_demo_fallback=False)  # type: ignore
    planner_id = UUID("10000000-0000-4000-a000-000000000002")

    proj_id = uuid4()
    v1_id = uuid4()
    eval_id = uuid4()
    cp1_id = uuid4()

    with psycopg.connect(db_url, row_factory=dict_row) as conn, conn.transaction():
        crit_id = conn.execute("SELECT id FROM criteria_versions WHERE status='active' LIMIT 1").fetchone()["id"]
        conn.execute(
            "INSERT INTO projects (id, code, status, created_by_user_id) VALUES (%s, %s, 'pending_review', %s)",
            (proj_id, f"HU33-SEQ-{uuid4().hex[:6].upper()}", planner_id),
        )
        conn.execute(
            """
            INSERT INTO project_versions
            (id, project_id, version_number, title, description, location_description, proposed_land_use,
             territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id)
            VALUES (%s, %s, 1, 'Proyecto Secuencia HU3.3', 'Desc', 'El Tambo', 'Parque', 'public', 250000, 600, %s)
            """,
            (v1_id, proj_id, planner_id),
        )
        conn.execute(
            """
            INSERT INTO evaluations
            (id, project_version_id, criteria_version_id, requested_by_user_id, status, idempotency_key)
            VALUES (%s, %s, %s, %s, 'pending_review', %s)
            """,
            (eval_id, v1_id, crit_id, planner_id, f"eval-seq-{uuid4()}"),
        )
        conn.execute(
            """
            INSERT INTO evaluation_checkpoints
            (id, project_id, project_version_id, evaluation_id, checkpoint_type, status, reason, risk_source, risk_level)
            VALUES (%s, %s, %s, %s, 'high_risk_pause', 'pending_review', 'Pausa inicial', 'environmental_risk', 'high')
            """,
            (cp1_id, proj_id, v1_id, eval_id),
        )

    try:
        # 1. Aprobacion inicial + reintento idempotente
        dec1_key = f"seq-dec1-{uuid4()}"
        d1 = svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=planner_id,
            actor_name="Gerente Planeamiento",
            actor_email="gerente@siprim.test",
            decision="approve_continue",
            justification="Primera aprobacion de prueba",
            conditions="Condicion 1",
            idempotency_key=dec1_key,
        )
        d1_dup = svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=planner_id,
            actor_name="Gerente Planeamiento",
            actor_email="gerente@siprim.test",
            decision="approve_continue",
            justification="Primera aprobacion de prueba",
            conditions="Condicion 1",
            idempotency_key=dec1_key,
        )
        assert d1_dup["duplicated"] is True
        assert d1_dup["decision_id"] == d1["decision_id"]

        # 2. Correccion posterior -> debe invalidar la continuacion hasta nueva aprobacion
        svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=planner_id,
            actor_name="Gerente Planeamiento",
            actor_email="gerente@siprim.test",
            decision="request_correction",
            justification="Se detecto observacion posterior en estudio",
            conditions="Subsanar plano de drenaje",
            idempotency_key=f"seq-corr-{uuid4()}",
        )
        with pytest.raises(ConfiguracionPMV1Error, match="solicita corrección"):
            svc.resume_project_checkpoint(
                project_id=proj_id,
                actor_id=planner_id,
                justification="Intento de reanudar tras correccion",
                conditions=None,
                idempotency_key=f"seq-res-blk-{uuid4()}",
            )

        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            assert conn.execute("SELECT status FROM evaluation_checkpoints WHERE id=%s", (cp1_id,)).fetchone()["status"] == "pending_review"
            assert conn.execute("SELECT status FROM projects WHERE id=%s", (proj_id,)).fetchone()["status"] == "pending_review"
            assert conn.execute("SELECT status FROM evaluations WHERE id=%s", (eval_id,)).fetchone()["status"] == "pending_review"
            assert conn.execute("SELECT COUNT(*) AS cnt FROM human_approvals WHERE project_id=%s", (proj_id,)).fetchone()["cnt"] == 0

        # 3. Nueva aprobacion valida -> luego alteracion de canonical_payload -> reanudacion rechazada!
        d2 = svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=planner_id,
            actor_name="Gerente Planeamiento",
            actor_email="gerente@siprim.test",
            decision="approve_continue",
            justification="Segunda aprobacion valida tras subsanacion",
            conditions="Monitoreo continuo",
            idempotency_key=f"seq-dec2-{uuid4()}",
        )
        with psycopg.connect(db_url) as conn, conn.transaction():
            conn.execute(
                "UPDATE checkpoint_review_decisions SET canonical_payload = canonical_payload || '{\"tampered\":true}'::jsonb WHERE id=%s",
                (d2["decision_id"],),
            )
        listed = svc.list_checkpoint_decisions(proj_id)["decisions"]
        assert listed[0]["verification_status"] == "failed"
        with pytest.raises(ConfiguracionPMV1Error, match="integridad SHA-256"):
            svc.resume_project_checkpoint(
                project_id=proj_id,
                actor_id=planner_id,
                justification="Intento con constancia alterada",
                conditions=None,
                idempotency_key=f"seq-res-tamper-{uuid4()}",
            )

        # 4. Nueva aprobacion integra -> reanudacion permitida + reintento idempotente de reanudacion
        svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=planner_id,
            actor_name="Gerente Planeamiento",
            actor_email="gerente@siprim.test",
            decision="approve_continue",
            justification="Tercera aprobacion integra definitiva",
            conditions=None,
            idempotency_key=f"seq-dec3-{uuid4()}",
        )
        res_key = f"seq-res-ok-{uuid4()}"
        res_ok = svc.resume_project_checkpoint(
            project_id=proj_id,
            actor_id=planner_id,
            justification="Reanudacion autorizada tras constancia integra",
            conditions=None,
            idempotency_key=res_key,
        )
        assert res_ok["status"] == "resolved"
        res_dup = svc.resume_project_checkpoint(
            project_id=proj_id,
            actor_id=planner_id,
            justification="Reanudacion autorizada tras constancia integra",
            conditions=None,
            idempotency_key=res_key,
        )
        assert res_dup["duplicated"] is True

        # 5. Version distinta v2 con checkpoint propio -> rechazo sin aprobacion en v2
        v2_id = uuid4()
        cp2_id = uuid4()
        with psycopg.connect(db_url) as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO project_versions
                (id, project_id, version_number, title, description, location_description, proposed_land_use,
                 territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id)
                VALUES (%s, %s, 2, 'Proyecto v2', 'Desc v2', 'El Tambo', 'Parque', 'public', 260000, 620, %s)
                """,
                (v2_id, proj_id, planner_id),
            )
            conn.execute(
                "UPDATE projects SET status='pending_review' WHERE id=%s",
                (proj_id,),
            )
            conn.execute(
                """
                INSERT INTO evaluation_checkpoints
                (id, project_id, project_version_id, checkpoint_type, status, reason, risk_source, risk_level)
                VALUES (%s, %s, %s, 'high_risk_pause', 'pending_review', 'Pausa en v2', 'environmental_risk', 'high')
                """,
                (cp2_id, proj_id, v2_id),
            )
        with pytest.raises(ConfiguracionPMV1Error, match="Debe registrar previamente"):
            svc.resume_project_checkpoint(
                project_id=proj_id,
                actor_id=planner_id,
                justification="Intento en v2 sin decision en v2",
                conditions=None,
                idempotency_key=f"seq-res-v2-{uuid4()}",
            )
    finally:
        with psycopg.connect(db_url) as conn, conn.transaction():
            conn.execute("DELETE FROM evaluation_checkpoints WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM economic_result_projections WHERE evaluation_id = %s", (eval_id,))
            conn.execute("DELETE FROM outbox_events WHERE aggregate_id = %s", (eval_id,))
            conn.execute("DELETE FROM evaluations WHERE project_version_id = %s", (v1_id,))
            conn.execute("DELETE FROM project_versions WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM projects WHERE id = %s", (proj_id,))
