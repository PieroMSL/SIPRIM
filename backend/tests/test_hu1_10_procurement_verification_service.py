from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import (
    PlatformService,
    ProcurementVerificationEngine,
)
from backend.infrastructure.config.dependencies import get_platform_service
from backend.infrastructure.config.settings import get_settings
from backend.main import create_app


def _cleanup_project(conn: psycopg.Connection, project_id: UUID) -> None:
    pv_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM project_versions WHERE project_id = %s",
            (project_id,),
        ).fetchall()
    ]
    conn.execute("DELETE FROM audit_events WHERE entity_id = %s", (project_id,))
    conn.execute("DELETE FROM procurement_verification_requests WHERE project_id = %s", (project_id,))
    conn.execute("DELETE FROM project_procurement_records WHERE project_id = %s", (project_id,))
    conn.execute("DELETE FROM legal_alert_resolutions WHERE project_id = %s", (project_id,))
    conn.execute("DELETE FROM legal_assessments WHERE project_id = %s", (project_id,))
    conn.execute("DELETE FROM project_legal_findings WHERE project_id = %s", (project_id,))
    if pv_ids:
        conn.execute("DELETE FROM zoning_review_requests WHERE project_version_id = ANY(%s)", (pv_ids,))
        conn.execute("DELETE FROM project_versions WHERE id = ANY(%s)", (pv_ids,))
    conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


@pytest.fixture
def hu110_client():
    settings = get_settings()
    app = create_app()
    platform_svc = PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=None,  # type: ignore[arg-type]
        enable_demo_fallback=False,
        auth_db_url=settings.auth_db_url,
    )
    app.dependency_overrides[get_platform_service] = lambda: platform_svc
    p_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with TestClient(app) as client, psycopg.connect(p_url, autocommit=True) as p_conn:
        yield client, p_conn, platform_svc


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_hu1_10_ca01_supported_preliminary_conclusion_with_current_oece_norms(
    hu110_client,
) -> None:
    """
    CA-HU1.10-01: Con antecedentes contractuales completos y normativa Ley N.° 32069 / D.S. 009-2025-EF
    vigente identificada del compendio OECE, muestra requisitos examinados y conclusión preliminar
    con norma, versión, fragmento localizable, fecha de consulta y enlace oficial verificable.
    """
    client, p_conn, _ = hu110_client
    planner_headers = _login(client, "planner@siprim.test", "Prueba2026!")
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        proj_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": f"Losa Deportiva HU1.10 Conforme {uniq}",
                "description": "Obra vecinal para equipamiento recreativo municipal.",
                "location": "Jr. Real cdra. 14, El Tambo, Huancayo",
                "proposed_land_use": "Recreación pública",
                "estimated_budget_pen": 320000,
                "beneficiaries_count": 800,
            },
        )
        assert proj_resp.status_code == 201, proj_resp.text
        proj = proj_resp.json()
        pid = UUID(proj["project_id"])
        pvid = proj["project_version_id"]
        created_ids.append(pid)

        ant_resp = client.put(
            f"/api/v1/projects/{pid}/procurement-antecedents",
            headers=planner_headers,
            json={
                "contracting_regime": "ley_32069",
                "contract_object": "obra",
                "contracting_phase": "actuaciones_preparatorias",
                "selection_procedure": "licitacion_publica",
                "requirement_defined": True,
                "in_cmn_and_viability": True,
                "budget_certification_declared": True,
                "pladicop_registration_planned": True,
                "no_fractionation_declared": True,
                "supporting_document_ref": "Exp. Técnico LP-004-2026 / Memo DEC-118",
                "contractual_summary": "Licitación pública de obra con requerimiento y certificación presupuestal en CMN.",
            },
        )
        assert ant_resp.status_code == 200, ant_resp.text
        ant_data = ant_resp.json()
        assert ant_data["has_record"] is True
        active_ids = {s["fragment_id"] for s in ant_data["active_normative_sources"]}
        hist_ids = {s["fragment_id"] for s in ant_data["historical_normative_sources"]}
        assert {"LEY32069-01", "LEY32069-03", "LEY32069-06", "DS009-2025-REG-54"}.issubset(active_ids)
        assert {"LEY26850-HIST", "LEY30225-HIST", "LEY32069-2024V1-HIST"}.issubset(hist_ids)

        idem = f"hu110-ca01-{uniq}"
        ver_resp = client.post(
            f"/api/v1/projects/{pid}/procurement-verification",
            headers={**planner_headers, "Idempotency-Key": idem},
        )
        assert ver_resp.status_code == 200, ver_resp.text
        data = ver_resp.json()
        assert data["project_version_id"] == pvid
        assert data["status"] == "verified_preliminary"
        assert data["preliminary_compliant"] is True
        assert data["certified_infraction"] is False
        assert data["requires_human_review"] is True  # Alcance estrictamente preliminar
        assert data["missing_fields"] == []
        assert len(data["examined_requirements"]) == 4
        for req in data["examined_requirements"]:
            assert req["status"] == "compliant_preliminary"
            assert req["norm_name"] is not None
            assert req["norm_version"] in ("2026-07-19-OECE", "2026-10-26-OECE")
            assert req["article_or_section"] is not None
            assert req["fragment_excerpt"] is not None
            assert "gob.pe" in (req["official_url"] or "")
            assert req["consulted_at"] == "2026-11-20"

        excluded_ids = {s["fragment_id"] for s in data["superseded_sources_excluded"]}
        assert {"LEY26850-HIST", "LEY30225-HIST", "LEY32069-2024V1-HIST"}.issubset(excluded_ids)

        # Idempotencia al repetir con la misma Idempotency-Key
        dup_resp = client.post(
            f"/api/v1/projects/{pid}/procurement-verification",
            headers={**planner_headers, "Idempotency-Key": idem},
        )
        assert dup_resp.status_code == 200
        assert dup_resp.json()["duplicated"] is True
        assert dup_resp.json()["verification_id"] == data["verification_id"]
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, pid)


