import os
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from backend.application.services.pmv1_services import (
    AuthService,
    NormativeSearchEngine,
    PlatformService,
    _load_fallback_seed_documents,
)
from backend.domain.exceptions.pmv1_exceptions import (
    ServicioNoDisponibleError,
)
from backend.infrastructure.config.dependencies import get_auth_service, get_platform_service
from backend.infrastructure.config.settings import get_settings
from backend.infrastructure.output.broker.celery_app import CeleryTaskDispatcher
from backend.main import create_app

DEAD_DB_URL = "postgresql://user:pass@127.0.0.1:59999/unreachable_db"


def test_keywords_extraction_and_stop_words():
    query = "El plan de desarrollo urbano y la zonificación de suelo en Huancayo"
    keywords = NormativeSearchEngine.extract_keywords(query)
    # Stop words must be filtered out
    assert "el" not in keywords
    assert "de" not in keywords
    assert "y" not in keywords
    assert "la" not in keywords
    assert "en" not in keywords
    # Key tokens must be preserved
    assert "plan" in keywords
    assert "desarrollo" in keywords
    assert "urbano" in keywords
    assert "zonificación" in keywords
    assert "suelo" in keywords
    assert "huancayo" in keywords


def test_nonsense_identifier_is_not_treated_as_normative_evidence():
    """CA-HU2.1-03/CA-HU2.3-02: una coincidencia incidental no inventa sustento."""
    platform = PlatformService(
        DEAD_DB_URL,
        CeleryTaskDispatcher(),
        enable_demo_fallback=True,
    )

    result = platform.search_normatives(
        "zzqxj-no-existe-987654",
        actor_id=uuid4(),
        only_in_force=True,
    )

    assert result["total_results"] == 0
    assert result["results"] == []
    assert result.get("alerts", []) == []


def test_score_chunk_exact_id_match():
    chunk = {
        "id": "PDUPDM-02",
        "document_name": "PDU/PDM Huancayo-El Tambo",
        "short_code": "PDU_EL_TAMBO",
        "topic": "Compatibilidad de uso de suelo",
        "content": "si el uso propuesto es incompatible requiere observacion",
        "has_alert": True,
    }
    keywords = NormativeSearchEngine.extract_keywords("PDUPDM-02")
    score = NormativeSearchEngine.score_chunk(chunk, "PDUPDM-02", keywords)
    assert score >= 50.0


def test_score_chunk_alert_boosting():
    alert_chunk = {
        "id": "PDUPDM-04",
        "document_name": "PDU/PDM Huancayo-El Tambo",
        "short_code": "PDU_EL_TAMBO",
        "topic": "Zonas de riesgo",
        "content": "propuesta ubicada en area de riesgo alto no mitigable",
        "has_alert": True,
    }
    non_alert_chunk = {
        "id": "PDUPDM-05",
        "document_name": "PDU/PDM Huancayo-El Tambo",
        "short_code": "PDU_EL_TAMBO",
        "topic": "Sistema vial",
        "content": "red vial y movilidad",
        "has_alert": False,
    }
    query = "proyecto en zona de riesgo alto"
    kw = NormativeSearchEngine.extract_keywords(query)
    score_alert = NormativeSearchEngine.score_chunk(alert_chunk, query, kw)
    score_non_alert = NormativeSearchEngine.score_chunk(non_alert_chunk, query, kw)
    assert score_alert > score_non_alert
    assert score_alert >= 50.0


def test_score_chunk_no_match():
    chunk = {
        "id": "DL1252-01",
        "document_name": "D. L. N.° 1252 - Invierte.pe",
        "short_code": "DL_1252",
        "topic": "Programación multianual",
        "content": "cierre de brechas prioritarias",
        "has_alert": False,
    }
    query = "arquitectura hospitalaria neonatologia"
    kw = NormativeSearchEngine.extract_keywords(query)
    score = NormativeSearchEngine.score_chunk(chunk, query, kw)
    assert score == 0.0


# === HU2.1 Acceptance Criteria Verification Tests ===


