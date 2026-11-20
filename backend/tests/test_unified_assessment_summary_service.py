import json
import psycopg
import pytest
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from backend.application.services.pmv1_services import (
    EconomicProcessor,
    PlatformService,
)
from backend.domain.exceptions.pmv1_exceptions import RecursoPMV1NoEncontradoError, ConfiguracionPMV1Error
from backend.infrastructure.config.settings import get_settings

PLANNER_ID = UUID("10000000-0000-4000-a000-000000000002")
ADMIN_ID = UUID("10000000-0000-4000-a000-000000000001")
ACTIVE_CRITERIA_ID = UUID("c0000000-0000-4000-a000-000000000001")


@pytest.fixture
def service():
    settings = get_settings()
    return PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=None,  # type: ignore
        enable_demo_fallback=False,
    )


@pytest.fixture
def db_conn():
    settings = get_settings()
    db_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    conn = psycopg.connect(db_url, autocommit=True)
    yield conn
    conn.close()


def _create_base_project(conn, code: str) -> tuple[UUID, UUID]:
    proj_id = uuid4()
    v1_id = uuid4()
    conn.execute(
        """
        INSERT INTO projects (id, code, created_by_user_id, status)
        VALUES (%s, %s, %s, 'ready')
        """,
        (proj_id, code, PLANNER_ID),
    )
    conn.execute(
        """
        INSERT INTO project_versions
        (id, project_id, version_number, title, description, location_description, proposed_land_use,
         territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id)
        VALUES (%s, %s, 1, 'Proyecto Multidimensional', 'Desc', 'Huancayo', 'Equipamiento', 'public', 150000, 1000, %s)
        """,
        (v1_id, proj_id, PLANNER_ID),
    )
    return proj_id, v1_id


