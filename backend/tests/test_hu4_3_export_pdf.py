from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi.testclient import TestClient
from pypdf import PdfReader

from backend.application.services.prioritization_pdf_service import (
    PROTOTYPE_DISCLAIMER,
    build_prioritization_pdf,
)
from backend.infrastructure.config.dependencies import get_current_user, get_platform_service
from backend.main import create_app


def _sample_ranking_multipage():
    ranked = []
    evidences = {}
    human_reviews = {}

    for idx in range(1, 7):
        pid = uuid4()
        vid = uuid4()
        score = round(92.0 - idx * 2.5, 2)
        ranked.append({
            "rank_position": idx,
            "project_id": pid,
            "project_version_id": vid,
            "code": f"DEMO-PDF-0{idx}",
            "version_number": 1 if idx != 2 else 2,
            "title": f"[DEMO] Proyecto Priorización Municipal #{idx} — El Tambo",
            "status": "completed",
            "location": "El Tambo, Huancayo",
            "estimated_budget_pen": Decimal("500000.00") + Decimal(idx * 50000),
            "beneficiaries_count": 1000 * idx,
            "global_score": score,
            "formula": f"([ECONOMIC: {score:.1f} * 100.0%]) / 100.0% = {score:.2f} pts",
            "explanation": "Puntuación global consolidada calculada a partir de la versión activa de criterios v1.",
            "criteria_version_number": 1,
            "weights": {"ECONOMIC": 100.0},
            "input_scores": {"ECONOMIC": score},
            "tie_break_reason": None,
            "has_coordinates": idx <= 3,
            "latitude": -12.05714 if idx <= 3 else None,
            "longitude": -75.21766 if idx <= 3 else None,
            "coordinates_origin": "public" if idx <= 3 else None,
            "coordinates_source": "INEI / Municipalidad El Tambo" if idx <= 3 else None,
            "allows_final_approval": True,
        })
        if idx % 2 == 1:
            evidences[str(vid)] = [{
                "document": "PDU Huancayo 2015-2025",
                "version": "v2024",
                "locator": "Art. 42.3",
                "excerpt": "Uso compatible para equipamiento público distrital sujeto a retiro municipal.",
            }]
        human_reviews[str(vid)] = {
            "summary": (
                "Sin dictamen humano formal emitido (recomendación no vinculante)"
                if idx != 1
                else "Evaluación jurídica completed (1 cita verificable) · Sin dictamen humano formal emitido"
            )
        }

    excl_vid = uuid4()
    human_reviews[str(excl_vid)] = {
        "summary": "Punto de control HITL pendiente de revisión (Faja marginal río Shullcas) · Sin dictamen humano formal emitido"
    }
    excluded = [{
        "project_id": uuid4(),
        "project_version_id": excl_vid,
        "code": "DEMO-EXCL-01",
        "version_number": 1,
        "title": "[DEMO] Canal en Faja Marginal",
        "status": "blocked",
        "exclusion_code": "blocked",
        "exclusion_reason": "Punto de control de riesgo alto pendiente de revisión humana.",
        "missing_dimensions": [],
        "blocking_reasons": ["Checkpoint HITL activo"],
        "has_coordinates": False,
        "latitude": None,
        "longitude": None,
    }]

    ranking = {
        "is_recommendation": True,
        "recommendation_notice": "Recomendación técnica preliminar.",
        "criteria_version_id": uuid4(),
        "criteria_version_number": 1,
        "total_projects": 7,
        "comparable_count": 6,
        "mapped_count": 3,
        "unmapped_count": 3,
        "excluded_count": 1,
        "ranked_projects": ranked,
        "mapped_projects": ranked[:3],
        "unmapped_projects": ranked[3:],
        "excluded_projects": excluded,
        "generated_at": datetime(2026, 11, 19, 15, 30, 0, tzinfo=timezone.utc),
    }
    return ranking, evidences, human_reviews


