from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import (
    EconomicProcessor,
    PlatformService,
    SocioeconomicReturnEngine,
)
from backend.domain.entities.evaluacion_economica import EvaluacionEconomica
from backend.infrastructure.config.dependencies import get_platform_service
from backend.infrastructure.config.settings import get_settings
from backend.infrastructure.output.broker.serverless_dispatcher import ServerlessTaskDispatcher
from backend.main import create_app


def _cleanup_project(platform_conn: psycopg.Connection, economic_conn: psycopg.Connection, project_id: UUID) -> None:
    pv_ids = [
        r[0]
        for r in platform_conn.execute(
            "SELECT id FROM project_versions WHERE project_id = %s", (project_id,)
        ).fetchall()
    ]
    if pv_ids:
        platform_conn.execute("DELETE FROM evaluation_dimension_dispatches WHERE project_version_id = ANY(%s)", (pv_ids,))
        platform_conn.execute("DELETE FROM social_accessibility_assessments WHERE project_version_id = ANY(%s)", (pv_ids,))
        platform_conn.execute("DELETE FROM environmental_assessments WHERE project_version_id = ANY(%s)", (pv_ids,))
        platform_conn.execute("DELETE FROM technical_feasibility_assessments WHERE project_version_id = ANY(%s)", (pv_ids,))
        platform_conn.execute("DELETE FROM legal_assessments WHERE project_version_id = ANY(%s)", (pv_ids,))
        ev_ids = [
            r[0]
            for r in platform_conn.execute(
                "SELECT id FROM evaluations WHERE project_version_id = ANY(%s)",
                (pv_ids,),
            ).fetchall()
        ]
        if ev_ids:
            platform_conn.execute("DELETE FROM outbox_events WHERE aggregate_id = ANY(%s)", (ev_ids,))
            platform_conn.execute(
                "DELETE FROM economic_result_projections WHERE evaluation_id = ANY(%s)",
                (ev_ids,),
            )
            platform_conn.execute(
                "DELETE FROM evaluations WHERE id = ANY(%s)",
                (ev_ids,),
            )
            ea_ids = [
                r[0]
                for r in economic_conn.execute(
                    "SELECT id FROM economic_assessments WHERE evaluation_id = ANY(%s)",
                    (ev_ids,),
                ).fetchall()
            ]
            if ea_ids:
                economic_conn.execute(
                    "DELETE FROM processed_requests WHERE assessment_id = ANY(%s)",
                    (ea_ids,),
                )
                economic_conn.execute(
                    "DELETE FROM assessment_metrics WHERE assessment_id = ANY(%s)",
                    (ea_ids,),
                )
                economic_conn.execute(
                    "DELETE FROM economic_assessments WHERE id = ANY(%s)",
                    (ea_ids,),
                )
        platform_conn.execute("DELETE FROM project_versions WHERE id = ANY(%s)", (pv_ids,))
    platform_conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


@pytest.fixture
def isolated_hu13_env():
    settings = get_settings()
    app = create_app()
    platform_svc = PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=ServerlessTaskDispatcher(),
        enable_demo_fallback=False,
        auth_db_url=settings.auth_db_url,
    )
    app.dependency_overrides[get_platform_service] = lambda: platform_svc
    p_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    e_url = settings.economic_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with (
        TestClient(app) as client,
        psycopg.connect(p_url, autocommit=True) as p_conn,
        psycopg.connect(e_url, autocommit=True) as e_conn,
    ):
        yield client, p_conn, e_conn


