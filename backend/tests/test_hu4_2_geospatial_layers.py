from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.application.dto.pmv1_dto import GeospatialLayerCreate
from backend.application.services.pmv1_services import PlatformService
from backend.infrastructure.config.dependencies import get_current_user, get_platform_service
from backend.main import create_app


VALID_POLYGON = {
    "type": "Polygon",
    "coordinates": [
        [
            [-75.241795, -12.036248],
            [-75.233532, -12.036248],
            [-75.233532, -12.030720],
            [-75.241795, -12.030720],
            [-75.241795, -12.036248],
        ]
    ],
    "properties": {"dni_beneficiario": "12345678", "nombre": "Dato personal que debe eliminarse"},
}


def test_hu4_2_geospatial_dto_validation_and_privacy_sanitization():
    # Valido: sanitiza y elimina properties (sin datos personales)
    layer = GeospatialLayerCreate(
        layer_type="influence_area",
        layer_name="Campus UNCP El Tambo",
        meaning="Poligono del campus universitario beneficiado",
        source_name="OpenStreetMap Nominatim (way/1022155495)",
        data_origin="public",
        availability_status="available",
        geojson_data=VALID_POLYGON,
        aggregate_beneficiaries_count=9400,
    )
    assert set(layer.geojson_data.keys()) == {"type", "coordinates"}
    assert "properties" not in layer.geojson_data

    # Valido: capa no disponible sin geometria ficticia
    unavail = GeospatialLayerCreate(
        layer_type="beneficiary_coverage",
        layer_name="Cobertura sin levantamiento",
        meaning="Sin poligono levantado",
        source_name="Pendiente de catastro",
        availability_status="unavailable",
        geojson_data=None,
    )
    assert unavail.geojson_data is None

    # Invalido: poligono no cerrado
    with pytest.raises(ValidationError):
        GeospatialLayerCreate(
            layer_type="influence_area",
            layer_name="Poligono abierto",
            meaning="Poligono sin cierre",
            source_name="Fuente test",
            geojson_data={
                "type": "Polygon",
                "coordinates": [[[-75.2, -12.0], [-75.1, -12.0], [-75.1, -12.1], [-75.2, -12.1]]],
            },
        )

    # Invalido: coordenadas fuera de rango
    with pytest.raises(ValidationError):
        GeospatialLayerCreate(
            layer_type="influence_area",
            layer_name="Fuera de rango",
            meaning="Latitud invalida",
            source_name="Fuente test",
            geojson_data={"type": "Point", "coordinates": [-75.2, -95.0]},
        )

    # Invalido: excede limite de vertices (> 500)
    many_pts = [[-75.2 + i * 0.0001, -12.0] for i in range(505)]
    with pytest.raises(ValidationError):
        GeospatialLayerCreate(
            layer_type="territorial_zone",
            layer_name="Exceso de vertices",
            meaning="Geometria demasiado grande",
            source_name="Fuente test",
            geojson_data={"type": "LineString", "coordinates": many_pts},
        )


def test_hu4_2_service_empty_geometry_never_invents_polygon():
    pid = uuid4()
    vid = uuid4()
    conn = MagicMock()
    cur_ver = MagicMock()
    cur_ver.fetchone.return_value = {"id": vid, "version_number": 1}
    cur_layers = MagicMock()
    cur_layers.fetchall.return_value = []
    conn.execute.side_effect = [cur_ver, cur_layers]
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False

    svc = PlatformService(db_url="postgresql://unused", dispatcher=None, enable_demo_fallback=False)
    with (
        patch.object(svc, "get_project", return_value={"project_id": pid, "project_version_id": vid, "code": "EXP-SIN-GEO"}),
        patch("backend.application.services.pmv1_services.psycopg.connect", return_value=ctx),
    ):
        res = svc.get_project_geospatial_layers(pid)

    assert res["has_available_geometry"] is False
    assert res["layers"] == []
    assert "ficticia" in res["empty_message"].lower()
    assert "personales" in res["privacy_notice"].lower()