def test_hu21_ca1_resolve_location_identifiable():
    """CA-HU2.1-01: Ubicacion identificable del fragmento (articulo, seccion o ID estable)."""
    # 1. Extrae de parentesis en topic
    chunk_topic = {
        "id": "LEY27972-01",
        "topic": "Competencias municipales (Art. 73)",
        "content": "Las municipalidades ejercen competencias...",
    }
    loc1 = NormativeSearchEngine.resolve_location(chunk_topic)
    assert loc1 == "Art. 73"

    # 2. Extrae de seccion/titulo en topic
    chunk_title = {
        "id": "PDUPDM-02",
        "topic": "Zonificación urbana (Título III del Reglamento de Zonificación)",
        "content": "Compatibilidad de usos...",
    }
    loc2 = NormativeSearchEngine.resolve_location(chunk_title)
    assert "Título III" in loc2

    # 3. Extrae de articulo al inicio del contenido
    chunk_content = {
        "id": "CUSTOM-01",
        "topic": "Disposiciones generales",
        "content": "Art. 15. Deberes de la entidad contratante respecto a...",
    }
    loc3 = NormativeSearchEngine.resolve_location(chunk_content)
    assert loc3 == "Art. 15"

    # 4. Fallback a ID estable cuando no hay rotulo especifico
    chunk_fallback = {
        "id": "SEC-999",
        "topic": "Definiciones generales",
        "content": "Para los efectos de la presente normativa...",
    }
    loc4 = NormativeSearchEngine.resolve_location(chunk_fallback)
    assert loc4 == "Fragmento ID SEC-999"


def test_hu21_ca2_inactive_sources_excluded_from_active_search():
    """CA-HU2.1-02: Fuentes inactivas/sustituidas no aparecen en busqueda vigente, pero se preservan en datos."""
    seed_docs = _load_fallback_seed_documents()
    assert len(seed_docs) > 0

    # Verificar que la norma inactiva LEY26850-HIST existe en el repositorio historico
    hist_doc = next((d for d in seed_docs if d["id"] == "LEY26850-HIST"), None)
    assert hist_doc is not None, "El registro historico debe preservarse en el repositorio de datos"
    assert hist_doc["in_force"] is False, "La norma derogada debe figurar con in_force=False"
    assert "Sustituida" in hist_doc["document_name"]

    # Simular busqueda con only_in_force=True para terminos de contrataciones
    query = "contrataciones y adquisiciones del estado adquisicion de bienes"
    kw = NormativeSearchEngine.extract_keywords(query)

    # Filtrar solo vigentes
    active_docs = [d for d in seed_docs if d["in_force"]]
    assert hist_doc not in active_docs, "La norma inactiva no debe estar en la lista de vigentes"

    # Puntuacion sobre documentos vigentes
    active_results = []
    for item in active_docs:
        score = NormativeSearchEngine.score_chunk(item, query, kw)
        if score > 0:
            active_results.append(item)

    # Comprobar que NINGUN resultado pertenece a la norma inactiva
    returned_ids = [r["id"] for r in active_results]
    assert "LEY26850-HIST" not in returned_ids


def test_hu21_ca3_no_evidence_returns_zero_results_without_inventing():
    """CA-HU2.1-03: Sin evidencia -> no genera normas ni articulos ficticios, devuelve lista vacia."""
    seed_docs = _load_fallback_seed_documents()
    active_docs = [d for d in seed_docs if d["in_force"]]

    # Consulta completamente ajena al corpus municipal
    irrelevant_query = "exploracion espacial de satelites en marte y propulsion ionica"
    kw = NormativeSearchEngine.extract_keywords(irrelevant_query)

    results = []
    for item in active_docs:
        score = NormativeSearchEngine.score_chunk(item, irrelevant_query, kw)
        if score > 0 and NormativeSearchEngine.is_relevant(item, irrelevant_query, kw):
            results.append(item)

    assert len(results) == 0, "No debe retornar fragmentos inventados para consultas sin sustento"


