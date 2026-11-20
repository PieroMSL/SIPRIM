import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from backend.application.services.pmv1_services import PlatformService
from backend.application.services.validated_auth_service import ValidatedAuthService
from backend.infrastructure.config.dependencies import (
    get_auth_service,
    get_current_user,
    get_platform_service,
)
from backend.main import create_app


def _make_auth_service(db_url: str = "postgresql://fake/auth_db") -> ValidatedAuthService:
    return ValidatedAuthService(
        db_url=db_url,
        jwt_secret="test-secret-key-min-32-characters-hu53",
        issuer="siprim-auth-test",
        audience="siprim-api-test",
        expiration_minutes=30,
    )


def test_hu5_3_history_endpoints_authorization_and_revocation():
    """Solo AUDITOR, ADMIN y SUPERADMIN acceden al historial; otros roles y tokens revocados reciben 403."""
    app = create_app()
    fake_auth = MagicMock()
    fake_platform = MagicMock()
    project_id = uuid4()

    app.dependency_overrides[get_auth_service] = lambda: fake_auth
    app.dependency_overrides[get_platform_service] = lambda: fake_platform

    # 1. Sin autenticacion -> 401
    with TestClient(app) as client:
        assert client.get("/api/v1/audit/projects").status_code == 401
        assert client.get(f"/api/v1/projects/{project_id}/history").status_code == 401

    # 2. Roles sin facultad de auditoria -> 403 sin fuga de historial
    for role in ("PLANNER", "MAYOR", "LEGAL_ADVISOR", "PLANNING_MANAGER", "CITIZEN"):
        app.dependency_overrides[get_current_user] = lambda r=role: {
            "sub": str(uuid4()),
            "roles": [r],
        }
        with TestClient(app) as client:
            res_list = client.get("/api/v1/audit/projects")
            res_hist = client.get(f"/api/v1/projects/{project_id}/history")
            assert res_list.status_code == 403
            assert res_hist.status_code == 403
            assert "events" not in res_hist.json()
            assert "versions" not in res_hist.json()
    fake_platform.list_auditable_projects.assert_not_called()
    fake_platform.get_project_history.assert_not_called()

    # 3. Roles autorizados: AUDITOR, ADMIN y SUPERADMIN -> 200
    fake_platform.list_auditable_projects.return_value = {"projects": []}
    fake_platform.get_project_history.return_value = {
        "project_id": project_id,
        "code": "PROY-HU53-01",
        "current_title": "Proyecto Auditado",
        "current_status": "evaluated",
        "current_version_id": uuid4(),
        "current_version_number": 1,
        "versions": [],
        "events": [],
        "historical_limitations": ["Limitacion de prueba"],
        "read_only": True,
        "generated_at": datetime.now(timezone.utc),
    }

    for allowed_role in ("AUDITOR", "ADMIN", "SUPERADMIN"):
        app.dependency_overrides[get_current_user] = lambda r=allowed_role: {
            "sub": str(uuid4()),
            "roles": [r],
        }
        with TestClient(app) as client:
            assert client.get("/api/v1/audit/projects").status_code == 200
            res_ok = client.get(f"/api/v1/projects/{project_id}/history")
            assert res_ok.status_code == 200
            assert res_ok.json()["read_only"] is True

    # 4. Revocacion inmediata: token previo con claim AUDITOR rechazado si auth_db ya revoco el rol
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

    auth_svc.get_active_user_roles = MagicMock(return_value=["MAYOR"])  # type: ignore[method-assign]
    with TestClient(app_rev) as client:
        res_revoked = client.get(
            f"/api/v1/projects/{project_id}/history",
            headers={"Authorization": f"Bearer {prior_jwt}"},
        )
        assert res_revoked.status_code == 403
        fake_platform_rev.get_project_history.assert_not_called()


