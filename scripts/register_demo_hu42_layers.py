"""Registra la capa distrital INEI (UBIGEO 120114) en una base de demostracion o preview.

Uso explicito (nunca se ejecuta automaticamente en produccion):
    python scripts/register_demo_hu42_layers.py --project-code DEMO-DIST-01 --allow-demo-write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

from backend.application.dto.pmv1_dto import GeospatialLayerCreate
from backend.infrastructure.config.dependencies import get_platform_service
from backend.infrastructure.config.settings import get_settings

GEO_DIR = Path(__file__).resolve().parent.parent / "database" / "geospatial" / "inei_el_tambo_120114"
GEOJSON_FILE = GEO_DIR / "inei_el_tambo_120114_siprim.geojson"
METADATA_FILE = GEO_DIR / "metadata.json"
DEFAULT_PLANNER_ID = UUID("10000000-0000-4000-a000-000000000002")


def load_verified_inei_geometry() -> tuple[dict, dict]:
    meta = json.loads(METADATA_FILE.read_text(encoding="utf-8"))
    raw_bytes = GEOJSON_FILE.read_bytes()
    actual_sha = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha != meta["simplified_sha256"]:
        raise RuntimeError(
            f"SHA-256 invalido para {GEOJSON_FILE.name}: esperado {meta['simplified_sha256']}, obtenido {actual_sha}"
        )
    return json.loads(raw_bytes.decode("utf-8")), meta


def main() -> None:
    parser = argparse.ArgumentParser(description="Registrar capa INEI El Tambo en base demo/preview")
    parser.add_argument("--project-code", required=True, help="Codigo del expediente [DEMO] con alcance distrital")
    parser.add_argument("--actor-id", default=str(DEFAULT_PLANNER_ID), help="UUID del usuario PLANNER que registra")
    parser.add_argument("--allow-demo-write", action="store_true", help="Confirmacion explicita de escritura demo/preview")
    args = parser.parse_args()

    if not args.allow_demo_write:
        raise SystemExit("Debe pasar --allow-demo-write para ejecutar este registro.")
    if os.environ.get("APP_ENV", "").lower() == "production":
        raise SystemExit("Prohibido ejecutar semillas/registros demo en APP_ENV=production.")

    settings = get_settings()
    if settings.app_env.lower() == "production":
        raise SystemExit("Prohibido ejecutar semillas/registros demo en configuracion production.")

    geojson_data, meta = load_verified_inei_geometry()
    service = get_platform_service()
    project = service.get_project_by_code(args.project_code)
    existing = service.get_project_geospatial_layers(project["project_id"])
    source_tag = "INEI GeoServer Interoperabilidad (ig_distrito, UBIGEO 120114"
    if any(source_tag in layer["source_name"] for layer in existing["layers"]):
        print(f"La capa INEI 120114 ya existe para {args.project_code} (v{project['version_number']}).")
        return

    payload = GeospatialLayerCreate(
        layer_type="beneficiary_coverage",
        layer_name="[DEMO] Cobertura territorial declarada del servicio distrital (El Tambo, UBIGEO 120114)",
        meaning=(
            "Zona de cobertura potencial declarada del servicio municipal distrital; "
            "no identifica beneficiarios efectivos ni constituye demarcacion catastral oficial."
        ),
        source_name=(
            f"INEI GeoServer Interoperabilidad (ig_distrito, UBIGEO {meta['ubigeo']}, "
            f"reg. {meta['source_record_date'][:10]}, consulta 2026-11-18, cartografia referencial); "
            f"alcance declarado en expediente {args.project_code}."
        ),
        data_origin="public",
        availability_status="available",
        geojson_data=geojson_data,
        aggregate_beneficiaries_count=None,
    )
    created = service.register_geospatial_layer(
        project_id=project["project_id"],
        data=payload,
        actor_id=UUID(args.actor_id),
    )
    print(f"Capa registrada: {created['layer_id']} en {args.project_code} v{project['version_number']}")


if __name__ == "__main__":
    main()
