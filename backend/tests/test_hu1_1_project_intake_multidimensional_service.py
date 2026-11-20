from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import PlatformService
from backend.infrastructure.config.dependencies import get_platform_service
from backend.infrastructure.config.settings import get_settings
from backend.infrastructure.output.broker.serverless_dispatcher import ServerlessTaskDispatcher
from backend.main import create_app


def _cleanup_project(p_conn: psycopg.Connection, e_conn: psycopg.Connection, project_id: UUID) -> None:
    pv_ids = [
        r[0]
        for r in p_conn.execute(
            "SELECT id FROM project_versions WHERE project_id = %s", (project_id,)
        ).fetchall()
    ]
    if pv_ids:
        ev_ids = [
            r[0]
            for r in p_conn.execute(
                "SELECT id FROM evaluations WHERE project_version_id = ANY(%s)", (pv_ids,)
            ).fetchall()
        ]
        if ev_ids:
            p_conn.execute("DELETE FROM outbox_events WHERE aggregate_id = ANY(%s)", (ev_ids,))
            p_conn.execute("DELETE FROM economic_result_projections WHERE evaluation_id = ANY(%s)", (ev_ids,))
            p_conn.execute("DELETE FROM evaluation_checkpoints WHERE evaluation_id = ANY(%s)", (ev_ids,))
            p_conn.execute("DELETE FROM evaluation_dimension_dispatches WHERE evaluation_id = ANY(%s)", (ev_ids,))
            p_conn.execute("DELETE FROM evaluations WHERE id = ANY(%s)", (ev_ids,))
            ea_ids = [
                r[0]
                for r in e_conn.execute(
                    "SELECT id FROM economic_assessments WHERE evaluation_id = ANY(%s)", (ev_ids,)
                ).fetchall()
            ]
            if ea_ids:
                e_conn.execute("DELETE FROM processed_requests WHERE assessment_id = ANY(%s)", (ea_ids,))
                e_conn.execute("DELETE FROM assessment_metrics WHERE assessment_id = ANY(%s)", (ea_ids,))
                e_conn.execute("DELETE FROM economic_assessments WHERE id = ANY(%s)", (ea_ids,))
        p_conn.execute("DELETE FROM evaluation_checkpoints WHERE project_version_id = ANY(%s)", (pv_ids,))
        p_conn.execute("DELETE FROM evaluation_dimension_dispatches WHERE project_version_id = ANY(%s)", (pv_ids,))
        p_conn.execute("DELETE FROM social_accessibility_assessments WHERE project_id = %s", (project_id,))
        p_conn.execute("DELETE FROM territorial_social_indicators WHERE project_id = %s", (project_id,))
        p_conn.execute("DELETE FROM project_demographic_equity_records WHERE project_id = %s", (project_id,))
        p_conn.execute("DELETE FROM environmental_assessments WHERE project_id = %s", (project_id,))
        p_conn.execute("DELETE FROM project_environmental_records WHERE project_version_id = ANY(%s)", (pv_ids,))
        p_conn.execute("DELETE FROM project_green_area_records WHERE project_version_id = ANY(%s)", (pv_ids,))
        p_conn.execute("DELETE FROM technical_feasibility_assessments WHERE project_id = %s", (project_id,))
        p_conn.execute("DELETE FROM project_technical_parameters WHERE project_version_id = ANY(%s)", (pv_ids,))
        p_conn.execute("DELETE FROM legal_assessments WHERE project_id = %s", (project_id,))
        p_conn.execute("DELETE FROM project_legal_findings WHERE project_id = %s", (project_id,))
        p_conn.execute("DELETE FROM project_versions WHERE id = ANY(%s)", (pv_ids,))
    p_conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


@pytest.fixture
def isolated_hu11_env():
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
        yield client, p_conn, e_conn, platform_svc