def test_hu1_10_ca02_possible_incompatibility_routed_to_legal_advisor_without_certifying_infraction(
    hu110_client,
) -> None:
    """
    CA-HU1.10-02: Ante posible incompatibilidad (p. ej. declarar 'contrato menor' sobre
    presupuesto de S/ 120,000 que excede 8 UIT = S/ 44,000 en 2026, u objeto/procedimiento
    incompatible), señala el requisito afectado, explica la observación y la remite a LEGAL_ADVISOR
    para validación humana sin certificar por sí sola una infracción definitiva.
    """
    client, p_conn, svc = hu110_client
    planner_headers = _login(client, "planner@siprim.test", "Prueba2026!")
    legal_headers = _login(client, "legal@siprim.test", "Prueba2026!")
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        proj_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": f"Pavimentación HU1.10 Incompatibilidad {uniq}",
                "description": "Mejoramiento vial vecinal.",
                "location": "Av. Ferrocarril cdra. 6, El Tambo",
                "proposed_land_use": "Infraestructura vial",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 600,
            },
        )
        assert proj_resp.status_code == 201
        pid = UUID(proj_resp.json()["project_id"])
        created_ids.append(pid)

        assert client.put(
            f"/api/v1/projects/{pid}/procurement-antecedents",
            headers=planner_headers,
            json={
                "contracting_regime": "ley_32069",
                "contract_object": "obra",
                "contracting_phase": "actuaciones_preparatorias",
                "selection_procedure": "contrato_menor",  # Incompatibilidad: S/ 120,000 > 8 UIT (S/ 44,000)
                "requirement_defined": True,
                "in_cmn_and_viability": True,
                "budget_certification_declared": True,
                "pladicop_registration_planned": True,
                "no_fractionation_declared": True,
                "supporting_document_ref": "Inf-AreaUsuaria-088-2026",
            },
        ).status_code == 200

        ver_resp = client.post(
            f"/api/v1/projects/{pid}/procurement-verification",
            headers={**planner_headers, "Idempotency-Key": f"hu110-ca02-{uniq}"},
        )
        assert ver_resp.status_code == 200
        data = ver_resp.json()
        assert data["status"] == "possible_incompatibility"
        assert data["preliminary_compliant"] is False
        assert data["certified_infraction"] is False  # Jamás certifica infracción definitiva
        assert data["requires_human_review"] is True
        assert data["sent_to_legal_advisor"] is True
        assert data["advisory_review_status"] == "pending"
        assert "34" in (data["affected_requirement"] or "") or "UIT" in (data["observation_explanation"] or "")
        assert "44,000" in (data["observation_explanation"] or "")

        # Llega a la vista del LEGAL_ADVISOR (GET /procurement-verification y GET /legal-assessment)
        adv_proc = client.get(f"/api/v1/projects/{pid}/procurement-verification", headers=legal_headers)
        assert adv_proc.status_code == 200
        assert adv_proc.json()["status"] == "possible_incompatibility"
        assert adv_proc.json()["sent_to_legal_advisor"] is True

        adv_legal = client.get(f"/api/v1/projects/{pid}/legal-assessment", headers=legal_headers)
        assert adv_legal.status_code == 200
        assert adv_legal.json()["procurement_verification"]["verification_id"] == data["verification_id"]

        # Un planner sin rol LEGAL_ADVISOR no puede registrar la validación del asesor
        forbidden_review = client.post(
            f"/api/v1/projects/{pid}/procurement-verification/review",
            headers=planner_headers,
            json={
                "advisory_review_status": "subsanacion_requerida",
                "advisory_review_notes": "Intento sin permiso legal.",
            },
        )
        assert forbidden_review.status_code == 403

        # El resumen unificado bloquea aprobación final mientras esté pendiente la observación HU1.10
        summary = svc.get_unified_assessment_summary(pid)
        assert summary["allows_final_approval"] is False
        assert any("HU1.10" in reason for reason in summary["blocking_reasons"])

        # LEGAL_ADVISOR registra su validación humana formal
        review_resp = client.post(
            f"/api/v1/projects/{pid}/procurement-verification/review",
            headers=legal_headers,
            json={
                "advisory_review_status": "subsanacion_requerida",
                "advisory_review_notes": "Debe adecuarse a procedimiento competitivo conforme Arts. 34.1 y 54.1 Ley N.° 32069.",
            },
        )
        assert review_resp.status_code == 200
        assert review_resp.json()["advisory_review_status"] == "subsanacion_requerida"
        assert "34.1" in review_resp.json()["advisory_review_notes"]
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, pid)


