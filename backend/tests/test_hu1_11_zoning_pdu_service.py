from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import PlatformService
from backend.application.services.zoning_catalog import (
    EL_TAMBO_AMH_ENVELOPE,
    OfficialZoningSourceMetadata,
    ZoningSourceValidationError,
    ZoningSourceValidator,
    build_synthetic_rehearsal_zoning_package,
)
from backend.infrastructure.config.dependencies import get_platform_service
from backend.infrastructure.config.settings import get_settings
from backend.main import create_app


def _cleanup_project(conn: psycopg.Connection, project_id: UUID) -> None:
    pv_ids = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM project_versions WHERE project_id = %s", (project_id,)
        ).fetchall()
    ]
    if pv_ids:
        conn.execute(
            "DELETE FROM zoning_review_requests WHERE project_version_id = ANY(%s)",
            (pv_ids,),
        )
        conn.execute(
            "DELETE FROM project_geospatial_layers WHERE project_version_id = ANY(%s)",
            (pv_ids,),
        )
        conn.execute(
            "DELETE FROM project_environmental_records WHERE project_version_id = ANY(%s)",
            (pv_ids,),
        )
        conn.execute(
            "DELETE FROM project_versions WHERE id = ANY(%s)",
            (pv_ids,),
        )
    conn.execute("DELETE FROM projects WHERE id = %s", (project_id,))


@pytest.fixture
def isolated_hu111_env():
    settings = get_settings()
    app = create_app()
    platform_svc = PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=None,  # type: ignore
        enable_demo_fallback=False,
        auth_db_url=settings.auth_db_url,
    )
    app.dependency_overrides[get_platform_service] = lambda: platform_svc
    p_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with TestClient(app) as client, psycopg.connect(p_url, autocommit=True) as conn:
        yield client, conn


def _login(client: TestClient, email: str) -> dict[str, str]:
    resp = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Prueba2026!"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _synthetic_polygon_geojson() -> dict:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [-75.2300, -12.0650],
                [-75.2050, -12.0650],
                [-75.2050, -12.0450],
                [-75.2300, -12.0450],
                [-75.2300, -12.0650],
            ]
        ],
    }


def _sanitized_geometry(geojson_obj: dict) -> dict:
    if geojson_obj.get("type") == "FeatureCollection":
        feats = geojson_obj.get("features") or []
        return _sanitized_geometry(feats[0]["geometry"])
    if geojson_obj.get("type") == "Feature":
        return _sanitized_geometry(geojson_obj["geometry"])
    return {"type": geojson_obj["type"], "coordinates": geojson_obj["coordinates"]}