def _login_planner(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": "planner@siprim.test", "password": "Prueba2026!"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_hu1_1_ca01_ca03_unique_registration_query_invalid_and_duplicate_rejection(
    isolated_hu11_env,
) -> None:
    """
    CA-HU1.1-01 & CA-HU1.1-03:
    - Registro válido persiste 1 sola vez, asigna project_id/code y permite consulta posterior sin recargar.
    - Solicitudes inválidas se rechazan con 422 sin persistir.
    - Repetición exacta con mismo Idempotency-Key devuelve el mismo expediente (duplicated=True).
    - Repetición exacta sin la misma Idempotency-Key (o código repetido) responde 409 sin crear duplicados.
    """
    client, p_conn, e_conn, _ = isolated_hu11_env
    headers = _login_planner(client)
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        # 1. Inválida: presupuesto negativo / campos vacíos -> 422 sin persistencia
        invalid = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "title": f"Carga Inválida HU1.1 {uniq}",
                "description": "",
                "location": "",
                "proposed_land_use": "Equipamiento urbano",
                "estimated_budget_pen": -500,
                "beneficiaries_count": 0,
            },
        )
        assert invalid.status_code == 422, invalid.text
        assert (
            p_conn.execute(
                "SELECT count(*) FROM project_versions WHERE title = %s",
                (f"Carga Inválida HU1.1 {uniq}",),
            ).fetchone()[0]
            == 0
        )

        # 2. Carga válida con Idempotency-Key
        payload = {
            "title": f"Mejoramiento Plaza Cívica HU1.1 {uniq}",
            "description": "Recuperación integral de plaza vecinal e iluminación LED.",
            "location": "Jr. Sebastián Lorente cdra. 3, El Tambo - Huancayo",
            "proposed_land_use": "Recreación pública",
            "estimated_budget_pen": 120000,
            "beneficiaries_count": 600,
        }
        reg_key = f"hu11-reg-{uniq}"
        r1 = client.post("/api/v1/projects", headers={**headers, "Idempotency-Key": reg_key}, json=payload)
        assert r1.status_code == 201, r1.text
        b1 = r1.json()
        pid = UUID(b1["project_id"])
        created_ids.append(pid)
        assert b1["code"].startswith("EXP-")
        assert b1["version_number"] == 1
        assert b1["status"] == "ready"
        assert b1["duplicated"] is False

        # 3. Consulta posterior por UUID y por código sin volver a cargar
        q_id = client.get(f"/api/v1/projects/{pid}", headers=headers)
        assert q_id.status_code == 200
        assert q_id.json()["project_id"] == str(pid)
        assert q_id.json()["title"] == payload["title"]

        q_code = client.get(f"/api/v1/projects/by-code/{b1['code']}", headers=headers)
        assert q_code.status_code == 200
        assert q_code.json()["project_id"] == str(pid)

        # 4. Repetición con misma Idempotency-Key -> devuelve mismo expediente, 0 duplicados
        r_idem = client.post("/api/v1/projects", headers={**headers, "Idempotency-Key": reg_key}, json=payload)
        assert r_idem.status_code == 201
        assert r_idem.json()["project_id"] == str(pid)
        assert r_idem.json()["duplicated"] is True

        # 5. Misma Idempotency-Key con carga distinta -> 409 Conflicto
        r_mismatch = client.post(
            "/api/v1/projects",
            headers={**headers, "Idempotency-Key": reg_key},
            json={**payload, "title": f"Otro proyecto {uniq}"},
        )
        assert r_mismatch.status_code == 409

        # 6. Repetición exacta con nueva clave (o sin clave) -> 409 Conflicto, no crea duplicados
        r_dup = client.post("/api/v1/projects", headers=headers, json=payload)
        assert r_dup.status_code == 409, r_dup.text
        assert "duplicad" in r_dup.json()["detail"].lower()

        total_rows = p_conn.execute(
            "SELECT count(*) FROM project_versions WHERE title = %s",
            (payload["title"],),
        ).fetchone()[0]
        assert total_rows == 1
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, e_conn, pid)