def test_hu1_10_ca03_missing_antecedents_abstains_and_never_infers_from_title_or_description(
    hu110_client,
) -> None:
    """
    CA-HU1.10-03: Aunque título y descripción afirmen cumplimiento de Licitación Pública y Ley 32069,
    sin antecedentes contractuales estructurados el sistema NO infiere cumplimiento, señala qué falta
    y deja el caso en 'Requiere revisión' con preliminary_compliant=None.
    """
    client, p_conn, _ = hu110_client
    planner_headers = _login(client, "planner@siprim.test", "Prueba2026!")
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        bait_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": f"Licitación Pública Ley N.° 32069 Art. 54 cumple PLADICOP Art. 41 {uniq}",
                "description": (
                    "El proyecto cumple íntegramente Ley 32069 Art. 46 requerimiento definido, "
                    "CMN aprobado, certificación presupuestal y sin fraccionamiento Art. 50."
                ),
                "location": "Sector Cajas Chico, El Tambo",
                "proposed_land_use": "Equipamiento urbano",
                "estimated_budget_pen": 250000,
                "beneficiaries_count": 500,
            },
        )
        assert bait_resp.status_code == 201
        pid = UUID(bait_resp.json()["project_id"])
        created_ids.append(pid)

        ver_resp = client.post(
            f"/api/v1/projects/{pid}/procurement-verification",
            headers={**planner_headers, "Idempotency-Key": f"hu110-ca03-bare-{uniq}"},
        )
        assert ver_resp.status_code == 200
        data = ver_resp.json()
        assert data["status"] == "requires_review"
        assert data["status_label"] == "Requiere revisión"
        assert data["preliminary_compliant"] is None
        assert data["certified_infraction"] is False
        assert data["requires_human_review"] is True
        assert len(data["missing_fields"]) >= 5
        assert any("Régimen de contratación" in m for m in data["missing_fields"])
        assert any("Procedimiento" in m for m in data["missing_fields"])
        assert "título o descripción" in data["summary"].lower()

        # También si tiene datos simulados o parciales, debe abstenerse sin inventar conformidad
        assert client.put(
            f"/api/v1/projects/{pid}/procurement-antecedents",
            headers=planner_headers,
            json={
                "contracting_regime": "ley_32069",
                "contract_object": "obra",
                "contracting_phase": "actuaciones_preparatorias",
                "selection_procedure": "licitacion_publica",
                "requirement_defined": True,
                "in_cmn_and_viability": True,
                "budget_certification_declared": True,
                "pladicop_registration_planned": True,
                "no_fractionation_declared": True,
                "supporting_document_ref": "Simulado demo",
                "data_origin": "simulated",
            },
        ).status_code == 200
        sim_ver = client.post(
            f"/api/v1/projects/{pid}/procurement-verification",
            headers={**planner_headers, "Idempotency-Key": f"hu110-ca03-sim-{uniq}"},
        ).json()
        assert sim_ver["status"] == "requires_review"
        assert sim_ver["preliminary_compliant"] is None
        assert any("simulados" in m.lower() for m in sim_ver["missing_fields"])
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, pid)


