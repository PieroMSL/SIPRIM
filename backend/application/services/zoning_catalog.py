from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any


EL_TAMBO_AMH_ENVELOPE: tuple[float, float, float, float] = (-75.38, -12.22, -75.06, -11.90)
ACCEPTED_CRS: frozenset[str] = frozenset({"EPSG:4326", "OGC:CRS84", "CRS84", "WGS84"})


@dataclass(frozen=True)
class OfficialZoningSourceMetadata:
    """Metadatos institucionales exigidos para incorporar una fuente espacial PDU/PDM verificable."""

    source_id: str
    issuer_entity: str
    document_title: str
    ordinance: str
    publication_date: str
    version: str
    coverage_area: str
    coverage_bbox: tuple[float, float, float, float]
    crs: str
    official_url: str | None
    usage_terms: str
    effective_date: str
    is_synthetic: bool = False
    expected_sha256: str | None = None
    zone_rules: dict[str, dict[str, Any]] = field(default_factory=dict)


class ZoningSourceValidationError(ValueError):
    """Error de validación de formato, CRS, cobertura o procedencia de capa territorial."""


class ZoningSourceValidator:
    """Validador reproducible e indexador espacial WGS84 (EPSG:4326) para capas PDU/PDM."""

    @staticmethod
    def canonical_geojson_sha256(geojson_obj: Any) -> str:
        canonical = json.dumps(
            geojson_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def ring_contains_point(ring: list[Any], lon: float, lat: float) -> bool:
        if len(ring) < 4:
            return False
        inside = False
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = float(ring[i][0]), float(ring[i][1])
            xj, yj = float(ring[j][0]), float(ring[j][1])
            intersects = ((yi > lat) != (yj > lat)) and (
                lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-18) + xi
            )
            if intersects:
                inside = not inside
            j = i
        return inside

    @classmethod
    def polygon_coords_contains(cls, coords: list[Any], lon: float, lat: float) -> bool:
        if not coords or not cls.ring_contains_point(coords[0], lon, lat):
            return False
        for hole in coords[1:]:
            if cls.ring_contains_point(hole, lon, lat):
                return False
        return True

    @classmethod
    def lookup_point(cls, geojson_obj: Any, lon: float, lat: float) -> dict[str, Any] | None:
        if not isinstance(geojson_obj, dict):
            return None
        gtype = geojson_obj.get("type")
        if gtype == "FeatureCollection":
            for feat in geojson_obj.get("features") or []:
                matched = cls.lookup_point(feat, lon, lat)
                if matched is not None:
                    return matched
            return None
        if gtype == "Feature":
            geom = geojson_obj.get("geometry")
            props = geojson_obj.get("properties")
            if cls.lookup_point(geom, lon, lat) is not None:
                return dict(props) if isinstance(props, dict) else {}
            return None
        coords = geojson_obj.get("coordinates") or []
        if gtype == "Polygon" and cls.polygon_coords_contains(coords, lon, lat):
            return {}
        if gtype == "MultiPolygon":
            for poly_coords in coords:
                if cls.polygon_coords_contains(poly_coords, lon, lat):
                    return {}
        return None

    @classmethod
    def _validate_polygon_rings(
        cls,
        poly_coords: Any,
        coverage_bbox: tuple[float, float, float, float] | None,
        bounds_acc: list[float],
    ) -> None:
        if not isinstance(poly_coords, list) or not poly_coords:
            raise ZoningSourceValidationError("Un Polygon requiere al menos un anillo exterior")
        for ring_idx, ring in enumerate(poly_coords):
            if not isinstance(ring, list) or len(ring) < 4:
                raise ZoningSourceValidationError(
                    f"Anillo #{ring_idx} inválido: se requieren al menos 4 vértices cerrados"
                )
            first, last = ring[0], ring[-1]
            if not (isinstance(first, (list, tuple)) and isinstance(last, (list, tuple))):
                raise ZoningSourceValidationError("Vértices de anillo con formato inválido")
            if float(first[0]) != float(last[0]) or float(first[1]) != float(last[1]):
                raise ZoningSourceValidationError(
                    f"Anillo #{ring_idx} no está cerrado (primer vértice distinto del último)"
                )
            for pt in ring:
                if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                    raise ZoningSourceValidationError("Coordenada GeoJSON inválida")
                lon = float(pt[0])
                lat = float(pt[1])
                if not (math.isfinite(lon) and math.isfinite(lat)):
                    raise ZoningSourceValidationError("Coordenada no finita en geometría")
                if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                    raise ZoningSourceValidationError(
                        f"Coordenada fuera de rango WGS84 EPSG:4326 ({lon}, {lat}); "
                        "reproyecte desde UTM a grados decimales antes de indexar"
                    )
                if coverage_bbox is not None:
                    min_lon, min_lat, max_lon, max_lat = coverage_bbox
                    if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                        raise ZoningSourceValidationError(
                            f"Coordenada ({lon}, {lat}) fuera de los límites declarados de cobertura {coverage_bbox}"
                        )
                bounds_acc[0] = min(bounds_acc[0], lon)
                bounds_acc[1] = min(bounds_acc[1], lat)
                bounds_acc[2] = max(bounds_acc[2], lon)
                bounds_acc[3] = max(bounds_acc[3], lat)

    @classmethod
    def validate_geometry_wgs84(
        cls,
        geojson_obj: Any,
        coverage_bbox: tuple[float, float, float, float] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(geojson_obj, dict):
            raise ZoningSourceValidationError("La capa GeoJSON debe ser un objeto JSON")

        features: list[dict[str, Any]] = []
        gtype = geojson_obj.get("type")
        if gtype == "FeatureCollection":
            raw_feats = geojson_obj.get("features")
            if not isinstance(raw_feats, list) or not raw_feats:
                raise ZoningSourceValidationError("FeatureCollection vacía")
            features = raw_feats
        elif gtype == "Feature":
            features = [geojson_obj]
        elif gtype in {"Polygon", "MultiPolygon"}:
            features = [{"type": "Feature", "properties": {}, "geometry": geojson_obj}]
        else:
            raise ZoningSourceValidationError(f"Tipo GeoJSON no soportado para zonificación: {gtype}")

        bounds_acc = [180.0, 90.0, -180.0, -90.0]
        geometry_types: set[str] = set()
        for idx, feat in enumerate(features):
            geom = feat.get("geometry") if isinstance(feat, dict) else None
            if not isinstance(geom, dict):
                raise ZoningSourceValidationError(f"Feature #{idx} sin geometría")
            gt = geom.get("type")
            coords = geom.get("coordinates")
            if gt == "Polygon":
                cls._validate_polygon_rings(coords, coverage_bbox, bounds_acc)
            elif gt == "MultiPolygon":
                if not isinstance(coords, list) or not coords:
                    raise ZoningSourceValidationError(f"MultiPolygon vacío en Feature #{idx}")
                for poly in coords:
                    cls._validate_polygon_rings(poly, coverage_bbox, bounds_acc)
            else:
                raise ZoningSourceValidationError(
                    f"Geometría '{gt}' no soportada; se requiere Polygon o MultiPolygon"
                )
            geometry_types.add(str(gt))

        return {
            "feature_count": len(features),
            "geometry_types": sorted(geometry_types),
            "bbox": tuple(bounds_acc),
            "sha256": cls.canonical_geojson_sha256(geojson_obj),
        }

    @classmethod
    def validate_package(
        cls,
        metadata: OfficialZoningSourceMetadata,
        geojson_obj: Any,
    ) -> dict[str, Any]:
        """Valida de forma reproducible metadatos institucionales, formato CRS, límites de cobertura y reglas de uso."""
        required_text = {
            "source_id": metadata.source_id,
            "issuer_entity": metadata.issuer_entity,
            "document_title": metadata.document_title,
            "ordinance": metadata.ordinance,
            "publication_date": metadata.publication_date,
            "version": metadata.version,
            "coverage_area": metadata.coverage_area,
            "usage_terms": metadata.usage_terms,
        }
        for fname, val in required_text.items():
            if not isinstance(val, str) or not val.strip():
                raise ZoningSourceValidationError(f"Metadato obligatorio vacío o inválido: {fname}")

        if metadata.crs.strip().upper() not in ACCEPTED_CRS:
            raise ZoningSourceValidationError(
                f"Sistema de coordenadas '{metadata.crs}' no aceptado; se exige EPSG:4326 / CRS84"
            )

        if not metadata.is_synthetic:
            if not metadata.official_url or not metadata.official_url.startswith("https://"):
                raise ZoningSourceValidationError(
                    "Toda fuente territorial oficial requiere official_url HTTPS verificable"
                )
            if not metadata.expected_sha256:
                raise ZoningSourceValidationError(
                    "Toda fuente territorial oficial requiere huella SHA-256 esperada para trazabilidad"
                )

        if not metadata.zone_rules:
            raise ZoningSourceValidationError(
                "El paquete territorial debe asociar códigos de zona con reglas explícitas de usos permitidos"
            )

        geom_summary = cls.validate_geometry_wgs84(geojson_obj, coverage_bbox=metadata.coverage_bbox)
        if (
            metadata.expected_sha256
            and geom_summary["sha256"].lower() != metadata.expected_sha256.strip().lower()
        ):
            raise ZoningSourceValidationError(
                "La huella SHA-256 de la geometría no coincide con expected_sha256"
            )

        # Verificar que las zonas etiquetadas en features existan en zone_rules cuando se declaran propiedades
        gtype = geojson_obj.get("type") if isinstance(geojson_obj, dict) else None
        feats = (
            geojson_obj.get("features", [])
            if gtype == "FeatureCollection"
            else ([geojson_obj] if gtype == "Feature" else [])
        )
        for feat in feats:
            props = feat.get("properties") or {}
            zcode = str(
                props.get("zone_code") or props.get("zonificacion") or props.get("ZONA") or ""
            ).strip().upper()
            if zcode and zcode not in {k.upper() for k in metadata.zone_rules}:
                raise ZoningSourceValidationError(
                    f"La zona '{zcode}' presente en el GeoJSON no tiene asociación en zone_rules"
                )

        return {
            **geom_summary,
            "source_id": metadata.source_id,
            "is_synthetic": metadata.is_synthetic,
            "zones_indexed": sorted(metadata.zone_rules.keys()),
        }


def build_synthetic_rehearsal_zoning_package() -> tuple[OfficialZoningSourceMetadata, dict[str, Any]]:
    """
    Construye un paquete GeoJSON SINTÉTICO claramente etiquetado para ensayos técnicos controlados.
    NO representa zonificación oficial real del PDM Huancayo ni debe usarse fuera de pruebas técnicas.
    """
    geojson_fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "zone_code": "ZRP",
                    "zone_label": "Zona de Recreación Pública (SINTÉTICO - NO OFICIAL)",
                    "synthetic": True,
                },
                "geometry": {
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
                },
            }
        ],
    }
    sha = ZoningSourceValidator.canonical_geojson_sha256(geojson_fc)
    metadata = OfficialZoningSourceMetadata(
        source_id="SYNTHETIC-REHEARSAL-PDM-EL-TAMBO-ZRP-V1",
        issuer_entity="SIMULADO_PRUEBA_TECNICA (NO EMITIDO POR MPH)",
        document_title="Capa Sintética de Ensayo Técnico ZRP (NO OFICIAL · Reglas PDUPDM-01/PDUPDM-06)",
        ordinance="ENSAYO-SINTETICO-NO-OFICIAL (Ref. O.M. N.° 636-2020-MPH/OM)",
        publication_date="2026-11-20",
        version="synthetic-rehearsal-v1",
        coverage_area="Polígono ficticio de ensayo en El Tambo (lon -75.23..-75.205, lat -12.065..-12.045)",
        coverage_bbox=EL_TAMBO_AMH_ENVELOPE,
        crs="EPSG:4326",
        official_url=None,
        usage_terms="EXCLUSIVO PARA PRUEBAS TÉCNICAS SINTÉTICAS; prohibido presentar como zonificación real.",
        effective_date="2026-11-20",
        is_synthetic=True,
        expected_sha256=sha,
        zone_rules={
            "ZRP": {
                "section_id": "PDUPDM-01 / PDUPDM-06",
                "description": "Zona de Recreación Pública (ensayo sintético)",
                "compatible_uses": ["recreacion publica", "parque", "area verde", "plaza", "alameda"],
                "incompatible_uses": ["comercio intensivo", "edificacion privada", "vivienda", "industria"],
            }
        },
    )
    return metadata, geojson_fc