def test_hu5_3_postgres_chronological_history_multiversion_and_readonly():
    """Reconstruye historial real multiversion en PostgreSQL sin duplicados, sin alterar registros ni inventar actores."""
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
    missing_actor_id = uuid4()  # Intencionalmente NO existe en auth_db
    proj_id = uuid4()
    v1_id = uuid4()
    v2_id = uuid4()
    eval_v1_id = uuid4()
    eval_v2_id = uuid4()
    cp1_id = uuid4()

    t0 = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)
    unique_code = f"HU53-PG-{str(proj_id)[:6].upper()}"

    try:
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            planner_role = conn.execute("SELECT id FROM roles WHERE code = 'PLANNER'").fetchone()[0]
            manager_role = conn.execute("SELECT id FROM roles WHERE code = 'PLANNING_MANAGER'").fetchone()[0]
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, is_active)
                VALUES
                  (%s, %s, 'Planificadora Prueba HU53', 'hash-no-expuesto', true),
                  (%s, %s, 'Gerente Prueba HU53', 'hash-no-expuesto', true)
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
                (proj_id, unique_code, planner_id, t0, t0 + timedelta(hours=5)),
            )
            conn.execute(
                """
                INSERT INTO project_versions
                (id, project_id, version_number, title, description, location_description, proposed_land_use,
                 territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id, created_at)
                VALUES (%s, %s, 1, 'Expediente Prueba v1', 'Desc v1', 'El Tambo', 'Educacion', 'public', 350000, 900, %s, %s)
                """,
                (v1_id, proj_id, planner_id, t0 + timedelta(minutes=1)),
            )
            conn.execute(
                """
                INSERT INTO project_geospatial_layers
                (id, project_version_id, layer_type, layer_name, meaning, source_name, data_origin,
                 availability_status, geometry_type, geojson_data, created_by_user_id, created_at)
                VALUES (%s, %s, 'beneficiary_coverage', 'Cobertura El Tambo', 'Area distrital referencial',
                        'INEI GeoServer', 'public', 'available', 'Polygon',
                        '{"type":"Polygon","coordinates":[[[-75.22,-12.06],[-75.21,-12.06],[-75.21,-12.05],[-75.22,-12.06]]]}'::jsonb,
                        %s, %s)
                """,
                (uuid4(), v1_id, planner_id, t0 + timedelta(minutes=5)),
            )
            conn.execute(
                """
                INSERT INTO project_pert_estimates
                (id, project_id, project_version_id, registered_by_user_id, time_unit,
                 optimistic_duration, most_likely_duration, pessimistic_duration,
                 expected_duration, standard_deviation, variance,
                 confidence_interval_low, confidence_interval_high,
                 formula_applied, disclaimer, explanation, created_at)
                VALUES (%s, %s, %s, %s, 'meses', 4, 6, 10, 6.33, 1.00, 1.00, 4.33, 8.33,
                        '(O + 4M + P)/6', 'Estimacion referencial', 'Analisis PERT', %s)
                """,
                (uuid4(), proj_id, v1_id, planner_id, t0 + timedelta(minutes=10)),
            )
            conn.execute(
                """
                INSERT INTO social_accessibility_assessments
                (id, project_id, project_version_id, requested_by_user_id, idempotency_key,
                 status, summary, results_json, social_score, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', 'Alta prioridad social', '{}'::jsonb, 88.5, %s)
                """,
                (uuid4(), proj_id, v1_id, planner_id, f"hu53-soc-{uuid4()}", t0 + timedelta(minutes=15)),
            )
            conn.execute(
                """
                INSERT INTO environmental_assessments
                (id, project_id, project_version_id, assessed_by_user_id, idempotency_key,
                 status, risk_level, requires_human_review, summary,
                 declared_vs_evidence_json, impacts_json, mitigations_json, environmental_score, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, 'evaluated', 'high', true, 'Riesgo ambiental alto en ribera',
                        '{}'::jsonb, '[]'::jsonb, '[]'::jsonb, 62.0, %s)
                """,
                (uuid4(), proj_id, v1_id, missing_actor_id, f"hu53-env-{uuid4()}", t0 + timedelta(minutes=20)),
            )
            conn.execute(
                """
                INSERT INTO technical_feasibility_assessments
                (id, project_id, project_version_id, requested_by_user_id, idempotency_key,
                 status, overall_result, overall_result_label, disclaimer, summary,
                 criteria_summary, findings_json, technical_score, dimension_conclusion, dimension_justification, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, 'completed', 'cumple', 'Cumple', 'Opinion preliminar',
                        'Estudios basicos completos', '{}'::jsonb, '[]'::jsonb, 81.0,
                        'Cumple criterios tecnicos', 'Sustento tecnico verificado', %s)
                """,
                (uuid4(), proj_id, v1_id, planner_id, f"hu53-tech-{uuid4()}", t0 + timedelta(minutes=25)),
            )
            conn.execute(
                """
                INSERT INTO legal_assessments
                (id, project_id, project_code, project_version_id, status, summary, findings_json,
                 verifiable_citations_count, unverifiable_citations_count, has_legal_alert, evaluated_at)
                VALUES (%s, %s, %s, %s, 'verified', 'Cumple marco normativo aplicable', '[]'::jsonb, 1, 0, false, %s)
                """,
                (uuid4(), proj_id, unique_code, v1_id, t0 + timedelta(minutes=30)),
            )
            # Audit_event simultaneo para probar deduplicacion con legal_assessments
            conn.execute(
                """
                INSERT INTO audit_events (actor_user_id, action, entity_type, entity_id, details, occurred_at)
                VALUES (%s, 'LEGAL_ASSESSMENT_EVALUATED', 'project', %s, %s, %s)
                """,
                (
                    planner_id,
                    proj_id,
                    json.dumps({"total_findings": 1, "verifiable_count": 1, "has_legal_alert": False}),
                    t0 + timedelta(minutes=30, seconds=1),
                ),
            )
            conn.execute(
                """
                INSERT INTO evaluations
                (id, project_version_id, criteria_version_id, requested_by_user_id, status, idempotency_key, requested_at)
                VALUES (%s, %s, %s, %s, 'pending_review', %s, %s)
                """,
                (eval_v1_id, v1_id, criteria_id, planner_id, f"idem-secret-{uuid4()}", t0 + timedelta(minutes=35)),
            )
            conn.execute(
                """
                INSERT INTO evaluation_checkpoints
                (id, project_id, project_version_id, evaluation_id, checkpoint_type, status, reason, risk_source, risk_level, created_at)
                VALUES (%s, %s, %s, %s, 'high_risk_pause', 'pending_review', 'Pausa automatica por riesgo ambiental alto', 'environmental_risk', 'high', %s)
                """,
                (cp1_id, proj_id, v1_id, eval_v1_id, t0 + timedelta(minutes=40)),
            )

        # HU3.3 decisiones secuenciales + HU3.4 reanudacion en v1
        d1 = svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=manager_id,
            actor_name="Gerente Prueba HU53",
            actor_email="manager@siprim.test",
            decision="request_correction",
            justification="Se requiere estudio hidrologico complementario antes de continuar.",
            conditions="Adjuntar modelamiento de crecida",
            idempotency_key=f"hu53-dec1-{uuid4()}",
        )
        d2 = svc.register_checkpoint_decision(
            project_id=proj_id,
            actor_id=manager_id,
            actor_name="Gerente Prueba HU53",
            actor_email="manager@siprim.test",
            decision="approve_continue",
            justification="Estudio hidrologico revisado y conforme.",
            conditions="Verificacion en obra",
            idempotency_key=f"hu53-dec2-{uuid4()}",
        )
        svc.resume_project_checkpoint(
            project_id=proj_id,
            actor_id=manager_id,
            justification="Se levanta pausa tras aprobacion del punto de control.",
            conditions="Mantener monitoreo ambiental",
            idempotency_key=f"hu53-res1-{uuid4()}",
        )

        # Version v2 posterior + proyeccion economica + dictamen final en v2
        with psycopg.connect(platform_db_url) as conn, conn.transaction():
            t_v2 = datetime.now(timezone.utc) + timedelta(minutes=1)
            conn.execute(
                """
                INSERT INTO project_versions
                (id, project_id, version_number, title, description, location_description, proposed_land_use,
                 territorial_data_origin, estimated_budget_pen, beneficiaries_count, created_by_user_id, created_at)
                VALUES (%s, %s, 2, 'Expediente Prueba v2 Actualizado', 'Desc v2', 'El Tambo', 'Educacion', 'public', 420000, 1150, %s, %s)
                """,
                (v2_id, proj_id, planner_id, t_v2),
            )
            conn.execute(
                """
                INSERT INTO evaluations
                (id, project_version_id, criteria_version_id, requested_by_user_id, status, idempotency_key, requested_at)
                VALUES (%s, %s, %s, %s, 'completed', %s, %s)
                """,
                (eval_v2_id, v2_id, criteria_id, planner_id, f"idem-v2-{uuid4()}", t_v2 + timedelta(seconds=10)),
            )
            conn.execute(
                """
                INSERT INTO economic_result_projections
                (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100,
                 status, explanation, algorithm_version, received_at)
                VALUES (%s, %s, 365.22, 86.40, 'completed', 'Proyeccion v2 calculada', 'hu1.3-v1', %s)
                """,
                (eval_v2_id, uuid4(), t_v2 + timedelta(seconds=20)),
            )
            conn.execute(
                """
                INSERT INTO human_approvals
                (id, project_id, project_version_id, decision, justification, conditions,
                 decided_by_user_id, idempotency_key, decided_at)
                VALUES (%s, %s, %s, 'approved', 'Version v2 aprobada para cartera prioritaria.',
                        'Ejecucion segun cronograma', %s, %s, %s)
                """,
                (uuid4(), proj_id, v2_id, manager_id, f"hu53-app-{uuid4()}", t_v2 + timedelta(seconds=30)),
            )
            conn.execute(
                "UPDATE projects SET status = 'approved', updated_at = %s WHERE id = %s",
                (t_v2 + timedelta(seconds=35), proj_id),
            )

            # Snapshot previo para verificar estricta solo-lectura
            counts_before = {
                tbl: conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE project_id = %s",
                    (proj_id,),
                ).fetchone()[0]
                for tbl in (
                    "project_versions",
                    "evaluation_checkpoints",
                    "checkpoint_review_decisions",
                    "human_approvals",
                )
            }
            updated_before = conn.execute(
                "SELECT updated_at FROM projects WHERE id = %s",
                (proj_id,),
            ).fetchone()[0]

        history = svc.get_project_history(proj_id)

        # Verificar solo-lectura en base de datos
        with psycopg.connect(platform_db_url, row_factory=dict_row) as conn:
            counts_after = {
                tbl: conn.execute(
                    f"SELECT COUNT(*) AS cnt FROM {tbl} WHERE project_id = %s",
                    (proj_id,),
                ).fetchone()["cnt"]
                for tbl in counts_before
            }
            updated_after = conn.execute(
                "SELECT updated_at FROM projects WHERE id = %s",
                (proj_id,),
            ).fetchone()["updated_at"]
        assert counts_after == counts_before
        assert updated_after == updated_before

        # Verificaciones de integridad cronologica, versiones, deduplicacion y seguridad
        assert history["read_only"] is True
        assert history["code"] == unique_code
        assert history["current_version_number"] == 2
        assert [v["version_number"] for v in history["versions"]] == [1, 2]

        events = history["events"]
        timestamps = [e["occurred_at"] for e in events]
        assert timestamps == sorted(timestamps), "La linea de tiempo debe ser cronologica"

        event_ids = [e["event_id"] for e in events]
        assert len(event_ids) == len(set(event_ids)), "No debe haber eventos duplicados"

        # Deduplicacion de LEGAL_ASSESSMENT vs LEGAL_ASSESSMENT_EVALUATED simultaneo
        legal_events = [e for e in events if "LEGAL_ASSESSMENT" in e["event_type"]]
        assert len(legal_events) == 1
        assert legal_events[0]["actor_name"] == "Planificadora Prueba HU53"

        # Registros de v1 siguen presentes tras crear v2
        v1_events = [e for e in events if e["version_number"] == 1]
        v2_events = [e for e in events if e["version_number"] == 2]
        assert any(e["event_type"] == "VERSION_CREATED" for e in v1_events)
        assert any(e["event_type"] == "CHECKPOINT_PAUSED" for e in v1_events)
        assert any(e["event_type"] == "CHECKPOINT_RESUMED" for e in v1_events)
        assert any(e["event_type"] == "HUMAN_APPROVAL" for e in v2_events)

        # Decisiones HU3.3 con huella SHA-256 valida y aviso de prototipo
        dec_events = [e for e in events if e["event_type"] == "CHECKPOINT_DECISION"]
        assert len(dec_events) == 2
        assert dec_events[0]["integrity_hash_sha256"] == d1["integrity_hash_sha256"]
        assert dec_events[1]["integrity_hash_sha256"] == d2["integrity_hash_sha256"]
        for de in dec_events:
            assert de["verification_status"] == "valid"
            assert "no constituye firma digital certificada" in (de["prototype_notice"] or "").lower()

        # Actor inexistente en auth_db no se inventa
        env_events = [e for e in events if e["event_type"] == "ENVIRONMENTAL_ASSESSMENT"]
        assert len(env_events) == 1
        assert env_events[0]["actor_user_id"] == missing_actor_id
        assert env_events[0]["actor_name"] is None

        # Sin fuga de secretos / claves de idempotencia / password_hash
        raw_dump = json.dumps(history, default=str)
        assert "idem-secret-" not in raw_dump
        assert "hash-no-expuesto" not in raw_dump
    finally:
        with psycopg.connect(platform_db_url) as conn, conn.transaction():
            conn.execute("DELETE FROM human_approvals WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM checkpoint_review_decisions WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM evaluation_checkpoints WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM economic_result_projections WHERE evaluation_id IN (%s, %s)", (eval_v1_id, eval_v2_id))
            conn.execute("DELETE FROM evaluations WHERE project_version_id IN (%s, %s)", (v1_id, v2_id))
            conn.execute("DELETE FROM legal_assessments WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM technical_feasibility_assessments WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM environmental_assessments WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM social_accessibility_assessments WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM project_pert_estimates WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM project_geospatial_layers WHERE project_version_id IN (%s, %s)", (v1_id, v2_id))
            conn.execute("DELETE FROM project_versions WHERE project_id = %s", (proj_id,))
            conn.execute("DELETE FROM projects WHERE id = %s", (proj_id,))
        with psycopg.connect(auth_db_url) as conn, conn.transaction():
            conn.execute("DELETE FROM user_roles WHERE user_id IN (%s, %s)", (planner_id, manager_id))
            conn.execute("DELETE FROM users WHERE id IN (%s, %s)", (planner_id, manager_id))