def test_hu1_1_ca02_single_start_distributes_same_version_to_five_specialists_and_shows_insufficient(
    isolated_hu11_env,
) -> None:
    """
    CA-HU1.1-02:
    - Un solo POST /api/v1/evaluations entrega el mismo project_version_id a los 5 especialistas:
      economic, social, environmental, technical, legal.
    - Sin inventar indicadores faltantes: económico completa (S/ 200), mientras dimensiones sin datos
      muestran 'insufficient_data' («Información insuficiente») con motivo verificable.
    - En un proyecto con insumos completos, los 5 especialistas completan/registran para la misma versión,
      sin aprobar automáticamente el proyecto.
    """
    client, p_conn, e_conn, _ = isolated_hu11_env
    headers = _login_planner(client)
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        # Caso A: Expediente válido recién cargado (solo datos básicos obligatorios, sin anexos de especialistas)
        p_bare = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "title": f"Losa Deportiva Base HU1.1 {uniq}",
                "description": "Construcción de losa multideportiva sin insumos sectoriales previos.",
                "location": "Sector Justicia Paz y Vida, El Tambo",
                "proposed_land_use": "Recreación pública",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 600,
            },
        )
        assert p_bare.status_code == 201
        bare_id = UUID(p_bare.json()["project_id"])
        bare_vid = UUID(p_bare.json()["project_version_id"])
        created_ids.append(bare_id)

        ev_bare = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu11-bare-{uniq}"},
            json={"project_version_id": str(bare_vid)},
        )
        assert ev_bare.status_code == 202, ev_bare.text
        eb = ev_bare.json()
        dispatches_b = {d["dimension_code"]: d for d in eb["dimension_dispatches"]}
        assert set(dispatches_b.keys()) == {"economic", "social", "environmental", "technical", "legal"}
        assert all(d["project_version_id"] == str(bare_vid) for d in dispatches_b.values())

        # Económico calcula S/ 200; las dimensiones sin indicadores adicionales muestran 'insufficient_data'
        assert dispatches_b["economic"]["status"] == "completed"
        assert dispatches_b["social"]["status"] == "insufficient_data"
        assert "insuficiente" in dispatches_b["social"]["status_label"].lower()
        assert dispatches_b["environmental"]["status"] == "insufficient_data"
        assert dispatches_b["technical"]["status"] == "insufficient_data"
        assert dispatches_b["legal"]["status"] == "insufficient_data"

        # Consulta posterior muestra los mismos 5 estados observables y NO aprueba automáticamente
        proj_after = client.get(f"/api/v1/projects/{bare_id}", headers=headers).json()
        assert len(proj_after["dimension_dispatches"]) == 5
        assert proj_after["status"] != "approved"
        summary_b = client.get(f"/api/v1/projects/{bare_id}/unified-summary", headers=headers).json()
        assert summary_b["allows_final_approval"] is False

        # Comprobar persistencia real en Postgres para los 5 especialistas con el mismo project_version_id
        db_dispatches = p_conn.execute(
            "SELECT dimension_code, project_version_id, status FROM evaluation_dimension_dispatches WHERE project_version_id = %s",
            (bare_vid,),
        ).fetchall()
        assert len(db_dispatches) == 5
        assert all(row[1] == bare_vid for row in db_dispatches)
        assert p_conn.execute("SELECT count(*) FROM social_accessibility_assessments WHERE project_version_id = %s", (bare_vid,)).fetchone()[0] == 1
        assert p_conn.execute("SELECT count(*) FROM environmental_assessments WHERE project_version_id = %s", (bare_vid,)).fetchone()[0] == 1
        assert p_conn.execute("SELECT count(*) FROM technical_feasibility_assessments WHERE project_version_id = %s", (bare_vid,)).fetchone()[0] == 1
        assert p_conn.execute("SELECT count(*) FROM legal_assessments WHERE project_version_id = %s", (bare_vid,)).fetchone()[0] == 1

        # Caso B: Expediente con insumos completos en las 5 dimensiones -> un solo inicio evalúa todas
        p_full = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "title": f"Parque Integral Completo HU1.1 {uniq}",
                "description": "Parque con estudios social, ambiental, técnico y legal completos.",
                "location": "Sector La Estancia, El Tambo",
                "proposed_land_use": "Recreación pública",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 600,
            },
        )
        assert p_full.status_code == 201
        full_id = UUID(p_full.json()["project_id"])
        full_vid = UUID(p_full.json()["project_version_id"])
        full_code = p_full.json()["code"]
        created_ids.append(full_id)

        for cat, val in (("salud", 380.0), ("educacion", 290.0), ("transporte", 5.0)):
            p_conn.execute(
                """
                INSERT INTO territorial_social_indicators
                (id, project_id, project_code, category, indicator_name, metric_value, metric_unit, has_sufficient_data, source_name, data_origin)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, 'Censo Prueba HU1.1', 'public')
                """,
                (uuid4(), full_id, full_code, cat, f"Indicador {cat}", val, "minutos" if cat == "transporte" else "metros"),
            )
        for dim, target, pop, pct, crit in (
            ("genero", "Mujeres", 310, 51.67, "Acceso seguro"),
            ("edad", "Ninos y adultos mayores", 210, 35.00, "Accesibilidad universal"),
        ):
            p_conn.execute(
                """
                INSERT INTO project_demographic_equity_records
                (id, project_id, project_code, dimension, has_sufficient_data, target_group, aggregated_population,
                 aggregated_percentage, equity_criterion, declared_benefit, verifiable_data, has_favorable_evidence,
                 score_effect_pts, effect_type, effect_explanation, source_name, data_origin)
                VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s, 'Beneficio paritario', 'Padron INEI', TRUE,
                        10.0, 'favorable', 'Cobertura verificada', 'INEI', 'public')
                """,
                (uuid4(), full_id, full_code, dim, target, pop, pct, crit),
            )
        p_conn.execute(
            """
            INSERT INTO project_environmental_records
            (id, project_id, project_version_id, project_code, has_sufficient_data, environmental_component,
             declared_impacts, declared_measures, verifiable_evidence, proximity_to_water_meters,
             is_in_protected_or_risk_area, source_name, data_origin)
            VALUES (%s, %s, %s, %s, TRUE, 'integral', 'Polvo menor', 'Riego periodico',
                    'Fuera de faja marginal (450 m)', 450.0, FALSE, 'CAT-MPH', 'public')
            """,
            (uuid4(), full_id, full_vid, full_code),
        )
        p_conn.execute(
            """
            INSERT INTO project_green_area_records
            (id, project_id, project_version_id, project_code, has_sufficient_data, total_project_area_m2,
             green_area_m2, green_area_percentage, delimitation_description, declared_affectation,
             verifiable_affectation, affectation_type, protection_measures, has_sufficient_measures,
             score_effect_pts, effect_type, calculation_explanation, source_name, data_origin)
            VALUES (%s, %s, %s, %s, TRUE, 1000.0, 250.0, 25.0, 'Delimitación perimétrica PL-01',
                    'Sin pérdida', 'Conservación verificada de 250 m2', 'conservacion_positiva',
                    'Cerco vivo y riego tecnificado', TRUE, 15.0, 'bonus', 'Conservación activa',
                    'CAT-MPH', 'public')
            """,
            (uuid4(), full_id, full_vid, full_code),
        )
        for c_code, c_name, norm_ref, rule, p_name, p_num, p_unit, p_txt, doc in (
            ("mecanica_suelos", "Capacidad portante RNE E.050", "RNE E.050", "Regla E.050", "q_adm", 1.50, "kg/cm2", None, "EMS CIP 101"),
            ("servicios_basicos", "Factibilidad SEDAM", "RNE OS.010", "Regla OS.010", "Factibilidad", None, None, "Factibilidad vigente SEDAM", "Certificado SEDAM 202"),
            ("topografia_taludes", "Pendiente media RNE CE.020", "RNE CE.020", "Regla CE.020", "Pendiente media", 8.5, "%", None, "Plano Topografico 303"),
            ("dimensionamiento_predio", "Area util de lote", "RNE A.010", "Regla A.010", "Area neta", 1200.0, "m2", None, "Partida SUNARP 404"),
        ):
            p_conn.execute(
                """
                INSERT INTO project_technical_parameters
                (id, project_id, project_version_id, criterion_code, criterion_name,
                 normative_reference, normative_version, has_sufficient_data, parameter_name,
                 parameter_value_numeric, parameter_unit, parameter_value_text, applied_rule,
                 supporting_document, source_origin, is_simulated)
                VALUES (%s, %s, %s, %s, %s, %s, 'v1', TRUE, %s, %s, %s, %s, %s, %s, 'public', FALSE)
                """,
                (uuid4(), full_id, full_vid, c_code, c_name, norm_ref, p_name, p_num, p_unit, p_txt, rule, doc),
            )
        p_conn.execute(
            """
            INSERT INTO project_legal_findings
            (id, project_id, project_version_id, project_code, finding_key, title, description, normative_chunk_id,
             has_precise_reference, custom_article_or_section, complies, requires_human_review)
            VALUES (%s, %s, %s, %s, 'competencia_municipal', 'Competencia municipal', 'Art. 73 LOM', 'LEY27972-05', TRUE, 'Art. 73.2', TRUE, FALSE)
            """,
            (uuid4(), full_id, full_vid, full_code),
        )

        ev_full = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu11-full-{uniq}"},
            json={"project_version_id": str(full_vid)},
        )
        assert ev_full.status_code == 202, ev_full.text
        ef = ev_full.json()
        df_map = {d["dimension_code"]: d for d in ef["dimension_dispatches"]}
        assert all(df_map[k]["status"] == "completed" for k in ("economic", "social", "environmental", "technical", "legal"))
        assert all(df_map[k]["project_version_id"] == str(full_vid) for k in df_map)

        # No aprueba automáticamente al proyecto ni siquiera cuando los 5 completaron
        appr = client.get(f"/api/v1/projects/{full_id}/approval", headers=headers).json()
        assert appr["current_approval"] is None
        assert appr["project_status"] != "approved"
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, e_conn, pid)