def test_hu1_11_ca03_abstains_with_real_pdm_catalog_and_inei_boundary(
    isolated_hu111_env,
) -> None:
    """
    CA-HU1.11-03:
    - Con el límite político-administrativo real del INEI (El Tambo, UBIGEO 120114) cargado en BD,
      el servidor lo ignora para clasificación zonal predial y responde 'requires_review' (compatible=None).
    - Devuelve la ordenanza base vigente O.M. N.° 636-2020-MPH/OM con URL real en gob.pe
      (sin desactivarla globalmente) y las modificaciones parciales registradas.
    """
    client, conn = isolated_hu111_env
    planner_headers = _login(client, "planner@siprim.test")
    legal_headers = _login(client, "legal@siprim.test")
    created_ids: list[UUID] = []
    try:
        inei_path = (
            Path(__file__).resolve().parents[2]
            / "database"
            / "geospatial"
            / "inei_el_tambo_120114"
            / "inei_el_tambo_120114_siprim.geojson"
        )
        inei_geo = _sanitized_geometry(json.loads(inei_path.read_text(encoding="utf-8")))

        created = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Pavimentación Jr. Parra del Riego - Sin GIS Zonal",
                "description": "Mejoramiento de vías urbanas sin plano GIS zonal.",
                "location": "Jr. Parra del Riego cdra. 2, El Tambo - Huancayo",
                "proposed_land_use": "Infraestructura vial urbana",
                "latitude": -12.0545,
                "longitude": -75.2185,
                "coordinates_origin": "field_survey",
                "coordinates_source": "Levantamiento GPS de campo",
                "estimated_budget_pen": 180000,
                "beneficiaries_count": 900,
            },
        )
        assert created.status_code == 201, created.text
        project_id = UUID(created.json()["project_id"])
        created_ids.append(project_id)

        layer_resp = client.post(
            f"/api/v1/projects/{project_id}/geospatial-layers",
            headers=planner_headers,
            json={
                "layer_type": "territorial_zone",
                "layer_name": "limite_distrital_el_tambo_inei",
                "meaning": "Polígono distrital administrativo INEI UBIGEO 120114",
                "source_name": "INEI GeoServer Interoperabilidad, ig_distrito",
                "data_origin": "public",
                "availability_status": "available",
                "geojson_data": inei_geo,
            },
        )
        assert layer_resp.status_code == 201, layer_resp.text

        assert (
            client.get(
                f"/api/v1/projects/{project_id}/zoning-precheck",
                headers=planner_headers,
            ).json()
            is None
        )

        z_resp = client.post(
            f"/api/v1/projects/{project_id}/zoning-precheck",
            headers={**planner_headers, "Idempotency-Key": f"hu111-abs-{uuid4().hex[:8]}"},
        )
        assert z_resp.status_code == 202, z_resp.text
        z = z_resp.json()
        assert z["status"] == "requires_review"
        assert z["compatible"] is None
        assert z["flagged_for_legal_review"] is False
        assert z["requires_human_review"] is True
        assert "inei" in z["limitations"].lower()
        assert "capa gis vectorial oficial" in z["limitations"].lower()
        assert z["base_ordinance_url"] == (
            "https://www.gob.pe/institucion/munihuancayo/normas-legales/838304-636-2020-mph-om"
        )
        assert all(ev["is_spatial_classification"] is False for ev in z["evidence"])
        assert all("838304-636-2020-mph-om" in (ev["official_url"] or "") for ev in z["evidence"])

        for h in (planner_headers, legal_headers):
            get_z = client.get(f"/api/v1/projects/{project_id}/zoning-precheck", headers=h)
            assert get_z.status_code == 200
            assert get_z.json()["compatible"] is None
            assert get_z.json()["review_id"] == z["review_id"]

        # Borrador sin location/proposed_land_use -> 422
        draft = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={"title": "Borrador incompleto HU1.11", "save_as_draft": True},
        )
        assert draft.status_code == 201
        d_pid = UUID(draft.json()["project_id"])
        created_ids.append(d_pid)
        blk = client.post(
            f"/api/v1/projects/{d_pid}/zoning-precheck",
            headers={**planner_headers, "Idempotency-Key": f"hu111-noloc-{uuid4().hex[:6]}"},
        )
        assert blk.status_code == 422
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_11_provenance_gate_rejects_user_declared_public_or_ordinance_name(
    isolated_hu111_env,
) -> None:
    """
    Compuerta de procedencia en servidor:
    Cambiar data_origin a 'public' u 'official_cadastre' y escribir 'O.M. N.° 636-2020-MPH/CM' o 'ZRP'
    al subir un polígono sintético mediante POST /geospatial-layers NO convierte la capa en evidencia oficial.
    Tanto para usos afines (parque) como conflictivos (comercio), el servidor debe responder
    status='requires_review' y compatible=None sin emitir dictamen zonal.
    """
    client, conn = isolated_hu111_env
    planner_headers = _login(client, "planner@siprim.test")
    created_ids: list[UUID] = []
    try:
        scenarios = [
            ("Recreación pública y parque arborizado", "public"),
            ("Comercio intensivo y edificación comercial privada", "official_cadastre"),
        ]
        for land_use, claimed_origin in scenarios:
            created = client.post(
                "/api/v1/projects",
                headers=planner_headers,
                json={
                    "title": f"Prueba Spoofing Procedencia ({claimed_origin})",
                    "description": "Expediente con polígono sintético subido por usuario.",
                    "location": "Sector Norte, El Tambo - Huancayo",
                    "proposed_land_use": land_use,
                    "latitude": -12.0545,
                    "longitude": -75.2185,
                    "coordinates_origin": "official_cadastre",
                    "coordinates_source": "Texto libre del usuario",
                    "estimated_budget_pen": 150000,
                    "beneficiaries_count": 400,
                },
            )
            assert created.status_code == 201
            pid = UUID(created.json()["project_id"])
            created_ids.append(pid)

            layer_resp = client.post(
                f"/api/v1/projects/{pid}/geospatial-layers",
                headers=planner_headers,
                json={
                    "layer_type": "territorial_zone",
                    "layer_name": "zonificacion_ZRP_O.M_636_2020_MPH_CM",
                    "meaning": "Zona de Recreación Pública ZRP declarada por planificador",
                    "source_name": "PDM Huancayo 2017-2037 (O.M. N.° 636-2020-MPH/CM)",
                    "data_origin": claimed_origin,
                    "availability_status": "available",
                    "geojson_data": _synthetic_polygon_geojson(),
                },
            )
            assert layer_resp.status_code == 201, layer_resp.text

            z_resp = client.post(
                f"/api/v1/projects/{pid}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-spoof-{uuid4().hex[:8]}"},
            )
            assert z_resp.status_code == 202, z_resp.text
            z = z_resp.json()
            assert z["status"] == "requires_review"
            assert z["compatible"] is None
            assert z["flagged_for_legal_review"] is False
            assert "verificación oficial independiente" in z["limitations"].lower()
            assert "data_origin='public'" in z["limitations"]

            # Confirmar en BD que no se persistió source_document/compatible falsos
            db_row = conn.execute(
                "SELECT status, compatible, source_document FROM zoning_review_requests WHERE id = %s",
                (UUID(z["review_id"]),),
            ).fetchone()
            assert db_row == ("requires_review", None, None)
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_11_partial_modifications_scope_and_abstention_on_location_uncertainty(
    isolated_hu111_env,
) -> None:
    """
    Vigencia por ámbito y predio:
    - O.M. N.° 636-2020-MPH/OM permanece vigente como norma base distrital sin desactivarse globalmente.
    - Ordenanzas 836, 838 y 839 de 2026 (y demás modificaciones de El Tambo) se registran con fecha, relación,
      alcance específico y URL verificable en gob.pe, marcando posible coincidencia únicamente en su sector/vía.
    - Aun con capa verificada previa a 2026, si existe una modificación parcial coincidente con la ubicación
      y no puede determinarse su alcance predial exacto, el servidor se abstiene (compatible=None).
    """
    client, conn = isolated_hu111_env
    planner_headers = _login(client, "planner@siprim.test")
    created_ids: list[UUID] = []
    try:
        # 1. Proyecto en Av. Progreso -> coincide solo con O.M. 836-2026-MPH/CM, no con 838 ni 839
        p1 = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Mejoramiento Av. Progreso El Tambo",
                "description": "Obra urbana en Av. Progreso.",
                "location": "Av. Progreso cdra. 4, El Tambo - Huancayo",
                "proposed_land_use": "Comercio Vecinal y equipamiento",
                "latitude": -12.0545,
                "longitude": -75.2185,
                "coordinates_origin": "field_survey",
                "coordinates_source": "GPS campo",
                "estimated_budget_pen": 220000,
                "beneficiaries_count": 750,
            },
        )
        assert p1.status_code == 201
        pid1 = UUID(p1.json()["project_id"])
        created_ids.append(pid1)

        z1 = client.post(
            f"/api/v1/projects/{pid1}/zoning-precheck",
            headers={**planner_headers, "Idempotency-Key": f"hu111-mod1-{uuid4().hex[:8]}"},
        ).json()
        assert z1["compatible"] is None
        assert "836-2026-MPH/CM" in z1["limitations"]

        by_ord = {a["ordinance"]: a for a in z1["normative_amendments"]}
        assert by_ord["O.M. N.° 636-2020-MPH/OM"]["applicability_status"] == "base_vigente"
        assert by_ord["O.M. N.° 836-2026-MPH/CM"]["applicability_status"] == "posible_coincidencia_pendiente_catastro"
        assert by_ord["O.M. N.° 836-2026-MPH/CM"]["publication_date"] == "2026-05-13"
        assert by_ord["O.M. N.° 836-2026-MPH/CM"]["official_url"] == (
            "https://www.gob.pe/institucion/munihuancayo/normas-legales/8165508-836-2026-mph-cm"
        )
        # 838 y 839 no se generalizan a Av. Progreso
        assert by_ord["O.M. N.° 838-2026-MPH/CM"]["applicability_status"] == "otro_predio_sector_el_tambo"
        assert by_ord["O.M. N.° 839-2026-MPH/CM"]["applicability_status"] == "otro_predio_sector_el_tambo"

        # 2. Aun con una capa base 2020 en el registro institucional (simulada en prueba unitaria),
        #    si la ubicación coincide con Av. Mariátegui / Francisca de la Calle (O.M. 839-2026-MPH/CM del 2026-05-13),
        #    no puede determinarse si la modificación alcanza al predio y debe abstenerse.
        poly = _synthetic_polygon_geojson()
        poly_hash = PlatformService._canonical_geojson_sha256(poly)
        p2 = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Parque sector Av. Mariátegui y Francisca de la Calle",
                "description": "Evaluación sujeta a modificación parcial 839-2026.",
                "location": "Av. Mariátegui y Av. Francisca de la Calle, El Tambo",
                "proposed_land_use": "Recreación pública y parque",
                "latitude": -12.0545,
                "longitude": -75.2185,
                "coordinates_origin": "official_cadastre",
                "coordinates_source": "Fixture unitario controlado",
                "estimated_budget_pen": 120000,
                "beneficiaries_count": 500,
            },
        )
        pid2 = UUID(p2.json()["project_id"])
        created_ids.append(pid2)
        client.post(
            f"/api/v1/projects/{pid2}/geospatial-layers",
            headers=planner_headers,
            json={
                "layer_type": "territorial_zone",
                "layer_name": "zrp_base_2020_fixture",
                "meaning": "Fixture base 2020",
                "source_name": "Fixture controlado",
                "data_origin": "public",
                "geojson_data": poly,
            },
        )

        verified_fixture = {
            poly_hash: {
                "zone_code": "ZRP",
                "effective_date": "2020-05-08",
                "official_source_name": "Plano base PDM 2020 (Fixture unitario)",
                "source_document": "PDM Huancayo 2017-2037 (O.M. N.° 636-2020-MPH/CM)",
                "source_version": "2020-OM636",
            }
        }
        with patch.dict(PlatformService._VERIFIED_OFFICIAL_ZONING_LAYERS, verified_fixture, clear=True):
            z2 = client.post(
                f"/api/v1/projects/{pid2}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-mod2-{uuid4().hex[:8]}"},
            ).json()
        assert z2["status"] == "requires_review"
        assert z2["compatible"] is None
        assert "839-2026-MPH/CM" in z2["limitations"]
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_11_water_threshold_50m_and_unbacked_abbreviations_abstain_with_risk_notice(
    isolated_hu111_env,
) -> None:
    """
    - El umbral de 50 m de proximidad a cuerpos de agua NO certifica compatibilidad ni incompatibilidad
      de zonificación sin delimitación oficial ANA ni polígono de riesgo verificado: informa riesgo y revisión humana.
    - Abreviaturas urbanas como 'RDM' sin Índice de Usos oficial parametrizado en repositorio también se abstienen.
    """
    client, conn = isolated_hu111_env
    planner_headers = _login(client, "planner@siprim.test")
    created_ids: list[UUID] = []
    try:
        created = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Obra ribereña con alerta hídrica sin polígono ANA",
                "description": "Verificación de riesgo hídrico sin umbral 50m automático.",
                "location": "Sector Centro Urbano, El Tambo - Huancayo",
                "proposed_land_use": "Equipamiento urbano vecinal",
                "latitude": -12.0545,
                "longitude": -75.2185,
                "coordinates_origin": "official_cadastre",
                "coordinates_source": "Catastro de prueba",
                "estimated_budget_pen": 250000,
                "beneficiaries_count": 700,
            },
        )
        pid = UUID(created.json()["project_id"])
        created_ids.append(pid)
        pv_id = UUID(created.json()["project_version_id"])

        # Registro ambiental con proximity_to_water_meters < 50 m pero SIN polígono oficial ANA/PDM
        conn.execute(
            """
            INSERT INTO project_environmental_records (
                id, project_id, project_code, project_version_id, has_sufficient_data,
                environmental_component, verifiable_evidence, proximity_to_water_meters,
                is_in_protected_or_risk_area, source_name, data_origin, description
            ) VALUES (%s, %s, %s, %s, TRUE, %s, %s, 25.0, TRUE, %s, 'public', %s)
            """,
            (
                uuid4(),
                pid,
                created.json()["code"],
                pv_id,
                "recurso_hidrico",
                "Registro de cercanía declarada a cauce de 25 m sin plano de faja marginal ANA.",
                "Inspección preliminar",
                "Proximidad a cauce requiere delimitación ANA",
            ),
        )

        # Incluso si existiera polígono 'RDM' verificado, ni '50 m' ni abreviatura 'RDM' certifican compatibilidad/incompatibilidad
        poly = _synthetic_polygon_geojson()
        poly_hash = PlatformService._canonical_geojson_sha256(poly)
        client.post(
            f"/api/v1/projects/{pid}/geospatial-layers",
            headers=planner_headers,
            json={
                "layer_type": "territorial_zone",
                "layer_name": "rdm_fixture",
                "meaning": "Fixture RDM",
                "source_name": "Fixture RDM",
                "data_origin": "public",
                "geojson_data": poly,
            },
        )
        verified_rdm = {
            poly_hash: {
                "zone_code": "RDM",
                "effective_date": "2026-06-01",
                "official_source_name": "Fixture RDM sin Índice de Usos",
                "source_document": "PDM Huancayo 2017-2037 (O.M. N.° 636-2020-MPH/OM)",
                "source_version": "2020-OM636",
            }
        }
        with patch.dict(PlatformService._VERIFIED_OFFICIAL_ZONING_LAYERS, verified_rdm, clear=True):
            z = client.post(
                f"/api/v1/projects/{pid}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-50m-{uuid4().hex[:8]}"},
            ).json()

        assert z["status"] == "requires_review"
        assert z["compatible"] is None
        assert z["environmental_risk_notice"] is not None
        assert "50 m" in z["environmental_risk_notice"]
        assert "ana" in z["environmental_risk_notice"].lower()
        assert "índice de usos" in z["limitations"].lower()
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_11_synthetic_geometry_unit_and_controlled_zrp_version_isolation(
    isolated_hu111_env,
) -> None:
    """
    Pruebas geométricas sintéticas y de aislamiento por versión:
    1. Prueba unitaria pura de geometría ray-casting sobre polígono con hueco y MultiPolygon (solo matemática espacial).
    2. Regla ZRP respaldada en PDUPDM-01/PDUPDM-06 bajo registro oficial simulado en fixture aislado
       + verificación estricta de aislamiento entre v1 y v2 (v2 NO hereda dictamen ni capa de v1).
    """
    # 1. Prueba geométrica sintética pura (NO representa evidencia territorial real)
    poly_with_hole = {
        "type": "Polygon",
        "coordinates": [
            [[-75.23, -12.06], [-75.20, -12.06], [-75.20, -12.04], [-75.23, -12.04], [-75.23, -12.06]],
            [[-75.22, -12.055], [-75.21, -12.055], [-75.21, -12.045], [-75.22, -12.045], [-75.22, -12.055]],
        ],
    }
    assert PlatformService._match_geojson_feature(poly_with_hole, -75.225, -12.05) is not None
    assert PlatformService._match_geojson_feature(poly_with_hole, -75.215, -12.05) is None  # dentro del hueco
    assert PlatformService._match_geojson_feature(poly_with_hole, -75.250, -12.05) is None  # fuera

    # 2. Aislamiento por versión con fixture controlado en memoria para ZRP intangible (PDUPDM-06)
    client, conn = isolated_hu111_env
    planner_headers = _login(client, "planner@siprim.test")
    legal_headers = _login(client, "legal@siprim.test")
    created_ids: list[UUID] = []
    try:
        poly = _synthetic_polygon_geojson()
        poly_hash = PlatformService._canonical_geojson_sha256(poly)
        created = client.post(
            "/api/v1/projects",
            headers=planner_headers,
            json={
                "title": "Edificación comercial en ZRP (Fixture unitario controlado)",
                "description": "Prueba unitaria de regla ZRP y aislamiento v1/v2.",
                "location": "Sector Parque Norte sin modificaciones, El Tambo",
                "proposed_land_use": "Comercio intensivo y edificación privada",
                "latitude": -12.0545,
                "longitude": -75.2185,
                "coordinates_origin": "official_cadastre",
                "coordinates_source": "Fixture unitario",
                "estimated_budget_pen": 300000,
                "beneficiaries_count": 800,
            },
        )
        pid = UUID(created.json()["project_id"])
        created_ids.append(pid)
        client.post(
            f"/api/v1/projects/{pid}/geospatial-layers",
            headers=planner_headers,
            json={
                "layer_type": "territorial_zone",
                "layer_name": "zrp_fixture_unit",
                "meaning": "Polígono sintético ZRP para prueba unitaria aislada",
                "source_name": "Fixture unitario controlado",
                "data_origin": "public",
                "geojson_data": poly,
            },
        )

        verified_zrp = {
            poly_hash: {
                "zone_code": "ZRP",
                "effective_date": "2026-06-01",
                "official_source_name": "Fixture unitario ZRP (no oficial real)",
                "source_document": "PDM Huancayo 2017-2037 (O.M. N.° 636-2020-MPH/CM)",
                "source_version": "2020-OM636",
            }
        }
        with patch.dict(PlatformService._VERIFIED_OFFICIAL_ZONING_LAYERS, verified_zrp, clear=True):
            z_v1 = client.post(
                f"/api/v1/projects/{pid}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-u-v1-{uuid4().hex[:8]}"},
            ).json()

        assert z_v1["status"] == "evidence_available"
        assert z_v1["compatible"] is False
        assert z_v1["flagged_for_legal_review"] is True
        assert "PDUPDM-06" in z_v1["evidence"][0]["locator"]
        assert "no certifica automáticamente una infracción" in z_v1["limitations"].lower()

        ev_action = conn.execute(
            "SELECT action FROM audit_events WHERE entity_id = %s",
            (UUID(z_v1["review_id"]),),
        ).fetchone()
        assert ev_action is not None and ev_action[0] == "ZONING_INCOMPATIBILITY_FLAGGED"

        legal_z = client.get(f"/api/v1/projects/{pid}/zoning-precheck", headers=legal_headers).json()
        assert legal_z["compatible"] is False

        # Crear v2 -> NO hereda capa ni dictamen de v1
        v2 = client.put(
            f"/api/v1/projects/{pid}",
            headers=planner_headers,
            json={"proposed_land_use": "Recreación pública", "create_new_version": True, "save_as_draft": False},
        ).json()
        assert v2["version_number"] == 2
        assert v2["zoning_precheck"] is None

        z_v2 = client.post(
            f"/api/v1/projects/{pid}/zoning-precheck",
            headers={**planner_headers, "Idempotency-Key": f"hu111-u-v2-{uuid4().hex[:8]}"},
        ).json()
        assert z_v2["status"] == "requires_review"
        assert z_v2["compatible"] is None
    finally:
        for pid in created_ids:
            _cleanup_project(conn, pid)