def _login_planner(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "planner@siprim.test", "password": "Prueba2026!"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_hu1_3_ca01_ca03_120k_600_end_to_end_and_no_invented_return(
    isolated_hu13_env,
) -> None:
    """
    CA-HU1.3-01 & CA-HU1.3-03:
    - S/ 120 000 y 600 beneficiarios producen S/ 200.00 por beneficiario y puntaje 100.00.
    - Expone fórmula 'presupuesto / beneficiarios', moneda 'PEN', redondeo HALF_UP y entradas exactas.
    - Al no existir método aprobado ni beneficios monetizados, socioeconomic_return es None y se informa
      explícitamente 'No calculable' con su motivo tanto en /evaluations/{id} como en /projects/{id}.
    """
    client, p_conn, e_conn = isolated_hu13_env
    headers = _login_planner(client)
    created_ids: list[UUID] = []
    try:
        created = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "title": "Proyecto HU1.3 120k 600",
                "description": "Prueba de cálculo de costo por beneficiario y abstención de retorno.",
                "location": "El Tambo - Huancayo",
                "proposed_land_use": "Equipamiento urbano",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 600,
            },
        )
        assert created.status_code == 201, created.text
        project_id = UUID(created.json()["project_id"])
        version_id = UUID(created.json()["project_version_id"])
        created_ids.append(project_id)

        queued = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu13-120k-{uuid4().hex[:8]}"},
            json={"project_version_id": str(version_id)},
        )
        assert queued.status_code == 202, queued.text
        eval_id = UUID(queued.json()["evaluation_id"])

        ev = client.get(f"/api/v1/evaluations/{eval_id}", headers=headers)
        assert ev.status_code == 200, ev.text
        body = ev.json()
        assert body["status"] == "completed"
        res = body["result"]
        assert Decimal(str(res["cost_per_beneficiary_pen"])) == Decimal("200.00")
        assert Decimal(str(res["score_0_100"])) == Decimal("100.00")
        assert res["indicator_name"] == "Costo por beneficiario"
        assert res["currency"] == "PEN"
        assert res["formula"] == "presupuesto / beneficiarios"
        assert "ROUND_HALF_UP" in res["rounding_rule"]
        assert Decimal(str(res["budget_pen"])) == Decimal("120000.00")
        assert res["beneficiaries_count"] == 600
        assert "120000" in res["explanation"]
        assert "600" in res["explanation"]
        assert "200.00" in res["explanation"]
        # CA-HU1.3-03: no se inventa retorno
        assert res["socioeconomic_return"] is None
        assert res["socioeconomic_return_status"] == "not_calculable"
        assert res["socioeconomic_return_label"] == "No calculable"
        assert "beneficios monetizados" in res["socioeconomic_return_reason"].lower()

        # También persiste y se recupera al recargar GET /projects/{id}
        proj = client.get(f"/api/v1/projects/{project_id}", headers=headers)
        assert proj.status_code == 200
        persisted = proj.json()["economic_evaluation"]
        assert persisted is not None
        assert persisted["evaluation_id"] == str(eval_id)
        assert Decimal(str(persisted["result"]["cost_per_beneficiary_pen"])) == Decimal("200.00")
        assert persisted["result"]["socioeconomic_return"] is None
        assert persisted["result"]["socioeconomic_return_label"] == "No calculable"

        # Verificar registro exacto en economic_db
        metric = e_conn.execute(
            """
            SELECT m.metric_code, m.numeric_value, m.unit, m.formula
            FROM assessment_metrics m
            JOIN economic_assessments ea ON ea.id = m.assessment_id
            WHERE ea.evaluation_id = %s AND m.metric_code = 'COST_PER_BENEFICIARY'
            """,
            (eval_id,),
        ).fetchone()
        assert metric is not None
        assert metric[1] == Decimal("200.00")
        assert metric[2] == "PEN/person"
        assert metric[3] == "budget / beneficiaries"
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, e_conn, pid)