def test_hu1_1_retry_idempotency_new_version_isolation_and_controlled_failure_recovery(
    isolated_hu11_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    - Reintentos con la misma o distinta Idempotency-Key sobre una versión ya distribuida no duplican evaluaciones.
    - Una falla controlada en un especialista ('technical') muestra estado real 'failed', impide marcar 'evaluated',
      conserva intactas las otras 4 dimensiones y permite recuperación segura con retry sólo sobre lo fallido.
    - Crear v2 aísla resultados y distribuye v2 de forma independiente.
    """
    client, p_conn, e_conn, svc = isolated_hu11_env
    headers = _login_planner(client)
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        reg = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "title": f"Recuperación Controlada HU1.1 {uniq}",
                "description": "Prueba falla controlada y reintento seguro sin duplicar dimensiones.",
                "location": "Av. Ferrocarril, El Tambo",
                "proposed_land_use": "Infraestructura vial",
                "estimated_budget_pen": 180000,
                "beneficiaries_count": 900,
            },
        )
        assert reg.status_code == 201
        pid = UUID(reg.json()["project_id"])
        v1_id = UUID(reg.json()["project_version_id"])
        created_ids.append(pid)

        orig_exec = svc._execute_specialist_dimension

        def _failing_tech(*args, **kwargs):
            if kwargs.get("dimension_code") == "technical":
                raise RuntimeError("Error simulado controlado del motor técnico")
            return orig_exec(*args, **kwargs)

        monkeypatch.setattr(svc, "_execute_specialist_dimension", _failing_tech)

        ev1 = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu11-fail-{uniq}"},
            json={"project_version_id": str(v1_id)},
        )
        assert ev1.status_code == 202, ev1.text
        b1 = ev1.json()
        assert b1["has_failed_dimensions"] is True
        dmap1 = {d["dimension_code"]: d for d in b1["dimension_dispatches"]}
        assert dmap1["technical"]["status"] == "failed"
        assert "error simulado controlado" in (dmap1["technical"]["error_message"] or "").lower()
        assert dmap1["economic"]["status"] == "completed"

        # El proyecto NO puede figurar como 'evaluated' ni habilitado para aprobación definitiva
        proj_mid = client.get(f"/api/v1/projects/{pid}", headers=headers).json()
        assert proj_mid["status"] != "evaluated"
        assert proj_mid["has_failed_dimensions"] is True
        summ_mid = client.get(f"/api/v1/projects/{pid}/unified-summary", headers=headers).json()
        assert summ_mid["allows_final_approval"] is False
        tech_dim = next(d for d in summ_mid["dimensions"] if d["dimension_code"] == "technical")
        assert tech_dim["status"] == "failed"

        # Recuperación segura: restaurar motor técnico y reintentar sólo lo fallido
        monkeypatch.undo()
        retry_res = client.post(f"/api/v1/projects/{pid}/evaluations/retry", headers=headers)
        assert retry_res.status_code == 200, retry_res.text
        rb = retry_res.json()
        assert rb["has_failed_dimensions"] is False
        dmap_r = {d["dimension_code"]: d for d in rb["dimension_dispatches"]}
        assert dmap_r["technical"]["status"] == "insufficient_data"
        assert dmap_r["technical"]["attempt_count"] == 2
        # Las demás dimensiones no se reejecutaron innecesariamente ni se duplicaron
        assert dmap_r["economic"]["attempt_count"] == 1
        assert dmap_r["social"]["attempt_count"] == 1
        assert dmap_r["social"]["result_reference_id"] == dmap1["social"]["result_reference_id"]

        # Reintento con nueva Idempotency-Key sobre la misma versión v1 -> no duplica evaluaciones
        ev_dup = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu11-dup2-{uniq}"},
            json={"project_version_id": str(v1_id)},
        )
        assert ev_dup.status_code == 202
        assert ev_dup.json()["duplicated"] is True
        assert ev_dup.json()["evaluation_id"] == b1["evaluation_id"]
        assert p_conn.execute("SELECT count(*) FROM evaluations WHERE project_version_id = %s", (v1_id,)).fetchone()[0] == 1

        # Nueva versión v2 -> aislada, sin resultados heredados de v1
        v2_resp = client.put(
            f"/api/v1/projects/{pid}",
            headers=headers,
            json={"estimated_budget_pen": 240000, "create_new_version": True, "save_as_draft": False},
        )
        assert v2_resp.status_code == 200
        v2_id = UUID(v2_resp.json()["project_version_id"])
        assert v2_resp.json()["version_number"] == 2
        assert v2_resp.json()["dimension_dispatches"] == []

        ev_v2 = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu11-v2-{uniq}"},
            json={"project_version_id": str(v2_id)},
        )
        assert ev_v2.status_code == 202
        assert ev_v2.json()["duplicated"] is False
        assert all(d["project_version_id"] == str(v2_id) for d in ev_v2.json()["dimension_dispatches"])
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, e_conn, pid)


def test_hu1_1_enforces_hu1_2_completeness_and_hu3_1_high_risk_checkpoint(
    isolated_hu11_env,
) -> None:
    """
    - Borrador incompleto (HU1.2) es bloqueado con 422 antes de distribuir a los 5 especialistas.
    - Riesgo ambiental alto (HU3.1) detiene la distribución en checkpoint 'pending_review' sin bypass.
    """
    client, p_conn, e_conn, _ = isolated_hu11_env
    headers = _login_planner(client)
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        draft = client.post(
            "/api/v1/projects",
            headers=headers,
            json={"title": f"Borrador HU1.1 {uniq}", "save_as_draft": True},
        )
        assert draft.status_code == 201
        d_pid = UUID(draft.json()["project_id"])
        d_vid = UUID(draft.json()["project_version_id"])
        created_ids.append(d_pid)

        blk = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu11-blk-{uniq}"},
            json={"project_version_id": str(d_vid)},
        )
        assert blk.status_code == 422
        assert p_conn.execute("SELECT count(*) FROM evaluation_dimension_dispatches WHERE project_version_id = %s", (d_vid,)).fetchone()[0] == 0

        # Proyecto completo con riesgo ambiental alto preexistente -> pausa HITL en las 5 dimensiones
        risky = client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "title": f"Obra Riesgo Alto HU1.1 {uniq}",
                "description": "Obra junto a río con riesgo alto ambiental.",
                "location": "Faja Río Shullcas, El Tambo",
                "proposed_land_use": "Maestranza",
                "estimated_budget_pen": 300000,
                "beneficiaries_count": 500,
            },
        )
        r_pid = UUID(risky.json()["project_id"])
        r_vid = UUID(risky.json()["project_version_id"])
        created_ids.append(r_pid)
        p_conn.execute(
            """
            INSERT INTO project_environmental_records
            (id, project_id, project_version_id, project_code, has_sufficient_data, environmental_component,
             verifiable_evidence, proximity_to_water_meters, is_in_protected_or_risk_area, source_name, data_origin)
            VALUES (%s, %s, %s, %s, TRUE, 'recurso_hidrico', 'En faja marginal a 12m', 12.0, TRUE, 'ANA', 'public')
            """,
            (uuid4(), r_pid, r_vid, risky.json()["code"]),
        )
        paused = client.post(
            "/api/v1/evaluations",
            headers={**headers, "Idempotency-Key": f"hu11-hitl-{uniq}"},
            json={"project_version_id": str(r_vid)},
        )
        assert paused.status_code == 202
        pb = paused.json()
        assert pb["status"] == "pending_review"
        assert pb["checkpoint"] is not None
        assert len(pb["dimension_dispatches"]) == 5
        assert all(d["status"] == "pending_review" for d in pb["dimension_dispatches"])
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, e_conn, pid)