def test_auth_service_db_down_raises_controlled_error_and_never_emits_session():
    """Ante falla de base de datos, la autenticacion falla de forma controlada y nunca emite sesion."""
    auth = AuthService(DEAD_DB_URL, "test-secret-12345-at-least-32-bytes-long-for-hmac", "test-issuer", "test-aud", 60)

    # Debe lanzar ServicioNoDisponibleError para cualquier usuario, incluidos roles admin/legal
    for test_email, test_pwd in [
        ("legal@siprim.demo", "demo2026"),
        ("admin@siprim.demo", "demo2026"),
        ("asesor.juridico@eltambo.gob.pe", "siprim2026"),
        ("usuario_desconocido@siprim.pe", "clave123"),
    ]:
        with pytest.raises(ServicioNoDisponibleError) as exc_info:
            auth.login(test_email, test_pwd)
        assert "no disponible" in str(exc_info.value).lower()


def test_platform_service_db_down_in_normal_mode_raises_controlled_error():
    """En modo normal (enable_demo_fallback=False por defecto), la caida de BD no activa datos simulados."""
    # Instancia en modo normal (desactivado por defecto)
    platform = PlatformService(DEAD_DB_URL, CeleryTaskDispatcher(), enable_demo_fallback=False)

    # 1. Busqueda normativa falla de forma controlada y NO retorna datos simulados
    with pytest.raises(ServicioNoDisponibleError) as exc_search:
        platform.search_normatives("obras y contrataciones municipales", actor_id=uuid4())
    assert "no disponible" in str(exc_search.value).lower()

    # 2. Listado de documentos normativos tambien falla de forma controlada
    with pytest.raises(ServicioNoDisponibleError) as exc_list:
        platform.list_normative_documents()
    assert "no disponible" in str(exc_list.value).lower()


def test_platform_service_explicit_demo_mode_allows_fallback_seed_corpus():
    """Cuando el modo demostracion se habilita expresamente (enable_demo_fallback=True), permite consultar el corpus didactico."""
    platform = PlatformService(DEAD_DB_URL, CeleryTaskDispatcher(), enable_demo_fallback=True)

    # 1. Listar documentos retorna las fuentes del seed
    docs = platform.list_normative_documents()
    assert len(docs) > 0
    short_codes = [d["short_code"] for d in docs]
    assert "LEY_27972" in short_codes

    # 2. Busqueda normativa encuentra fragmentos vigentes con ubicacion identificable
    res = platform.search_normatives("competencias municipales", actor_id=uuid4(), only_in_force=True)
    assert res["total_results"] > 0
    hit = res["results"][0]
    assert hit["location"] is not None
    assert hit["version"] is not None
    assert hit["in_force"] is True

    # 3. Excluye norma inactiva LEY26850-HIST de la busqueda vigente
    res_contrat = platform.search_normatives("contrataciones y adquisiciones del estado", actor_id=uuid4(), only_in_force=True)
    hit_ids = [r["id"] for r in res_contrat["results"]]
    assert "LEY26850-HIST" not in hit_ids

    # 4. Consulta sin sustento retorna 0 resultados sin inventar articulos
    res_empty = platform.search_normatives("satelites y naves espaciales en orbita", actor_id=uuid4(), only_in_force=True)
    assert res_empty["total_results"] == 0
    assert len(res_empty["results"]) == 0


def test_auth_service_issue_token_for_authorized_development_sessions():
    """issue_token permite firmar sesiones autorizadas sin contrasenas ni dependencias de BD."""
    auth = AuthService(DEAD_DB_URL, "secret-key-456-at-least-32-bytes-long-for-hmac", "siprim-auth", "siprim-api", 60)
    user_id = uuid4()
    session = auth.issue_token(user_id, ["LEGAL_ADVISOR"], full_name="Asesor Juridico Autorizado")

    assert session["access_token"] is not None
    assert session["user_id"] == user_id
    assert session["roles"] == ["LEGAL_ADVISOR"]

    # El token firmado es decodificable y valido
    decoded = auth.decode(session["access_token"])
    assert decoded["sub"] == str(user_id)
    assert decoded["roles"] == ["LEGAL_ADVISOR"]