def test_hu1_11_official_gob_pe_urls_catalog_integrity() -> None:
    """Verifica que las URLs oficiales mostradas correspondan a publicaciones reales de MPH en gob.pe."""
    assert PlatformService._PDM_OFFICIAL_URL == (
        "https://www.gob.pe/institucion/munihuancayo/normas-legales/838304-636-2020-mph-om"
    )
    assert "1352467" not in PlatformService._PDM_OFFICIAL_URL
    assert "1085223" not in PlatformService._PDM_OFFICIAL_URL

    by_ord = {item["ordinance"]: item for item in PlatformService._PDM_MODIFICATION_ORDINANCES}
    assert by_ord["O.M. N.° 836-2026-MPH/CM"]["official_url"] == (
        "https://www.gob.pe/institucion/munihuancayo/normas-legales/8165508-836-2026-mph-cm"
    )
    assert by_ord["O.M. N.° 838-2026-MPH/CM"]["official_url"] == (
        "https://www.gob.pe/institucion/munihuancayo/normas-legales/8165556-838-2026-mph-cm"
    )
    assert by_ord["O.M. N.° 839-2026-MPH/CM"]["official_url"] == (
        "https://www.gob.pe/institucion/munihuancayo/normas-legales/8165564-839-2026-mph-cm"
    )
    for item in PlatformService._PDM_MODIFICATION_ORDINANCES:
        assert item["official_url"].startswith("https://www.gob.pe/institucion/munihuancayo/")
        assert item["publication_date"]
        assert item["relation_to_base"]
        assert item["scope_summary"]