def test_hu4_3_pdf_generation_multipage_accents_evidence_and_disclaimer():
    ranking, evidences, human_reviews = _sample_ranking_multipage()
    pdf_bytes = build_prioritization_pdf(ranking, evidences, human_reviews)

    assert pdf_bytes.startswith(b"%PDF-")
    reader = PdfReader(BytesIO(pdf_bytes))
    assert len(reader.pages) >= 2

    page1_text = reader.pages[0].extract_text() or ""
    all_text = "\n".join((p.extract_text() or "") for p in reader.pages)

    # Aviso obligatorio en primera pagina y pie de pagina
    assert "Reporte preliminar del prototipo" in page1_text
    assert "No constituye resoluci" in page1_text
    assert "Página 1" in page1_text
    assert "Página 2" in (reader.pages[1].extract_text() or "")

    # Orden, puntajes, criterios, acentos y evidencia vs sin evidencia
    assert "DEMO-PDF-01" in all_text
    assert "89.50 / 100" in all_text
    assert "PDU Huancayo 2015-2025" in all_text
    assert "Art. 42.3" in all_text
    assert "Sin evidencia normativa verificable registrada" in all_text
    assert "DEMO-EXCL-01" in all_text
    assert "Punto de control HITL pendiente" in all_text
    assert PROTOTYPE_DISCLAIMER[:40] in all_text
    assert "<b>" not in all_text
    assert "</b>" not in all_text
    assert "<br/>" not in all_text


def test_hu4_3_pdf_endpoint_auth_and_headers():
    app = create_app()
    ranking, evidences, human_reviews = _sample_ranking_multipage()
    sample_pdf = build_prioritization_pdf(ranking, evidences, human_reviews)

    fake_svc = MagicMock()
    fake_svc.export_prioritization_ranking_pdf.return_value = (
        sample_pdf,
        "siprim-ranking-priorizacion-20261119-153000.pdf",
    )
    app.dependency_overrides[get_platform_service] = lambda: fake_svc

    # 1. Sin sesion -> 401
    with TestClient(app) as client:
        assert client.get("/api/v1/projects/ranking/pdf").status_code == 401

    # 2. Roles sin facultad -> 403
    for role in ("CITIZEN", "LEGAL_ADVISOR"):
        app.dependency_overrides[get_current_user] = lambda r=role: {"sub": str(uuid4()), "roles": [r]}
        with TestClient(app) as client:
            assert client.get("/api/v1/projects/ranking/pdf").status_code == 403

    # 3. MAYOR, ADMIN y PLANNER -> 200 PDF valido con cabeceras de seguridad
    for role in ("MAYOR", "ADMIN", "PLANNER"):
        app.dependency_overrides[get_current_user] = lambda r=role: {"sub": str(uuid4()), "roles": [r]}
        with TestClient(app) as client:
            res = client.get("/api/v1/projects/ranking/pdf")
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("application/pdf")
            assert "siprim-ranking-priorizacion-" in res.headers["content-disposition"]
            assert res.headers["cache-control"] == "no-store"
            assert res.headers["x-content-type-options"] == "nosniff"
            assert res.content.startswith(b"%PDF-")


def _run_service_pdf_with_mocked_db(
    norm_rows: list[dict],
    zoning_rows: list[dict],
    finding_rows: list[dict],
) -> str:
    from unittest.mock import patch
    from backend.application.services.pmv1_services import PlatformService

    vid = uuid4()
    pid = uuid4()
    ranking = {
        "criteria_version_id": uuid4(),
        "criteria_version_number": 1,
        "total_projects": 1,
        "comparable_count": 1,
        "mapped_count": 0,
        "unmapped_count": 1,
        "excluded_count": 0,
        "ranked_projects": [
            {
                "project_id": pid,
                "project_version_id": vid,
                "code": "PRY-TEST-REF",
                "title": "Proyecto de prueba de procedencia normativa",
                "version_number": 1,
                "estimated_budget_pen": 100000.0,
                "beneficiaries_count": 500,
                "global_score": 88.0,
                "rank_position": 1,
                "criteria_version_id": uuid4(),
                "criteria_version_number": 1,
                "weights": {"ECONOMIC": 100.0},
                "input_scores": {"ECONOMIC": 88.0},
                "formula": "([ECONOMIC: 88.0 * 100.0%]) / 100.0% = 88.00 pts",
                "explanation": "Calculo determinista.",
                "tie_break_reason": None,
                "has_coordinates": False,
                "latitude": None,
                "longitude": None,
                "coordinates_origin": None,
                "coordinates_source": None,
            }
        ],
        "mapped_projects": [],
        "unmapped_projects": [],
        "excluded_projects": [],
        "generated_at": datetime(2026, 11, 19, 16, 0, 0, tzinfo=timezone.utc),
    }

    # Normaliza los UUID de las filas simuladas para que coincidan con pid/vid
    for row in zoning_rows:
        row.setdefault("project_version_id", vid)
    for row in finding_rows:
        row.setdefault("project_id", pid)
        row.setdefault("project_version_id", vid)

    query_results = [
        norm_rows,
        zoning_rows,
        finding_rows,
        [],  # evaluation_checkpoints
        [],  # human_approvals
        [],  # legal_assessments
    ]

    class _FakeCursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _FakeConn:
        def __init__(self):
            self._idx = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, *_args, **_kwargs):
            rows = query_results[self._idx]
            self._idx += 1
            return _FakeCursor(rows)

    svc = PlatformService(db_url="postgresql://fake/db", dispatcher=None, enable_demo_fallback=False)  # type: ignore
    svc.get_project_map_ranking = lambda: ranking  # type: ignore[method-assign]

    with patch("backend.application.services.pmv1_services.psycopg.connect", return_value=_FakeConn()):
        pdf_bytes, _ = svc.export_prioritization_ranking_pdf()

    reader = PdfReader(BytesIO(pdf_bytes))
    raw_text = "\n".join((p.extract_text() or "") for p in reader.pages)
    return " ".join(raw_text.split())