def test_hu2_1_engine_rejects_superficial_generic_keyword_matches():
    """CA-HU2.1-03: palabras genericas como 'ley' o 'reglamento' no fabrican coincidencias."""
    docs = _load_fallback_seed_documents()
    ley_mun = next(d for d in docs if d["id"] == "LEY27972-01")
    ley_cont = next(d for d in docs if d["id"] == "LEY32069-03")
    hist_26850 = next(d for d in docs if d["id"] == "LEY26850-HIST")

    for superficial_query in (
        "ley marciana",
        "reglamento de pesca artesanal en el pacifico",
        "impuestos municipales para mascotas domesticas",
        "decreto supremo espacial",
    ):
        kws = NormativeSearchEngine.extract_keywords(superficial_query)
        assert not NormativeSearchEngine.is_relevant(ley_mun, superficial_query, kws), superficial_query
        assert not NormativeSearchEngine.is_relevant(ley_cont, superficial_query, kws), superficial_query
        assert not NormativeSearchEngine.is_relevant(hist_26850, superficial_query, kws), superficial_query

    # Consultas pertinentes reales si califican
    for valid_query, target_doc in (
        ("contratos menores pladicop", ley_cont),
        ("competencia municipal funciones", ley_mun),
        ("competencias", ley_mun),
    ):
        kws = NormativeSearchEngine.extract_keywords(valid_query)
        assert NormativeSearchEngine.is_relevant(target_doc, valid_query, kws), valid_query


def test_hu2_1_unavailable_db_returns_503_and_demo_mode_is_explicit():
    """Sin fallback a demo, BD caida devuelve 503; con fallback activado se identifica explicitamente."""
    app = create_app()
    platform_strict = PlatformService(
        DEAD_DB_URL,
        CeleryTaskDispatcher(),
        enable_demo_fallback=False,
    )
    auth = AuthService(
        DEAD_DB_URL,
        "secret-key-456-at-least-32-bytes-long-for-hmac",
        "siprim-auth",
        "siprim-api",
        60,
    )
    session = auth.issue_token(uuid4(), ["LEGAL_ADVISOR"], full_name="Asesor Test")
    headers = {"Authorization": f"Bearer {session['access_token']}"}
    app.dependency_overrides[get_platform_service] = lambda: platform_strict
    app.dependency_overrides[get_auth_service] = lambda: auth

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/normative/search",
            headers=headers,
            json={"query": "zonificacion", "only_in_force": True},
        )
        assert resp.status_code == 503, resp.text

    platform_demo = PlatformService(
        DEAD_DB_URL,
        CeleryTaskDispatcher(),
        enable_demo_fallback=True,
    )
    demo_res = platform_demo.search_normatives("zonificacion municipal", actor_id=uuid4(), only_in_force=True)
    assert demo_res["is_demo_fallback"] is True
    assert "MODO DEMOSTRACIÓN" in demo_res["disclaimer"]