def test_hu1_11_zoning_catalog_validator_enforces_crs_bounds_and_zone_rules() -> None:
    """
    Valida el pipeline reproducible para incorporar futuras fuentes oficiales (CRS, cobertura, SHA-256 y reglas de uso).
    """
    meta, fc = build_synthetic_rehearsal_zoning_package()
    summary = ZoningSourceValidator.validate_package(meta, fc)
    assert summary["feature_count"] == 1
    assert summary["zones_indexed"] == ["ZRP"]

    # Rechaza coordenadas proyectadas UTM (no WGS84 EPSG:4326)
    utm_geojson = {
        "type": "Polygon",
        "coordinates": [[[475000, 8665000], [476000, 8665000], [476000, 8666000], [475000, 8666000], [475000, 8665000]]],
    }
    with pytest.raises(ZoningSourceValidationError, match="WGS84"):
        ZoningSourceValidator.validate_geometry_wgs84(utm_geojson, EL_TAMBO_AMH_ENVELOPE)

    # Rechaza polígono fuera del ámbito territorial declarado
    outside_envelope = {
        "type": "Polygon",
        "coordinates": [[[-77.05, -12.06], [-77.04, -12.06], [-77.04, -12.05], [-77.05, -12.05], [-77.05, -12.06]]],
    }
    with pytest.raises(ZoningSourceValidationError, match="cobertura"):
        ZoningSourceValidator.validate_geometry_wgs84(outside_envelope, EL_TAMBO_AMH_ENVELOPE)

    # Rechaza paquete oficial sin URL HTTPS ni SHA-256 válido
    bad_official_meta = OfficialZoningSourceMetadata(
        source_id="BAD-OFFICIAL",
        issuer_entity="MPH",
        document_title="Plano sin HTTPS",
        ordinance="O.M. N.° 636-2020-MPH/OM",
        publication_date="2020-05-08",
        version="2020",
        coverage_area="El Tambo",
        coverage_bbox=EL_TAMBO_AMH_ENVELOPE,
        crs="EPSG:4326",
        official_url="http://inseguro.local/plano.geojson",
        usage_terms="Uso público",
        effective_date="2020-05-08",
        is_synthetic=False,
        expected_sha256="deadbeef",
        zone_rules=meta.zone_rules,
    )
    with pytest.raises(ZoningSourceValidationError, match="HTTPS"):
        ZoningSourceValidator.validate_package(bad_official_meta, fc)