def test_hu1_3_rounding_invalid_inputs_and_version_isolation(
    isolated_hu13_env,
) -> None:
    """
    - Redondeo ROUND_HALF_UP exacto (incluyendo mitades .xx5).
    - Presupuesto o beneficiarios faltantes / <= 0 rechazados sin divisiones ni retornos ficticios.
    - Al editar/crear nueva versión (v2), v2 NO reutiliza silenciosamente el resultado de v1.
    """
    # Redondeo HALF_UP en EconomicProcessor y EvaluacionEconomica heredada
    assert EconomicProcessor.calculate(Decimal("100000"), 3, Decimal("200"), Decimal("500"))[0] == Decimal("33333.33")
    assert EconomicProcessor.calculate(Decimal("10010"), 2000, Decimal("200"), Decimal("500"))[0] == Decimal("5.01")
    assert EconomicProcessor.calculate(Decimal("10005"), 2000, Decimal("200"), Decimal("500"))[0] == Decimal("5.00")

    legacy = EvaluacionEconomica.calcular(1, Decimal("120000"), 600)
    assert legacy.costo_por_habitante == Decimal("200.00")
    assert legacy.formula == "presupuesto / beneficiarios"
    assert legacy.retorno_socioeconomico is None

    for bad_budget, bad_ben in ((Decimal("0"), 10), (Decimal("-100"), 10), (Decimal("1000"), 0), (Decimal("1000"), -5)):
        with pytest.raises(ValueError):
            EconomicProcessor.calculate(bad_budget, bad_ben, Decimal("200"), Decimal("500"))
        with pytest.raises(ValueError):
            EvaluacionEconomica.calcular(1, bad_budget, bad_ben)

    client, p_conn, e_conn = isolated_hu13_env
    headers = _login_planner(client)
    created_ids: list[UUID] = []
    try:
        # Borrador sin presupuesto ni beneficiarios no permite evaluar ni calcula división alguna
        draft = client.post(
            "/api/v1/projects",
            headers=headers,
            json={"title": "Borrador sin monto HU1.3", "save_as_draft": True},
        )
        assert draft.status_code == 201
        draft_pid = UUID(draft.json()["project_id"])
        draft_vid = UUID(draft.json()["project_version_id"])
        created_ids.append(draft_pid)
        blk = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu13-blk-{uuid4().hex[:6]}"},
            json={"project_version_id": str(draft_vid)},
        )
        assert blk.status_code == 422
        assert p_conn.execute("SELECT COUNT(*) FROM evaluations WHERE project_version_id = %s", (draft_vid,)).fetchone()[0] == 0

        # Proyecto completo v1 (120 000 / 600 -> S/ 200.00) -> luego v2 (90 000 / 300 -> S/ 300.00)
        v1 = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "title": "Proyecto Multi-Versión HU1.3",
                "description": "Versión 1 inicial.",
                "location": "El Tambo",
                "proposed_land_use": "Equipamiento",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 600,
            },
        )
        assert v1.status_code == 201
        pid = UUID(v1.json()["project_id"])
        v1_id = UUID(v1.json()["project_version_id"])
        created_ids.append(pid)

        ev1 = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu13-v1-{uuid4().hex[:6]}"},
            json={"project_version_id": str(v1_id)},
        )
        assert ev1.status_code == 202
        p_after_v1 = client.get(f"/api/v1/projects/{pid}", headers=headers).json()
        assert Decimal(str(p_after_v1["economic_evaluation"]["result"]["cost_per_beneficiary_pen"])) == Decimal("200.00")

        # Crear nueva versión v2 con distinto presupuesto/beneficiarios
        v2 = client.put(
            f"/api/v1/projects/{pid}",
            headers=headers,
            json={
                "description": "Versión 2 replanteada.",
                "estimated_budget_pen": 90000,
                "beneficiaries_count": 300,
                "create_new_version": True,
                "save_as_draft": False,
            },
        )
        assert v2.status_code == 200, v2.text
        v2_body = v2.json()
        v2_id = UUID(v2_body["project_version_id"])
        assert v2_body["version_number"] == 2
        # v2 recién creada NO hereda ni reutiliza el resultado económico de v1
        assert v2_body["economic_evaluation"] is None

        # Evaluar v2 produce S/ 300.00 y puntaje 66.67
        ev2 = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu13-v2-{uuid4().hex[:6]}"},
            json={"project_version_id": str(v2_id)},
        )
        assert ev2.status_code == 202
        p_after_v2 = client.get(f"/api/v1/projects/{pid}", headers=headers).json()
        assert p_after_v2["economic_evaluation"]["project_version_id"] == str(v2_id)
        assert Decimal(str(p_after_v2["economic_evaluation"]["result"]["cost_per_beneficiary_pen"])) == Decimal("300.00")
        assert Decimal(str(p_after_v2["economic_evaluation"]["result"]["score_0_100"])) == Decimal("66.67")
        res_v2 = p_after_v2["economic_evaluation"]["result"]
        assert res_v2["socioeconomic_return"] is None
        assert res_v2["socioeconomic_return_integration_status"] == "disabled_pending_method_approval"
        assert len(res_v2["socioeconomic_return_missing_requirements"]) >= 4

        # Validar rechazo HTTP 422 ante presupuesto/beneficiarios <= 0 en registro y edición
        for bad_payload in (
            {
                "title": "Presupuesto cero HU1.3",
                "estimated_budget_pen": 0,
                "beneficiaries_count": 100,
                "save_as_draft": True,
            },
            {
                "title": "Beneficiarios negativos HU1.3",
                "estimated_budget_pen": 50000,
                "beneficiaries_count": -10,
                "save_as_draft": True,
            },
        ):
            bad_resp = client.post("/api/v1/projects", headers=headers, json=bad_payload)
            assert bad_resp.status_code == 422, bad_resp.text

        bad_put = client.put(
            f"/api/v1/projects/{pid}",
            headers=headers,
            json={"estimated_budget_pen": 0, "beneficiaries_count": 10},
        )
        assert bad_put.status_code == 422, bad_put.text
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, e_conn, pid)