def test_hu2_1_api_ca01_ca02_ca03_rbac_and_coexistence_on_isolated_postgres():
    """HU2.1 completo sobre PostgreSQL aislado: RBAC, CA-HU2.1-01, CA-HU2.1-02 (coexistencia 2024-v1/OECE) y CA-HU2.1-03."""
    settings = get_settings()
    app = create_app()
    platform_svc = PlatformService(
        db_url=settings.platform_db_url,
        dispatcher=None,  # type: ignore[arg-type]
        enable_demo_fallback=False,
        auth_db_url=settings.auth_db_url,
    )
    auth_svc = AuthService(
        settings.auth_db_url,
        settings.jwt_secret.get_secret_value(),
        settings.jwt_issuer,
        settings.jwt_audience,
        settings.jwt_expiration_minutes,
    )
    app.dependency_overrides[get_platform_service] = lambda: platform_svc
    app.dependency_overrides[get_auth_service] = lambda: auth_svc

    p_url = settings.platform_db_url.replace("postgresql+psycopg://", "postgresql://", 1)
    admin_p_url = os.environ.get("TEST_ADMIN_PLATFORM_DB_URL", p_url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )
    stale_id = f"LEY32069-STALE-{uuid4().hex[:6].upper()}"
    declared_id = f"NORMA-DECL-{uuid4().hex[:6].upper()}"

    with TestClient(app) as client, psycopg.connect(admin_p_url, autocommit=True) as p_conn:
        try:
            # --- 1. RBAC ---
            unauth = client.post("/api/v1/normative/search", json={"query": "contratos menores"})
            assert unauth.status_code == 401
            assert client.get("/api/v1/normative/documents").status_code == 401

            planner_login = client.post(
                "/api/v1/auth/login",
                json={"email": "planner@siprim.test", "password": "Prueba2026!"},
            )
            assert planner_login.status_code == 200
            planner_headers = {"Authorization": f"Bearer {planner_login.json()['access_token']}"}
            assert (
                client.post(
                    "/api/v1/normative/search",
                    headers=planner_headers,
                    json={"query": "contratos menores"},
                ).status_code
                == 403
            )
            assert client.get("/api/v1/normative/documents", headers=planner_headers).status_code == 403

            citizen_token = auth_svc.issue_token(uuid4(), ["CITIZEN"], full_name="Ciudadano")["access_token"]
            citizen_headers = {"Authorization": f"Bearer {citizen_token}"}
            assert (
                client.post(
                    "/api/v1/normative/search",
                    headers=citizen_headers,
                    json={"query": "contratos menores"},
                ).status_code
                == 401
            )

            admin_login = client.post(
                "/api/v1/auth/login",
                json={"email": "admin@siprim.test", "password": "Prueba2026!"},
            )
            assert admin_login.status_code == 200
            admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}
            assert client.get("/api/v1/normative/documents", headers=admin_headers).status_code == 200

            legal_login = client.post(
                "/api/v1/auth/login",
                json={"email": "legal@siprim.test", "password": "Prueba2026!"},
            )
            assert legal_login.status_code == 200
            legal_headers = {"Authorization": f"Bearer {legal_login.json()['access_token']}"}

            # --- 2. CA-HU2.1-01: Catalogo y consulta pertinente con trazabilidad de fuente ---
            catalog_resp = client.get("/api/v1/normative/documents", headers=legal_headers)
            assert catalog_resp.status_code == 200, catalog_resp.text
            catalog = catalog_resp.json()
            assert catalog["total_documents"] > 0
            docs_list = catalog["documents"]
            assert any(d["in_force"] for d in docs_list)
            assert any(not d["in_force"] for d in docs_list)
            by_code_ver = {(d["short_code"], d["version"]): d for d in docs_list}
            ley_oece = by_code_ver[("LEY_32069", "2026-07-19-OECE")]
            assert ley_oece["in_force"] is True
            assert ley_oece["official_url"].startswith("https://")
            assert ley_oece["chunks_count"] >= 1
            ds_oece = by_code_ver[("REG_32069", "2026-10-26-OECE")]
            assert ds_oece["in_force"] is True
            assert ds_oece["official_url"].startswith("https://")
            assert by_code_ver[("LEY_30225", "2019-TUO-derogada")]["in_force"] is False
            assert by_code_ver[("LEY_32069_HIST", "2024-v1-historica")]["in_force"] is False

            ca01_resp = client.post(
                "/api/v1/normative/search",
                headers=legal_headers,
                json={"query": "contratos menores pladicop", "only_in_force": True, "limit": 5},
            )
            assert ca01_resp.status_code == 200, ca01_resp.text
            ca01 = ca01_resp.json()
            assert ca01["is_demo_fallback"] is False
            assert "MODO DEMOSTRACIÓN" not in ca01["disclaimer"]
            assert ca01["total_results"] >= 1
            hit_map = {h["id"]: h for h in ca01["results"]}
            assert "LEY32069-03" in hit_map
            hit = hit_map["LEY32069-03"]
            assert hit["document_name"]
            assert hit["version"] == "2026-07-19-OECE"
            assert hit["location"] == "Arts. 34, 53 y 54"
            assert "Contratos menores" in hit["content"]
            assert hit["in_force"] is True
            assert hit["data_origin"] == "public"
            assert hit["official_url"].startswith("https://")

            # Distincion visible entre origen 'public' y 'declared'
            p_conn.execute(
                """
                INSERT INTO normative_documents
                    (id, document_name, short_code, version, topic, content, in_force, has_alert,
                     data_origin, official_url, consulted_at, amendments_summary, effective_from)
                VALUES
                    (%s, %s, 'LINEAMIENTO_DECL', %s, 'Seccion 2 - Contratos menores',
                     'Guia declarada internamente para contratos menores pladicop.',
                     TRUE, FALSE, 'declared', NULL, NULL, NULL, NULL)
                """,
                (
                    declared_id,
                    "Lineamiento interno declarado sobre contratos menores",
                    "2026-declared",
                ),
            )
            decl_resp = client.post(
                "/api/v1/normative/search",
                headers=legal_headers,
                json={"query": "contratos menores pladicop", "only_in_force": True, "limit": 10},
            )
            assert decl_resp.status_code == 200
            decl_origins = {h["id"]: h["data_origin"] for h in decl_resp.json()["results"]}
            assert decl_origins.get("LEY32069-03") == "public"
            assert decl_origins.get(declared_id) == "declared"

            # --- 3. CA-HU2.1-02: Coexistencia de versiones anteriores y exclusion de inactivas/sustituidas ---
            p_conn.execute(
                """
                INSERT INTO normative_documents
                    (id, document_name, short_code, version, topic, content, in_force, has_alert,
                     data_origin, official_url, consulted_at, amendments_summary, effective_from)
                VALUES
                    (%s, %s, 'LEY_32069', '2024-v1', 'Art. 1 (version previa)',
                     'Contrataciones del estado regimen vigente 30225 Ley 32069 version previa 2024-v1.',
                     TRUE, FALSE, 'public', NULL, NULL, NULL, NULL)
                """,
                (
                    stale_id,
                    "Ley N.° 32069 (Ley General de Contrataciones Públicas)",
                ),
            )

            ca02_active = client.post(
                "/api/v1/normative/search",
                headers=legal_headers,
                json={"query": "contrataciones del estado vigente 30225", "only_in_force": True, "limit": 10},
            )
            assert ca02_active.status_code == 200, ca02_active.text
            active_data = ca02_active.json()
            active_ids = {h["id"] for h in active_data["results"]}
            assert "LEY26850-HIST" not in active_ids
            assert "LEY30225-HIST" not in active_ids
            assert "LEY32069-2024V1-HIST" not in active_ids
            assert stale_id not in active_ids
            excluded_ids = {h["id"] for h in active_data["historical_references_excluded"]}
            assert excluded_ids & {"LEY26850-HIST", "LEY30225-HIST", "LEY32069-2024V1-HIST", stale_id}

            # Modo historico explicito permite auditar sin confundir con norma vigente
            ca02_all = client.post(
                "/api/v1/normative/search",
                headers=legal_headers,
                json={"query": "contrataciones del estado vigente 30225", "only_in_force": False, "limit": 10},
            )
            assert ca02_all.status_code == 200
            all_by_id = {h["id"]: h for h in ca02_all.json()["results"]}
            assert "LEY30225-HIST" in all_by_id
            assert all_by_id["LEY30225-HIST"]["in_force"] is False

            # --- 4. CA-HU2.1-03: Sin coincidencias adecuadas -> insuficiencia sin fabricar normas ---
            for empty_query in (
                "propulsion ionica de satelites en orbita lunar",
                "reglamento de pesca artesanal en el pacifico",
                "impuestos municipales para mascotas domesticas",
                "ley marciana",
            ):
                empty_resp = client.post(
                    "/api/v1/normative/search",
                    headers=legal_headers,
                    json={"query": empty_query, "only_in_force": True, "limit": 5},
                )
                assert empty_resp.status_code == 200, empty_resp.text
                empty_data = empty_resp.json()
                assert empty_data["total_results"] == 0, empty_query
                assert empty_data["results"] == [], empty_query
                assert empty_data["alerts_found"] == [], empty_query
        finally:
            p_conn.execute("DELETE FROM normative_documents WHERE id = ANY(%s)", ([stale_id, declared_id],))
