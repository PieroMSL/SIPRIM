from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.application.dto.pmv1_dto import PMV1ProjectCreate
from backend.application.services.pmv1_services import PlatformService
from backend.infrastructure.config.dependencies import get_current_user, get_platform_service
from backend.main import create_app


CRIT_ACTIVE_ID = uuid4()
CRIT_OLD_ID = uuid4()


def _make_summary(
    project_id,
    version_id,
    code,
    version_number,
    *,
    score: float | None,
    status: str = "completed",
    is_complete: bool = True,
    missing: list[str] | None = None,
    blocking: list[str] | None = None,
):
    return {
        "project_id": project_id,
        "project_code": code,
        "project_version_id": version_id,
        "version_number": version_number,
        "is_complete": is_complete,
        "status": status,
        "status_label": status,
        "summary_text": f"Resumen {code}",
        "dimensions": [],
        "missing_dimensions": missing or [],
        "global_score": {
            "score": score,
            "is_calculated": score is not None and is_complete,
            "formula": f"SCORE({code}) = {score}" if score is not None else "No calculable",
            "criteria_version_id": CRIT_ACTIVE_ID,
            "criteria_version_number": 1,
            "weights": {"ECONOMIC": 40.0, "SOCIAL": 30.0, "ENVIRONMENTAL": 15.0, "TECHNICAL": 15.0},
            "input_scores": {"ECONOMIC": score or 0.0},
            "explanation": f"Calculado con criterios activos v1 para {code}.",
        },
        "allows_final_approval": is_complete and status != "blocked",
        "blocking_reasons": blocking or [],
        "generated_at": datetime.now(timezone.utc),
    }


def _mock_conn(project_rows):
    conn = MagicMock()
    cur_crit = MagicMock()
    cur_crit.fetchone.return_value = {"id": CRIT_ACTIVE_ID, "version_number": 1}
    cur_projs = MagicMock()
    cur_projs.fetchall.return_value = project_rows
    conn.execute.side_effect = [cur_crit, cur_projs]
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx


def test_hu4_1_ranking_order_tiebreak_exclusions_and_coordinates():
    p1, v1 = uuid4(), uuid4()
    p2, v2 = uuid4(), uuid4()
    p3, v3 = uuid4(), uuid4()
    p4, v4 = uuid4(), uuid4()
    p5, v5 = uuid4(), uuid4()
    p6, v6 = uuid4(), uuid4()

    rows = [
        # P1: empate 80.0, 2000 beneficiarios, con coordenadas
        {
            "project_id": p1, "code": "EXP-001", "db_status": "evaluated",
            "project_version_id": v1, "version_number": 2,
            "title": "Mejoramiento Parque Tambo", "location": "El Tambo",
            "estimated_budget_pen": Decimal("500000.00"), "beneficiaries_count": 2000,
            "latitude": Decimal("-12.054400"), "longitude": Decimal("-75.219800"),
            "coordinates_origin": "official_cadastre",
            "coordinates_source": "Municipalidad de El Tambo",
            "eval_criteria_version_id": CRIT_ACTIVE_ID,
        },
        # P2: empate 80.0, 4500 beneficiarios (debe ir #1 por desempate), SIN coordenadas
        {
            "project_id": p2, "code": "EXP-002", "db_status": "evaluated",
            "project_version_id": v2, "version_number": 1,
            "title": "Agua Potable Anexo Paccha", "location": "Paccha",
            "estimated_budget_pen": Decimal("800000.00"), "beneficiaries_count": 4500,
            "latitude": None, "longitude": None,
            "coordinates_origin": None, "coordinates_source": None,
            "eval_criteria_version_id": CRIT_ACTIVE_ID,
        },
        # P3: mayor puntaje 91.5 -> debe ir #1 absoluto, con coordenadas
        {
            "project_id": p3, "code": "EXP-003", "db_status": "evaluated",
            "project_version_id": v3, "version_number": 1,
            "title": "Puesto de Salud Saños", "location": "Saños Chico",
            "estimated_budget_pen": Decimal("620000.00"), "beneficiaries_count": 3000,
            "latitude": Decimal("-12.038100"), "longitude": Decimal("-75.207200"),
            "coordinates_origin": "public",
            "coordinates_source": "MINSA RENAES",
            "eval_criteria_version_id": CRIT_ACTIVE_ID,
        },
        # P4: incompleto -> excluido
        {
            "project_id": p4, "code": "EXP-004", "db_status": "evaluating",
            "project_version_id": v4, "version_number": 1,
            "title": "Puente Incompleto", "location": "La Esperanza",
            "estimated_budget_pen": Decimal("300000.00"), "beneficiaries_count": 900,
            "latitude": None, "longitude": None,
            "coordinates_origin": None, "coordinates_source": None,
            "eval_criteria_version_id": CRIT_ACTIVE_ID,
        },
        # P5: bloqueado por HITL/alerta -> excluido
        {
            "project_id": p5, "code": "EXP-005", "db_status": "pending_review",
            "project_version_id": v5, "version_number": 1,
            "title": "Canal en Faja Marginal", "location": "Río Shullcas",
            "estimated_budget_pen": Decimal("400000.00"), "beneficiaries_count": 1200,
            "latitude": Decimal("-12.060000"), "longitude": Decimal("-75.210000"),
            "coordinates_origin": "declared", "coordinates_source": "Ficha",
            "eval_criteria_version_id": CRIT_ACTIVE_ID,
        },
        # P6: evaluado con otra version de criterios -> excluido
        {
            "project_id": p6, "code": "EXP-006", "db_status": "evaluated",
            "project_version_id": v6, "version_number": 1,
            "title": "Proyecto Criterios Antiguos", "location": "El Tambo",
            "estimated_budget_pen": Decimal("250000.00"), "beneficiaries_count": 1500,
            "latitude": None, "longitude": None,
            "coordinates_origin": None, "coordinates_source": None,
            "eval_criteria_version_id": CRIT_OLD_ID,
        },
    ]

    summaries = {
        p1: _make_summary(p1, v1, "EXP-001", 2, score=80.0),
        p2: _make_summary(p2, v2, "EXP-002", 1, score=80.0),
        p3: _make_summary(p3, v3, "EXP-003", 1, score=91.5),
        p4: _make_summary(p4, v4, "EXP-004", 1, score=None, status="incomplete", is_complete=False, missing=["Ambiental"]),
        p5: _make_summary(p5, v5, "EXP-005", 1, score=75.0, status="blocked", is_complete=True, blocking=["Alerta legal activa"]),
        p6: _make_summary(p6, v6, "EXP-006", 1, score=85.0),
    }

    svc = PlatformService(db_url="postgresql://unused", dispatcher=None, enable_demo_fallback=False)
    with (
        patch("backend.application.services.pmv1_services.psycopg.connect", return_value=_mock_conn(rows)),
        patch.object(svc, "get_unified_assessment_summary", side_effect=lambda pid, version_id=None: summaries[pid]),
    ):
        result = svc.get_project_map_ranking()

    assert result["is_recommendation"] is True
    assert "no constituye aprobacion municipal" in result["recommendation_notice"].lower()
    assert result["comparable_count"] == 3
    assert result["mapped_count"] == 2
    assert result["unmapped_count"] == 1
    assert result["excluded_count"] == 3

    # Orden determinista: EXP-003 (91.5) -> EXP-002 (80.0, 4500 benef) -> EXP-001 (80.0, 2000 benef)
    ranked = result["ranked_projects"]
    assert [r["code"] for r in ranked] == ["EXP-003", "EXP-002", "EXP-001"]
    assert [r["rank_position"] for r in ranked] == [1, 2, 3]
    assert ranked[0]["tie_break_reason"] is None
    assert ranked[1]["tie_break_reason"] is not None
    assert ranked[2]["tie_break_reason"] is not None

    # Mapa vs lista complementaria sin coordenadas ficticias
    assert [m["code"] for m in result["mapped_projects"]] == ["EXP-003", "EXP-001"]
    assert [u["code"] for u in result["unmapped_projects"]] == ["EXP-002"]
    assert result["unmapped_projects"][0]["latitude"] is None
    assert result["unmapped_projects"][0]["longitude"] is None

    # Excluidos con motivo explicito
    excl_by_code = {e["code"]: e for e in result["excluded_projects"]}
    assert excl_by_code["EXP-004"]["exclusion_code"] == "incomplete"
    assert excl_by_code["EXP-005"]["exclusion_code"] == "blocked"
    assert excl_by_code["EXP-006"]["exclusion_code"] == "criteria_version_mismatch"