def test_hu4_2_geospatial_endpoints_permissions():
    app = create_app()
    pid = uuid4()
    vid = uuid4()
    fake_svc = MagicMock()
    fake_svc.get_project_geospatial_layers.return_value = {
        "project_id": pid,
        "project_code": "DEMO-01",
        "project_version_id": vid,
        "version_number": 1,
        "has_available_geometry": True,
        "layers": [],
        "empty_message": None,
        "privacy_notice": "Sin datos personales.",
    }
    fake_svc.register_geospatial_layer.return_value = {
        "layer_id": uuid4(),
        "project_version_id": vid,
        "layer_type": "influence_area",
        "layer_type_label": "Area de influencia",
        "layer_name": "Campus UNCP",
        "meaning": "Area oficial",
        "source_name": "OSM way/1022155495",
        "data_origin": "public",
        "availability_status": "available",
        "geometry_type": "Polygon",
        "geojson_data": {"type": "Polygon", "coordinates": VALID_POLYGON["coordinates"]},
        "aggregate_beneficiaries_count": 9400,
        "created_at": datetime.now(timezone.utc),
    }
    app.dependency_overrides[get_platform_service] = lambda: fake_svc

    payload = {
        "layer_type": "influence_area",
        "layer_name": "Campus UNCP",
        "meaning": "Area de influencia oficial",
        "source_name": "OpenStreetMap Nominatim (way/1022155495)",
        "data_origin": "public",
        "geojson_data": VALID_POLYGON,
    }

    # 1. Sin sesion -> 401
    with TestClient(app) as client:
        assert client.get(f"/api/v1/projects/{pid}/geospatial-layers").status_code == 401
        assert client.post(f"/api/v1/projects/{pid}/geospatial-layers", json=payload).status_code == 401

    # 2. CITIZEN y LEGAL_ADVISOR -> 403 en lectura y escritura
    for role in ("CITIZEN", "LEGAL_ADVISOR"):
        app.dependency_overrides[get_current_user] = lambda r=role: {"sub": str(uuid4()), "roles": [r]}
        with TestClient(app) as client:
            assert client.get(f"/api/v1/projects/{pid}/geospatial-layers").status_code == 403
            assert client.post(f"/api/v1/projects/{pid}/geospatial-layers", json=payload).status_code == 403

    # 3. MAYOR lee (200), pero no puede registrar capas (403)
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid4()), "roles": ["MAYOR"]}
    with TestClient(app) as client:
        assert client.get(f"/api/v1/projects/{pid}/geospatial-layers").status_code == 200
        assert client.post(f"/api/v1/projects/{pid}/geospatial-layers", json=payload).status_code == 403

    # 4. PLANNER puede registrar (201) y consultar (200)
    app.dependency_overrides[get_current_user] = lambda: {"sub": str(uuid4()), "roles": ["PLANNER"]}
    with TestClient(app) as client:
        assert client.get(f"/api/v1/projects/{pid}/geospatial-layers").status_code == 200
        assert client.post(f"/api/v1/projects/{pid}/geospatial-layers", json=payload).status_code == 201


def test_hu4_2_inei_el_tambo_simplified_geojson_passes_validation_and_hash():
    import hashlib
    import json
    from pathlib import Path

    geo_dir = Path(__file__).resolve().parents[2] / "database" / "geospatial" / "inei_el_tambo_120114"
    meta = json.loads((geo_dir / "metadata.json").read_text(encoding="utf-8"))
    raw_bytes = (geo_dir / "inei_el_tambo_120114_siprim.geojson").read_bytes()
    assert hashlib.sha256(raw_bytes).hexdigest() == meta["simplified_sha256"]

    geojson_obj = json.loads(raw_bytes.decode("utf-8"))
    layer = GeospatialLayerCreate(
        layer_type="beneficiary_coverage",
        layer_name="[DEMO] Cobertura territorial declarada del servicio distrital (El Tambo, UBIGEO 120114)",
        meaning="Zona de cobertura potencial declarada; no identifica beneficiarios efectivos.",
        source_name="INEI GeoServer Interoperabilidad (ig_distrito, UBIGEO 120114, consulta 2026-11-18)",
        data_origin="public",
        availability_status="available",
        geojson_data=geojson_obj,
        aggregate_beneficiaries_count=None,
    )
    assert layer.geojson_data["type"] == "MultiPolygon"
    assert len(layer.geojson_data["coordinates"][0][0]) == meta["simplified_vertices"]