def test_hu1_10_version_isolation_and_exclusion_of_superseded_sources(
    hu110_client,
) -> None:
    """
    Aislamiento por versión (v1 vs v2) y exclusión estricta de fuentes sustituidas / 2024-v1 pre-vigencia.
    """
    client, p_conn, _ = hu110_client
    planner_headers = _login(client, "planner@siprim.test", "Prueba2026!")
    created_ids: list[UUID] = []
    uniq = uuid4().hex[:8]
    try:
        p_resp = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": f"Centro Cultural HU1.10 Versiones {uniq}",
                "description": "Aislamiento de antecedentes contractuales v1 vs v2.",
                "location": "El Tambo, Huancayo",
                "proposed_land_use": "Equipamiento cultural",
                "estimated_budget_pen": 180000,
                "beneficiaries_count": 700,
            },
        )
        assert p_resp.status_code == 201
        pid = UUID(p_resp.json()["project_id"])
        v1_id = p_resp.json()["project_version_id"]
        created_ids.append(pid)

        # v1 con posible incompatibilidad (objeto obra + concurso público)
        client.put(
            f"/api/v1/projects/{pid}/procurement-antecedents",
            headers=planner_headers,
            json={
                "contracting_regime": "ley_32069",
                "contract_object": "obra",
                "contracting_phase": "seleccion",
                "selection_procedure": "concurso_publico",
                "requirement_defined": True,
                "in_cmn_and_viability": True,
                "budget_certification_declared": True,
                "pladicop_registration_planned": True,
                "no_fractionation_declared": True,
                "supporting_document_ref": "Exp-v1-obs",
            },
        )
        v1_res = client.post(
            f"/api/v1/projects/{pid}/procurement-verification",
            headers={**planner_headers, "Idempotency-Key": f"hu110-v1-{uniq}"},
        ).json()
        assert v1_res["status"] == "possible_incompatibility"

        # Crear v2 modificando proyecto -> v2 no hereda antecedentes ni resultado de v1
        upd = client.put(
            f"/api/v1/projects/{pid}",
            headers=planner_headers,
            json={
                "title": f"Centro Cultural HU1.10 v2 Mitigado {uniq}",
                "description": "Versión 2 con procedimiento competitivo corregido.",
                "location": "El Tambo, Huancayo",
                "proposed_land_use": "Equipamiento cultural",
                "estimated_budget_pen": 180000,
                "beneficiaries_count": 700,
                "save_as_draft": False,
                "create_new_version": True,
            },
        )
        assert upd.status_code == 200
        v2_id = upd.json()["project_version_id"]
        assert v2_id != v1_id

        v2_ant_empty = client.get(f"/api/v1/projects/{pid}/procurement-antecedents", headers=planner_headers).json()
        assert v2_ant_empty["project_version_id"] == v2_id
        assert v2_ant_empty["has_record"] is False

        v2_ver_empty = client.get(f"/api/v1/projects/{pid}/procurement-verification", headers=planner_headers).json()
        assert v2_ver_empty is None

        # Registrar v2 corregido -> v2 compatible preliminar; v1 conserva su registro histórico
        client.put(
            f"/api/v1/projects/{pid}/procurement-antecedents",
            headers=planner_headers,
            json={
                "contracting_regime": "ley_32069",
                "contract_object": "obra",
                "contracting_phase": "actuaciones_preparatorias",
                "selection_procedure": "licitacion_publica",
                "requirement_defined": True,
                "in_cmn_and_viability": True,
                "budget_certification_declared": True,
                "pladicop_registration_planned": True,
                "no_fractionation_declared": True,
                "supporting_document_ref": "Exp-v2-subsanado",
            },
        )
        v2_res = client.post(
            f"/api/v1/projects/{pid}/procurement-verification",
            headers={**planner_headers, "Idempotency-Key": f"hu110-v2-{uniq}"},
        ).json()
        assert v2_res["status"] == "verified_preliminary"
        assert v2_res["preliminary_compliant"] is True

        v1_check = client.get(
            f"/api/v1/projects/{pid}/procurement-verification?version_id={v1_id}",
            headers=planner_headers,
        ).json()
        assert v1_check["status"] == "possible_incompatibility"

        # Exclusión estricta de fuentes históricas y de semilla desactualizada 2024-v1
        stale_chunks = [
            {
                "id": "LEY32069-03",
                "document_name": "Ley N.° 32069",
                "short_code": "LEY_32069",
                "version": "2024-v1",  # Texto pre-vigencia desactualizado
                "content": "Art. 54.- ...",
                "in_force": True,
                "data_origin": "public",
            },
            {
                "id": "LEY30225-HIST",
                "document_name": "Ley N.° 30225 (Sustituida)",
                "short_code": "LEY_30225",
                "version": "2019-TUO-derogada",
                "content": "Texto derogado.",
                "in_force": False,
                "data_origin": "public",
            },
        ]
        stale_eval = ProcurementVerificationEngine.evaluate(
            project={"estimated_budget_pen": 180000},
            antecedent={
                "contracting_regime": "ley_32069",
                "contract_object": "obra",
                "contracting_phase": "actuaciones_preparatorias",
                "selection_procedure": "licitacion_publica",
                "requirement_defined": True,
                "in_cmn_and_viability": True,
                "budget_certification_declared": True,
                "pladicop_registration_planned": True,
                "no_fractionation_declared": True,
                "supporting_document_ref": "Exp-Test",
                "data_origin": "declared",
            },
            all_chunks=stale_chunks,
        )
        assert stale_eval["status"] == "requires_review"
        assert stale_eval["preliminary_compliant"] is None
        assert len(stale_eval["superseded_sources_excluded"]) == 2
        assert any("normativo vigente" in m.lower() for m in stale_eval["missing_fields"])
    finally:
        for pid in created_ids:
            _cleanup_project(p_conn, pid)
