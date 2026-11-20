from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import PlatformService
from backend.infrastructure.config.dependencies import get_platform_service
from backend.infrastructure.config.settings import get_settings
from backend.main import create_app


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []

    def dispatch_economic(self, payload: dict) -> None:
        self.dispatched.append(payload)


def _cleanup_project(conn: psycopg.Connection, project_id: UUID) -> None:
    pv_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM project_versions WHERE project_id = %s", (project_id,)
        ).fetchall()
    ]
    if pv_ids:
        ev_ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM evaluations WHERE project_version_id = ANY(%s)",
                (pv_ids,),
            ).fetchall()
        ]
        if ev_ids:
            conn.execute("DELETE FROM outbox_events WHERE aggregate_id = ANY(%s)", (ev_ids,))
            conn.execute(
                "DELETE FROM economic_result_projections WHERE evaluation_id = ANY(%s)",
                (ev_ids,),
            )
            conn.execute(
                "DELETE FROM evaluation_checkpoints WHERE evaluation_id = ANY(%s)",
                (ev_ids,),
            )
            conn.execute("DELETE FROM evaluations WHERE id = ANY(%s)", (ev_ids,))
        conn.execute(
            "DELETE FROM social_accessibility_assessments WHERE project_id = %s",
            (project_id,),
        )
        conn.execute(
            "DELETE FROM environmental_assessments WHERE project_id = %s",
            (project_id,),
        )
        conn.execute(
            "DELETE FROM technical_feasibility_assessments WHERE project_id = %s",
            (project_id,),
        )
        conn.execute(
            "DELETE FROM legal_assessments WHERE project_id = %s",
            (project_id,),
        )
        conn.execute(
            "DELETE FROM evaluation_checkpoints WHERE project_version_id = ANY(%s)",
            (pv_ids,),
        )
        conn.execute("DELETE FROM project_versions WHERE id = ANY(%s)", (pv_ids,))
    conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


@pytest.fixture
def isolated_env():
    settings = get_settings()
    dispatcher = _RecordingDispatcher()
    app = create_app()
    platform_svc = PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=dispatcher,
        enable_demo_fallback=False,
        auth_db_url=settings.auth_db_url,
    )
    app.dependency_overrides[get_platform_service] = lambda: platform_svc
    db_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with TestClient(app) as client, psycopg.connect(db_url, autocommit=True) as conn:
        yield client, conn, dispatcher, platform_svc