def test_hu1_11_controlled_synthetic_rehearsal_mode_and_out_of_coverage_isolation() -> None:
    """
    Mecanismo espacial completo con datos SINTÉTICOS claramente etiquetados:
    - Por defecto (enable_synthetic_zoning_rehearsal=False) sigue absteniéndose incluso ante el polígono sintético.
    - Con enable_synthetic_zoning_rehearsal=True y capa 'simulated':
      1) Uso compatible dentro de ZRP sintético -> compatible=True + is_synthetic_data=True + banner sintético.
      2) Uso incompatible dentro de ZRP sintético -> compatible=False + revisión jurídica + is_synthetic_data=True.
      3) Punto fuera del polígono sintético -> compatible=None + coverage_status='outside_coverage'.
      4) Capa usuario con data_origin='public' -> sigue rechazada (nunca pasa por oficial ni por sintética).
    """
    settings = get_settings()
    p_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    _, synthetic_fc = build_synthetic_rehearsal_zoning_package()

    # 1. Modo estricto por defecto: se abstiene también ante capa simulated sintética
    strict_app = create_app()
    strict_svc = PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=None,  # type: ignore
        enable_demo_fallback=False,
        auth_db_url=settings.auth_db_url,
        enable_synthetic_zoning_rehearsal=False,
    )
    strict_app.dependency_overrides[get_platform_service] = lambda: strict_svc

    # 2. App de ensayo técnico sintético controlado
    synth_app = create_app()
    synth_svc = PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=None,  # type: ignore
        enable_demo_fallback=False,
        auth_db_url=settings.auth_db_url,
        enable_synthetic_zoning_rehearsal=True,
    )
    synth_app.dependency_overrides[get_platform_service] = lambda: synth_svc

    created_ids: list[UUID] = []
    with (
        TestClient(strict_app) as strict_client,
        TestClient(synth_app) as synth_client,
        psycopg.connect(p_url, autocommit=True) as conn,
    ):
        try:
            planner_headers = _login(synth_client, "planner@siprim.test")

            def _create_with_layer(
                *,
                title: str,
                land_use: str,
                lat: float,
                lon: float,
                origin: str,
            ) -> UUID:
                c_resp = synth_client.post(
                    "/api/v1/projects",
                    headers=planner_headers,
                    json={
                        "title": title,
                        "description": "Prueba controlada de mecanismo espacial HU1.11.",
                        "location": "Sector Parque Norte sin modificaciones, El Tambo",
                        "proposed_land_use": land_use,
                        "latitude": lat,
                        "longitude": lon,
                        "coordinates_origin": "field_survey" if origin != "simulated" else "simulated",
                        "coordinates_source": "Coordenada de prueba controlada",
                        "estimated_budget_pen": 180000,
                        "beneficiaries_count": 500,
                    },
                )
                assert c_resp.status_code == 201, c_resp.text
                pid = UUID(c_resp.json()["project_id"])
                created_ids.append(pid)
                l_resp = synth_client.post(
                    f"/api/v1/projects/{pid}/geospatial-layers",
                    headers=planner_headers,
                    json={
                        "layer_type": "territorial_zone",
                        "layer_name": "ensayo_sintetico_zrp",
                        "meaning": "Polígono sintético de prueba técnica ZRP",
                        "source_name": "Ensayo técnico controlado",
                        "data_origin": origin,
                        "availability_status": "available",
                        "geojson_data": _sanitized_geometry(synthetic_fc),
                    },
                )
                assert l_resp.status_code == 201, l_resp.text
                return pid

            # Caso compatible sintético (dentro del polígono: -12.0545, -75.2185)
            pid_comp = _create_with_layer(
                title="Parque Arborizado (Ensayo Sintético HU1.11)",
                land_use="Recreación pública y parque arborizado",
                lat=-12.0545,
                lon=-75.2185,
                origin="simulated",
            )
            # Confirmar primero que en modo estricto por defecto SE ABSTIENE
            z_strict = strict_client.post(
                f"/api/v1/projects/{pid_comp}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-str-{uuid4().hex[:8]}"},
            ).json()
            assert z_strict["compatible"] is None
            assert z_strict["status"] == "requires_review"
            assert z_strict["is_synthetic_data"] is False

            # Crear otro para ensayo sintético compatible (vía nueva idempotency)
            pid_comp2 = _create_with_layer(
                title="Parque Vecinal (Ensayo Sintético Compatible)",
                land_use="Recreación pública y parque arborizado",
                lat=-12.0545,
                lon=-75.2185,
                origin="simulated",
            )
            z_comp = synth_client.post(
                f"/api/v1/projects/{pid_comp2}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-syn-ok-{uuid4().hex[:8]}"},
            ).json()
            assert z_comp["compatible"] is True
            assert z_comp["coverage_status"] == "inside_coverage"
            assert z_comp["is_synthetic_data"] is True
            assert z_comp["provenance_mode"] == "synthetic_rehearsal"
            assert "DATOS SINTÉTICOS DE PRUEBA TÉCNICA" in z_comp["preliminary_conclusion"]

            # Caso incompatible sintético -> deriva a revisión jurídica + etiqueta sintética
            pid_inc = _create_with_layer(
                title="Centro Comercial en ZRP (Ensayo Sintético Incompatible)",
                land_use="Comercio intensivo y edificación privada",
                lat=-12.0545,
                lon=-75.2185,
                origin="simulated",
            )
            z_inc = synth_client.post(
                f"/api/v1/projects/{pid_inc}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-syn-bad-{uuid4().hex[:8]}"},
            ).json()
            assert z_inc["compatible"] is False
            assert z_inc["flagged_for_legal_review"] is True
            assert z_inc["is_synthetic_data"] is True
            assert z_inc["coverage_status"] == "inside_coverage"

            # Caso fuera de cobertura (-12.0200, -75.1800 está fuera del cuadro [-75.23..-75.205, -12.065..-12.045])
            pid_out = _create_with_layer(
                title="Predio Fuera de Cobertura (Ensayo Sintético HU1.11)",
                land_use="Recreación pública",
                lat=-12.0200,
                lon=-75.1800,
                origin="simulated",
            )
            z_out = synth_client.post(
                f"/api/v1/projects/{pid_out}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-syn-out-{uuid4().hex[:8]}"},
            ).json()
            assert z_out["compatible"] is None
            assert z_out["status"] == "requires_review"
            assert z_out["coverage_status"] == "outside_coverage"
            assert z_out["is_synthetic_data"] is True

            # Spoofing con data_origin='public' sigue rechazado incluso cuando rehearsal está activo
            pid_spoof = _create_with_layer(
                title="Intento Public Falso con Rehearsal Activo",
                land_use="Recreación pública y parque arborizado",
                lat=-12.0545,
                lon=-75.2185,
                origin="public",
            )
            z_spoof = synth_client.post(
                f"/api/v1/projects/{pid_spoof}/zoning-precheck",
                headers={**planner_headers, "Idempotency-Key": f"hu111-spoof-ok-{uuid4().hex[:8]}"},
            ).json()
            assert z_spoof["compatible"] is None
            assert z_spoof["status"] == "requires_review"
            assert z_spoof["coverage_status"] == "not_available"
            assert z_spoof["is_synthetic_data"] is False
        finally:
            for pid in created_ids:
                _cleanup_project(conn, pid)