def test_hu4_1_coordinates_validation_in_dto():
    # Valido con coordenadas
    valid = PMV1ProjectCreate(
        title="Proyecto Con Coordenadas",
        description="Descripcion",
        location="El Tambo",
        proposed_land_use="Salud",
        estimated_budget_pen=Decimal("100000.00"),
        beneficiaries_count=500,
        latitude=-12.0544,
        longitude=-75.2198,
        coordinates_source="Catastro Municipal",
    )
    assert valid.coordinates_origin == "declared"

    # Invalido: solo latitud sin longitud
    with pytest.raises(ValidationError):
        PMV1ProjectCreate(
            title="Solo Latitud",
            latitude=-12.0544,
        )

    # Invalido: coordenadas declaradas sin fuente identificable
    with pytest.raises(ValidationError):
        PMV1ProjectCreate(
            title="Sin Fuente",
            latitude=-12.0544,
            longitude=-75.2198,
        )

    # Invalido: latitud fuera de rango
    with pytest.raises(ValidationError):
        PMV1ProjectCreate(
            title="Latitud Invalida",
            latitude=-95.0,
            longitude=-75.2,
            coordinates_source="Catastro",
        )


def test_hu4_1_ranking_endpoint_auth_and_mayor_scope():
    app = create_app()
    fake_svc = MagicMock()
    fake_svc.get_project_map_ranking.return_value = {
        "is_recommendation": True,
        "recommendation_notice": "Recomendacion preliminar.",
        "criteria_version_id": CRIT_ACTIVE_ID,
        "criteria_version_number": 1,
        "total_projects": 0,
        "comparable_count": 0,
        "mapped_count": 0,
        "unmapped_count": 0,
        "excluded_count": 0,
        "ranked_projects": [],
        "mapped_projects": [],
        "unmapped_projects": [],
        "excluded_projects": [],
        "generated_at": datetime.now(timezone.utc),
    }
    app.dependency_overrides[get_platform_service] = lambda: fake_svc

    # 1. Sin autenticacion -> 401
    with TestClient(app) as client:
        assert client.get("/api/v1/projects/ranking").status_code == 401

    # 2. Roles no autorizados (CITIZEN, LEGAL_ADVISOR) -> 403
    for role in ("CITIZEN", "LEGAL_ADVISOR"):
        app.dependency_overrides[get_current_user] = lambda r=role: {
            "sub": str(uuid4()),
            "roles": [r],
        }
        with TestClient(app) as client:
            assert client.get("/api/v1/projects/ranking").status_code == 403

    # 3. Rol MAYOR accede a ranking (200), pero NO puede crear proyectos ni aprobar (403)
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(uuid4()),
        "roles": ["MAYOR"],
    }
    with TestClient(app) as client:
        assert client.get("/api/v1/projects/ranking").status_code == 200
        assert client.post("/api/v1/projects", json={"title": "No autorizado"}).status_code == 403
        assert (
            client.post(
                f"/api/v1/projects/{uuid4()}/approval",
                json={"decision": "approved", "justification": "Intento indebido"},
                headers={"Idempotency-Key": "k1"},
            ).status_code
            == 403
        )

    # 4. ADMIN y PLANNER tambien autorizados en ranking -> 200
    for role in ("ADMIN", "PLANNER"):
        app.dependency_overrides[get_current_user] = lambda r=role: {
            "sub": str(uuid4()),
            "roles": [r],
        }
        with TestClient(app) as client:
            assert client.get("/api/v1/projects/ranking").status_code == 200