def test_hu1_3_socioeconomic_return_integration_hook_contract() -> None:
    """
    Punto de integración para método futuro aprobado (CA-HU1.3-02 / CA-HU1.3-03):
    - Por defecto está deshabilitado (_APPROVED_METHOD is None) y no inventa retorno.
    - Con un calculador de prueba controlado en memoria: exige todas las variables y fuentes;
      si falta un dato responde 'Información insuficiente', y con datos completos expone valor,
      variables, método y fuentes sin alterar la producción por defecto.
    """
    assert SocioeconomicReturnEngine._APPROVED_METHOD is None
    default_out = SocioeconomicReturnEngine.evaluate(
        budget_pen=Decimal("120000"),
        beneficiaries_count=600,
        socioeconomic_inputs=None,
    )
    assert default_out["socioeconomic_return"] is None
    assert default_out["socioeconomic_return_status"] == "not_calculable"
    assert default_out["socioeconomic_return_integration_status"] == "disabled_pending_method_approval"
    assert len(default_out["socioeconomic_return_missing_requirements"]) >= 4

    class _ContractTestMethod:
        method_name = "Método Contractual de Prueba VANS (NO APROBADO EN PRODUCCIÓN)"
        method_version = "test-contract-v1"
        formula = "sum(B_t / (1 + r)^t) - I_0"
        required_fields = ("annual_benefit_pen", "social_discount_rate", "horizon_years")

        def compute(self, *, budget_pen: Decimal, beneficiaries_count: int, inputs: dict):
            annual = Decimal(str(inputs["annual_benefit_pen"]))
            r = Decimal(str(inputs["social_discount_rate"]))
            n = int(inputs["horizon_years"])
            pv = sum(annual / ((Decimal("1") + r) ** t) for t in range(1, n + 1))
            vans = (pv - budget_pen).quantize(Decimal("0.01"))
            return {
                "value": vans,
                "label": f"S/ {vans} (VANS)",
                "variables": {
                    "inversion_inicial_pen": float(budget_pen),
                    "beneficiarios": beneficiaries_count,
                    "beneficio_anual_pen": float(annual),
                    "tasa_social_descuento": float(r),
                    "horizonte_anios": n,
                },
            }

    with patch.object(SocioeconomicReturnEngine, "_APPROVED_METHOD", _ContractTestMethod()):
        # Si falta un dato o la fuente -> abstención explícita (CA-HU1.3-03)
        incomplete = SocioeconomicReturnEngine.evaluate(
            budget_pen=Decimal("120000"),
            beneficiaries_count=600,
            socioeconomic_inputs={"annual_benefit_pen": 30000, "sources": []},
        )
        assert incomplete["socioeconomic_return"] is None
        assert incomplete["socioeconomic_return_label"] == "Información insuficiente"
        assert "social_discount_rate" in incomplete["socioeconomic_return_missing_requirements"]
        assert any("sources" in m for m in incomplete["socioeconomic_return_missing_requirements"])

        # Con método + todas las entradas + fuentes trazables -> CA-HU1.3-02
        complete = SocioeconomicReturnEngine.evaluate(
            budget_pen=Decimal("120000"),
            beneficiaries_count=600,
            socioeconomic_inputs={
                "annual_benefit_pen": 35000,
                "social_discount_rate": "0.08",
                "horizon_years": 5,
                "sources": ["Ficha Técnica Invierte.pe N.° 001 (prueba contractual)"],
            },
        )
        assert complete["socioeconomic_return_status"] == "calculated"
        assert complete["socioeconomic_return"] == Decimal("19744.85")
        assert complete["socioeconomic_return_method"]["formula"] == "sum(B_t / (1 + r)^t) - I_0"
        assert complete["socioeconomic_return_variables"]["tasa_social_descuento"] == 0.08
        assert complete["socioeconomic_return_sources"] == [
            "Ficha Técnica Invierte.pe N.° 001 (prueba contractual)"
        ]