def test_hu4_3_rejects_simulated_source_as_verifiable_evidence():
    text = _run_service_pdf_with_mocked_db(
        norm_rows=[
            {
                "id": "SIM-01",
                "document_name": "Norma Simulada Municipal",
                "short_code": "SIM_01",
                "version": "2025-sim",
                "data_origin": "simulated",
                "in_force": True,
            }
        ],
        zoning_rows=[],
        finding_rows=[
            {
                "normative_chunk_id": "SIM-01",
                "custom_article_or_section": "Art. 10",
                "title": "Hallazgo simulado",
                "notes": "Nota interna",
                "norm_id": "SIM-01",
                "norm_title": "Norma Simulada Municipal",
                "norm_version": "2025-sim",
                "norm_content": "Texto simulado para pruebas.",
                "norm_origin": "simulated",
                "norm_in_force": True,
            }
        ],
    )
    assert "Sin evidencia normativa verificable registrada para esta versión" in text
    assert "Fuente simulada/demo — pendiente de cotejo" in text
    assert "Norma Simulada Municipal" in text


def test_hu4_3_rejects_unlinked_reference_without_source_document():
    text = _run_service_pdf_with_mocked_db(
        norm_rows=[],
        zoning_rows=[
            {
                "source_document": "PDU Huancayo 2015-2025",
                "source_version": "v2024",
                "source_locator": "Art. 42.3",
                "source_excerpt": "Uso compatible para equipamiento urbano distrital sujeto a retiro municipal.",
                "limitations": "Verificacion preliminar",
            }
        ],
        finding_rows=[
            {
                "normative_chunk_id": None,
                "custom_article_or_section": "Art. 99",
                "title": "Referencia manual sin documento",
                "notes": "Anotacion del evaluador sin norma vinculada",
                "norm_id": None,
                "norm_title": None,
                "norm_version": None,
                "norm_content": None,
                "norm_origin": None,
                "norm_in_force": None,
            }
        ],
    )
    assert "Sin evidencia normativa verificable registrada para esta versión" in text
    assert "Sin documento fuente vinculado — pendiente de cotejo" in text
    assert "PDU Huancayo 2015-2025" in text


def test_hu4_3_accepts_verifiable_public_or_official_reference():
    text = _run_service_pdf_with_mocked_db(
        norm_rows=[
            {
                "id": "PDUPDM-01",
                "document_name": "PDM Huancayo 2017-2037 (O.M. N.° 636-2020-MPH/CM)",
                "short_code": "PDU_EL_TAMBO",
                "version": "2020-OM636",
                "data_origin": "public",
                "in_force": True,
            }
        ],
        zoning_rows=[],
        finding_rows=[
            {
                "normative_chunk_id": "PDUPDM-01",
                "custom_article_or_section": "Título III (Zonificación Urbana)",
                "title": "Compatibilidad de equipamiento urbano",
                "notes": "Cotejado contra ordenanza vigente",
                "norm_id": "PDUPDM-01",
                "norm_title": "PDM Huancayo 2017-2037 (O.M. N.° 636-2020-MPH/CM)",
                "norm_version": "2020-OM636",
                "norm_content": "PDM Huancayo 2017-2037, Título III: Zonificación Urbana y Parámetros Urbanísticos.",
                "norm_origin": "public",
                "norm_in_force": True,
            }
        ],
    )
    assert "PDM Huancayo 2017-2037 (O.M. N." in text
    assert "2020-OM636" in text
    assert "Sin evidencia normativa verificable registrada para esta versión" not in text
    assert "Referencias registradas pendientes de cotejo" not in text