def test_hu1_12_ca01_consolidation_of_all_five_dimensions(service, db_conn):
    """
    CA-HU1.12-01: Reúne los resultados de las cinco dimensiones (económica, social, ambiental,
    técnica y jurídica) en una sola vista con la conclusión y justificación de la versión vigente.
    Verifica además la correcta identificación de datos técnicos simulados (HU1.8) y citas (HU2.2).
    """
    code = f"PRY-ALL-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # 1. Económica
    eval_id = uuid4()
    db_conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
        VALUES (%s, %s, %s, %s, %s, 'completed')
        """,
        (eval_id, v1_id, ACTIVE_CRITERIA_ID, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO economic_result_projections (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100, status, explanation, algorithm_version)
        VALUES (%s, %s, 150.00, 85.00, 'completed', 'Costo unitario dentro del rango de excelencia', 'v1.0')
        """,
        (eval_id, uuid4()),
    )

    # 2. Social
    db_conn.execute(
        """
        INSERT INTO social_accessibility_assessments (
            id, project_id, project_version_id, requested_by_user_id, idempotency_key,
            status, summary, results_json, equity_json, social_score, score_explanation
        ) VALUES (
            %s, %s, %s, %s, %s,
            'completed', 'Cobertura social óptima', '[{"is_simulated": true}]'::jsonb, '{}'::jsonb, 90.00, 'Alta proximidad a postas y colegios'
        )
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 3. Ambiental
    db_conn.execute(
        """
        INSERT INTO environmental_assessments (
            id, project_id, project_version_id, assessed_by_user_id, idempotency_key,
            status, risk_level, risk_observation, requires_human_review, favorable,
            summary, declared_vs_evidence_json, impacts_json, mitigations_json, environmental_score
        ) VALUES (
            %s, %s, %s, %s, %s,
            'sufficient', 'low', 'Sin riesgos significativos', FALSE, TRUE,
            'Impacto ambiental bajo y controlado', '{"is_simulated": true}'::jsonb, '[]'::jsonb, '[]'::jsonb, 80.00
        )
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 4. Técnica (con datos simulados HU1.8)
    db_conn.execute(
        """
        INSERT INTO technical_feasibility_assessments (
            id, project_id, project_version_id, requested_by_user_id, idempotency_key,
            status, overall_result, overall_result_label, is_viable, is_preliminary_opinion,
            disclaimer, summary, criteria_summary, findings_json, technical_score,
            dimension_conclusion, dimension_justification
        ) VALUES (
            %s, %s, %s, %s, %s,
            'completed', 'cumple', 'Viable (Cumple)', TRUE, TRUE,
            'Datos preliminares', 'Viabilidad técnica favorable', '{"total": 4, "cumple": 4}'::jsonb,
            '[{"criterion_code": "mecanica_suelos", "is_simulated": true}]'::jsonb, 78.50,
            'Diseño geométrico y mecánica de suelos cumplen tolerancias normativas.',
            'Cálculos sustentados en parámetros preliminares de ingeniería.'
        )
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 5. Jurídica (con citas verificadas HU2.2)
    db_conn.execute(
        """
        INSERT INTO legal_assessments (
            id, project_id, project_code, project_version_id, status, summary,
            findings_json, total_findings, verifiable_citations_count, unverifiable_citations_count,
            requires_human_review, has_legal_alert, is_resolved
        ) VALUES (
            %s, %s, %s, %s, 'verified', 'Dictamen normativo conforme',
            '[]'::jsonb, 3, 3, 0,
            FALSE, FALSE, FALSE
        )
        """,
        (uuid4(), proj_id, code, v1_id),
    )

    # Ejecutar consulta del resumen
    res = service.get_unified_assessment_summary(project_id=proj_id)

    assert res["project_id"] == proj_id
    assert res["version_number"] == 1
    assert res["is_complete"] is True
    assert res["status"] == "completed"
    assert res["allows_final_approval"] is True
    assert len(res["dimensions"]) == 5

    dim_map = {d["dimension_code"]: d for d in res["dimensions"]}
    assert set(dim_map.keys()) == {"economic", "social", "environmental", "technical", "legal"}

    # Conclusión y justificación presentes en todas
    for code_d, dim in dim_map.items():
        assert dim["conclusion"], f"Falta conclusion en {code_d}"
        assert dim["justification"], f"Falta justificacion en {code_d}"
        assert dim["status"] == "completed"

    # Distinción de datos técnicos simulados (HU1.8)
    assert dim_map["technical"]["is_simulated"] is True
    assert "simulado" in dim_map["technical"]["warning"].lower()

    # Distinción de dimensión jurídica sin score arbitrario
    assert dim_map["legal"]["score"] is None
    assert dim_map["legal"]["details"]["verifiable_citations_count"] == 3


def test_hu1_12_ca02_explainable_global_score_calculation(service, db_conn):
    """
    CA-HU1.12-02: Expone las puntuaciones de entrada, sus pesos y la fórmula utilizada
    para obtener la valoración consolidada a partir de la versión activa de criterios.
    """
    code = f"PRY-SC-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # Insertar versión de criterios activa con pesos específicos
    crit_id = uuid4()
    temp_version = int(datetime.now(timezone.utc).timestamp()) % 1000000 + 10000
    try:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE status = 'active'")
        db_conn.execute(
            """
            INSERT INTO criteria_versions (id, version_number, status, excellent_cost_pen, unacceptable_cost_pen, created_by_user_id, activated_at)
            VALUES (%s, %s, 'active', 200, 500, %s, now())
            """,
            (crit_id, temp_version, ADMIN_ID),
        )
        db_conn.execute(
            """
            INSERT INTO criterion_weights (criteria_version_id, criterion_code, weight_percent)
            VALUES
                (%s, 'ECONOMIC', 50.00),
                (%s, 'TECHNICAL', 50.00)
            """,
            (crit_id, crit_id),
        )
        # Económica: score 80
        eval_id = uuid4()
        db_conn.execute(
            """
            INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
            VALUES (%s, %s, %s, %s, %s, 'completed')
            """,
            (eval_id, v1_id, crit_id, PLANNER_ID, str(uuid4())),
        )
        db_conn.execute(
            """
            INSERT INTO economic_result_projections (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100, status, explanation, algorithm_version)
        VALUES (%s, %s, 180.00, 80.00, 'completed', 'Económica aceptable', 'v1.0')
            """,
            (eval_id, uuid4()),
        )

        # Social: score 90 (no tiene peso en esta versión de política)
        db_conn.execute(
            """
            INSERT INTO social_accessibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, summary, results_json, equity_json, social_score, score_explanation)
            VALUES (%s, %s, %s, %s, %s, 'completed', 'Social ok', '[]'::jsonb, '{}'::jsonb, 90.00, 'Ok')
            """,
            (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
        )

        # Ambiental: score 70
        db_conn.execute(
            """
            INSERT INTO environmental_assessments (id, project_id, project_version_id, assessed_by_user_id, idempotency_key, status, risk_level, risk_observation, requires_human_review, favorable, summary, declared_vs_evidence_json, impacts_json, mitigations_json, environmental_score)
            VALUES (%s, %s, %s, %s, %s, 'sufficient', 'low', 'Bajo', FALSE, TRUE, 'Ambiental ok', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 70.00)
            """,
            (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
        )

        # Técnica: score 90
        db_conn.execute(
            """
            INSERT INTO technical_feasibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, overall_result, overall_result_label, is_viable, is_preliminary_opinion, disclaimer, summary, criteria_summary, findings_json, technical_score, dimension_conclusion, dimension_justification)
            VALUES (%s, %s, %s, %s, %s, 'completed', 'cumple', 'Viable', TRUE, TRUE, 'Discl', 'Summary', '{}'::jsonb, '[]'::jsonb, 90.00, 'Conc', 'Just')
            """,
            (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
        )

        # Jurídica: completada sin alertas
        db_conn.execute(
            """
            INSERT INTO legal_assessments (id, project_id, project_code, project_version_id, status, summary, findings_json, total_findings, verifiable_citations_count, unverifiable_citations_count, requires_human_review, has_legal_alert, is_resolved)
            VALUES (%s, %s, %s, %s, 'verified', 'Legal ok', '[]'::jsonb, 1, 1, 0, FALSE, FALSE, FALSE)
            """,
            (uuid4(), proj_id, code, v1_id),
        )

        res = service.get_unified_assessment_summary(project_id=proj_id)
        gs = res["global_score"]

        assert gs["is_calculated"] is True
        # (80 * 50% + 90 * 50%) / 100% = 85.0 pts
        assert gs["score"] == 85.0
        assert gs["criteria_version_number"] == temp_version
        assert gs["weights"] == {"ECONOMIC": 50.0, "TECHNICAL": 50.0}
        assert "85.00 pts" in gs["formula"]
        assert "ECONOMIC: 80.0" in gs["formula"]
        assert "TECHNICAL: 90.0" in gs["formula"]

    finally:
        # Restaurar criterios v1 como activos
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE id = %s", (crit_id,))
        db_conn.execute("UPDATE criteria_versions SET status = 'active' WHERE id = %s", (ACTIVE_CRITERIA_ID,))


def test_hu1_12_ca03_incomplete_dimensions_detected_and_blocks_approval(service, db_conn):
    """
    CA-HU1.12-03: Si alguna dimensión sigue pendiente, identifica la dimensión afectada
    y no presenta una evaluación incompleta como resultado final aprobado.
    """
    code = f"PRY-INC-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # Solo registramos económica; social, ambiental, técnica y jurídica quedan pendientes
    eval_id = uuid4()
    db_conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
        VALUES (%s, %s, %s, %s, %s, 'completed')
        """,
        (eval_id, v1_id, ACTIVE_CRITERIA_ID, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO economic_result_projections (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100, status, explanation, algorithm_version)
        VALUES (%s, %s, 150.00, 85.00, 'completed', 'Económica ok', 'v1.0')
        """,
        (eval_id, uuid4()),
    )

    res = service.get_unified_assessment_summary(project_id=proj_id)

    assert res["is_complete"] is False
    assert res["status"] == "incomplete"
    assert res["allows_final_approval"] is False
    assert set(res["missing_dimensions"]) == {"Social", "Ambiental", "Tecnica", "Juridica"}

    # Puntuación global no calculable
    assert res["global_score"]["is_calculated"] is False
    assert res["global_score"]["score"] is None
    assert "faltan dimensiones indispensables" in res["global_score"]["formula"]


def test_hu1_12_ca03_failed_or_insufficient_data_dimension_detected(service, db_conn):
    """
    CA-HU1.12-03: Dimensión técnica con información insuficiente o no viable se detecta como incompleta.
    """
    code = f"PRY-FAIL-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # 1. Económica ok
    eval_id = uuid4()
    db_conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
        VALUES (%s, %s, %s, %s, %s, 'completed')
        """,
        (eval_id, v1_id, ACTIVE_CRITERIA_ID, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO economic_result_projections (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100, status, explanation, algorithm_version)
        VALUES (%s, %s, 150.00, 85.00, 'completed', 'Ok', 'v1.0')
        """,
        (eval_id, uuid4()),
    )

    # 2. Social ok
    db_conn.execute(
        """
        INSERT INTO social_accessibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, summary, results_json, equity_json, social_score, score_explanation)
        VALUES (%s, %s, %s, %s, %s, 'completed', 'Ok', '[]'::jsonb, '{}'::jsonb, 80.00, 'Ok')
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 3. Ambiental ok
    db_conn.execute(
        """
        INSERT INTO environmental_assessments (id, project_id, project_version_id, assessed_by_user_id, idempotency_key, status, risk_level, risk_observation, requires_human_review, favorable, summary, declared_vs_evidence_json, impacts_json, mitigations_json, environmental_score)
        VALUES (%s, %s, %s, %s, %s, 'sufficient', 'low', 'Bajo', FALSE, TRUE, 'Ok', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 80.00)
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 4. Técnica con INFORMACIÓN INSUFICIENTE
    db_conn.execute(
        """
        INSERT INTO technical_feasibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, overall_result, overall_result_label, is_viable, is_preliminary_opinion, disclaimer, summary, criteria_summary, findings_json, technical_score, dimension_conclusion, dimension_justification)
        VALUES (%s, %s, %s, %s, %s, 'completed', 'informacion_insuficiente', 'Información insuficiente', FALSE, TRUE, 'Discl', 'Faltan parámetros', '{}'::jsonb, '[]'::jsonb, NULL, 'Falta mecánica de suelos', 'No se puede dictaminar')
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 5. Jurídica ok
    db_conn.execute(
        """
        INSERT INTO legal_assessments (id, project_id, project_code, project_version_id, status, summary, findings_json, total_findings, verifiable_citations_count, unverifiable_citations_count, requires_human_review, has_legal_alert, is_resolved)
        VALUES (%s, %s, %s, %s, 'verified', 'Ok', '[]'::jsonb, 1, 1, 0, FALSE, FALSE, FALSE)
        """,
        (uuid4(), proj_id, code, v1_id),
    )

    res = service.get_unified_assessment_summary(project_id=proj_id)

    assert res["is_complete"] is False
    assert "Tecnica (Informacion insuficiente)" in res["missing_dimensions"]
    assert res["allows_final_approval"] is False
    assert res["global_score"]["is_calculated"] is False


def test_hu1_12_active_legal_alert_blocks_approval_and_marks_blocked(service, db_conn):
    """
    HU2.3 / HU1.12: Alerta Legal activa en la versión vigente bloquea el resumen y la aprobación.
    """
    code = f"PRY-LBLK-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # Completamos todas las evaluaciones, pero la jurídica tiene ALERTA LEGAL activa
    eval_id = uuid4()
    db_conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
        VALUES (%s, %s, %s, %s, %s, 'completed')
        """,
        (eval_id, v1_id, ACTIVE_CRITERIA_ID, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO economic_result_projections (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100, status, explanation, algorithm_version)
        VALUES (%s, %s, 150.00, 95.00, 'completed', 'Ok', 'v1.0')
        """,
        (eval_id, uuid4()),
    )
    db_conn.execute(
        """
        INSERT INTO social_accessibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, summary, results_json, equity_json, social_score, score_explanation)
        VALUES (%s, %s, %s, %s, %s, 'completed', 'Ok', '[]'::jsonb, '{}'::jsonb, 90.00, 'Ok')
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO environmental_assessments (id, project_id, project_version_id, assessed_by_user_id, idempotency_key, status, risk_level, risk_observation, requires_human_review, favorable, summary, declared_vs_evidence_json, impacts_json, mitigations_json, environmental_score)
        VALUES (%s, %s, %s, %s, %s, 'sufficient', 'low', 'Bajo', FALSE, TRUE, 'Ok', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 85.00)
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO technical_feasibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, overall_result, overall_result_label, is_viable, is_preliminary_opinion, disclaimer, summary, criteria_summary, findings_json, technical_score, dimension_conclusion, dimension_justification)
        VALUES (%s, %s, %s, %s, %s, 'completed', 'cumple', 'Viable', TRUE, TRUE, 'Discl', 'Ok', '{}'::jsonb, '[]'::jsonb, 88.00, 'Ok', 'Ok')
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )
    # Jurídica con alerta legal activa
    db_conn.execute(
        """
        INSERT INTO legal_assessments (
            id, project_id, project_code, project_version_id, status, summary,
            findings_json, total_findings, verifiable_citations_count, unverifiable_citations_count,
            requires_human_review, has_legal_alert, legal_alert_motive, legal_alert_norm, is_resolved
        ) VALUES (
            %s, %s, %s, %s, 'alerta_legal', 'Incompatibilidad de zonificación detectada',
            '[]'::jsonb, 1, 1, 0,
            TRUE, TRUE, 'Uso prohibido en zona residencial', 'PDU Huancayo 2024', FALSE
        )
        """,
        (uuid4(), proj_id, code, v1_id),
    )

    res = service.get_unified_assessment_summary(project_id=proj_id)

    assert res["status"] == "blocked"
    assert res["allows_final_approval"] is False
    assert len(res["blocking_reasons"]) > 0
    assert any("Alerta Legal" in r for r in res["blocking_reasons"])

    legal_dim = next(d for d in res["dimensions"] if d["dimension_code"] == "legal")
    assert legal_dim["status"] == "blocked"
    assert legal_dim["has_blocker"] is True
    assert "Uso prohibido" in legal_dim["blocker_message"]


def test_hu1_12_version_isolation_new_version_does_not_leak_prior_evaluations(service, db_conn):
    """
    Aislamiento por versión: Las evaluaciones finalizadas en v1 no se presentan como vigentes en v2.
    """
    code = f"PRY-VER-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # Completar v1 al 100%
    eval_id = uuid4()
    db_conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
        VALUES (%s, %s, %s, %s, %s, 'completed')
        """,
        (eval_id, v1_id, ACTIVE_CRITERIA_ID, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO economic_result_projections (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100, status, explanation, algorithm_version)
        VALUES (%s, %s, 150.00, 85.00, 'completed', 'Ok', 'v1.0')
        """,
        (eval_id, uuid4()),
    )
    db_conn.execute(
        """
        INSERT INTO social_accessibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, summary, results_json, equity_json, social_score, score_explanation)
        VALUES (%s, %s, %s, %s, %s, 'completed', 'Ok', '[]'::jsonb, '{}'::jsonb, 90.00, 'Ok')
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO environmental_assessments (id, project_id, project_version_id, assessed_by_user_id, idempotency_key, status, risk_level, risk_observation, requires_human_review, favorable, summary, declared_vs_evidence_json, impacts_json, mitigations_json, environmental_score)
        VALUES (%s, %s, %s, %s, %s, 'sufficient', 'low', 'Bajo', FALSE, TRUE, 'Ok', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 85.00)
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO technical_feasibility_assessments (id, project_id, project_version_id, requested_by_user_id, idempotency_key, status, overall_result, overall_result_label, is_viable, is_preliminary_opinion, disclaimer, summary, criteria_summary, findings_json, technical_score, dimension_conclusion, dimension_justification)
        VALUES (%s, %s, %s, %s, %s, 'completed', 'cumple', 'Viable', TRUE, TRUE, 'Discl', 'Ok', '{}'::jsonb, '[]'::jsonb, 88.00, 'Ok', 'Ok')
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO legal_assessments (id, project_id, project_code, project_version_id, status, summary, findings_json, total_findings, verifiable_citations_count, unverifiable_citations_count, requires_human_review, has_legal_alert, is_resolved)
        VALUES (%s, %s, %s, %s, 'verified', 'Ok', '[]'::jsonb, 1, 1, 0, FALSE, FALSE, FALSE)
        """,
        (uuid4(), proj_id, code, v1_id),
    )

    # Verificar que v1 está completa
    res_v1 = service.get_unified_assessment_summary(project_id=proj_id, version_id=v1_id)
    assert res_v1["is_complete"] is True
    assert res_v1["version_number"] == 1

    # Crear versión v2
    v2_id = uuid4()
    db_conn.execute(
        """
        INSERT INTO project_versions
        (id, project_id, version_number, title, description, location_description, proposed_land_use,
         territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id)
        VALUES (%s, %s, 2, 'Proyecto Versión 2 Subsanada', 'Desc 2', 'Huancayo 2', 'Equipamiento', 'public', 160000, 1100, %s)
        """,
        (v2_id, proj_id, PLANNER_ID),
    )
    db_conn.execute(
        "UPDATE projects SET status = 'ready' WHERE id = %s",
        (proj_id,),
    )

    # Consultar resumen para v2 (debe mostrar las 5 dimensiones pendientes)
    res_v2 = service.get_unified_assessment_summary(project_id=proj_id, version_id=v2_id)
    assert res_v2["version_number"] == 2
    assert res_v2["is_complete"] is False
    assert res_v2["status"] == "incomplete"
    assert res_v2["allows_final_approval"] is False
    assert len(res_v2["missing_dimensions"]) == 5
    for d in res_v2["dimensions"]:
        assert d["status"] == "pending"


def test_hu1_12_read_only_side_effect_free(service, db_conn):
    """
    Comprueba que get_unified_assessment_summary es una operación estrictamente de solo lectura,
    sin efectos secundarios ni mutaciones de estado en la base de datos.
    """
    code = f"PRY-RO-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # Capturar conteos de tablas
    t_names = [
        "projects", "project_versions", "evaluations", "economic_result_projections",
        "social_accessibility_assessments", "environmental_assessments",
        "technical_feasibility_assessments", "legal_assessments", "human_approvals", "audit_events"
    ]
    counts_before = {t: db_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in t_names}

    # Llamar 3 veces seguidas
    r1 = service.get_unified_assessment_summary(project_id=proj_id)
    r2 = service.get_unified_assessment_summary(project_id=proj_id)
    r3 = service.get_unified_assessment_summary(project_id=proj_id)

    assert r1["summary_text"] == r2["summary_text"] == r3["summary_text"]

    counts_after = {t: db_conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in t_names}
    assert counts_before == counts_after, "La consulta GET generó efectos secundarios en la base de datos"

def test_hu1_12_api_endpoint(service, db_conn):
    """
    Comprueba el endpoint GET /api/v1/projects/{project_id}/unified-summary
    con autenticación y serialización DTO correcta.
    """
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.infrastructure.config.dependencies import get_current_user

    code = f"PRY-API-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": PLANNER_ID,
        "roles": ["PLANNER"],
        "email": "planificador@siprim.gob.pe"
    }

    with TestClient(app) as client:
        res = client.get(f"/api/v1/projects/{proj_id}/unified-summary")
        assert res.status_code == 200
        data = res.json()
        assert data["project_id"] == str(proj_id)
        assert len(data["dimensions"]) == 5
        assert data["is_complete"] is False
        assert "missing_dimensions" in data
        assert "global_score" in data



def test_hu1_12_legal_dimension_in_requires_review_blocks_final_approval_and_marks_incomplete(service, db_conn):
    """
    HU1.12 CORRECCIÓN: Una dimensión jurídica en 'requires_review' (por citas no verificables
    o dictamen legal pendiente) NO puede contar como evaluación final plenamente resuelta
    ni habilitar la aprobación definitiva.
    - is_complete debe ser False.
    - allows_final_approval debe ser False en el resumen y en la compuerta humana.
    - blocking_reasons debe identificar explícitamente el pendiente legal.
    - submit_human_approval debe rechazar la decisión 'approved'.
    """
    code = f"PRY-REQREV-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    # 1. Económica completa
    eval_id = uuid4()
    db_conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
        VALUES (%s, %s, %s, %s, %s, 'completed')
        """,
        (eval_id, v1_id, ACTIVE_CRITERIA_ID, PLANNER_ID, str(uuid4())),
    )
    db_conn.execute(
        """
        INSERT INTO economic_result_projections (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100, status, explanation, algorithm_version)
        VALUES (%s, %s, 180.00, 85.00, 'completed', 'Costo óptimo por habitante', 'v1.0')
        """,
        (eval_id, uuid4()),
    )

    # 2. Social completa
    db_conn.execute(
        """
        INSERT INTO social_accessibility_assessments (
            id, project_id, project_version_id, requested_by_user_id, idempotency_key,
            status, summary, results_json, equity_json, social_score, score_explanation
        ) VALUES (
            %s, %s, %s, %s, %s,
            'completed', 'Cobertura favorable', '[]'::jsonb, '{}'::jsonb, 90.00, 'Acceso óptimo'
        )
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 3. Ambiental completa
    db_conn.execute(
        """
        INSERT INTO environmental_assessments (
            id, project_id, project_version_id, assessed_by_user_id, idempotency_key,
            status, risk_level, risk_observation, requires_human_review, favorable,
            summary, declared_vs_evidence_json, impacts_json, mitigations_json, environmental_score
        ) VALUES (
            %s, %s, %s, %s, %s,
            'sufficient', 'low', 'Sin riesgo ambiental', FALSE, TRUE,
            'Bajo impacto', '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 85.00
        )
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 4. Técnica completa
    db_conn.execute(
        """
        INSERT INTO technical_feasibility_assessments (
            id, project_id, project_version_id, requested_by_user_id, idempotency_key,
            status, overall_result, overall_result_label, is_viable, is_preliminary_opinion,
            disclaimer, summary, criteria_summary, findings_json, technical_score,
            score_explanation, dimension_conclusion, dimension_justification
        ) VALUES (
            %s, %s, %s, %s, %s,
            'completed', 'cumple', 'Viable (Cumple)', TRUE, FALSE,
            'Dictamen técnico preliminar', 'Diseño conforme', '{}'::jsonb, '[]'::jsonb, 80.00,
            'Puntuación técnica 80.0/100', 'Conclusión técnica favorable', 'Justificación técnica completa'
        )
        """,
        (uuid4(), proj_id, v1_id, PLANNER_ID, str(uuid4())),
    )

    # 5. Jurídica PENDIENTE en 'requires_review' con citas no verificables (HU2.2)
    db_conn.execute(
        """
        INSERT INTO legal_assessments (
            id, project_id, project_code, project_version_id, status, summary, findings_json,
            total_findings, verifiable_citations_count, unverifiable_citations_count,
            requires_human_review, has_legal_alert, is_resolved, evaluated_at
        ) VALUES (
            %s, %s, %s, %s, 'requires_review',
            'Dictamen legal con citas no verificables pendientes de cotejo manual',
            '[]'::jsonb, 2, 1, 1, TRUE, FALSE, FALSE, now()
        )
        """,
        (uuid4(), proj_id, code, v1_id),
    )

    # Marcar proyecto como evaluated preliminarmente
    db_conn.execute("UPDATE projects SET status = 'evaluated' WHERE id = %s", (proj_id,))

    # 1. Consultar resumen unificado (HU1.12)
    res = service.get_unified_assessment_summary(project_id=proj_id)

    # Verificaciones estrictas
    assert res["is_complete"] is False, "Una dimensión jurídica en requires_review no puede considerarse completa"
    assert res["allows_final_approval"] is False, "Una dimensión jurídica en requires_review no puede habilitar aprobación"
    assert res["status"] == "blocked"
    assert any("Juridica" in d for d in res["missing_dimensions"]), "Debe reportar la dimensión jurídica pendiente"
    assert any("requires_review" in r or "juridica" in r.lower() for r in res["blocking_reasons"])

    # 2. Consultar compuerta humana (HU1.12 / HU2.2)
    human_gate = service.get_human_approval(proj_id)
    assert human_gate["allows_final_approval"] is False, "La compuerta humana debe reportar allows_final_approval=False"

    # 3. Intentar aprobación formal: debe ser rechazada por la compuerta backend
    with pytest.raises(ConfiguracionPMV1Error) as exc_info:
        service.submit_human_approval(
            project_id=proj_id,
            actor_id=PLANNER_ID,
            decision="approved",
            justification="Se aprueba indebidamente ignorando citas no verificables.",
            conditions=None,
            idempotency_key=f"appr-fail-reqrev-{uuid4()}",
        )
    assert "requires_review" in str(exc_info.value)


def _seed_social_inputs(conn, proj_id: UUID, code: str, categories: list[str]) -> None:
    specs = {
        "salud": ("Distancia a establecimiento de salud", 420.0, "metros", "RENAES MINSA Oficial"),
        "educacion": ("Distancia a colegio publico", 310.0, "metros", "ESCALE MINEDU Oficial"),
        "transporte": ("Tiempo al paradero troncal", 5.0, "minutos", "Plan Vial MPH"),
    }
    for cat in categories:
        name, val, unit, src = specs[cat]
        conn.execute(
            """
            INSERT INTO territorial_social_indicators (
                id, project_id, project_code, category, indicator_name,
                metric_value, metric_unit, has_sufficient_data, source_name, data_origin
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s, 'public')
            """,
            (uuid4(), proj_id, code, cat, name, val, unit, src),
        )
    for dim, target, pop, pct, crit, pts, eff_expl in [
        (
            "genero",
            "Mujeres jefas de hogar",
            520,
            52.00,
            "Acceso equitativo en equipamiento vecinal",
            5.00,
            "Bonificacion por impacto favorable en equidad de genero.",
        ),
        (
            "edad",
            "Ninos, jovenes y adultos mayores",
            340,
            34.00,
            "Atencion prioritaria intergeneracional",
            5.00,
            "Bonificacion por cobertura inclusiva en ciclo de vida.",
        ),
    ]:
        conn.execute(
            """
            INSERT INTO project_demographic_equity_records (
                id, project_id, project_code, dimension, has_sufficient_data, target_group,
                aggregated_population, aggregated_percentage, equity_criterion, declared_benefit,
                verifiable_data, has_favorable_evidence, score_effect_pts, effect_type,
                effect_explanation, source_name, data_origin
            ) VALUES (
                %s, %s, %s, %s, TRUE, %s,
                %s, %s, %s, 'Beneficio paritario',
                'Padron INEI sectorial', TRUE, %s, 'favorable',
                %s, 'INEI', 'public'
            )
            """,
            (uuid4(), proj_id, code, dim, target, pop, pct, crit, pts, eff_expl),
        )


def _seed_environmental_inputs(conn, proj_id: UUID, code: str, *, include_green_area: bool = True) -> None:
    conn.execute(
        """
        INSERT INTO project_environmental_records (
            id, project_id, project_code, has_sufficient_data, environmental_component,
            declared_impacts, declared_measures, verifiable_evidence,
            proximity_to_water_meters, is_in_protected_or_risk_area, source_name, data_origin
        ) VALUES (
            %s, %s, %s, TRUE, 'aire_acustico_residuos',
            'Polvo y ruido menor en obra', 'Riego periodico y cerco',
            'Predio urbano consolidado fuera de faja marginal',
            380.00, FALSE, 'CENEPRED / PDM Huancayo', 'public'
        )
        """,
        (uuid4(), proj_id, code),
    )
    if include_green_area:
        conn.execute(
            """
            INSERT INTO project_green_area_records (
                id, project_id, project_code, has_sufficient_data, total_project_area_m2,
                green_area_m2, green_area_percentage, delimitation_description,
                declared_affectation, verifiable_affectation, affectation_type,
                protection_measures, has_sufficient_measures, source_name, data_origin
            ) VALUES (
                %s, %s, %s, TRUE, 1200.00,
                360.00, 30.00, 'Perimetro cerco vivo sector norte',
                'Sin afectacion negativa', 'Conservacion de cobertura arborea',
                'conservacion_positiva', 'Riego tecnificado y reposicion', TRUE,
                'Catastro Ambiental MPH', 'public'
            )
            """,
            (uuid4(), proj_id, code),
        )


def _seed_technical_params(conn, proj_id: UUID, version_id: UUID, *, q_adm: float = 1.50) -> None:
    rows = [
        ("mecanica_suelos", "Capacidad portante RNE E.050", "RNE E.050", "Regla E.050", "q_adm", q_adm, "kg/cm2", None, "EMS CIP 101"),
        ("servicios_basicos", "Factibilidad SEDAM", "RNE OS.010", "Regla OS.010", "Factibilidad", None, None, "Factibilidad vigente SEDAM", "Certificado SEDAM 202"),
        ("topografia_taludes", "Pendiente media RNE CE.020", "RNE CE.020", "Regla CE.020", "Pendiente media", 8.5, "%", None, "Plano Topografico 303"),
        ("dimensionamiento_predio", "Area util de lote", "RNE A.010", "Regla A.010", "Area neta", 1200.0, "m2", None, "Partida SUNARP 404"),
    ]
    for code, name, norm_ref, rule, p_name, p_num, p_unit, p_txt, doc in rows:
        conn.execute(
            """
            INSERT INTO project_technical_parameters (
                id, project_id, project_version_id, criterion_code, criterion_name,
                normative_reference, has_sufficient_data, parameter_name,
                parameter_value_numeric, parameter_unit, parameter_value_text,
                applied_rule, supporting_document, source_origin, is_simulated
            ) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, 'public', FALSE)
            """,
            (uuid4(), proj_id, version_id, code, name, norm_ref, p_name, p_num, p_unit, p_txt, rule, doc),
        )


def _seed_economic_completion(conn, version_id: UUID, criteria_id: UUID, budget: Decimal, beneficiaries: int) -> None:
    eval_id = uuid4()
    cost_pen, score_0_100 = EconomicProcessor.calculate(
        budget=budget,
        beneficiaries=beneficiaries,
        excellent=Decimal("200.00"),
        unacceptable=Decimal("500.00"),
    )
    conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status)
        VALUES (%s, %s, %s, %s, %s, 'completed')
        """,
        (eval_id, version_id, criteria_id, PLANNER_ID, str(uuid4())),
    )
    conn.execute(
        """
        INSERT INTO economic_result_projections (
            evaluation_id, economic_assessment_id, cost_per_beneficiary_pen,
            score_0_100, status, explanation, algorithm_version
        ) VALUES (%s, %s, %s, %s, 'completed', %s, %s)
        """,
        (
            eval_id,
            uuid4(),
            cost_pen,
            score_0_100,
            f"Costo unitario {cost_pen} PEN calculado por EconomicProcessor.",
            "economic-v1.0",
        ),
    )


def _seed_verified_legal(conn, proj_id: UUID, code: str, version_id: UUID) -> None:
    conn.execute(
        """
        INSERT INTO legal_assessments (
            id, project_id, project_code, project_version_id, status, summary,
            findings_json, total_findings, verifiable_citations_count,
            unverifiable_citations_count, requires_human_review, has_legal_alert, is_resolved
        ) VALUES (
            %s, %s, %s, %s, 'verified', 'Normativa urbanistica y competencias verificadas.',
            '[]'::jsonb, 1, 1, 0, FALSE, FALSE, FALSE
        )
        """,
        (uuid4(), proj_id, code, version_id),
    )


def test_hu1_12_e2e_real_engines_recognize_social_complete_and_formula(service, db_conn):
    """
    HU1.12 End-to-End con motores reales: SocialAccessibilityEngine ('complete'),
    EnvironmentalAssessmentEngine ('sufficient' + green area score),
    TechnicalFeasibilityEngine ('cumple'), EconomicProcessor y dictamen juridico verificado.
    """
    code = f"PRY-E2E-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    _seed_economic_completion(db_conn, v1_id, ACTIVE_CRITERIA_ID, Decimal("150000.00"), 1000)
    _seed_social_inputs(db_conn, proj_id, code, ["salud", "educacion", "transporte"])
    _seed_environmental_inputs(db_conn, proj_id, code, include_green_area=True)
    _seed_technical_params(db_conn, proj_id, v1_id, q_adm=1.50)
    _seed_verified_legal(db_conn, proj_id, code, v1_id)

    soc_eval = service.evaluate_social_accessibility(proj_id, PLANNER_ID, f"soc-{uuid4()}")
    env_eval = service.evaluate_environmental_assessment(proj_id, PLANNER_ID, f"env-{uuid4()}")
    tech_eval = service.evaluate_technical_feasibility(proj_id, PLANNER_ID, f"tech-{uuid4()}")

    assert soc_eval["status"] == "complete"
    assert env_eval["status"] == "sufficient"
    assert env_eval["environmental_score"] is not None
    assert tech_eval["overall_result"] == "cumple"

    res = service.get_unified_assessment_summary(proj_id)
    dim_map = {d["dimension_code"]: d for d in res["dimensions"]}

    assert res["is_complete"] is True
    assert res["status"] == "completed"
    assert res["allows_final_approval"] is True
    assert res["missing_dimensions"] == []
    assert res["blocking_reasons"] == []
    assert all(d["status"] == "completed" for d in dim_map.values())
    assert dim_map["social"]["status"] == "completed"
    assert dim_map["social"]["status_label"] == "Completada"
    assert res["global_score"]["is_calculated"] is True
    assert res["global_score"]["score"] is not None
    assert len(res["global_score"]["weights"]) >= 1
    assert "=" in res["global_score"]["formula"]


@pytest.mark.parametrize("soc_categories,expected_soc_status,expected_missing", [
    (["salud", "educacion"], "warning", "Social (Parcial / Incompleta)"),
    ([], "failed", "Social (Informacion insuficiente)"),
])
def test_hu1_12_e2e_social_partial_or_insufficient_blocks_even_with_100pct_economic_weight(
    service, db_conn, soc_categories, expected_soc_status, expected_missing
):
    """
    CA-HU1.12-03: Social real en 'partial' o 'insufficient' debe impedir completitud
    y aprobacion definitiva incluso cuando ECONOMIC tiene peso 100% y SOCIAL 0%.
    """
    code = f"PRY-SOCB-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    crit_id = uuid4()
    temp_ver = int(datetime.now(timezone.utc).timestamp()) % 1000000 + 20000 + (1 if soc_categories else 2)
    try:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE status = 'active'")
        db_conn.execute(
            """
            INSERT INTO criteria_versions (
                id, version_number, status, excellent_cost_pen, unacceptable_cost_pen,
                created_by_user_id, activated_at
            ) VALUES (%s, %s, 'active', 200, 500, %s, now())
            """,
            (crit_id, temp_ver, ADMIN_ID),
        )
        db_conn.execute(
            """
            INSERT INTO criterion_weights (criteria_version_id, criterion_code, weight_percent)
            VALUES (%s, 'ECONOMIC', 100.00), (%s, 'SOCIAL', 0.00)
            """,
            (crit_id, crit_id),
        )

        _seed_economic_completion(db_conn, v1_id, crit_id, Decimal("150000.00"), 1000)
        if soc_categories:
            _seed_social_inputs(db_conn, proj_id, code, soc_categories)
        else:
            db_conn.execute(
                """
                INSERT INTO territorial_social_indicators (
                    id, project_id, project_code, category, indicator_name,
                    metric_value, metric_unit, has_sufficient_data, source_name, data_origin
                ) VALUES (%s, %s, %s, 'salud', 'Salud sin dato', NULL, NULL, FALSE, 'Sin dato', 'public')
                """,
                (uuid4(), proj_id, code),
            )
        _seed_environmental_inputs(db_conn, proj_id, code, include_green_area=True)
        _seed_technical_params(db_conn, proj_id, v1_id, q_adm=1.50)
        _seed_verified_legal(db_conn, proj_id, code, v1_id)

        service.evaluate_social_accessibility(proj_id, PLANNER_ID, f"soc-{uuid4()}")
        service.evaluate_environmental_assessment(proj_id, PLANNER_ID, f"env-{uuid4()}")
        service.evaluate_technical_feasibility(proj_id, PLANNER_ID, f"tech-{uuid4()}")

        res = service.get_unified_assessment_summary(proj_id)
        soc_dim = next(d for d in res["dimensions"] if d["dimension_code"] == "social")

        assert soc_dim["status"] == expected_soc_status
        assert soc_dim["has_blocker"] is True
        assert expected_missing in res["missing_dimensions"]
        assert res["is_complete"] is False
        assert res["allows_final_approval"] is False
        assert res["status"] == "blocked"
        assert res["global_score"]["is_calculated"] is False
        assert res["global_score"]["score"] is None
    finally:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE id = %s", (crit_id,))
        db_conn.execute("UPDATE criteria_versions SET status = 'active' WHERE id = %s", (ACTIVE_CRITERIA_ID,))


def test_hu1_12_e2e_technical_observado_blocks_approval_ranking_and_pdf_with_100pct_economic_weight(
    service, db_conn
):
    """
    CA-HU1.12-03 + integracion HU3.x/HU4.1/HU4.3:
    Una evaluacion tecnica real con overall_result='observado' (is_viable=False) debe bloquear
    allows_final_approval, rechazar submit_human_approval('approved') sin escrituras,
    y excluir el expediente del ranking comparable (HU4.1) y del PDF (HU4.3) incluso con ECONOMIC=100%.
    """
    code = f"PRY-OBS-{uuid4().hex[:6]}"
    proj_id, v1_id = _create_base_project(db_conn, code)

    crit_id = uuid4()
    temp_ver = int(datetime.now(timezone.utc).timestamp()) % 1000000 + 30000
    try:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE status = 'active'")
        db_conn.execute(
            """
            INSERT INTO criteria_versions (
                id, version_number, status, excellent_cost_pen, unacceptable_cost_pen,
                created_by_user_id, activated_at
            ) VALUES (%s, %s, 'active', 200, 500, %s, now())
            """,
            (crit_id, temp_ver, ADMIN_ID),
        )
        db_conn.execute(
            """
            INSERT INTO criterion_weights (criteria_version_id, criterion_code, weight_percent)
            VALUES (%s, 'ECONOMIC', 100.00), (%s, 'TECHNICAL', 0.00)
            """,
            (crit_id, crit_id),
        )

        _seed_economic_completion(db_conn, v1_id, crit_id, Decimal("150000.00"), 1000)
        _seed_social_inputs(db_conn, proj_id, code, ["salud", "educacion", "transporte"])
        _seed_environmental_inputs(db_conn, proj_id, code, include_green_area=True)
        _seed_technical_params(db_conn, proj_id, v1_id, q_adm=0.65)
        _seed_verified_legal(db_conn, proj_id, code, v1_id)

        service.evaluate_social_accessibility(proj_id, PLANNER_ID, f"soc-{uuid4()}")
        service.evaluate_environmental_assessment(proj_id, PLANNER_ID, f"env-{uuid4()}")
        tech = service.evaluate_technical_feasibility(proj_id, PLANNER_ID, f"tech-{uuid4()}")
        assert tech["overall_result"] == "observado"
        assert tech["is_viable"] is False

        db_conn.execute("UPDATE projects SET status = 'evaluated' WHERE id = %s", (proj_id,))

        res = service.get_unified_assessment_summary(proj_id)
        tech_dim = next(d for d in res["dimensions"] if d["dimension_code"] == "technical")

        assert tech_dim["status"] == "warning"
        assert tech_dim["status_label"] == "Observado"
        assert tech_dim["has_blocker"] is True
        assert "Tecnica (Observado)" in res["missing_dimensions"]
        assert any("observado" in reason.lower() for reason in res["blocking_reasons"])
        assert res["is_complete"] is False
        assert res["status"] == "blocked"
        assert res["allows_final_approval"] is False
        assert res["global_score"]["is_calculated"] is False
        assert res["global_score"]["score"] is None

        approvals_before = db_conn.execute(
            "SELECT COUNT(*) FROM human_approvals WHERE project_id = %s",
            (proj_id,),
        ).fetchone()[0]
        with pytest.raises(ConfiguracionPMV1Error) as exc_info:
            service.submit_human_approval(
                project_id=proj_id,
                actor_id=PLANNER_ID,
                decision="approved",
                justification="Intento de aprobacion con tecnica observada.",
                conditions=None,
                idempotency_key=f"appr-obs-{uuid4()}",
            )
        assert "observado" in str(exc_info.value).lower()
        approvals_after = db_conn.execute(
            "SELECT COUNT(*) FROM human_approvals WHERE project_id = %s",
            (proj_id,),
        ).fetchone()[0]
        assert approvals_before == approvals_after == 0

        ranking = service.get_project_map_ranking()
        ranked_ids = {str(item["project_id"]) for item in ranking["ranked_projects"]}
        excluded_map = {str(item["project_id"]): item for item in ranking["excluded_projects"]}
        assert str(proj_id) not in ranked_ids
        assert str(proj_id) in excluded_map
        assert excluded_map[str(proj_id)]["exclusion_code"] == "blocked"

        pdf_bytes, filename = service.export_prioritization_ranking_pdf()
        assert pdf_bytes.startswith(b"%PDF-")
        assert filename.endswith(".pdf")
    finally:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE id = %s", (crit_id,))
        db_conn.execute("UPDATE criteria_versions SET status = 'active' WHERE id = %s", (ACTIVE_CRITERIA_ID,))


def test_hu1_12_e2e_environmental_missing_conservation_and_pending_economic_block_approval(service, db_conn):
    """
    CA-HU1.12-03: Ambiental sin datos/puntaje de areas verdes y Economica en 'queued'/'failed'
    no se presentan nunca como completas ni aprobables, aun con peso 100% en otro criterio.
    """
    code_env = f"PRY-ENVM-{uuid4().hex[:6]}"
    proj_env, v_env = _create_base_project(db_conn, code_env)

    _seed_economic_completion(db_conn, v_env, ACTIVE_CRITERIA_ID, Decimal("150000.00"), 1000)
    _seed_social_inputs(db_conn, proj_env, code_env, ["salud", "educacion", "transporte"])
    _seed_environmental_inputs(db_conn, proj_env, code_env, include_green_area=False)
    _seed_technical_params(db_conn, proj_env, v_env, q_adm=1.50)
    _seed_verified_legal(db_conn, proj_env, code_env, v_env)

    service.evaluate_social_accessibility(proj_env, PLANNER_ID, f"soc-{uuid4()}")
    env_res = service.evaluate_environmental_assessment(proj_env, PLANNER_ID, f"env-{uuid4()}")
    assert env_res["environmental_score"] is None
    service.evaluate_technical_feasibility(proj_env, PLANNER_ID, f"tech-{uuid4()}")

    summary_env = service.get_unified_assessment_summary(proj_env)
    env_dim = next(d for d in summary_env["dimensions"] if d["dimension_code"] == "environmental")
    assert env_dim["status"] == "failed"
    assert env_dim["has_blocker"] is True
    assert any("Ambiental" in m for m in summary_env["missing_dimensions"])
    assert summary_env["is_complete"] is False
    assert summary_env["allows_final_approval"] is False
    assert summary_env["global_score"]["is_calculated"] is False

    # Economica fallida o en cola
    code_ec = f"PRY-ECF-{uuid4().hex[:6]}"
    proj_ec, v_ec = _create_base_project(db_conn, code_ec)
    db_conn.execute(
        """
        INSERT INTO evaluations (id, project_version_id, criteria_version_id, requested_by_user_id, idempotency_key, status, error_message)
        VALUES (%s, %s, %s, %s, %s, 'failed', 'Timeout de cola economica')
        """,
        (uuid4(), v_ec, ACTIVE_CRITERIA_ID, PLANNER_ID, str(uuid4())),
    )
    _seed_social_inputs(db_conn, proj_ec, code_ec, ["salud", "educacion", "transporte"])
    _seed_environmental_inputs(db_conn, proj_ec, code_ec, include_green_area=True)
    _seed_technical_params(db_conn, proj_ec, v_ec, q_adm=1.50)
    _seed_verified_legal(db_conn, proj_ec, code_ec, v_ec)
    service.evaluate_social_accessibility(proj_ec, PLANNER_ID, f"soc-{uuid4()}")
    service.evaluate_environmental_assessment(proj_ec, PLANNER_ID, f"env-{uuid4()}")
    service.evaluate_technical_feasibility(proj_ec, PLANNER_ID, f"tech-{uuid4()}")

    summary_ec = service.get_unified_assessment_summary(proj_ec)
    econ_dim = next(d for d in summary_ec["dimensions"] if d["dimension_code"] == "economic")
    assert econ_dim["status"] == "failed"
    assert econ_dim["has_blocker"] is True
    assert "Economica (Fallida)" in summary_ec["missing_dimensions"]
    assert summary_ec["is_complete"] is False
    assert summary_ec["allows_final_approval"] is False


@pytest.mark.parametrize("roles,expected_status", [
    (["PLANNER"], 200),
    (["PLANNING_MANAGER"], 200),
    (["ADMIN"], 200),
    (["SUPERADMIN"], 200),
    (["MAYOR"], 403),
    (["AUDITOR"], 403),
    (["LEGAL_ADVISOR"], 403),
    (["CITIZEN"], 403),
])
def test_hu1_12_unified_summary_endpoint_rbac(db_conn, roles, expected_status):
    """
    HU1.12 RBAC: GET /unified-summary permite lectura a PLANNER, PLANNING_MANAGER, ADMIN y SUPERADMIN;
    deniega con 403 a MAYOR, AUDITOR, LEGAL_ADVISOR y CITIZEN; y exige autenticacion (401).
    PLANNING_MANAGER mantiene solo lectura (POST /social-assessment sigue en 403).
    """
    from fastapi.testclient import TestClient
    from backend.main import create_app
    from backend.infrastructure.config.dependencies import get_current_user

    code = f"PRY-RBAC-{uuid4().hex[:6]}"
    proj_id, _ = _create_base_project(db_conn, code)

    unauth_app = create_app()
    with TestClient(unauth_app) as anon_client:
        anon_res = anon_client.get(f"/api/v1/projects/{proj_id}/unified-summary")
        assert anon_res.status_code == 401

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: {
        "sub": str(PLANNER_ID),
        "user_id": PLANNER_ID,
        "roles": roles,
        "email": f"{roles[0].lower()}@siprim.gob.pe",
    }

    with TestClient(app) as client:
        res = client.get(f"/api/v1/projects/{proj_id}/unified-summary")
        assert res.status_code == expected_status

        if roles == ["PLANNING_MANAGER"]:
            write_res = client.post(
                f"/api/v1/projects/{proj_id}/social-assessment",
                headers={"Idempotency-Key": f"mgr-deny-{uuid4()}"},
            )
            assert write_res.status_code == 403


def _seed_five_complete_evaluated_dimensions(service: PlatformService, db_conn, code: str) -> tuple[UUID, UUID]:
    proj_id, v1_id = _create_base_project(db_conn, code)
    _seed_economic_completion(db_conn, v1_id, ACTIVE_CRITERIA_ID, Decimal("150000.00"), 1000)
    _seed_social_inputs(db_conn, proj_id, code, ["salud", "educacion", "transporte"])
    _seed_environmental_inputs(db_conn, proj_id, code, include_green_area=True)
    _seed_technical_params(db_conn, proj_id, v1_id, q_adm=1.50)
    _seed_verified_legal(db_conn, proj_id, code, v1_id)

    service.evaluate_social_accessibility(proj_id, PLANNER_ID, f"soc-{uuid4()}")
    service.evaluate_environmental_assessment(proj_id, PLANNER_ID, f"env-{uuid4()}")
    service.evaluate_technical_feasibility(proj_id, PLANNER_ID, f"tech-{uuid4()}")
    db_conn.execute("UPDATE projects SET status = 'evaluated' WHERE id = %s", (proj_id,))
    return proj_id, v1_id


def _assert_uncalculated_score_blocks_gates(service: PlatformService, db_conn, proj_id: UUID, expected_reason_token: str) -> None:
    res = service.get_unified_assessment_summary(proj_id)

    # Distincion estricta: 5 dimensiones evaluadas vs puntuacion global no calculable
    assert res["is_complete"] is True
    assert res["missing_dimensions"] == []
    assert "Dimensiones finalizadas: 5/5" in res["summary_text"]
    assert res["global_score"]["score"] is None
    assert res["global_score"]["is_calculated"] is False
    assert res["allows_final_approval"] is False
    assert res["status"] == "blocked"
    assert len(res["blocking_reasons"]) >= 1
    assert any(expected_reason_token in r.lower() for r in res["blocking_reasons"]), res["blocking_reasons"]

    gate = service.get_human_approval(proj_id)
    assert gate["allows_final_approval"] is False

    approvals_before = db_conn.execute(
        "SELECT COUNT(*) FROM human_approvals WHERE project_id = %s",
        (proj_id,),
    ).fetchone()[0]
    status_before = db_conn.execute("SELECT status FROM projects WHERE id = %s", (proj_id,)).fetchone()[0]

    with pytest.raises(ConfiguracionPMV1Error) as exc_info:
        service.submit_human_approval(
            project_id=proj_id,
            actor_id=PLANNER_ID,
            decision="approved",
            justification="Intento de aprobacion sin puntuacion global calculable.",
            conditions=None,
            idempotency_key=f"appr-nogs-{uuid4()}",
        )
    assert expected_reason_token in str(exc_info.value).lower()

    approvals_after = db_conn.execute(
        "SELECT COUNT(*) FROM human_approvals WHERE project_id = %s",
        (proj_id,),
    ).fetchone()[0]
    status_after = db_conn.execute("SELECT status FROM projects WHERE id = %s", (proj_id,)).fetchone()[0]
    assert approvals_before == approvals_after == 0
    assert status_before == status_after == "evaluated"

    ranking = service.get_project_map_ranking()
    ranked_ids = {str(item["project_id"]) for item in ranking["ranked_projects"]}
    excluded_ids = {str(item["project_id"]) for item in ranking["excluded_projects"]}
    assert str(proj_id) not in ranked_ids
    assert str(proj_id) in excluded_ids

    pdf_bytes, filename = service.export_prioritization_ranking_pdf()
    assert pdf_bytes.startswith(b"%PDF-")
    assert filename.endswith(".pdf")


def test_hu1_12_five_dimensions_complete_without_active_criteria_blocks_approval(service, db_conn):
    """
    Si las 5 dimensiones estan completas pero no existe una version activa de criterios,
    global_score.is_calculated es False, allows_final_approval es False y todas las compuertas bloquean.
    """
    code = f"PRY-NOCRIT-{uuid4().hex[:6]}"
    proj_id, _ = _seed_five_complete_evaluated_dimensions(service, db_conn, code)

    try:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE status = 'active'")
        _assert_uncalculated_score_blocks_gates(service, db_conn, proj_id, "version activa de criterios")
    finally:
        db_conn.execute("UPDATE criteria_versions SET status = 'active' WHERE id = %s", (ACTIVE_CRITERIA_ID,))


def test_hu1_12_five_dimensions_complete_without_weights_blocks_approval(service, db_conn):
    """
    Si existe version activa de criterios sin pesos en criterion_weights, aunque las 5 dimensiones
    esten evaluadas, global_score no es calculable y se bloquea la aprobacion definitiva y el ranking.
    """
    code = f"PRY-NOWGT-{uuid4().hex[:6]}"
    proj_id, _ = _seed_five_complete_evaluated_dimensions(service, db_conn, code)
    crit_id = uuid4()
    temp_ver = int(datetime.now(timezone.utc).timestamp()) % 1000000 + 40000

    try:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE status = 'active'")
        db_conn.execute(
            """
            INSERT INTO criteria_versions (
                id, version_number, status, excellent_cost_pen, unacceptable_cost_pen,
                created_by_user_id, activated_at
            ) VALUES (%s, %s, 'active', 200, 500, %s, now())
            """,
            (crit_id, temp_ver, ADMIN_ID),
        )
        _assert_uncalculated_score_blocks_gates(service, db_conn, proj_id, "no tiene pesos")
    finally:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE id = %s", (crit_id,))
        db_conn.execute("UPDATE criteria_versions SET status = 'active' WHERE id = %s", (ACTIVE_CRITERIA_ID,))


def test_hu1_12_five_dimensions_complete_with_zero_quantitative_weights_blocks_approval(service, db_conn):
    """
    Si todos los pesos cuantitativos activos suman 0%, no se inventa puntaje ni se permite
    habilitar la aprobacion definitiva aunque las 5 dimensiones esten completas.
    """
    code = f"PRY-ZERO-{uuid4().hex[:6]}"
    proj_id, _ = _seed_five_complete_evaluated_dimensions(service, db_conn, code)
    crit_id = uuid4()
    temp_ver = int(datetime.now(timezone.utc).timestamp()) % 1000000 + 41000

    try:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE status = 'active'")
        db_conn.execute(
            """
            INSERT INTO criteria_versions (
                id, version_number, status, excellent_cost_pen, unacceptable_cost_pen,
                created_by_user_id, activated_at
            ) VALUES (%s, %s, 'active', 200, 500, %s, now())
            """,
            (crit_id, temp_ver, ADMIN_ID),
        )
        db_conn.execute(
            """
            INSERT INTO criterion_weights (criteria_version_id, criterion_code, weight_percent)
            VALUES
                (%s, 'ECONOMIC', 0.00),
                (%s, 'SOCIAL', 0.00),
                (%s, 'ENVIRONMENTAL', 0.00),
                (%s, 'TECHNICAL', 0.00)
            """,
            (crit_id, crit_id, crit_id, crit_id),
        )
        _assert_uncalculated_score_blocks_gates(service, db_conn, proj_id, "es 0%")
    finally:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE id = %s", (crit_id,))
        db_conn.execute("UPDATE criteria_versions SET status = 'active' WHERE id = %s", (ACTIVE_CRITERIA_ID,))


def test_hu1_12_five_dimensions_complete_with_missing_weighted_criterion_score_blocks_approval(service, db_conn):
    """
    Si la politica activa pondera un criterio con peso > 0 que carece de puntuacion cuantitativa
    (p. ej. criterio adicional o sin nota), global_score.is_calculated es False y la aprobacion se bloquea.
    """
    code = f"PRY-MISSC-{uuid4().hex[:6]}"
    proj_id, _ = _seed_five_complete_evaluated_dimensions(service, db_conn, code)
    crit_id = uuid4()
    temp_ver = int(datetime.now(timezone.utc).timestamp()) % 1000000 + 42000

    try:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE status = 'active'")
        db_conn.execute(
            """
            INSERT INTO criteria_versions (
                id, version_number, status, excellent_cost_pen, unacceptable_cost_pen,
                created_by_user_id, activated_at
            ) VALUES (%s, %s, 'active', 200, 500, %s, now())
            """,
            (crit_id, temp_ver, ADMIN_ID),
        )
        db_conn.execute(
            """
            INSERT INTO criterion_weights (criteria_version_id, criterion_code, weight_percent)
            VALUES
                (%s, 'ECONOMIC', 60.00),
                (%s, 'LEGAL', 40.00)
            """,
            (crit_id, crit_id),
        )
        _assert_uncalculated_score_blocks_gates(service, db_conn, proj_id, "criterio(s) ponderado(s)")
    finally:
        db_conn.execute("UPDATE criteria_versions SET status = 'retired' WHERE id = %s", (crit_id,))
        db_conn.execute("UPDATE criteria_versions SET status = 'active' WHERE id = %s", (ACTIVE_CRITERIA_ID,))