def _login(client: TestClient, email: str, password: str = "Prueba2026!") -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_hu1_2_ca01_blocks_incomplete_draft_evaluation_with_zero_writes_and_reports_fields(
    isolated_env,
) -> None:
    """
    CA-HU1.2-01:
    - Permite guardar un borrador incompleto mediante flujo oficial (save_as_draft=True).
    - Valida e informa en lenguaje claro los campos faltantes (incluidos textos en blanco).
    - Al intentar POST /api/v1/evaluations con expediente incompleto, el servidor rechaza con 422,
      no crea registros en evaluations/outbox_events/audit_events/checkpoints, no cambia status
      y no dispara Celery.
    """
    client, conn, dispatcher, _ = isolated_env
    planner_headers = _login(client, "planner@siprim.test")

    created_ids: list[UUID] = []
    try:
        # 1. Creación estándar (save_as_draft=False) con campos faltantes o solo espacios -> 422 sin persistir
        bad_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Proyecto Sin Datos Completos",
                "description": "   ",
                "location": "",
                "proposed_land_use": None,
                "estimated_budget_pen": 100000,
                "beneficiaries_count": 500,
            },
        )
        assert bad_resp.status_code == 422
        bad_body = bad_resp.json()
        assert bad_body["missing_fields"] == ["description", "location", "proposed_land_use"]
        assert "Descripción" in bad_body["detail"]
        assert "Ubicación" in bad_body["detail"]
        assert "Uso de suelo propuesto" in bad_body["detail"]

        # 2. Guardado explícito de borrador incompleto (save_as_draft=True) -> 201 en status='draft'
        draft_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Borrador Sintético HU1.2",
                "description": "",
                "location": None,
                "proposed_land_use": None,
                "estimated_budget_pen": 250000,
                "beneficiaries_count": 1000,
                "save_as_draft": True,
            },
        )
        assert draft_resp.status_code == 201, draft_resp.text
        draft = draft_resp.json()
        project_id = UUID(draft["project_id"])
        version_id = UUID(draft["project_version_id"])
        created_ids.append(project_id)
        assert draft["status"] == "draft"
        assert draft["location"] is None
        assert draft["proposed_land_use"] is None

        # 3. Validación oficial identifica los campos obligatorios faltantes
        val_resp = client.post(
            f"/api/v1/projects/{project_id}/validate",
            headers=planner_headers,
        )
        assert val_resp.status_code == 200
        val_body = val_resp.json()
        assert val_body["complete"] is False
        assert val_body["status"] == "incomplete"
        assert val_body["missing_fields"] == ["description", "location", "proposed_land_use"]
        assert val_body["missing_field_labels"] == [
            "Descripción",
            "Ubicación",
            "Uso de suelo propuesto",
        ]
        assert "Ubicación" in val_body["message"]

        audit_before = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id = %s", (project_id,)
        ).fetchone()[0]

        # 4. Bypass directo a POST /api/v1/evaluations -> 422 sin efectos secundarios
        eval_resp = client.post(
            "/api/v1/evaluations",
            headers={**planner_headers, "Idempotency-Key": f"hu12-blocked-{uuid4().hex[:8]}"},
            json={"project_version_id": str(version_id)},
        )
        assert eval_resp.status_code == 422, eval_resp.text
        eval_body = eval_resp.json()
        assert eval_body["missing_fields"] == ["description", "location", "proposed_land_use"]
        assert "Uso de suelo propuesto" in eval_body["detail"]

        # Verificar cero escrituras derivadas y cero despacho a Celery
        assert len(dispatcher.dispatched) == 0
        eval_count = conn.execute(
            "SELECT COUNT(*) FROM evaluations WHERE project_version_id = %s", (version_id,)
        ).fetchone()[0]
        assert eval_count == 0
        checkpoint_count = conn.execute(
            "SELECT COUNT(*) FROM evaluation_checkpoints WHERE project_version_id = %s",
            (version_id,),
        ).fetchone()[0]
        assert checkpoint_count == 0
        status_now = conn.execute(
            "SELECT status FROM projects WHERE id = %s", (project_id,)
        ).fetchone()[0]
        assert status_now == "draft"
        audit_after = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id = %s", (project_id,)
        ).fetchone()[0]
        assert audit_after == audit_before
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_2_ca02_complete_draft_clears_warning_enables_evaluation_and_enforces_rbac(
    isolated_env,
) -> None:
    """
    CA-HU1.2-02:
    - Completar los datos obligatorios faltantes mediante PUT /api/v1/projects/{id}.
    - Rechaza actualización incompleta con save_as_draft=False (422).
    - Revalidar después de completar limpia los campos faltantes y permite solicitar evaluación (202).
    - Reutilizar la Idempotency-Key del intento fallido previo NO queda contaminada y ejecuta bien.
    - Respeta RBAC (401 sin token, 403 para asesor jurídico sin permiso PLANNER).
    """
    client, conn, dispatcher, _ = isolated_env
    planner_headers = _login(client, "planner@siprim.test")
    legal_headers = _login(client, "legal@siprim.test")

    created_ids: list[UUID] = []
    try:
        draft_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Parque Recreativo Incompleto",
                "description": "",
                "location": "",
                "proposed_land_use": "Equipamiento urbano",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 600,
                "save_as_draft": True,
            },
        )
        assert draft_resp.status_code == 201
        project_id = UUID(draft_resp.json()["project_id"])
        version_id = UUID(draft_resp.json()["project_version_id"])
        created_ids.append(project_id)

        # RBAC checks
        assert client.put(f"/api/v1/projects/{project_id}", json={"description": "X"}).status_code == 401
        assert (
            client.put(
                f"/api/v1/projects/{project_id}",
                headers=legal_headers,
                json={"description": "Intento asesor"},
            ).status_code
            == 403
        )
        assert (
            client.post(f"/api/v1/projects/{project_id}/validate", headers=legal_headers).status_code
            == 403
        )

        # Intento fallido de evaluación previa con clave X
        idem_key = f"hu12-retry-{uuid4().hex[:8]}"
        blocked = client.post(
            "/api/v1/evaluations",
            headers={**planner_headers, "Idempotency-Key": idem_key},
            json={"project_version_id": str(version_id)},
        )
        assert blocked.status_code == 422

        # Intento de completar sin ubicación -> 422 y status sigue en draft
        partial_update = client.put(
            f"/api/v1/projects/{project_id}",
            headers=planner_headers,
            json={
                "description": "Mejoramiento integral del parque sector sur.",
                "location": "   ",
            },
        )
        assert partial_update.status_code == 422
        assert "location" in partial_update.json()["missing_fields"]
        assert (
            conn.execute("SELECT status FROM projects WHERE id = %s", (project_id,)).fetchone()[0]
            == "draft"
        )

        # Completado válido de todos los campos obligatorios
        completed = client.put(
            f"/api/v1/projects/{project_id}",
            headers=planner_headers,
            json={
                "title": "Parque Recreativo Sector Sur",
                "description": "Mejoramiento integral de áreas verdes e iluminación peatonal.",
                "location": "Av. Ferrocarril cdra. 8, El Tambo - Huancayo",
                "proposed_land_use": "Recreación pública y áreas verdes",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 600,
                "save_as_draft": False,
            },
        )
        assert completed.status_code == 200, completed.text
        completed_body = completed.json()
        assert completed_body["status"] == "ready"
        assert completed_body["location"] == "Av. Ferrocarril cdra. 8, El Tambo - Huancayo"

        # Revalidación limpia advertencias
        val_after = client.post(
            f"/api/v1/projects/{project_id}/validate",
            headers=planner_headers,
        )
        assert val_after.status_code == 200
        assert val_after.json()["complete"] is True
        assert val_after.json()["missing_fields"] == []
        assert val_after.json()["missing_field_labels"] == []

        # Ahora solicitar evaluación con la misma Idempotency-Key sí encola una vez
        accepted = client.post(
            "/api/v1/evaluations",
            headers={**planner_headers, "Idempotency-Key": idem_key},
            json={"project_version_id": str(version_id)},
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "queued"
        assert accepted.json()["duplicated"] is False
        assert len(dispatcher.dispatched) == 1
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_2_version_isolation_validates_target_version_independently(
    isolated_env,
) -> None:
    """
    Asegura que al cambiar de versión (v1 completa vs v2 incompleta en BD), validate_project
    y request_evaluation inspeccionan exactamente la versión solicitada sin mezclar estados.
    """
    client, conn, dispatcher, _ = isolated_env
    planner_headers = _login(client, "planner@siprim.test")

    created_ids: list[UUID] = []
    try:
        v1_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Proyecto Multi-Versión HU1.2",
                "description": "Versión 1 completa.",
                "location": "Sector Centro - Huancayo",
                "proposed_land_use": "Equipamiento urbano",
                "estimated_budget_pen": 150000,
                "beneficiaries_count": 750,
            },
        )
        assert v1_resp.status_code == 201
        project_id = UUID(v1_resp.json()["project_id"])
        v1_id = UUID(v1_resp.json()["project_version_id"])
        created_ids.append(project_id)

        # Crear v2 como borrador incompleto mediante create_new_version=True + save_as_draft=True
        v2_resp = client.put(
            f"/api/v1/projects/{project_id}",
            headers=planner_headers,
            json={
                "location": None,
                "proposed_land_use": "",
                "save_as_draft": True,
                "create_new_version": True,
            },
        )
        assert v2_resp.status_code == 200, v2_resp.text
        v2_id = UUID(v2_resp.json()["project_version_id"])
        assert v2_resp.json()["version_number"] == 2
        assert v2_resp.json()["status"] == "draft"

        # v2 es incompleta; v1 sigue completa al consultar con ?version_id=v1
        val_v2 = client.post(
            f"/api/v1/projects/{project_id}/validate?version_id={v2_id}",
            headers=planner_headers,
        )
        assert val_v2.status_code == 200
        assert val_v2.json()["complete"] is False
        assert val_v2.json()["version_number"] == 2
        assert val_v2.json()["missing_fields"] == ["location", "proposed_land_use"]

        val_v1 = client.post(
            f"/api/v1/projects/{project_id}/validate?version_id={v1_id}",
            headers=planner_headers,
        )
        assert val_v1.status_code == 200
        assert val_v1.json()["complete"] is True
        assert val_v1.json()["version_number"] == 1

        # v2 bloqueada en evaluación (422); v1 no alterada
        eval_v2 = client.post(
            "/api/v1/evaluations",
            headers={**planner_headers, "Idempotency-Key": f"v2-block-{uuid4().hex[:6]}"},
            json={"project_version_id": str(v2_id)},
        )
        assert eval_v2.status_code == 422
        assert len(dispatcher.dispatched) == 0

        # Guardas de valores numéricos <= 0 y título en blanco -> 422 incluso con save_as_draft=True
        invalid_nums = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "   ",
                "description": "Desc",
                "location": "Huancayo",
                "proposed_land_use": "Equipamiento",
                "estimated_budget_pen": 0,
                "beneficiaries_count": -5,
                "save_as_draft": True,
            },
        )
        assert invalid_nums.status_code == 422
        assert set(invalid_nums.json()["missing_fields"]) >= {
            "title",
            "estimated_budget_pen (> 0)",
            "beneficiaries_count (> 0)",
        }
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_2_blocks_all_multidimensional_evaluations_and_allows_reads_until_completed(
    isolated_env,
) -> None:
    """
    Verifica integralmente HU1.2 sobre TODAS las vías de evaluación y consulta:
    - Borrador sin presupuesto ni beneficiarios (ni descripción/ubicación/uso de suelo)
      se persiste vía flujo oficial (save_as_draft=True) gracias a la migración 14_...sql.
    - Las consultas de solo lectura (GET proyecto, historial, resumen unificado, ranking,
      vistas previas social/ambiental/técnica/jurídica) operan sin fallar ni escribir filas.
    - Todas las vías que crean/persisten evaluaciones (económica, social, ambiental, técnica,
      jurídica y predicción ML sin presupuesto) rechazan con 422 y ZERO escrituras en BD.
    - Tras completar todos los datos obligatorios vía PUT /api/v1/projects/{id}, TODAS
      las vías vuelven a funcionar con normalidad.
    """
    client, conn, dispatcher, _ = isolated_env
    planner_headers = _login(client, "planner@siprim.test")
    legal_headers = _login(client, "legal@siprim.test")
    admin_headers = _login(client, "admin@siprim.test")

    created_ids: list[UUID] = []
    try:
        # 1. Crear borrador mínimo (solo title) sin descripción, ubicación, uso de suelo, presupuesto ni beneficiarios
        draft_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Borrador Totalmente Mínimo HU1.2",
                "save_as_draft": True,
            },
        )
        assert draft_resp.status_code == 201, draft_resp.text
        draft = draft_resp.json()
        project_id = UUID(draft["project_id"])
        version_id = UUID(draft["project_version_id"])
        created_ids.append(project_id)
        assert draft["status"] == "draft"
        assert draft["estimated_budget_pen"] is None
        assert draft["beneficiaries_count"] is None

        # 2. Validate identifica los 5 campos faltantes en español
        val_resp = client.post(f"/api/v1/projects/{project_id}/validate", headers=planner_headers)
        assert val_resp.status_code == 200
        assert val_resp.json()["complete"] is False
        assert val_resp.json()["missing_fields"] == [
            "description",
            "location",
            "proposed_land_use",
            "estimated_budget_pen",
            "beneficiaries_count",
        ]

        # 3. Consultas de solo lectura funcionan sobre el borrador sin 500 ni escrituras
        assert client.get(f"/api/v1/projects/{project_id}", headers=planner_headers).status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/social-assessment", headers=planner_headers).status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/environmental-assessment", headers=planner_headers).status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/technical-assessment", headers=planner_headers).status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/legal-assessment", headers=legal_headers).status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/unified-summary", headers=planner_headers).status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/history", headers=admin_headers).status_code == 200
        assert client.get("/api/v1/projects/ranking", headers=planner_headers).status_code == 200

        # Las consultas previas NO deben haber persistido ninguna evaluación jurídica/social/ambiental/técnica
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legal_assessments WHERE project_id = %s",
                (project_id,),
            ).fetchone()[0]
            == 0
        )

        audit_before = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_id = %s", (project_id,)
        ).fetchone()[0]

        # 4. Intentar TODAS las vías de creación/persistencia de evaluaciones sobre el borrador -> 422 con cero escrituras
        for method_call in (
            lambda: client.post(
                "/api/v1/evaluations",
                headers={**planner_headers, "Idempotency-Key": f"econ-blk-{uuid4().hex[:6]}"},
                json={"project_version_id": str(version_id)},
            ),
            lambda: client.post(
                f"/api/v1/projects/{project_id}/social-assessment",
                headers={**planner_headers, "Idempotency-Key": f"soc-blk-{uuid4().hex[:6]}"},
            ),
            lambda: client.post(
                f"/api/v1/projects/{project_id}/environmental-assessment",
                headers={**planner_headers, "Idempotency-Key": f"env-blk-{uuid4().hex[:6]}"},
            ),
            lambda: client.post(
                f"/api/v1/projects/{project_id}/technical-assessment",
                headers={**planner_headers, "Idempotency-Key": f"tech-blk-{uuid4().hex[:6]}"},
            ),
            lambda: client.post(
                f"/api/v1/projects/{project_id}/legal-assessment",
                headers={**legal_headers, "Idempotency-Key": f"leg-blk-{uuid4().hex[:6]}"},
            ),
            lambda: client.post(
                f"/api/v1/ml/projects/{project_id}/predict",
                headers=planner_headers,
                json={
                    "project_version_id": str(version_id),
                    "function": "TRANSPORTE",
                    "department": "JUNIN",
                    "execution_mode": "ADMINISTRACIÓN DIRECTA",
                    "investment_type": "PROYECTO DE INVERSION",
                    "planned_days": 120,
                },
            ),
        ):
            r = method_call()
            assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"
            body = r.json()
            assert "missing_fields" in body and len(body["missing_fields"]) >= 1

        # Verificar cero escrituras en TODAS las tablas de evaluación, auditoría, checkpoints y status
        assert len(dispatcher.dispatched) == 0
        assert conn.execute("SELECT COUNT(*) FROM evaluations WHERE project_version_id = %s", (version_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM social_accessibility_assessments WHERE project_id = %s", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM environmental_assessments WHERE project_id = %s", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM technical_feasibility_assessments WHERE project_id = %s", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM legal_assessments WHERE project_id = %s", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM evaluation_checkpoints WHERE project_id = %s", (project_id,)).fetchone()[0] == 0
        assert conn.execute("SELECT status FROM projects WHERE id = %s", (project_id,)).fetchone()[0] == "draft"
        assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE entity_id = %s", (project_id,)).fetchone()[0] == audit_before

        # 5. Completar TODOS los datos obligatorios (incluido presupuesto y beneficiarios)
        done = client.put(
            f"/api/v1/projects/{project_id}",
            headers=planner_headers,
            json={
                "description": "Construcción de losa deportiva multideportiva en El Tambo.",
                "location": "Jr. Libertad cdra. 2, El Tambo - Huancayo",
                "proposed_land_use": "Equipamiento recreativo y deportivo",
                "estimated_budget_pen": 320000,
                "beneficiaries_count": 1500,
                "save_as_draft": False,
            },
        )
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "ready"
        assert float(done.json()["estimated_budget_pen"]) == 320000.0
        assert done.json()["beneficiaries_count"] == 1500

        val_ok = client.post(f"/api/v1/projects/{project_id}/validate", headers=planner_headers)
        assert val_ok.status_code == 200
        assert val_ok.json()["complete"] is True

        # 6. Tras completar el expediente, TODAS las vías de evaluación vuelven a quedar habilitadas
        assert (
            client.post(
                f"/api/v1/projects/{project_id}/social-assessment",
                headers={**planner_headers, "Idempotency-Key": f"soc-ok-{uuid4().hex[:6]}"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/projects/{project_id}/environmental-assessment",
                headers={**planner_headers, "Idempotency-Key": f"env-ok-{uuid4().hex[:6]}"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/projects/{project_id}/technical-assessment",
                headers={**planner_headers, "Idempotency-Key": f"tech-ok-{uuid4().hex[:6]}"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/v1/projects/{project_id}/legal-assessment",
                headers={**legal_headers, "Idempotency-Key": f"leg-ok-{uuid4().hex[:6]}"},
            ).status_code
            == 200
        )
        econ_ok = client.post(
            "/api/v1/evaluations",
            headers={**planner_headers, "Idempotency-Key": f"econ-ok-{uuid4().hex[:6]}"},
            json={"project_version_id": str(version_id)},
        )
        assert econ_ok.status_code == 202
        assert len(dispatcher.dispatched) == 1
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)
