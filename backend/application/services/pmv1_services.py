from __future__ import annotations

import os
import socket
import hashlib
import hmac
import json
import math
import re
import secrets
import string
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, ClassVar, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import jwt
import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from backend.application.dto.pmv1_dto import (
    PMV1ProjectCreate,
    PMV1ProjectUpdate,
    PertEstimateCreate,
    ProcurementAntecedentUpsert,
)
from backend.application.services.prioritization_pdf_service import build_prioritization_pdf
from backend.application.services.zoning_catalog import (
    OfficialZoningSourceMetadata,
    ZoningSourceValidator,
    build_synthetic_rehearsal_zoning_package,
)
from backend.infrastructure.config.settings import get_settings
from backend.domain.exceptions.pmv1_exceptions import (
    AccesoDenegadoError,
    VersionNoPertenecienteError,
    ConfiguracionPMV1Error,
    CredencialesInvalidasError,
    EstimacionPERTInvalidaError,
    ExpedientePMV1IncompletoError,
    RecursoPMV1NoEncontradoError,
    ServicioNoDisponibleError,
)


def _dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


_EXPEDIENTE_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _allocate_expediente_code(conn: psycopg.Connection[Any]) -> str:
    """Código legible único para búsqueda rápida (EXP- + 6 caracteres)."""
    for _ in range(12):
        suffix = "".join(
            secrets.choice(_EXPEDIENTE_CODE_ALPHABET) for _ in range(6)
        )
        code = f"EXP-{suffix}"
        exists = conn.execute(
            "SELECT 1 FROM projects WHERE upper(code) = upper(%s)", (code,)
        ).fetchone()
        if not exists:
            return code
    raise ConfiguracionPMV1Error("No se pudo generar un código de expediente único")


class TaskDispatcher(Protocol):
    def dispatch_economic(self, payload: dict[str, Any]) -> None: ...


def _is_db_available(url: str) -> bool:
    try:
        if "@" in url:
            host_port = url.split("@", 1)[1].split("/", 1)[0]
            if ":" in host_port:
                host, port_str = host_port.split(":", 1)
                port = int(port_str)
            else:
                host, port = host_port, 5432
        else:
            host, port = "127.0.0.1", 5432
        if host in ("localhost", "127.0.0.1"):
            host = "127.0.0.1"
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except Exception:
        return False


def _load_fallback_seed_documents() -> list[dict[str, Any]]:
    seed_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "database", "platform_db", "02_seed.sql"
    )
    if not os.path.exists(seed_path):
        return []
    try:
        with open(seed_path, "r", encoding="utf-8") as f:
            text = f.read()
        idx = text.find("INSERT INTO normative_documents")
        idx_end = text.find("ON CONFLICT (id)", idx)
        if idx == -1 or idx_end == -1:
            return []
        block = text[idx:idx_end]
        tuples = re.findall(
            r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'((?:''|[^'])*)'\s*,\s*(TRUE|FALSE)\s*,\s*(TRUE|FALSE)\s*,\s*'([^']+)'\s*\)",
            block,
        )
        docs = []
        for t in tuples:
            docs.append({
                "id": t[0],
                "document_name": t[1],
                "short_code": t[2],
                "version": t[3],
                "topic": t[4],
                "content": t[5].replace("''", "'"),
                "in_force": t[6] == "TRUE",
                "has_alert": t[7] == "TRUE",
                "data_origin": t[8],
            })
        return docs
    except Exception:
        return []


HU33_PROTOTYPE_NOTICE = (
    "Constancia de integridad SHA-256 del prototipo académico SIPRIM: "
    "detecta alteraciones del contenido respecto a la huella almacenada, "
    "pero no constituye firma digital certificada con validez jurídica."
)


def _compute_checkpoint_decision_sha256(
    *,
    decision_id: UUID,
    checkpoint_id: UUID,
    project_id: UUID,
    project_version_id: UUID,
    decision: str,
    justification: str,
    conditions: str | None,
    decided_by_user_id: UUID,
    decided_by_name: str | None,
    decided_by_email: str | None,
    decided_at: datetime,
) -> tuple[dict[str, Any], str]:
    """Calcula la representacion canonica y el resumen SHA-256 de una decision HITL (HU3.3)."""
    dt_utc = decided_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    canonical = {
        "schema_version": "hu3.3-v2",
        "decision_id": str(decision_id),
        "checkpoint_id": str(checkpoint_id),
        "project_id": str(project_id),
        "project_version_id": str(project_version_id),
        "decision": decision,
        "justification": justification,
        "conditions": conditions or "",
        "decided_by_user_id": str(decided_by_user_id),
        "decided_by_name": decided_by_name or "",
        "decided_by_email": decided_by_email or "",
        "decided_at_utc": dt_utc,
    }
    serialized = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return canonical, digest


class AuthService:
    def __init__(
        self,
        db_url: str,
        jwt_secret: str,
        issuer: str,
        audience: str,
        expiration_minutes: int,
    ) -> None:
        self.db_url = _dsn(db_url)
        self.jwt_secret = jwt_secret
        self.issuer = issuer
        self.audience = audience
        self.expiration_minutes = expiration_minutes
        self.password_hasher = PasswordHasher()
        # Hash ficticio para igualar el costo Argon2 cuando el usuario no existe o está inactivo.
        self._dummy_password_hash = self.password_hasher.hash("siprim-dummy-timing-equalizer")

    def issue_token(
        self,
        user_id: UUID,
        roles: list[str],
        full_name: str = "",
    ) -> dict[str, Any]:
        """Emite una sesion con token JWT firmado a partir de identidad verificada o autorizada."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=self.expiration_minutes)
        claims = {
            "sub": str(user_id),
            "roles": list(roles),
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "exp": expires,
        }
        token = jwt.encode(claims, self.jwt_secret, algorithm="HS256")
        return {
            "access_token": token,
            "expires_in": self.expiration_minutes * 60,
            "user_id": user_id,
            "full_name": full_name,
            "roles": list(roles),
        }

    def login(self, email: str, password: str) -> dict[str, Any]:
        clean_email = email.strip().lower()
        try:
            if not _is_db_available(self.db_url):
                raise psycopg.OperationalError("Base de datos de autenticacion no disponible")
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT u.id, u.full_name, u.password_hash, u.is_active,
                           COALESCE(array_agg(r.code) FILTER (WHERE r.code IS NOT NULL), '{}') AS roles
                    FROM users u
                    LEFT JOIN user_roles ur ON ur.user_id = u.id
                    LEFT JOIN roles r ON r.id = ur.role_id
                    WHERE u.email = %s
                    GROUP BY u.id
                    """,
                    (clean_email,),
                ).fetchone()
            if not row or not row["is_active"]:
                try:
                    self.password_hasher.verify(self._dummy_password_hash, password)
                except (VerifyMismatchError, InvalidHashError):
                    pass
                raise CredencialesInvalidasError("Credenciales invalidas")
            try:
                self.password_hasher.verify(row["password_hash"], password)
            except (VerifyMismatchError, InvalidHashError):
                raise CredencialesInvalidasError("Credenciales invalidas") from None
            return self.issue_token(row["id"], row["roles"], row["full_name"])
        except psycopg.OperationalError as err:
            raise ServicioNoDisponibleError(
                "Servicio de autenticacion no disponible: la base de datos no responde."
            ) from err

    def decode(self, token: str) -> dict[str, Any]:
        try:
            return jwt.decode(
                token,
                self.jwt_secret,
                algorithms=["HS256"],
                issuer=self.issuer,
                audience=self.audience,
            )
        except jwt.PyJWTError as exc:
            raise CredencialesInvalidasError("Token invalido o expirado") from exc

    def get_active_user_roles(self, user_id: UUID) -> list[str]:
        """Consulta los roles vigentes y estado activo de la cuenta en auth_db (HU5.1)."""
        try:
            if not _is_db_available(self.db_url):
                raise psycopg.OperationalError("Base de datos de autenticacion no disponible")
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT u.is_active,
                           COALESCE(
                               array_agg(r.code ORDER BY r.code) FILTER (WHERE r.code IS NOT NULL),
                               '{}'
                           ) AS roles
                    FROM users u
                    LEFT JOIN user_roles ur ON ur.user_id = u.id
                    LEFT JOIN roles r ON r.id = ur.role_id
                    WHERE u.id = %s
                    GROUP BY u.id
                    """,
                    (user_id,),
                ).fetchone()
            if not row or not row["is_active"]:
                raise CredencialesInvalidasError("Usuario inactivo o no autorizado")
            return list(row["roles"])
        except psycopg.OperationalError as err:
            raise ServicioNoDisponibleError(
                "Servicio de autenticacion no disponible para validar permisos vigentes."
            ) from err

    def get_current_user_profile(self, user_id: UUID) -> dict[str, Any]:
        """Devuelve identidad y roles vigentes del usuario autenticado desde auth_db."""
        try:
            if not _is_db_available(self.db_url):
                raise psycopg.OperationalError("Base de datos de autenticacion no disponible")
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                row = conn.execute(
                    """
                    SELECT u.id, u.email, u.full_name, u.is_active,
                           COALESCE(
                               array_agg(r.code ORDER BY r.code) FILTER (WHERE r.code IS NOT NULL),
                               '{}'
                           ) AS roles
                    FROM users u
                    LEFT JOIN user_roles ur ON ur.user_id = u.id
                    LEFT JOIN roles r ON r.id = ur.role_id
                    WHERE u.id = %s
                    GROUP BY u.id
                    """,
                    (user_id,),
                ).fetchone()
            if not row or not row["is_active"]:
                raise CredencialesInvalidasError("Usuario inactivo o no autorizado")
            return {
                "user_id": row["id"],
                "email": row["email"],
                "full_name": row["full_name"],
                "is_active": bool(row["is_active"]),
                "roles": list(row["roles"]),
            }
        except psycopg.OperationalError as err:
            raise ServicioNoDisponibleError(
                "Servicio de autenticacion no disponible: la base de datos no responde."
            ) from err

    def list_users_and_roles(self) -> dict[str, Any]:
        """Lista usuarios municipales (sin hashes ni secretos) y catalogo de roles (HU5.1)."""
        try:
            if not _is_db_available(self.db_url):
                raise psycopg.OperationalError("Base de datos de autenticacion no disponible")
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                role_rows = conn.execute(
                    "SELECT code, name FROM roles ORDER BY code ASC"
                ).fetchall()
                user_rows = conn.execute(
                    """
                    SELECT u.id, u.email, u.full_name, u.is_active, u.created_at, u.updated_at,
                           COALESCE(
                               array_agg(r.code ORDER BY r.code) FILTER (WHERE r.code IS NOT NULL),
                               '{}'
                           ) AS roles
                    FROM users u
                    LEFT JOIN user_roles ur ON ur.user_id = u.id
                    LEFT JOIN roles r ON r.id = ur.role_id
                    GROUP BY u.id
                    ORDER BY u.created_at ASC, u.email ASC
                    """
                ).fetchall()
            available_roles = [
                {
                    "code": r["code"],
                    "name": r["name"],
                    "privileged": r["code"] == "SUPERADMIN",
                }
                for r in role_rows
            ]
            users = [
                {
                    "id": u["id"],
                    "email": u["email"],
                    "full_name": u["full_name"],
                    "is_active": bool(u["is_active"]),
                    "roles": list(u["roles"]),
                    "created_at": u["created_at"],
                    "updated_at": u["updated_at"],
                }
                for u in user_rows
            ]
            return {"users": users, "available_roles": available_roles}
        except psycopg.OperationalError as err:
            raise ServicioNoDisponibleError(
                "Servicio de autenticacion no disponible: la base de datos no responde."
            ) from err

    def update_user_roles(
        self,
        target_user_id: UUID,
        new_roles: list[str],
        actor_roles: list[str],
    ) -> dict[str, Any]:
        """Actualiza transaccionalmente los roles de un usuario municipal en auth_db (HU5.1)."""
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in new_roles:
            code = raw.strip().upper()
            if not code:
                raise ConfiguracionPMV1Error("El codigo de rol no puede estar vacio.")
            if code in seen:
                raise ConfiguracionPMV1Error(f"Rol duplicado en la solicitud: {code}")
            seen.add(code)
            normalized.append(code)

        try:
            if not _is_db_available(self.db_url):
                raise psycopg.OperationalError("Base de datos de autenticacion no disponible")
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                with conn.transaction():
                    # Serializa cambios de rol para garantizar atomicidad en la regla del ultimo administrador activo.
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext('siprim_admin_role_guard'))")
                    user_row = conn.execute(
                        """
                        SELECT id, email, full_name, is_active, created_at, updated_at
                        FROM users
                        WHERE id = %s
                        FOR UPDATE
                        """,
                        (target_user_id,),
                    ).fetchone()
                    if not user_row:
                        raise RecursoPMV1NoEncontradoError("Usuario municipal no encontrado.")

                    current_rows = conn.execute(
                        """
                        SELECT r.code
                        FROM user_roles ur
                        JOIN roles r ON r.id = ur.role_id
                        WHERE ur.user_id = %s
                        """,
                        (target_user_id,),
                    ).fetchall()
                    current_roles = [r["code"] for r in current_rows]

                    catalog_rows = conn.execute("SELECT id, code FROM roles").fetchall()
                    role_id_by_code = {r["code"]: r["id"] for r in catalog_rows}
                    invalid_codes = [c for c in normalized if c not in role_id_by_code]
                    if invalid_codes:
                        raise ConfiguracionPMV1Error(
                            f"Rol invalido no registrado en el catalogo: {', '.join(invalid_codes)}"
                        )

                    actor_is_superadmin = "SUPERADMIN" in actor_roles
                    if not actor_is_superadmin and (
                        "SUPERADMIN" in normalized or "SUPERADMIN" in current_roles
                    ):
                        raise AccesoDenegadoError(
                            "Solo una cuenta SUPERADMIN puede asignar o modificar el rol SUPERADMIN."
                        )

                    if not user_row["is_active"] and normalized:
                        raise ConfiguracionPMV1Error(
                            "No se pueden asignar roles a una cuenta municipal inactiva."
                        )

                    had_admin = ("ADMIN" in current_roles) or ("SUPERADMIN" in current_roles)
                    will_have_admin = ("ADMIN" in normalized) or ("SUPERADMIN" in normalized)
                    if had_admin and not will_have_admin:
                        other_admins = conn.execute(
                            """
                            SELECT COUNT(DISTINCT u.id) AS cnt
                            FROM users u
                            JOIN user_roles ur ON ur.user_id = u.id
                            JOIN roles r ON r.id = ur.role_id
                            WHERE u.is_active = TRUE
                              AND u.id <> %s
                              AND r.code IN ('ADMIN', 'SUPERADMIN')
                            """,
                            (target_user_id,),
                        ).fetchone()
                        if not other_admins or int(other_admins["cnt"]) == 0:
                            raise ConfiguracionPMV1Error(
                                "No se puede retirar el rol al ultimo administrador activo del sistema."
                            )

                    conn.execute("DELETE FROM user_roles WHERE user_id = %s", (target_user_id,))
                    for code in normalized:
                        conn.execute(
                            "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s)",
                            (target_user_id, role_id_by_code[code]),
                        )
                    updated_row = conn.execute(
                        "UPDATE users SET updated_at = now() WHERE id = %s RETURNING updated_at",
                        (target_user_id,),
                    ).fetchone()

            return {
                "user": {
                    "id": user_row["id"],
                    "email": user_row["email"],
                    "full_name": user_row["full_name"],
                    "is_active": bool(user_row["is_active"]),
                    "roles": sorted(normalized),
                    "created_at": user_row["created_at"],
                    "updated_at": updated_row["updated_at"] if updated_row else user_row["updated_at"],
                },
                "message": "Roles actualizados correctamente.",
            }
        except psycopg.OperationalError as err:
            raise ServicioNoDisponibleError(
                "Servicio de autenticacion no disponible: la base de datos no responde."
            ) from err



class NormativeSearchEngine:
    """Motor de ponderacion de relevancia para consultas normativas referenciales (HU2.1 / RF12)."""

    @classmethod
    def resolve_location(cls, chunk: dict[str, Any]) -> str:
        """Extrae o resuelve la ubicacion identificable (articulo, seccion o ID estable) de un fragmento."""
        topic = str(chunk.get("topic", ""))
        text_content = str(chunk.get("content", ""))
        chunk_id = str(chunk.get("id", ""))
        # 1. Referencia explicita entre parentesis en el tema (Art., Titulo, Reglamento, etc.)
        m_topic = re.search(
            r"\(([^)]*(?:Art|Título|Titulo|Reglamento|RNE|SINAGERD|Sección|Seccion|Ley DUS|Índice|Indice)[^)]*)\)",
            topic,
            re.IGNORECASE,
        )
        if m_topic:
            return m_topic.group(1).strip()
        # 2. Articulo explicito al inicio del contenido
        m_content = re.match(r"^(Art(?:[ií]culo)?\.\s*\d+[^\.\-\:]*)", text_content, re.IGNORECASE)
        if m_content:
            return m_content.group(1).strip()
        # 3. Identificador estable del fragmento como garantia de localizacion
        return f"Fragmento ID {chunk_id}"

    STOP_WORDS: ClassVar[set[str]] = {
        "a", "al", "ante", "bajo", "cabe", "como", "con", "contra", "de", "del",
        "desde", "el", "ella", "ellas", "ellos", "en", "entre", "es", "esa", "esas",
        "ese", "esos", "esta", "estas", "este", "estos", "fue", "fueron", "hacia",
        "hasta", "la", "las", "le", "les", "lo", "los", "me", "mi", "mis", "muy",
        "nos", "nosotros", "o", "para", "pero", "por", "que", "qué", "se", "según",
        "sin", "so", "sobre", "son", "su", "sus", "te", "ti", "tu", "tus", "un",
        "una", "unas", "unos", "y", "ya", "no"
    }

    @classmethod
    def extract_keywords(cls, query: str) -> list[str]:
        words = re.findall(r"[\wáéíóúüñ]+", query.lower())
        return [w for w in words if len(w) > 1 and w not in cls.STOP_WORDS]

    @classmethod
    def score_chunk(cls, chunk: dict[str, Any], query: str, keywords: list[str]) -> float:
        score = 0.0
        q_lower = query.lower()
        id_lower = str(chunk.get("id", "")).lower()
        topic_lower = str(chunk.get("topic", "")).lower()
        content_lower = str(chunk.get("content", "")).lower()
        doc_lower = str(chunk.get("document_name", "")).lower()
        code_lower = str(chunk.get("short_code", "")).lower()

        # Coincidencia directa por id (ej: "PDUPDM-02", "LEY27972-01")
        if id_lower and (id_lower in q_lower or q_lower in id_lower):
            score += 50.0

        # Coincidencia de frase completa
        if q_lower in topic_lower:
            score += 35.0
        if q_lower in content_lower:
            score += 25.0
        if q_lower in doc_lower or q_lower in code_lower:
            score += 15.0

        # Coincidencias por palabra clave
        for kw in keywords:
            if kw in id_lower:
                score += 20.0
            if kw in topic_lower:
                score += 15.0
            if kw in content_lower:
                score += 5.0
            if kw in doc_lower or kw in code_lower:
                score += 4.0

        # Ponderacion de alertas si la consulta alude a temas de riesgo/suelo/incompatibilidad
        alert_terms = {"alerta", "riesgo", "incompatible", "incompatibilidad", "conflicto", "vulnera", "suelo", "zonificacion", "proteccion"}
        if chunk.get("has_alert") and any(at in q_lower for at in alert_terms):
            score += 20.0

        return round(score, 2)

    GENERIC_META_WORDS: ClassVar[set[str]] = {
        "ley", "leyes", "norma", "normas", "normativa", "normativo", "normativas",
        "reglamento", "reglamentos", "decreto", "supremo", "legislativo",
        "articulo", "artículo", "articulos", "artículos", "seccion", "sección",
        "capitulo", "capítulo", "titulo", "título", "vigente", "vigentes",
        "actual", "actuales", "consulta", "consultar", "publico", "público",
        "publica", "pública", "publicos", "públicos", "publicas", "públicas",
        "sistema", "proyecto", "proyectos", "general", "peru", "perú", "estado",
    }

    @classmethod
    def is_relevant(
        cls, chunk: dict[str, Any], query: str, keywords: list[str]
    ) -> bool:
        """Evita resultados espurios o coincidencias superficiales sin sustento (HU2.1).

        Una consulta por identificador exacto (p. ej. "PDUPDM-02") o frase literal
        específica (>= 5 caracteres y no meramente genérica) siempre es relevante.
        Para consultas libres, separa términos meta/genéricos ("ley", "reglamento",
        "norma", etc.) de las palabras sustantivas y exige cobertura real para no
        devolver normas ante consultas ajenas o coincidencias débiles (CA-HU2.1-03).
        """
        normalized_query = query.strip().lower()
        searchable_parts = (
            str(chunk.get("id", "")),
            str(chunk.get("topic", "")),
            str(chunk.get("content", "")),
            str(chunk.get("document_name", "")),
            str(chunk.get("short_code", "")),
        )
        lowered_parts = tuple(part.lower() for part in searchable_parts)

        if normalized_query and normalized_query == lowered_parts[0]:
            return True

        if (
            len(normalized_query) >= 5
            and normalized_query not in cls.GENERIC_META_WORDS
            and any(normalized_query in part for part in lowered_parts[1:])
        ):
            return True

        if not keywords:
            return False

        substantive = [kw for kw in keywords if kw not in cls.GENERIC_META_WORDS]
        if not substantive:
            return False

        searchable_tokens: set[str] = set()
        for part in lowered_parts:
            searchable_tokens.update(re.findall(r"[\wáéíóúüñ]+", part))

        matched_substantive = {kw for kw in substantive if kw in searchable_tokens}
        if len(substantive) == 1:
            return len(matched_substantive) == 1

        required_matches = math.ceil(len(substantive) * 0.6)
        return len(matched_substantive) >= required_matches




class LegalAssessmentEngine:
    """Motor de evaluación y contraste de citas normativas verificables (HU2.2).

    Cumple estrictamente los criterios de aceptación:
    - CA-HU2.2-01: Cuando el sustento identifica un artículo o sección precisa,
      el informe jurídico muestra denominación de la norma, versión, artículo o sección
      y fragmento correspondiente para poder cotejarlo.
    - CA-HU2.2-02: Cuando el material recuperado no permite ubicar el artículo o fragmento
      exacto, el sistema indica 'Cita no verificable' y marca el hallazgo para revisión humana,
      sin inventar un número de artículo ficticio y sin confirmar cumplimiento.
    - Regla de rigor normativo (HU2.2):
      * Si la fuente es un resumen no oficial o un dato simulado, la cita se marca como no verificable.
      * Si el fragmento normativo no contiene el artículo exacto o es apócrifo, se rechaza la pseudovalidación.
      * Si cualquier hallazgo requiere revisión o no es verificable, la evaluación completa
        refleja requires_human_review=True y estado de advertencia.
    """

    @classmethod
    def _verify_exact_article_in_chunk(
        cls,
        claimed_loc: str,
        chunk_content: str,
        chunk_topic: str,
    ) -> tuple[bool, str | None]:
        """Coteja si el artículo o sección alegado existe realmente en el texto o tema del fragmento."""
        if not claimed_loc or not claimed_loc.strip():
            return False, None
        claimed = claimed_loc.strip()
        full_text = f"{chunk_topic} \n {chunk_content}"

        # 1. Coincidencia directa exacta de la cadena
        if claimed.lower() in full_text.lower():
            return True, claimed

        # 2. Búsqueda de números de artículo específicos (evitar falsos positivos como 'Art. 999')
        art_nums = re.findall(r"\b\d+(?:\.\d+)*(?:[a-z])?\b", claimed)
        if art_nums:
            matched_any = False
            for num in art_nums:
                pattern = rf"(?:Art|Arts|Art[ií]culo|Art[ií]culos)?\.?\s*(?:[Nn][°º]\s*)?{re.escape(num)}\b"
                if re.search(pattern, full_text, re.IGNORECASE):
                    matched_any = True
                    break
            if matched_any:
                return True, claimed
            return False, None

        # 3. Numerales romanos o secciones explícitas (e.g. Título III, Art. IV TP)
        roman_match = re.search(
            r"\b(?:T[ií]tulo|Cap[ií]tulo|Art(?:[ií]culo)?\.?)\s+([IVXLCDM]+(?:\s+TP)?)\b",
            claimed,
            re.IGNORECASE,
        )
        if roman_match and roman_match.group(0).lower() in full_text.lower():
            return True, claimed

        # 4. Palabras clave normativas identificables (SINAGERD, Ley DUS, RNE)
        law_match = re.search(r"\b(?:Ley\s+\d+|SINAGERD|RNE\s+[A-Z0-9\.]+)\b", claimed, re.IGNORECASE)
        if law_match and law_match.group(0).lower() in full_text.lower():
            return True, claimed

        return False, None

    @classmethod
    def build_citation(
        cls,
        finding_row: dict[str, Any],
        chunk: dict[str, Any] | None,
    ) -> dict[str, Any]:
        has_precise = bool(finding_row.get("has_precise_reference", True))
        custom_loc = finding_row.get("custom_article_or_section")

        # 1. Caso sin referencia precisa o sin fragmento normativo
        if not has_precise or not chunk:
            return {
                "fragment_id": chunk.get("id") if chunk else None,
                "document_name": chunk.get("document_name") if chunk else "Fuente no especificada",
                "short_code": chunk.get("short_code") if chunk else None,
                "version": chunk.get("version") if chunk else "No determinada",
                "article_or_section": None,  # ESTRICTAMENTE NONE: jamás se fabrica un artículo
                "fragment_content": chunk.get("content") if chunk else None,
                "citation_status": "no_verificable",
                "is_verifiable": False,
                "status_label": "Cita no verificable",
                "limitation_note": (
                    "El sustento no identifica un artículo o sección precisa en la normativa vigente. "
                    "Conforme a CA-HU2.2-02, el hallazgo se marca para revisión humana sin crear un número de artículo ficticio."
                ),
                "in_force": chunk.get("in_force", True) if chunk else False,
                "is_simulated": (chunk.get("data_origin") == "simulated") if chunk else True,
                "data_origin": chunk.get("data_origin", "simulated") if chunk else "simulated",
                "citation_hash": None,
            }

        # 2. Caso norma no vigente / derogada / sustituida
        if not chunk.get("in_force", True):
            return {
                "fragment_id": chunk.get("id"),
                "document_name": chunk.get("document_name"),
                "short_code": chunk.get("short_code"),
                "version": chunk.get("version"),
                "article_or_section": None,  # No aplicable como cita vigente
                "fragment_content": chunk.get("content"),
                "citation_status": "no_verificable",
                "is_verifiable": False,
                "status_label": "Cita no verificable (Norma sustituida o derogada)",
                "limitation_note": (
                    f"La norma '{chunk.get('document_name')}' ({chunk.get('version')}) está marcada como derogada o sustituida. "
                    "No es admisible como sustento legal aplicable para obra pública vigente (CA-HU2.2-02)."
                ),
                "in_force": False,
                "is_simulated": chunk.get("data_origin") == "simulated",
                "data_origin": chunk.get("data_origin", "public"),
                "citation_hash": None,
            }

        # 3. Caso fuente simulada o corpus no oficial (HU2.2)
        # Si la fuente es un resumen no oficial o dato simulado, NO debe marcarse como verificable
        is_simulated_chunk = (
            chunk.get("data_origin") == "simulated"
            or bool(chunk.get("is_simulated", False))
            or bool(finding_row.get("is_simulated", False))
            or chunk.get("data_origin") not in ("public", "oficial", "official")
        )
        if is_simulated_chunk:
            return {
                "fragment_id": chunk.get("id"),
                "document_name": chunk.get("document_name"),
                "short_code": chunk.get("short_code"),
                "version": chunk.get("version"),
                "article_or_section": None,  # No se certifica artículo de fuente simulada
                "fragment_content": chunk.get("content"),
                "citation_status": "no_verificable",
                "is_verifiable": False,
                "status_label": "Cita no verificable (Fuente o corpus simulado)",
                "limitation_note": (
                    "La fuente consultada corresponde a datos simulados o corpus no oficial. "
                    "Conforme a HU2.2, no puede certificarse como cita normativa comprobada ni sustento oficial."
                ),
                "in_force": True,
                "is_simulated": True,
                "data_origin": "simulated",
                "citation_hash": None,
            }

        # 4. Caso norma vigente: resolución y cotejo riguroso del artículo
        chunk_content = str(chunk.get("content", ""))
        chunk_topic = str(chunk.get("topic", ""))

        if custom_loc and str(custom_loc).strip():
            candidate_loc = str(custom_loc).strip()
            # Cotejar si el artículo alegado realmente existe en el fragmento o tema
            is_matched, verified_loc = cls._verify_exact_article_in_chunk(
                candidate_loc, chunk_content, chunk_topic
            )
            if not is_matched:
                return {
                    "fragment_id": chunk.get("id"),
                    "document_name": chunk.get("document_name"),
                    "short_code": chunk.get("short_code"),
                    "version": chunk.get("version"),
                    "article_or_section": None,  # Cita apócrifa o no coincidente: jamás validar
                    "fragment_content": chunk.get("content"),
                    "citation_status": "no_verificable",
                    "is_verifiable": False,
                    "status_label": "Cita no verificable (Artículo no coincide con el fragmento)",
                    "limitation_note": (
                        f"El artículo o acápite alegado ('{candidate_loc}') no coincide ni se encuentra "
                        "en el texto del fragmento normativo oficial. Conforme a HU2.2, se rechaza la pseudovalidación "
                        "y se requiere revisión humana."
                    ),
                    "in_force": True,
                    "is_simulated": False,
                    "data_origin": chunk.get("data_origin", "public"),
                    "citation_hash": None,
                }
            loc = verified_loc or candidate_loc
        else:
            loc = NormativeSearchEngine.resolve_location(chunk)

        # Validar si loc contiene un artículo o sección identificable real y no es genérico
        is_identifiable_loc = bool(
            re.search(
                r"(Art|Título|Titulo|Capítulo|Capitulo|Sección|Seccion|Reglamento|Ley DUS|RNE|SINAGERD|Índice|Indice)",
                loc,
                re.IGNORECASE,
            )
        ) and not loc.startswith("Fragmento ID")

        if not is_identifiable_loc:
            return {
                "fragment_id": chunk.get("id"),
                "document_name": chunk.get("document_name"),
                "short_code": chunk.get("short_code"),
                "version": chunk.get("version"),
                "article_or_section": None,  # Nunca inventar artículo ficticio
                "fragment_content": chunk.get("content"),
                "citation_status": "no_verificable",
                "is_verifiable": False,
                "status_label": "Cita no verificable (Fragmento sin artículo)",
                "limitation_note": (
                    "El fragmento normativo no contiene un artículo, sección o acápite identificable. "
                    "Conforme a CA-HU2.2-02, no se fabrica un número de artículo y se requiere revisión humana."
                ),
                "in_force": True,
                "is_simulated": False,
                "data_origin": chunk.get("data_origin", "public"),
                "citation_hash": None,
            }

        # 5. Caso exitoso: Cita exacta, oficial y comprobable (CA-HU2.2-01)
        citation_raw = f"{chunk['id']}:{chunk.get('version')}:{loc}:{chunk.get('content')}"
        c_hash = hashlib.sha256(citation_raw.encode("utf-8")).hexdigest()

        return {
            "fragment_id": chunk.get("id"),
            "document_name": chunk.get("document_name"),
            "short_code": chunk.get("short_code"),
            "version": chunk.get("version"),
            "article_or_section": loc,
            "fragment_content": chunk.get("content"),
            "citation_status": "verificable",
            "is_verifiable": True,
            "status_label": "Cita exacta y comprobable",
            "limitation_note": None,
            "in_force": True,
            "is_simulated": False,
            "data_origin": chunk.get("data_origin", "public"),
            "citation_hash": c_hash,
        }

    @classmethod
    def evaluate(
        cls,
        project_id: UUID,
        project_code: str,
        project_version_id: UUID | None,
        finding_rows: list[dict[str, Any]],
        chunks_by_id: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        assessment_id = uuid4()
        findings_output: list[dict[str, Any]] = []
        verifiable_count = 0
        unverifiable_count = 0

        for r in finding_rows:
            chunk_id = r.get("normative_chunk_id")
            chunk = chunks_by_id.get(str(chunk_id)) if chunk_id else None
            citation = cls.build_citation(r, chunk)

            # Regla de confirmación jurídica: un hallazgo no verificable NUNCA puede estar jurídicamente confirmado
            complies = r.get("complies")
            if not citation["is_verifiable"]:
                complies = None  # abstención / requiere revisión humana
                requires_review = True
            else:
                requires_review = bool(r.get("requires_human_review", False))

            if citation["is_verifiable"]:
                verifiable_count += 1
            else:
                unverifiable_count += 1

            findings_output.append({
                "finding_id": str(r.get("id") or uuid4()),
                "finding_key": r.get("finding_key", "general"),
                "title": r.get("title", "Hallazgo legal"),
                "description": r.get("description", ""),
                "complies": complies,
                "requires_human_review": requires_review,
                "citation": citation,
            })

        total = len(findings_output)
        any_unverifiable = (unverifiable_count > 0)
        any_requires_review = any(f["requires_human_review"] for f in findings_output)
        non_compliant_findings = [f for f in findings_output if f["complies"] is False]

        has_legal_alert = False
        legal_alert_motive = None
        legal_alert_norm = None

        if total == 0:
            status = "insufficient"
            status_label = "Información jurídica insuficiente"
            summary = "El expediente no registra antecedentes jurídicos ni hallazgos normativos vinculados."
            requires_human = True
        elif non_compliant_findings:
            # CA-HU2.3-01: Alerta por posible incompatibilidad sustentada con plan urbano o zona intangible
            first_nc = non_compliant_findings[0]
            has_legal_alert = True
            legal_alert_motive = first_nc.get("description") or first_nc.get("title")
            cit = first_nc.get("citation", {})
            norm_name = cit.get("document_name") or "Normativa territorial"
            art = cit.get("article_or_section") or "Disposición aplicable"
            legal_alert_norm = f"{norm_name} ({art})"

            status = "alerta_legal"
            status_label = "Alerta Legal: Incompatibilidad Normativa/Territorial"
            summary = (
                f"ALERTA LEGAL (HU2.3): Se identificó incompatibilidad con el plan urbano o afectación a zona intangible. "
                f"Sustento normativo: {legal_alert_norm}. Motivo: {legal_alert_motive}. "
                "Conforme a las reglas de control, se bloquea la aprobación definitiva y priorización hasta que la alerta sea resuelta o levantada por Asesoría Jurídica."
            )
            requires_human = True
        elif any_unverifiable or any_requires_review:
            # CA-HU2.3-02: Situación no comprobada -> Requiere revisión (sin alerta legal infundada)
            status = "requires_review"
            status_label = "Requiere revisión humana"
            summary = (
                f"Revisión jurídica preliminar: {verifiable_count} cita(s) exacta(s) y {unverifiable_count} "
                "cita(s) no verificable(s) o pendiente(s) de revisión humana. "
                "Conforme a CA-HU2.3-02, no se formula una alerta legal sin comprobación fehaciente; se requiere cotejo manual por el Asesor Jurídico."
            )
            requires_human = True
        else:
            status = "verified"
            status_label = "Citas normativas verificadas"
            summary = f"Revisión jurídica completa con {verifiable_count} cita(s) exacta(s) y comprobable(s)."
            requires_human = False

        is_any_simulated = any(f["citation"].get("is_simulated", False) for f in findings_output)

        return {
            "assessment_id": str(assessment_id),
            "project_id": str(project_id),
            "project_code": project_code,
            "project_version_id": str(project_version_id) if project_version_id else None,
            "status": status,
            "status_label": status_label,
            "summary": summary,
            "findings": findings_output,
            "total_findings": total,
            "verifiable_citations_count": verifiable_count,
            "unverifiable_citations_count": unverifiable_count,
            "requires_human_review": requires_human,
            "has_legal_alert": has_legal_alert,
            "legal_alert_motive": legal_alert_motive,
            "legal_alert_norm": legal_alert_norm,
            "is_resolved": False,
            "resolution_justification": None,
            "evaluated_at": datetime.now(timezone.utc),
            "is_simulated_corpus": is_any_simulated,
        }


class ProcurementVerificationEngine:
    """Motor determinista de verificación preliminar de Ley de Contrataciones Públicas (HU1.10).

    Normativa vigente contrastada contra el Compendio Oficial OECE (consultado 2026-11-20):
    - Ley N.° 32069 (vigente desde 22/04/2025; texto SPIJ/OECE act. 19/07/2026 con Leyes
      32103, 32185, 32187, 32515, D.Leg. 1715 y Ley 32732).
    - Reglamento D.S. N.° 009-2025-EF (vigente desde 22/04/2025; act. 26/10/2026 con
      D.S. N.° 001-2026-EF y D.S. N.° 222-2026-EF).
    - UIT 2026 oficial: S/ 5,500.00 (D.S. N.° 301-2025-EF) -> tope 8 UIT contratos menores = S/ 44,000.00.

    Principios estrictos HU1.10:
    - NUNCA infiere cumplimiento contractual a partir de title o description.
    - Excluye fuentes históricas/sustituidas (in_force=False) o simuladas (data_origin='simulated').
    - Si faltan antecedentes contractuales o sustento normativo vigente, responde 'requires_review'
      indicando qué falta (preliminary_compliant=None).
    - Ante posible incompatibilidad, señala el requisito y la remite a LEGAL_ADVISOR sin certificar infracción.
    """

    UIT_2026_PEN = Decimal("5500.00")
    MINOR_CONTRACT_MAX_UIT = 8
    MINOR_CONTRACT_MAX_PEN = Decimal("44000.00")
    CONSULTED_AT = "2026-11-20"
    OECE_COMPENDIUM_URL = (
        "https://www.gob.pe/institucion/oece/colecciones/45029-ley-n-32069-ley-general-de-contrataciones-publicas"
    )
    REGIME_LABELS: ClassVar[dict[str, str]] = {
        "ley_32069": "Ley N.° 32069 y D.S. N.° 009-2025-EF (Régimen general vigente)",
        "ley_30225_historica": "Ley N.° 30225 (Régimen anterior sustituido el 22/04/2025)",
        "ley_26850_historica": "Ley N.° 26850 (Norma histórica derogada)",
        "regimen_especial": "Régimen especial de contratación",
        "administracion_directa": "Ejecución por administración directa",
    }
    OBJECT_LABELS: ClassVar[dict[str, str]] = {
        "obra": "Obra pública",
        "bien": "Bienes",
        "servicio": "Servicios",
        "consultoria_obra": "Consultoría de obra",
    }
    PHASE_LABELS: ClassVar[dict[str, str]] = {
        "programacion_cmn": "Programación Multianual (PMBSO / CMN)",
        "actuaciones_preparatorias": "Actuaciones preparatorias",
        "seleccion": "Fase de selección",
        "ejecucion_contractual": "Ejecución contractual",
    }
    PROCEDURE_LABELS: ClassVar[dict[str, str]] = {
        "licitacion_publica": "Licitación pública",
        "concurso_publico": "Concurso público",
        "modalidad_abreviada": "Modalidad abreviada de procedimiento competitivo",
        "subasta_inversa_electronica": "Subasta inversa electrónica",
        "comparacion_precios": "Comparación de precios",
        "contrato_menor": "Contrato menor (<= 8 UIT)",
        "contratacion_no_competitiva": "Procedimiento no competitivo / Contratación directa",
    }

    @classmethod
    def _serialize_source(cls, chunk: dict[str, Any]) -> dict[str, Any]:
        in_force = bool(chunk.get("in_force", False))
        origin = str(chunk.get("data_origin") or "simulated")
        if not in_force:
            note = "Histórica / sustituida: conservada solo como antecedente y excluida de conformidad vigente."
        elif origin not in ("public", "official"):
            note = f"Origen '{origin}': excluido como fuente normativa oficial vinculante."
        else:
            note = "Vigente y verificable en Compendio OECE / MEF."
        eff = chunk.get("effective_from")
        con = chunk.get("consulted_at")
        return {
            "fragment_id": str(chunk["id"]),
            "document_name": str(chunk.get("document_name") or ""),
            "short_code": str(chunk.get("short_code") or ""),
            "version": str(chunk.get("version") or ""),
            "in_force": in_force,
            "data_origin": origin,
            "effective_from": eff.isoformat() if hasattr(eff, "isoformat") else (str(eff) if eff else None),
            "consulted_at": con.isoformat() if hasattr(con, "isoformat") else (str(con) if con else cls.CONSULTED_AT),
            "amendments_summary": chunk.get("amendments_summary"),
            "official_url": chunk.get("official_url") or cls.OECE_COMPENDIUM_URL,
            "status_note": note,
        }

    @classmethod
    def evaluate(
        cls,
        *,
        project: dict[str, Any],
        antecedent: dict[str, Any] | None,
        all_chunks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        active_by_id: dict[str, dict[str, Any]] = {}
        active_sources: list[dict[str, Any]] = []
        excluded_sources: list[dict[str, Any]] = []

        for c in all_chunks:
            cid = str(c["id"])
            serialized = cls._serialize_source(c)
            in_force = bool(c.get("in_force", False))
            origin = str(c.get("data_origin") or "simulated")
            version_str = str(c.get("version") or "").lower()
            is_stale_2024_seed = (
                cid.startswith("LEY32069-")
                and ("2024-v1" in version_str or "histor" in version_str)
            )
            if in_force and origin in ("public", "official") and not is_stale_2024_seed:
                active_by_id[cid] = c
                active_sources.append(serialized)
            else:
                if is_stale_2024_seed and in_force:
                    serialized["status_note"] = (
                        "Versión referencial '2024-v1' anterior a entrada en vigor (22/04/2025) y modificatorias Ley 32187; excluida por desactualizada."
                    )
                excluded_sources.append(serialized)

        active_ley32069 = [c for cid, c in active_by_id.items() if cid.startswith("LEY32069-")]
        active_reg = [c for cid, c in active_by_id.items() if cid.startswith("DS009-2025-")]

        missing_fields: list[str] = []
        examined: list[dict[str, Any]] = []

        if not active_ley32069 or not active_reg:
            missing_fields.append(
                "Sustento normativo vigente y actualizado de la Ley N.° 32069 y su Reglamento D.S. N.° 009-2025-EF en normative_documents"
            )

        rec = antecedent or {}
        if rec.get("data_origin") == "simulated":
            missing_fields.append(
                "Origen verificable de antecedentes contractuales (los datos ingresados figuran como simulados)"
            )

        regime = (rec.get("contracting_regime") or "").strip().lower() or None
        obj = (rec.get("contract_object") or "").strip().lower() or None
        phase = (rec.get("contracting_phase") or "").strip().lower() or None
        proc = (rec.get("selection_procedure") or "").strip().lower() or None
        req_def = rec.get("requirement_defined")
        in_cmn = rec.get("in_cmn_and_viability")
        budget_cert = rec.get("budget_certification_declared")
        pladicop = rec.get("pladicop_registration_planned")
        no_frac = rec.get("no_fractionation_declared")
        doc_ref = (rec.get("supporting_document_ref") or "").strip() or None

        if not regime:
            missing_fields.append("Régimen de contratación aplicable")
        if not obj:
            missing_fields.append("Objeto contractual (obra, bien, servicio o consultoría)")
        if not phase:
            missing_fields.append("Fase contractual (PMBSO/CMN, actuaciones preparatorias, selección o ejecución)")
        if not proc:
            missing_fields.append("Procedimiento o modalidad de contratación prevista")
        if req_def is None:
            missing_fields.append("Confirmación de requerimiento técnico / expediente de obra definido (Art. 46)")
        if in_cmn is None:
            missing_fields.append("Confirmación de inclusión en CMN y viabilidad Invierte.pe (Reg. Arts. 46.1.c / 54.3)")
        if budget_cert is None:
            missing_fields.append("Confirmación de certificación o previsión presupuestal (Reg. Art. 54.2.f)")
        if pladicop is None:
            missing_fields.append("Confirmación de registro en PLADICOP (Art. 41 mod. Ley 32187)")
        if no_frac is None:
            missing_fields.append("Confirmación de ausencia de fraccionamiento (Art. 50 / Reg. Art. 45)")
        if not doc_ref:
            missing_fields.append("Documento o referencia contractual verificable (ej. TdR, EETT, Informe DEC o PAC/CMN)")

        def _chunk_meta(cid: str, fallback_art: str) -> dict[str, Any]:
            c = active_by_id.get(cid)
            if not c:
                return {
                    "norm_id": None,
                    "norm_name": "Fuente normativa vigente no disponible en repositorio",
                    "norm_version": None,
                    "article_or_section": None,
                    "fragment_excerpt": None,
                    "official_url": cls.OECE_COMPENDIUM_URL,
                    "consulted_at": cls.CONSULTED_AT,
                    "amendments_note": None,
                }
            con = c.get("consulted_at")
            return {
                "norm_id": str(c["id"]),
                "norm_name": str(c.get("document_name") or ""),
                "norm_version": str(c.get("version") or ""),
                "article_or_section": fallback_art,
                "fragment_excerpt": str(c.get("content") or ""),
                "official_url": str(c.get("official_url") or cls.OECE_COMPENDIUM_URL),
                "consulted_at": con.isoformat() if hasattr(con, "isoformat") else cls.CONSULTED_AT,
                "amendments_note": c.get("amendments_summary"),
            }

        # Requisito 1: Régimen normativo aplicable y exclusión de normas sustituidas
        meta_r1 = _chunk_meta("LEY32069-01", "Art. 5.1 (Vigencia 22/04/2025 - D.S. 009-2025-EF)")
        if not regime:
            r1_status = "missing_data"
            r1_label = "Falta dato"
            r1_ev = "Sin régimen contractual declarado en el expediente (no se infiere del título ni de la descripción)."
            r1_comp = "Se requiere declarar expresamente el régimen de contratación aplicable."
        elif regime in ("ley_30225_historica", "ley_26850_historica"):
            r1_status = "possible_incompatibility"
            r1_label = "Posible incompatibilidad"
            r1_ev = f"Régimen declarado: {cls.REGIME_LABELS.get(regime, regime)}."
            r1_comp = (
                "Posible incompatibilidad normativa: el expediente invoca un régimen sustituido/derogado "
                "(Ley N.° 30225 o Ley N.° 26850), cuando desde el 22/04/2025 rige la Ley N.° 32069 y su Reglamento D.S. N.° 009-2025-EF. "
                "Remitido a Asesoría Jurídica para verificar si existe régimen transitorio aplicable, sin certificar infracción automática."
            )
        elif regime in ("regimen_especial", "administracion_directa"):
            r1_status = "requires_review"
            r1_label = "Requiere revisión"
            r1_ev = f"Régimen declarado: {cls.REGIME_LABELS.get(regime, regime)}."
            r1_comp = "Régimen especial o administración directa requiere validación humana sobre exclusión del ámbito de la Ley N.° 32069."
        elif not active_by_id.get("LEY32069-01"):
            r1_status = "missing_data"
            r1_label = "Falta sustento normativo"
            r1_ev = f"Régimen declarado: {cls.REGIME_LABELS.get(regime, regime)}."
            r1_comp = "Falta fragmento vigente de Ley N.° 32069 en normative_documents para emitir conclusión preliminar."
        else:
            r1_status = "compliant_preliminary"
            r1_label = "Compatible preliminar"
            r1_ev = f"Régimen declarado: {cls.REGIME_LABELS.get(regime, regime)}."
            r1_comp = "El expediente declara acogerse al marco vigente de la Ley N.° 32069 (en vigor desde 22/04/2025)."

        examined.append({
            "requirement_code": "REGIMEN_VIGENTE",
            "requirement_name": "Régimen contractual vigente (Ley N.° 32069)",
            "status": r1_status,
            "status_label": r1_label,
            "expedient_evidence": r1_ev,
            "comparison_detail": r1_comp,
            **meta_r1,
        })

        # Requisito 2: Objeto, procedimiento de selección y tope de cuantía (Arts. 34, 53, 54 Ley 32069 + UIT 2026)
        meta_r2 = _chunk_meta("LEY32069-03", "Arts. 34, 53 y 54 (concordado D.S. 301-2025-EF UIT 2026)")
        raw_budget = project.get("estimated_budget_pen")
        budget_pen = Decimal(str(raw_budget)) if raw_budget is not None else None
        if not obj or not phase or not proc:
            r2_status = "missing_data"
            r2_label = "Falta dato"
            r2_ev = f"Objeto: {obj or 'No indicado'} · Fase: {phase or 'No indicada'} · Procedimiento: {proc or 'No indicado'}."
            r2_comp = "Faltan antecedentes contractuales (objeto, fase o procedimiento) para contrastar Arts. 34, 53 y 54; no se infiere de descripción."
        elif proc == "contrato_menor" and budget_pen is not None and budget_pen > cls.MINOR_CONTRACT_MAX_PEN:
            r2_status = "possible_incompatibility"
            r2_label = "Posible incompatibilidad"
            r2_ev = (
                f"Procedimiento: Contrato menor (<= 8 UIT) · Objeto: {cls.OBJECT_LABELS.get(obj, obj)} · "
                f"Presupuesto estimado: S/ {budget_pen:,.2f}."
            )
            r2_comp = (
                f"Posible incompatibilidad con el Art. 34.1 de la Ley N.° 32069: se declara contrato menor pese a que "
                f"el presupuesto estimado (S/ {budget_pen:,.2f}) supera el tope legal de 8 UIT vigentes para 2026 "
                f"(S/ {cls.MINOR_CONTRACT_MAX_PEN:,.2f} según D.S. N.° 301-2025-EF). "
                "Debe remitirse a Asesoría Jurídica y DEC para evaluar procedimiento competitivo, sin certificar infracción definitiva."
            )
        elif obj == "obra" and proc == "concurso_publico":
            r2_status = "possible_incompatibility"
            r2_label = "Posible incompatibilidad"
            r2_ev = f"Objeto: Obra pública · Procedimiento declarado: Concurso público · Fase: {cls.PHASE_LABELS.get(phase, phase)}."
            r2_comp = (
                "Posible incompatibilidad con el Art. 54.1 de la Ley N.° 32069: la licitación pública corresponde a bienes y obras "
                "(Art. 54.1.a), mientras el concurso público aplica a servicios (Art. 54.1.b). "
                "Se remite a Asesoría Jurídica para revisión humana sin certificar infracción automática."
            )
        elif obj in ("servicio", "consultoria_obra") and proc == "licitacion_publica":
            r2_status = "possible_incompatibility"
            r2_label = "Posible incompatibilidad"
            r2_ev = f"Objeto: {cls.OBJECT_LABELS.get(obj, obj)} · Procedimiento declarado: Licitación pública."
            r2_comp = (
                "Posible incompatibilidad con el Art. 54.1 de la Ley N.° 32069: el concurso público corresponde a servicios/consultorías "
                "(Art. 54.1.b), mientras la licitación pública aplica a bienes y obras (Art. 54.1.a). Remitido a Asesoría Jurídica."
            )
        elif proc == "contratacion_no_competitiva":
            r2_status = "requires_review"
            r2_label = "Requiere revisión"
            r2_ev = f"Procedimiento no competitivo declarado · Objeto: {cls.OBJECT_LABELS.get(obj, obj)}."
            r2_comp = (
                "Conforme al Art. 54.3 de la Ley N.° 32069 y Art. 46.1.b del Reglamento (D.S. N.° 009-2025-EF mod. D.S. 001-2026-EF), "
                "todo procedimiento no competitivo exige validación jurídica expresa de causal legal habilitante."
            )
        elif not active_by_id.get("LEY32069-03"):
            r2_status = "missing_data"
            r2_label = "Falta sustento normativo"
            r2_ev = f"Objeto: {cls.OBJECT_LABELS.get(obj, obj)} · Procedimiento: {cls.PROCEDURE_LABELS.get(proc, proc)}."
            r2_comp = "Falta fragmento vigente LEY32069-03 para validar procedimiento."
        else:
            r2_status = "compliant_preliminary"
            r2_label = "Compatible preliminar"
            budget_txt = f"S/ {budget_pen:,.2f}" if budget_pen is not None else "No especificado"
            r2_ev = (
                f"Objeto: {cls.OBJECT_LABELS.get(obj, obj)} · Fase: {cls.PHASE_LABELS.get(phase, phase)} · "
                f"Procedimiento: {cls.PROCEDURE_LABELS.get(proc, proc)} · Monto: {budget_txt}."
            )
            r2_comp = (
                "Consistente preliminarmente con los Arts. 34, 53 y 54.1 de la Ley N.° 32069 "
                "(sujeto a verificación de cuantía y bases por la DEC conforme al D.S. N.° 009-2025-EF mod. D.S. N.° 001-2026-EF)."
            )

        examined.append({
            "requirement_code": "PROCEDIMIENTO_Y_CUANTIA",
            "requirement_name": "Objeto, procedimiento de selección y límite 8 UIT (Arts. 34, 53 y 54)",
            "status": r2_status,
            "status_label": r2_label,
            "expedient_evidence": r2_ev,
            "comparison_detail": r2_comp,
            **meta_r2,
        })

        # Requisito 3: Requerimiento, inclusión en CMN, viabilidad y certificación presupuestal
        meta_r3 = _chunk_meta("DS009-2025-REG-54", "Ley Art. 46; Reg. D.S. 009-2025-EF Arts. 44, 46.1.c, 54.2 y 54.3")
        if req_def is None or in_cmn is None or budget_cert is None or not doc_ref:
            r3_status = "missing_data"
            r3_label = "Falta dato"
            r3_ev = (
                f"Requerimiento definido: {req_def if req_def is not None else 'No informado'} · "
                f"CMN/Viabilidad: {in_cmn if in_cmn is not None else 'No informado'} · "
                f"Certificación presupuestal: {budget_cert if budget_cert is not None else 'No informado'} · "
                f"Doc. ref.: {doc_ref or 'No informado'}."
            )
            r3_comp = "Faltan antecedentes sobre requerimiento, CMN/viabilidad Invierte.pe, certificación presupuestal o documento verificable."
        elif req_def is False or in_cmn is False or budget_cert is False:
            r3_status = "possible_incompatibility"
            r3_label = "Posible incompatibilidad"
            neg = []
            if req_def is False:
                neg.append("sin requerimiento técnico/funcional definido (Art. 46 / Reg. Art. 44)")
            if in_cmn is False:
                neg.append("sin registro en CMN aprobado o viabilidad Invierte.pe (Reg. Arts. 46.1.c y 54.3)")
            if budget_cert is False:
                neg.append("sin certificación o previsión presupuestal (Reg. Art. 54.2.f)")
            r3_ev = f"Antecedente {doc_ref}: se declara incumplimiento preliminar en {', '.join(neg)}."
            r3_comp = (
                f"Posible incompatibilidad con actuaciones preparatorias obligatorias ({'; '.join(neg)}). "
                "Remitido a Asesoría Jurídica para evaluación humana sin certificar infracción automática."
            )
        elif not active_by_id.get("DS009-2025-REG-54") or not active_by_id.get("LEY32069-02"):
            r3_status = "missing_data"
            r3_label = "Falta sustento normativo"
            r3_ev = f"Documento contractual declarado: {doc_ref}."
            r3_comp = "Faltan fragmentos vigentes de Art. 46 y Reglamento Arts. 44/54 para emitir conclusión preliminar."
        else:
            r3_status = "compliant_preliminary"
            r3_label = "Compatible preliminar"
            r3_ev = f"Doc. sustento: {doc_ref}. Requerimiento definido, previsto en CMN/Invierte.pe y con certificación presupuestal declarada."
            r3_comp = (
                "Alineado preliminarmente con Art. 46 de la Ley N.° 32069 y Arts. 44, 46.1.c y 54.2/54.3 del D.S. N.° 009-2025-EF "
                "(mod. D.S. N.° 001-2026-EF), sujeto a cotejo del expediente de contratación aprobado."
            )

        examined.append({
            "requirement_code": "REQUERIMIENTO_CMN_PRESUPUESTO",
            "requirement_name": "Requerimiento, CMN/Invierte.pe y certificación presupuestal (Art. 46; Reg. Art. 54)",
            "status": r3_status,
            "status_label": r3_label,
            "expedient_evidence": r3_ev,
            "comparison_detail": r3_comp,
            **meta_r3,
        })

        # Requisito 4: Trazabilidad en PLADICOP (Art. 41 mod. Ley 32187) y no fraccionamiento (Art. 50 / Reg. Art. 45)
        meta_r4 = _chunk_meta("LEY32069-06", "Art. 41 (mod. Ley 32187) y Art. 50 / Reg. Art. 45")
        if pladicop is None or no_frac is None:
            r4_status = "missing_data"
            r4_label = "Falta dato"
            r4_ev = (
                f"Registro en PLADICOP: {pladicop if pladicop is not None else 'No informado'} · "
                f"Sin fraccionamiento: {no_frac if no_frac is not None else 'No informado'}."
            )
            r4_comp = "Falta declarar previsión de registro en PLADICOP (Art. 41 mod. Ley 32187) y verificación de no fraccionamiento (Art. 50)."
        elif pladicop is False or no_frac is False:
            r4_status = "possible_incompatibility"
            r4_label = "Posible incompatibilidad"
            issues = []
            if pladicop is False:
                issues.append("omisión de registro obligatorio en PLADICOP (Art. 41.2 mod. Ley N.° 32187)")
            if no_frac is False:
                issues.append("indicio o declaración de fraccionamiento prohibido (Art. 50.1 / Reg. Art. 45)")
            r4_ev = f"Declaración contractual advierte: {', '.join(issues)}."
            r4_comp = (
                f"Posible incompatibilidad con reglas de transparencia y planificación ({'; '.join(issues)}). "
                "Remitido a Asesoría Jurídica para validación humana sin certificar infracción automática."
            )
        elif not active_by_id.get("LEY32069-06") or not active_by_id.get("LEY32069-02"):
            r4_status = "missing_data"
            r4_label = "Falta sustento normativo"
            r4_ev = "PLADICOP y no fraccionamiento declarados en expediente."
            r4_comp = "Falta fragmento vigente Art. 41 (mod. Ley 32187) en repositorio."
        else:
            r4_status = "compliant_preliminary"
            r4_label = "Compatible preliminar"
            r4_ev = "Se declara registro previsto en PLADICOP y ausencia de fraccionamiento indebido."
            r4_comp = (
                "Compatible preliminarmente con Art. 41.1/41.2 (mod. por Ley N.° 32187, vig. 01/01/2025) y Art. 50.1 de la Ley N.° 32069 / "
                "Art. 45 del Reglamento D.S. N.° 009-2025-EF."
            )

        examined.append({
            "requirement_code": "PLADICOP_Y_NO_FRACCIONAMIENTO",
            "requirement_name": "Trazabilidad PLADICOP (Art. 41 mod. Ley 32187) y no fraccionamiento (Art. 50)",
            "status": r4_status,
            "status_label": r4_label,
            "expedient_evidence": r4_ev,
            "comparison_detail": r4_comp,
            **meta_r4,
        })

        # Consolidar resultado estricto CA-HU1.10-01 / CA-HU1.10-02 / CA-HU1.10-03
        incompatibilities = [e for e in examined if e["status"] == "possible_incompatibility"]
        manual_triggers = [e for e in examined if e["status"] == "requires_review"]

        if incompatibilities:
            first_inc = incompatibilities[0]
            affected_req = f"{first_inc['requirement_name']} ({first_inc.get('article_or_section') or 'Ley 32069'})"
            obs_text = " ".join(e["comparison_detail"] for e in incompatibilities)
            return {
                "status": "possible_incompatibility",
                "status_label": "Posible incompatibilidad (Derivado a Asesoría Jurídica)",
                "preliminary_compliant": False,
                "requires_human_review": True,
                "certified_infraction": False,
                "sent_to_legal_advisor": True,
                "advisory_review_status": "pending",
                "summary": (
                    f"Posible incompatibilidad contractual detectada en '{affected_req}'. "
                    f"{obs_text} "
                    "El sistema NO certifica infracción definitiva y remite la observación al Asesor Jurídico para validación humana (CA-HU1.10-02)."
                ),
                "affected_requirement": affected_req,
                "observation_explanation": obs_text,
                "missing_fields": missing_fields,
                "examined_requirements": examined,
                "normative_sources": active_sources,
                "superseded_sources_excluded": excluded_sources,
                "consulted_at": cls.CONSULTED_AT,
                "evaluated_at": now,
            }

        if missing_fields or manual_triggers:
            reasons_txt = "; ".join(missing_fields) if missing_fields else "; ".join(m["comparison_detail"] for m in manual_triggers)
            return {
                "status": "requires_review",
                "status_label": "Requiere revisión",
                "preliminary_compliant": None,  # Abstención estricta (CA-HU1.10-03)
                "requires_human_review": True,
                "certified_infraction": False,
                "sent_to_legal_advisor": True,
                "advisory_review_status": "pending",
                "summary": (
                    "Revisión contractual no concluyente (CA-HU1.10-03): "
                    f"faltan antecedentes o requieren cotejo humano ({reasons_txt}). "
                    "No se infiere cumplimiento desde el título o descripción general del proyecto y no se afirma cumplimiento ni incumplimiento definitivo."
                ),
                "affected_requirement": None,
                "observation_explanation": None,
                "missing_fields": missing_fields,
                "examined_requirements": examined,
                "normative_sources": active_sources,
                "superseded_sources_excluded": excluded_sources,
                "consulted_at": cls.CONSULTED_AT,
                "evaluated_at": now,
            }

        # CA-HU1.10-01: Conclusión preliminar sustentada con norma, versión y fragmento verificable
        return {
            "status": "verified_preliminary",
            "status_label": "Conclusión preliminar sustentada",
            "preliminary_compliant": True,
            "requires_human_review": True,  # Alcance estrictamente preliminar sujeto a revisión humana/DEC
            "certified_infraction": False,
            "sent_to_legal_advisor": False,
            "advisory_review_status": "not_required",
            "summary": (
                "Conclusión preliminar sustentada (CA-HU1.10-01): los antecedentes contractuales declarados para esta versión "
                "resultan preliminarmente compatibles con los 4 bloques examinados de la Ley N.° 32069 (v. 2026-07-19-OECE) y "
                "su Reglamento D.S. N.° 009-2025-EF (v. 2026-10-26-OECE, mod. D.S. N.° 001-2026-EF). "
                "Esta conclusión tiene carácter preliminar y está sujeta a validación documental por la DEC y Asesoría Jurídica."
            ),
            "affected_requirement": None,
            "observation_explanation": None,
            "missing_fields": [],
            "examined_requirements": examined,
            "normative_sources": active_sources,
            "superseded_sources_excluded": excluded_sources,
            "consulted_at": cls.CONSULTED_AT,
            "evaluated_at": now,
        }


class PertDurationEngine:
    """Motor de cálculo y validación de estimación de duración de obra e incertidumbre (PERT) - HU1.9.

    Criterios de aceptación:
    - CA-HU1.9-01: Duración esperada con fórmula (a + 4m + b)/6, desviación estándar (b - a)/6,
      varianza ((b - a)/6)², e indicación expresa de estimación referencial y no plazo garantizado.
    - CA-HU1.9-02: Validación estricta de completitud, números finitos y 0 <= a <= m <= b,
      señalando con precisión qué valor debe corregirse y por qué.
    """

    FORMULA_APPLIED = (
        "Duración esperada: μ = (a + 4m + b) / 6; "
        "Desviación estándar (incertidumbre): σ = (b - a) / 6; "
        "Varianza: σ² = ((b - a) / 6)²"
    )

    DISCLAIMER = (
        "La duración calculada bajo metodología PERT representa una aproximación probabilística sujeta a "
        "variabilidad e incertidumbre durante la ejecución de la obra. No constituye compromiso contractual "
        "ni plazo garantizado de entrega. Esta estimación no reemplaza la programación detallada de obra ni "
        "el cronograma vinculante de ejecución."
    )

    @classmethod
    def validate_inputs(
        cls,
        optimistic: Any,
        most_likely: Any,
        pessimistic: Any,
        time_unit: str = "meses",
    ) -> tuple[Decimal, Decimal, Decimal, str]:
        """Valida que a, m, b estén completos, sean números finitos y cumplan 0 <= a <= m <= b."""
        # 1. Completitud
        if optimistic is None:
            raise EstimacionPERTInvalidaError(
                "Falta ingresar la duración optimista (a).",
                campo="optimistic_duration",
                motivo="Falta el valor de duración optimista (a). Debe ser un número mayor o igual a 0.",
            )
        if most_likely is None:
            raise EstimacionPERTInvalidaError(
                "Falta ingresar la duración más probable (m).",
                campo="most_likely_duration",
                motivo="Falta el valor de duración más probable (m). Debe cumplir a <= m <= b.",
            )
        if pessimistic is None:
            raise EstimacionPERTInvalidaError(
                "Falta ingresar la duración pesimista (b).",
                campo="pessimistic_duration",
                motivo="Falta el valor de duración pesimista (b). Debe cumplir m <= b.",
            )

        # 2. Conversión a Decimal y verificación de finitud
        try:
            a_val = float(optimistic)
            if math.isnan(a_val) or math.isinf(a_val):
                raise ValueError()
            a_dec = Decimal(str(optimistic)).quantize(Decimal("0.0001"))
        except Exception:
            raise EstimacionPERTInvalidaError(
                "La duración optimista (a) debe ser un número finito válido.",
                campo="optimistic_duration",
                motivo="El valor ingresado para la duración optimista no es un número finito.",
            )

        try:
            m_val = float(most_likely)
            if math.isnan(m_val) or math.isinf(m_val):
                raise ValueError()
            m_dec = Decimal(str(most_likely)).quantize(Decimal("0.0001"))
        except Exception:
            raise EstimacionPERTInvalidaError(
                "La duración más probable (m) debe ser un número finito válido.",
                campo="most_likely_duration",
                motivo="El valor ingresado para la duración más probable no es un número finito.",
            )

        try:
            b_val = float(pessimistic)
            if math.isnan(b_val) or math.isinf(b_val):
                raise ValueError()
            b_dec = Decimal(str(pessimistic)).quantize(Decimal("0.0001"))
        except Exception:
            raise EstimacionPERTInvalidaError(
                "La duración pesimista (b) debe ser un número finito válido.",
                campo="pessimistic_duration",
                motivo="El valor ingresado para la duración pesimista no es un número finito.",
            )

        # 3. No negatividad (0 <= a, 0 <= m, 0 <= b)
        if a_dec < Decimal("0"):
            raise EstimacionPERTInvalidaError(
                f"La duración optimista (a = {a_dec:.2f}) no puede ser negativa; debe ser mayor o igual a 0.",
                campo="optimistic_duration",
                motivo="La duración optimista no puede ser un valor negativo.",
            )
        if m_dec < Decimal("0"):
            raise EstimacionPERTInvalidaError(
                f"La duración más probable (m = {m_dec:.2f}) no puede ser negativa; debe ser mayor o igual a 0.",
                campo="most_likely_duration",
                motivo="La duración más probable no puede ser un valor negativo.",
            )
        if b_dec < Decimal("0"):
            raise EstimacionPERTInvalidaError(
                f"La duración pesimista (b = {b_dec:.2f}) no puede ser negativa; debe ser mayor o igual a 0.",
                campo="pessimistic_duration",
                motivo="La duración pesimista no puede ser un valor negativo.",
            )

        # 4. Orden jerárquico 0 <= a <= m <= b
        if a_dec > m_dec:
            raise EstimacionPERTInvalidaError(
                f"Orden inválido: la duración optimista (a = {a_dec:.2f}) no puede ser mayor que la duración más probable (m = {m_dec:.2f}).",
                campo="optimistic_duration",
                motivo=f"La estimación optimista ({a_dec:.2f}) supera a la más probable ({m_dec:.2f}), violando la condición a <= m.",
            )
        if m_dec > b_dec:
            raise EstimacionPERTInvalidaError(
                f"Orden inválido: la duración más probable (m = {m_dec:.2f}) no puede ser mayor que la duración pesimista (b = {b_dec:.2f}).",
                campo="most_likely_duration",
                motivo=f"La estimación más probable ({m_dec:.2f}) supera a la pesimista ({b_dec:.2f}), violando la condición m <= b.",
            )
        if a_dec > b_dec:
            raise EstimacionPERTInvalidaError(
                f"Orden inválido: la duración optimista (a = {a_dec:.2f}) no puede ser mayor que la duración pesimista (b = {b_dec:.2f}).",
                campo="pessimistic_duration",
                motivo=f"La estimación optimista ({a_dec:.2f}) supera a la pesimista ({b_dec:.2f}), violando la condición a <= b.",
            )

        clean_unit = time_unit.strip().lower() if time_unit and time_unit.strip() else "meses"
        if clean_unit not in ("meses", "días", "dias", "semanas"):
            clean_unit = "meses"
        if clean_unit == "dias":
            clean_unit = "días"

        return a_dec, m_dec, b_dec, clean_unit

    @classmethod
    def calculate(
        cls,
        optimistic: Decimal,
        most_likely: Decimal,
        pessimistic: Decimal,
        time_unit: str = "meses",
    ) -> dict[str, Any]:
        """Calcula duración esperada PERT, desviación estándar, varianza e intervalo."""
        six = Decimal("6")
        expected = (optimistic + (Decimal("4") * most_likely) + pessimistic) / six
        std_dev = (pessimistic - optimistic) / six
        variance = std_dev * std_dev

        interval_low = expected - std_dev
        if interval_low < Decimal("0"):
            interval_low = Decimal("0")
        interval_high = expected + std_dev

        explanation = (
            f"Duración esperada PERT: ({optimistic:.2f} + 4×{most_likely:.2f} + {pessimistic:.2f}) / 6 = {expected:.2f} {time_unit}. "
            f"Incertidumbre (desviación estándar PERT): ({pessimistic:.2f} - {optimistic:.2f}) / 6 = ±{std_dev:.2f} {time_unit}. "
            f"Varianza del plazo: {variance:.4f} {time_unit}². "
            f"Rango esperado (μ ± σ): [{interval_low:.2f}, {interval_high:.2f}] {time_unit}."
        )

        return {
            "time_unit": time_unit,
            "optimistic_duration": optimistic,
            "most_likely_duration": most_likely,
            "pessimistic_duration": pessimistic,
            "expected_duration": expected,
            "standard_deviation": std_dev,
            "variance": variance,
            "confidence_interval_low": interval_low,
            "confidence_interval_high": interval_high,
            "formula_applied": cls.FORMULA_APPLIED,
            "disclaimer": cls.DISCLAIMER,
            "explanation": explanation,
        }

class RiskCheckpointEngine:
    """Motor de gestión de puntos de control (checkpoints) y pausas de flujo ante riesgos altos (HU3.1).

    Criterios cumplidos:
    - CA-HU3.1-01: Si una evaluación detecta un riesgo alto configurado para revisión humana,
      el sistema detiene el procesamiento en un punto de control persistente, establece el estado
      'Pendiente de revisión' (pending_review) y no continúa automáticamente hacia la finalización.
    - CA-HU3.1-02: Si no hay riesgo alto (riesgo bajo o moderado, sin causa de revisión), el flujo
      continúa normalmente hacia la siguiente etapa sin solicitar revisión humana innecesaria.
    """

    PAUSE_RISK_LEVELS: ClassVar[set[str]] = {"high"}

    @classmethod
    def detect_high_risk_condition(
        cls,
        conn: psycopg.Connection,
        project_version_id: UUID,
    ) -> dict[str, Any] | None:
        """Detecta si la versión del expediente cumple una condición configurada de riesgo alto.

        CA-HU3.4-01: Evita que el mismo riesgo ya autorizado vuelva a crear la misma pausa,
        sin ignorar riesgos nuevos o evaluaciones en nuevas versiones.
        """
        with conn.cursor(row_factory=dict_row) as cur:
            # Consultar si existen puntos de control ya autorizados y resueltos para esta versión específica
            resolved_cps = cur.execute(
                """
                SELECT id, risk_source, reason, details
                FROM evaluation_checkpoints
                WHERE project_version_id = %s AND status = 'resolved'
                """,
                (project_version_id,),
            ).fetchall()

            # 1. Comprobar evaluaciones ambientales registradas para la versión
            row = cur.execute(
                """
                SELECT id, project_id, project_version_id, risk_level, risk_observation,
                       requires_human_review, declared_vs_evidence_json, impacts_json
                FROM environmental_assessments
                WHERE project_version_id = %s
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (project_version_id,),
            ).fetchone()

        if row:
            if row["risk_level"] in cls.PAUSE_RISK_LEVELS and row.get("requires_human_review"):
                declared_ev = row.get("declared_vs_evidence_json") or {}
                prox = declared_ev.get("proximity_to_water_meters")
                in_risk = declared_ev.get("is_in_protected_or_risk_area")

                cause_items = []
                if prox is not None and float(prox) < 50.0:
                    cause_items.append(f"Proximidad crítica a cuerpo de agua ({float(prox):.1f} m < 50 m reglamentarios)")
                if in_risk:
                    cause_items.append("Emplazamiento en zona clasificada de riesgo territorial o faja marginal intangible")

                cause = (
                    f"Causa técnica de pausa: Riesgo ambiental alto por {'; '.join(cause_items)}."
                    if cause_items
                    else "Causa técnica de pausa: Detección de condición de riesgo ambiental territorial alto."
                )

                verifiable = declared_ev.get("verifiable_evidence")
                if verifiable and verifiable.strip():
                    obs = f"Comprobación técnica requerida: {verifiable.strip()} Requiere verificar delimitación física de faja marginal y sustento de mitigación formal."
                elif row.get("risk_observation") and row["risk_observation"].strip() != cause:
                    obs = row["risk_observation"].strip()
                else:
                    obs = "Observación pendiente de aclaración"

                env_id_str = str(row["id"])
                # Comprobar si este riesgo específico ya fue resuelto y autorizado por la Gerencia en esta versión
                is_already_authorized = any(
                    cp["risk_source"] == "environmental"
                    and (
                        (isinstance(cp["details"], dict) and cp["details"].get("environmental_assessment_id") == env_id_str)
                        or cp["reason"] == cause
                    )
                    for cp in resolved_cps
                )

                if not is_already_authorized:
                    return {
                        "risk_source": "environmental",
                        "risk_source_label": "Impacto ambiental territorial (Recurso hídrico / Ecosistema)",
                        "risk_level": row["risk_level"],
                        "risk_level_label": "Riesgo Alto",
                        "reason": cause,
                        "details": {
                            "environmental_assessment_id": env_id_str,
                            "risk_level": row["risk_level"],
                            "cause": cause,
                            "observation": obs,
                            "risk_observation": obs,
                            "requires_human_review": True,
                            "declared_vs_evidence": declared_ev,
                        },
                    }
            # Si ya existe una evaluación formal registrada para esta versión y no es de riesgo alto
            # (o el riesgo ya fue formalmente resuelto), no hereda riesgos ni registros de versiones anteriores.
            return None

        # 2. Si aún no existe fila en environmental_assessments, inspeccionar project_environmental_records
        # asegurando que solo aplique a registros de esta versión específica (o v1 si es retrocompatible)
        with conn.cursor(row_factory=dict_row) as cur:
            rec_row = cur.execute(
            """
            SELECT r.id, r.project_id, r.environmental_component, r.declared_impacts,
                   r.verifiable_evidence, r.proximity_to_water_meters, r.is_in_protected_or_risk_area,
                   r.source_name
            FROM project_environmental_records r
            JOIN project_versions pv ON pv.id = %s
            WHERE (
                (r.project_version_id = pv.id)
                OR (r.project_version_id IS NULL AND r.project_id = pv.project_id AND pv.version_number = 1)
            ) AND (
                r.is_in_protected_or_risk_area = TRUE
                OR (r.proximity_to_water_meters IS NOT NULL AND r.proximity_to_water_meters < 50.0)
            )
            LIMIT 1
            """,
            (project_version_id,),
        ).fetchone()

        if rec_row:
            cause_text = (
                f"Causa técnica de pausa: Componente '{rec_row['environmental_component']}'. "
                f"Distancia crítica a cuerpo de agua ({rec_row['proximity_to_water_meters']} m < 50 m reglamentarios) en zona clasificada de riesgo territorial."
            )
            obs_text = (
                f"Comprobación técnica requerida: {rec_row['verifiable_evidence']}. Requiere verificar delimitación física y sustento de mitigaciones aprobado por ANA."
                if rec_row.get("verifiable_evidence")
                else "Observación pendiente de aclaración"
            )

            rec_id_str = str(rec_row["id"])
            is_rec_already_authorized = any(
                cp["risk_source"] == "environmental"
                and (
                    (isinstance(cp["details"], dict) and cp["details"].get("record_id") == rec_id_str)
                    or cp["reason"] == cause_text
                )
                for cp in resolved_cps
            )

            if not is_rec_already_authorized:
                return {
                    "risk_source": "environmental",
                    "risk_source_label": "Impacto ambiental territorial (Recurso hídrico / Ecosistema)",
                    "risk_level": "high",
                    "risk_level_label": "Riesgo Alto",
                    "reason": cause_text,
                    "details": {
                        "record_id": rec_id_str,
                        "component": rec_row["environmental_component"],
                        "proximity_to_water_meters": float(rec_row["proximity_to_water_meters"]) if rec_row["proximity_to_water_meters"] else None,
                        "is_in_protected_or_risk_area": rec_row["is_in_protected_or_risk_area"],
                        "source_name": rec_row["source_name"],
                        "cause": cause_text,
                        "observation": obs_text,
                        "risk_observation": obs_text,
                    },
                }

        return None

    @classmethod
    def get_active_checkpoint(
        cls,
        conn: psycopg.Connection,
        project_version_id: UUID,
    ) -> dict[str, Any] | None:
        """Recupera el punto de control pendiente activo para la versión específica del expediente (HU3.1 / HU3.2)."""
        row = conn.execute(
            """
            SELECT id AS checkpoint_id, project_id, project_version_id, evaluation_id,
                   checkpoint_type, status, reason, risk_source, risk_level, details,
                   resolved_by_user_id, resolution_motive, resolution_conditions, resolved_at,
                   created_at, updated_at
            FROM evaluation_checkpoints
            WHERE project_version_id = %s AND status = 'pending_review'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_version_id,),
        ).fetchone()

        if not row:
            return None

        details = row["details"] if isinstance(row["details"], dict) else {}

        # 1. Causa específica de la pausa
        raw_cause = details.get("cause") or row.get("reason")
        cause = raw_cause.strip() if isinstance(raw_cause, str) and raw_cause.strip() else None

        # 2. Observación concreta que requiere revisión humana
        # Buscar en details exclusivamente por la observación de revisión humana
        raw_obs = details.get("observation") or details.get("risk_observation")
        if not raw_obs and isinstance(details.get("declared_vs_evidence"), dict):
            raw_obs = details["declared_vs_evidence"].get("verifiable_evidence")

        # REGLA HU3.2: NO copiar 'cause' a 'observation' cuando esta última falte.
        # Mostrar 'Observación pendiente de aclaración' y conservar el estado pendiente.
        if raw_obs and isinstance(raw_obs, str) and raw_obs.strip():
            # Si raw_obs es idéntico a cause (datos previos no diferenciados)
            if raw_obs.strip() == (cause or "").strip() and isinstance(details.get("declared_vs_evidence"), dict):
                verifiable = details["declared_vs_evidence"].get("verifiable_evidence")
                if verifiable and isinstance(verifiable, str) and verifiable.strip():
                    observation = f"Comprobación técnica requerida: {verifiable.strip()}"
                else:
                    observation = "Observación pendiente de aclaración"
            else:
                observation = raw_obs.strip()
        else:
            observation = "Observación pendiente de aclaración"

        fallback_cause = "Información incompleta: La causa técnica específica de la pausa no se encuentra registrada."
        reason_text = cause or fallback_cause

        return {
            "checkpoint_id": row["checkpoint_id"],
            "project_id": row["project_id"],
            "project_version_id": row["project_version_id"],
            "evaluation_id": row["evaluation_id"],
            "checkpoint_type": row["checkpoint_type"],
            "status": row["status"],
            "status_label": "Pendiente de revisión",
            "reason": reason_text,
            "cause": cause,
            "observation": observation,
            "risk_source": row["risk_source"] or "no_especificado",
            "risk_source_label": {
                "environmental": "Impacto ambiental territorial (Recurso hídrico / Ecosistema)",
                "legal": "Conformidad jurídica / Citas normativas",
            }.get(row["risk_source"], row["risk_source"] or "Origen no especificado"),
            "risk_level": row["risk_level"] or "high",
            "risk_level_label": {
                "high": "Riesgo Alto",
                "moderate": "Riesgo Moderado",
                "low": "Riesgo Bajo",
            }.get(row["risk_level"], row["risk_level"] or "Riesgo Alto"),
            "requires_human_review": True,
            "allows_final_approval": False,
            "details": details,
            "resolved_by_user_id": row.get("resolved_by_user_id"),
            "resolution_motive": row.get("resolution_motive"),
            "resolution_conditions": row.get("resolution_conditions"),
            "resolved_at": row.get("resolved_at"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def get_latest_checkpoint(
        cls,
        conn: psycopg.Connection,
        project_version_id: UUID,
    ) -> dict[str, Any] | None:
        """Recupera el último punto de control (pendiente o resuelto) para la versión del expediente (HU3.4)."""
        row = conn.execute(
            """
            SELECT id AS checkpoint_id, project_id, project_version_id, evaluation_id,
                   checkpoint_type, status, reason, risk_source, risk_level, details,
                   resolved_by_user_id, resolution_motive, resolution_conditions, resolved_at,
                   created_at, updated_at
            FROM evaluation_checkpoints
            WHERE project_version_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_version_id,),
        ).fetchone()

        if not row:
            return None

        details = row["details"] if isinstance(row["details"], dict) else {}
        raw_cause = details.get("cause") or row.get("reason")
        cause = raw_cause.strip() if isinstance(raw_cause, str) and raw_cause.strip() else None

        raw_obs = details.get("observation") or details.get("risk_observation")
        if not raw_obs and isinstance(details.get("declared_vs_evidence"), dict):
            raw_obs = details["declared_vs_evidence"].get("verifiable_evidence")

        if raw_obs and isinstance(raw_obs, str) and raw_obs.strip():
            if raw_obs.strip() == (cause or "").strip() and isinstance(details.get("declared_vs_evidence"), dict):
                verifiable = details["declared_vs_evidence"].get("verifiable_evidence")
                if verifiable and isinstance(verifiable, str) and verifiable.strip():
                    observation = f"Comprobación técnica requerida: {verifiable.strip()}"
                else:
                    observation = "Observación pendiente de aclaración"
            else:
                observation = raw_obs.strip()
        else:
            observation = "Observación pendiente de aclaración"

        is_pending = (row["status"] == "pending_review")
        return {
            "checkpoint_id": row["checkpoint_id"],
            "project_id": row["project_id"],
            "project_version_id": row["project_version_id"],
            "evaluation_id": row["evaluation_id"],
            "checkpoint_type": row["checkpoint_type"],
            "status": row["status"],
            "status_label": "Pendiente de revisión" if is_pending else "Reanudado / Resuelto",
            "reason": cause or (row.get("reason") or "Información no especificada"),
            "cause": cause,
            "observation": observation,
            "risk_source": row["risk_source"] or "no_especificado",
            "risk_source_label": {
                "environmental": "Impacto ambiental territorial (Recurso hídrico / Ecosistema)",
                "legal": "Conformidad jurídica / Citas normativas",
            }.get(row["risk_source"], row["risk_source"]),
            "risk_level": row["risk_level"],
            "risk_level_label": {
                "high": "Riesgo Alto",
                "moderate": "Riesgo Moderado",
                "low": "Riesgo Bajo",
            }.get(row["risk_level"], row["risk_level"]),
            "requires_human_review": is_pending,
            "allows_final_approval": not is_pending,
            "details": details,
            "resolved_by_user_id": row.get("resolved_by_user_id"),
            "resolution_motive": row.get("resolution_motive"),
            "resolution_conditions": row.get("resolution_conditions"),
            "resolved_at": row.get("resolved_at"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def has_pending_checkpoint(
        cls,
        conn: psycopg.Connection,
        project_version_id: UUID,
    ) -> bool:
        """Indica si existe un punto de control pendiente de revisión en la versión."""
        return cls.get_active_checkpoint(conn, project_version_id) is not None

    @classmethod
    def create_or_get_checkpoint(
        cls,
        conn: psycopg.Connection,
        project_id: UUID,
        project_version_id: UUID,
        evaluation_id: UUID | None,
        risk_info: dict[str, Any],
    ) -> dict[str, Any]:
        """Crea o recupera idempotentemente el punto de control persistente."""
        checkpoint_id = uuid4()
        row = conn.execute(
            """
            INSERT INTO evaluation_checkpoints
            (id, project_id, project_version_id, evaluation_id, checkpoint_type, status,
             reason, risk_source, risk_level, details, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'high_risk_pause', 'pending_review', %s, %s, %s, %s, now(), now())
            ON CONFLICT (project_version_id, risk_source, checkpoint_type) DO UPDATE SET
                status = 'pending_review',
                evaluation_id = COALESCE(EXCLUDED.evaluation_id, evaluation_checkpoints.evaluation_id),
                reason = EXCLUDED.reason,
                risk_level = EXCLUDED.risk_level,
                details = EXCLUDED.details,
                updated_at = now()
            RETURNING id, project_id, project_version_id, evaluation_id, checkpoint_type,
                      status, reason, risk_source, risk_level, details, created_at, updated_at
            """,
            (
                checkpoint_id,
                project_id,
                project_version_id,
                evaluation_id,
                risk_info["reason"],
                risk_info["risk_source"],
                risk_info["risk_level"],
                Jsonb(risk_info.get("details", {})),
            ),
        ).fetchone()

        # Actualizar estado del proyecto a 'pending_review'
        conn.execute(
            "UPDATE projects SET status = 'pending_review', updated_at = now() WHERE id = %s",
            (project_id,),
        )

        # Si hay evaluación asociada, pausarla
        if evaluation_id:
            conn.execute(
                """
                UPDATE evaluations
                SET status = 'pending_review', error_message = %s
                WHERE id = %s
                """,
                (risk_info["reason"], evaluation_id),
            )

        # Registrar evento de auditoría
        conn.execute(
            """
            INSERT INTO audit_events
            (action, entity_type, entity_id, details)
            VALUES ('EVALUATION_PAUSED_HIGH_RISK', 'evaluation_checkpoint', %s, %s)
            """,
            (
                row["id"],
                Jsonb(
                    {
                        "project_id": str(project_id),
                        "project_version_id": str(project_version_id),
                        "evaluation_id": str(evaluation_id) if evaluation_id else None,
                        "risk_source": risk_info["risk_source"],
                        "risk_level": risk_info["risk_level"],
                        "reason": risk_info["reason"],
                    }
                ),
            ),
        )

        return {
            "checkpoint_id": row["id"],
            "project_id": row["project_id"],
            "project_version_id": row["project_version_id"],
            "evaluation_id": row["evaluation_id"],
            "checkpoint_type": row["checkpoint_type"],
            "status": row["status"],
            "status_label": "Pendiente de revisión",
            "reason": row["reason"],
            "risk_source": row["risk_source"],
            "risk_source_label": {
                "environmental": "Impacto ambiental territorial (Recurso hídrico / Ecosistema)",
                "legal": "Conformidad jurídica / Citas normativas",
            }.get(row["risk_source"], row["risk_source"]),
            "risk_level": row["risk_level"],
            "risk_level_label": {
                "high": "Riesgo Alto",
                "moderate": "Riesgo Moderado",
                "low": "Riesgo Bajo",
            }.get(row["risk_level"], row["risk_level"]),
            "requires_human_review": True,
            "allows_final_approval": False,
            "details": row["details"] if isinstance(row["details"], dict) else {},
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }


class TechnicalFeasibilityEngine:
    """Motor de evaluación de viabilidad técnica de proyectos (HU1.8 - PMV2 Sprint 2).

    Criterios de Aceptación:
    - CA-HU1.8-01: Verificación técnica sustentada. Identifica cada criterio verificado,
      su estado y el sustento disponible, presentándolo como OPINIÓN PRELIMINAR automatizada,
      sin atribuirle valor de certificación profesional (ni sustituye la firma de ingenieros
      colegiados Ley 28858 / RNE).
    - CA-HU1.8-02: Incumplimiento u observación técnica. Si un criterio no se cumple según
      los datos aportados o presenta condicionamientos técnicos, señala cuál está observado,
      el motivo técnico y qué debe corregir o revisar el planificador con recomendaciones
      no prescriptivas solicitando evaluación del profesional responsable.
    - CA-HU1.8-03: Imposibilidad de emitir conclusión. Si faltan datos indispensables para
      comprobar un criterio, o si los datos provienen de fuentes simuladas sin comprobación física,
      indica 'Información insuficiente' o 'Pendiente de revisión técnica' y NO declara viable al proyecto.
      El puntaje técnico resultante es estrictamente None (cero falsos positivos; no entrega puntajes
      ficticios a HU1.12).
    """

    PRELIMINARY_OPINION_DISCLAIMER: ClassVar[str] = (
        "ADVERTENCIA DE CONTROL Y ALCANCE LEGAL: Esta evaluación técnica constituye una opinión preliminar automatizada "
        "basada en los parámetros y documentos registrados en la plataforma. No tiene carácter de certificación profesional, "
        "no constituye liquidación ni conformidad de obra, y no sustituye la responsabilidad funcional ni la firma colegiada "
        "de los ingenieros especialistas responsables (Ley N.° 28858 y Reglamento Nacional de Edificaciones - RNE)."
    )

    CANONICAL_CRITERIA: ClassVar[tuple[dict[str, str], ...]] = (
        {
            "code": "mecanica_suelos",
            "name": "Capacidad portante y mecánica de suelos",
            "normative_reference": 'RNE Norma Técnica E.050 "Suelos y Cimentaciones" (R.M. N.° 406-2018-VIVIENDA)',
            "normative_version": "2018 (actualizada)",
            "applied_rule": "Estudio de Mecánica de Suelos (EMS) conforme a RNE E.050 (Art. 13-15). La capacidad portante y tipo de cimentación requieren validación por el especialista geotécnico según las cargas del proyecto.",
        },
        {
            "code": "servicios_basicos",
            "name": "Factibilidad de servicios básicos de agua y saneamiento",
            "normative_reference": "Directiva General Invierte.pe (D.L. 1252) y RNE Norma Técnica OS.010",
            "normative_version": "2019 (vigente)",
            "applied_rule": "Factibilidad de servicios de agua y saneamiento otorgada por la EPS prestadora (SEDAM Huancayo), con punto de empalme certificado y sin condicionamientos técnicos pendientes.",
        },
        {
            "code": "topografia_taludes",
            "name": "Pendiente topográfica y estabilidad física de terreno",
            "normative_reference": 'RNE Norma Técnica CE.020 "Estabilización de Suelos y Taludes" y CENEPRED',
            "normative_version": "2012 (vigente)",
            "applied_rule": "Levantamiento topográfico perimétrico-altimétrico y evaluación de estabilidad física conforme a RNE CE.020 y CENEPRED.",
        },
        {
            "code": "dimensionamiento_predio",
            "name": "Dimensionamiento y compatibilidad de lote normativo",
            "normative_reference": "Plan de Desarrollo Metropolitano Huancayo 2017-2037 (O.M. N.° 636-MPH) y RNE A.010",
            "normative_version": "2017 (vigente)",
            "applied_rule": "Compatibilidad de uso y dimensionamiento normativo de predio conforme al PDM Huancayo 2017-2037 y RNE A.010.",
        },
    )

    @classmethod
    def evaluate_criterion(cls, meta: dict[str, str], row: dict[str, Any] | None) -> dict[str, Any]:
        code = meta["code"]
        name = meta["name"]
        norm_ref = meta["normative_reference"]
        norm_ver = meta["normative_version"]
        applied_rule = meta["applied_rule"]

        # Caso 1: Datos insuficientes o no registrados
        if (
            not row
            or not row.get("has_sufficient_data")
            or (row.get("parameter_value_numeric") is None and not row.get("parameter_value_text"))
        ):
            if code == "mecanica_suelos":
                explanation = "Falta estudio de mecánica de suelos (EMS) suscrito por ingeniero civil/geotécnico colegiado."
                guidance = "Adjuntar Informe Técnico de Mecánica de Suelos con fines de cimentación conforme a la Norma E.050 del RNE, suscrito por profesional habilitado (CIP)."
            elif code == "servicios_basicos":
                explanation = "No se dispone de certificado de factibilidad de servicios de agua y saneamiento emitido por la EPS prestadora (SEDAM Huancayo)."
                guidance = "Gestionar y anexar el Certificado de Factibilidad de Servicios de Agua Potable y Alcantarillado emitido por SEDAM Huancayo o la entidad prestadora correspondiente."
            elif code == "topografia_taludes":
                explanation = "Falta levantamiento topográfico perimétrico con curvas de nivel o evaluación de estabilidad de taludes."
                guidance = "Adjuntar plano topográfico perimétrico y altimétrico firmado por ingeniero civil o topógrafo colegiado, con curvas de nivel y cálculo de pendiente media."
            elif code == "dimensionamiento_predio":
                explanation = "Falta partida registral, plano de independización o certificado de parámetros urbanísticos que acredite dimensionamiento y zonificación."
                guidance = "Presentar Partida Registral SUNARP saneada o Certificado de Parámetros Urbanísticos y Edificatorios emitido por la Municipalidad Distrital de El Tambo."
            else:
                explanation = f"Faltan datos técnicos o sustento documental indispensable para el criterio {name}."
                guidance = "Registrar la información técnica requerida y adjuntar el documento sustentatorio de respaldo."

            return {
                "criterion_code": code,
                "criterion_name": name,
                "normative_reference": norm_ref,
                "normative_version": norm_ver,
                "has_sufficient_data": False,
                "parameter_name": row.get("parameter_name") if row else None,
                "parameter_value_numeric": None,
                "parameter_unit": row.get("parameter_unit") if row else None,
                "parameter_value_text": None,
                "applied_rule": applied_rule,
                "supporting_document": None,
                "source_origin": row.get("source_origin") if row else "simulated",
                "is_simulated": row.get("is_simulated", True) if row else True,
                "status": "informacion_insuficiente",
                "status_label": "Información insuficiente",
                "explanation": explanation,
                "correction_guidance": guidance,
            }

        val_num = float(row["parameter_value_numeric"]) if row.get("parameter_value_numeric") is not None else None
        val_txt = row.get("parameter_value_text")
        unit = row.get("parameter_unit") or ""
        doc = row.get("supporting_document")
        origin = row.get("source_origin") or "simulated"
        is_sim = row.get("is_simulated", True)
        param_name = row.get("parameter_name")

        # Caso 2: Criterio observado (por incompatibilidad de capacidad portante o condicionamiento de servicios)
        is_observed = False
        obs_explanation = ""
        obs_guidance = ""

        if code == "mecanica_suelos":
            if val_num is not None and val_num < 0.80:
                is_observed = True
                obs_explanation = (
                    f"Capacidad portante admisible informada ({val_num:.2f} {unit}) es baja para cimentación superficial directa convencional. "
                    "El suelo presenta limitaciones de capacidad de carga según los sondeos informados."
                )
                obs_guidance = (
                    "El ingeniero especialista en geotecnia y estructuras responsable del proyecto debe evaluar y proponer la solución "
                    "técnica adecuada (dimensionamiento de cimentación, mejoramiento de suelo o alternativas estructurales) conforme al EMS."
                )

        elif code == "servicios_basicos":
            txt_lower = (val_txt or "").lower()
            if "condiciona" in txt_lower or "no factible" in txt_lower or "ampliación" in txt_lower or (val_num is not None and val_num > 100.0):
                is_observed = True
                obs_explanation = (
                    f"Factibilidad de servicios condicionada: SEDAM Huancayo reporta que la conexión no es inmediata ({val_txt or f'distancia {val_num:.2f} m'}). "
                    "Requiere infraestructura complementaria o ampliación de colector."
                )
                obs_guidance = (
                    "Gestionar con la EPS prestadora (SEDAM Huancayo) las condiciones técnicas de empalme e incorporar los componentes "
                    "de saneamiento requeridos en el expediente del proyecto."
                )

        elif code == "topografia_taludes":
            if val_num is not None and val_num > 25.0:
                is_observed = True
                obs_explanation = (
                    f"Pendiente media del terreno informada ({val_num:.2f} {unit}) es pronunciada y presenta riesgo potencial de inestabilidad física o desprendimiento."
                )
                obs_guidance = (
                    "El profesional responsable de geotecnia/topografía debe realizar un estudio de estabilidad de taludes y plantear "
                    "obras de sostenimiento físico (muros de contención o banquetas de estabilización) conforme a RNE CE.020."
                )

        elif code == "dimensionamiento_predio":
            is_width = "metro" in unit.lower() or "m" == unit.strip()
            threshold = 6.0 if is_width else 200.0
            if val_num is not None and val_num < threshold:
                is_observed = True
                obs_explanation = (
                    f"Dimensión informada ({val_num:.2f} {unit}) es reducida respecto al programa arquitectónico tentativo o sección de faja vial requerida."
                )
                obs_guidance = (
                    "El planificador y arquitecto proyectista deben revisar la compatibilidad del anteproyecto con el área útil del predio "
                    "o gestionar servidumbres/ampliaciones según el PDM Huancayo y RNE A.010."
                )

        if is_observed:
            return {
                "criterion_code": code,
                "criterion_name": name,
                "normative_reference": norm_ref,
                "normative_version": norm_ver,
                "has_sufficient_data": True,
                "parameter_name": param_name,
                "parameter_value_numeric": val_num,
                "parameter_unit": unit,
                "parameter_value_text": val_txt,
                "applied_rule": applied_rule,
                "supporting_document": doc,
                "source_origin": origin,
                "is_simulated": is_sim,
                "status": "observado",
                "status_label": "Observado",
                "explanation": obs_explanation,
                "correction_guidance": obs_guidance,
            }

        # Caso 3: Parámetro simulado sin comprobación física
        if is_sim:
            val_display = f"{val_num:.2f} {unit}" if val_num is not None else (val_txt or "Registrado")
            return {
                "criterion_code": code,
                "criterion_name": name,
                "normative_reference": norm_ref,
                "normative_version": norm_ver,
                "has_sufficient_data": True,
                "parameter_name": param_name,
                "parameter_value_numeric": val_num,
                "parameter_unit": unit,
                "parameter_value_text": val_txt,
                "applied_rule": applied_rule,
                "supporting_document": doc,
                "source_origin": origin,
                "is_simulated": True,
                "status": "simulado_no_verificado",
                "status_label": "Sustento no comprobado (simulado)",
                "explanation": (
                    f"El parámetro registrado ({val_display}) corresponde a un dato simulado para demostración del flujo. "
                    "El documento referenciado no está comprobado en el archivo físico del expediente ni constituye certificación profesional."
                ),
                "correction_guidance": (
                    "Presentar el documento técnico oficial original suscrito por el ingeniero especialista colegiado (CIP) "
                    "para su cotejo y verificación en el expediente municipal."
                ),
            }

        # Caso 4: Parámetro real verificado y conforme
        val_display = f"{val_num:.2f} {unit}" if val_num is not None else (val_txt or "Registrado")
        return {
            "criterion_code": code,
            "criterion_name": name,
            "normative_reference": norm_ref,
            "normative_version": norm_ver,
            "has_sufficient_data": True,
            "parameter_name": param_name,
            "parameter_value_numeric": val_num,
            "parameter_unit": unit,
            "parameter_value_text": val_txt,
            "applied_rule": applied_rule,
            "supporting_document": doc,
            "source_origin": origin,
            "is_simulated": False,
            "status": "cumple",
            "status_label": "Cumple con sustento verificado",
            "explanation": f"Parámetro técnico ({val_display}) cotejado con documento de sustento verificable ({doc}) y validado conforme a normativa aplicable.",
            "correction_guidance": None,
        }

    @classmethod
    def evaluate(
        cls,
        project_id: UUID,
        project_code: str,
        project_version_id: UUID,
        version_number: int,
        parameter_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        rows_by_code = {r["criterion_code"]: r for r in parameter_rows}
        evaluated_criteria: list[dict[str, Any]] = []

        for meta in cls.CANONICAL_CRITERIA:
            row = rows_by_code.get(meta["code"])
            item = cls.evaluate_criterion(meta, row)
            evaluated_criteria.append(item)

        total = len(evaluated_criteria)
        cumple_count = sum(1 for c in evaluated_criteria if c["status"] == "cumple")
        observado_count = sum(1 for c in evaluated_criteria if c["status"] == "observado")
        insuficiente_count = sum(1 for c in evaluated_criteria if c["status"] == "informacion_insuficiente")
        simulado_count = sum(1 for c in evaluated_criteria if c["status"] == "simulado_no_verificado")

        criteria_summary = {
            "total": total,
            "cumple": cumple_count,
            "observado": observado_count,
            "informacion_insuficiente": insuficiente_count,
            "simulado_no_verificado": simulado_count,
        }

        # Reglas de dictamen global (cero falsos positivos)
        if insuficiente_count > 0:
            overall_result = "informacion_insuficiente"
            overall_result_label = "Información insuficiente"
            is_viable = False
            technical_score = None
            score_explanation = (
                "Puntaje técnico no determinado: existen criterios técnicos con información insuficiente "
                "que impiden una cuantificación objetiva (CA-HU1.8-03)."
            )
            summary = (
                f"Evaluación técnica preliminar inconclusa: {insuficiente_count} de {total} criterios carecen "
                "de información técnica indispensable o sustento en el expediente. No se declara la viabilidad técnica del proyecto."
            )
            dimension_conclusion = "No viable preliminarmente por falta de información técnica indispensable."
            dimension_justification = (
                f"El expediente presenta {insuficiente_count} criterio(s) con información técnica faltante o insuficiente. "
                "Conforme al principio de precaución técnica y CA-HU1.8-03, no es posible emitir una calificación de viabilidad "
                "hasta subsanar los parámetros técnicos requeridos."
            )
        elif observado_count > 0:
            overall_result = "observado"
            overall_result_label = "Observado"
            is_viable = False
            technical_score = None
            score_explanation = (
                f"Puntaje técnico no determinado: se identificaron {observado_count} observación(es) técnica(s) "
                "que requieren evaluación del profesional responsable antes de cualquier ponderación cuantitativa."
            )
            summary = (
                f"Evaluación técnica preliminar con observaciones: {observado_count} de {total} criterios presentan "
                "incompatibilidades o condicionamientos técnicos que deben ser subsanados por los especialistas del proyecto."
            )
            dimension_conclusion = "No viable preliminarmente por observaciones técnicas críticas no subsanadas."
            dimension_justification = (
                f"Se identificaron {observado_count} observación(es) técnica(s) relativas a los parámetros evaluados. "
                "El proyecto no califica como viable hasta que el profesional responsable incorpore las medidas técnicas recomendadas."
            )
        elif simulado_count > 0:
            overall_result = "pendiente_revision"
            overall_result_label = "Pendiente de revisión técnica"
            is_viable = False
            technical_score = None
            score_explanation = (
                "Puntaje técnico no determinado: los parámetros provienen de registros simulados de demostración "
                "sin sustento documental comprobado en el expediente físico (CA-HU1.8-01 / CA-HU1.8-03)."
            )
            summary = (
                "Preevaluación técnica en modo demostración: Los parámetros registrados corresponden a datos simulados "
                "y no cuentan con sustento documental comprobado en el archivo físico del expediente. "
                "Conforme a las reglas de control, no se otorga calificación de viabilidad ni puntaje numérico "
                "hasta contar con estudios oficiales verificados y suscritos por ingenieros colegiados (CIP)."
            )
            dimension_conclusion = "Pendiente de verificación técnica presencial/documental."
            dimension_justification = (
                "Los antecedentes del expediente contienen parámetros simulados para fines de demostración del software. "
                "No constituyen prueba documental verificable ni habilitan certificación técnica de viabilidad."
            )
        else:
            overall_result = "cumple"
            overall_result_label = "Viable preliminarmente"
            is_viable = True
            technical_score = 100.0
            score_explanation = (
                f"Puntaje técnico de 100.0/100: todos los criterios técnicos reglamentarios ({total}/{total}) "
                "cuentan con sustento documental verificado y cumplen los requerimientos de ingeniería aplicables."
            )
            summary = (
                "Evaluación técnica preliminar favorable: todos los criterios técnicos analizados cumplen "
                "con la normativa vigente y cuentan con sustento documental verificado en el expediente."
            )
            dimension_conclusion = "Viable preliminarmente conforme a los parámetros técnicos evaluados."
            dimension_justification = (
                "Todos los parámetros evaluados cuentan con sustento documental verificable y cumplen con los "
                "criterios de ingeniería del RNE y la normativa municipal aplicable."
            )

        now = datetime.now(timezone.utc)

        return {
            "project_id": project_id,
            "project_code": project_code,
            "project_version_id": project_version_id,
            "version_number": version_number,
            "status": "completed",
            "overall_result": overall_result,
            "overall_result_label": overall_result_label,
            "is_viable": is_viable,
            "is_preliminary_opinion": True,
            "disclaimer": cls.PRELIMINARY_OPINION_DISCLAIMER,
            "summary": summary,
            "criteria_summary": criteria_summary,
            "criteria": evaluated_criteria,
            "technical_score": technical_score,
            "score_explanation": score_explanation,
            "dimension_conclusion": dimension_conclusion,
            "dimension_justification": dimension_justification,
            "evaluated_at": now,
        }


class PlatformService:
    def __init__(
        self,
        db_url: str,
        dispatcher: TaskDispatcher | None = None,
        enable_demo_fallback: bool = False,
        auth_db_url: str | None = None,
        enable_synthetic_zoning_rehearsal: bool = False,
    ) -> None:
        self.db_url = _dsn(db_url)
        self.dispatcher = dispatcher
        self.enable_demo_fallback = enable_demo_fallback
        self.auth_db_url = _dsn(auth_db_url) if auth_db_url else None
        self.enable_synthetic_zoning_rehearsal = enable_synthetic_zoning_rehearsal

    _EXPEDIENTE_SCHEMA_DRAFT_FIELDS: ClassVar[set[str]] = {
        "title",
    }

    @staticmethod
    def _missing_from_values(values: dict[str, Any]) -> list[str]:
        required_order = (
            "title",
            "description",
            "location",
            "proposed_land_use",
            "estimated_budget_pen",
            "beneficiaries_count",
        )
        missing: list[str] = []
        for name in required_order:
            value = values.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                missing.append(name)

        budget = values.get("estimated_budget_pen")
        if budget is not None and Decimal(str(budget)) <= 0:
            missing.append("estimated_budget_pen (> 0)")

        beneficiaries = values.get("beneficiaries_count")
        if beneficiaries is not None and int(beneficiaries) <= 0:
            missing.append("beneficiaries_count (> 0)")
        return missing

    @classmethod
    def _ensure_complete_project(cls, values: dict[str, Any]) -> None:
        missing = cls._missing_from_values(values)
        if missing:
            raise ExpedientePMV1IncompletoError(missing)

    @classmethod
    def _missing(cls, data: PMV1ProjectCreate) -> list[str]:
        values = {
            "title": data.title,
            "description": data.description,
            "location": data.location,
            "proposed_land_use": data.proposed_land_use,
            "estimated_budget_pen": data.estimated_budget_pen,
            "beneficiaries_count": data.beneficiaries_count,
        }
        return cls._missing_from_values(values)

    @staticmethod
    def _project_content_fingerprint(data: PMV1ProjectCreate) -> str:
        payload = {
            "code": (data.code or "").strip().upper(),
            "title": (data.title or "").strip(),
            "description": (data.description or "").strip(),
            "location": (data.location or "").strip(),
            "proposed_land_use": (data.proposed_land_use or "").strip(),
            "latitude": float(data.latitude) if data.latitude is not None else None,
            "longitude": float(data.longitude) if data.longitude is not None else None,
            "estimated_budget_pen": (
                str(Decimal(data.estimated_budget_pen).quantize(Decimal("0.01")))
                if data.estimated_budget_pen is not None
                else None
            ),
            "beneficiaries_count": int(data.beneficiaries_count) if data.beneficiaries_count is not None else None,
            "save_as_draft": bool(data.save_as_draft),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def create_project(
        self,
        data: PMV1ProjectCreate,
        actor_id: UUID,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        missing = self._missing(data)
        if missing and not data.save_as_draft:
            raise ExpedientePMV1IncompletoError(missing)
        if data.save_as_draft:
            invalid_draft_fields = [
                f
                for f in missing
                if f
                in (
                    "title",
                    "estimated_budget_pen (> 0)",
                    "beneficiaries_count (> 0)",
                )
            ]
            if invalid_draft_fields:
                raise ExpedientePMV1IncompletoError(invalid_draft_fields)

        cleaned_idem = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None
        fingerprint = self._project_content_fingerprint(data)
        project_status = "draft" if missing else "ready"
        audit_action = "PROJECT_DRAFT_CREATED" if missing else "PROJECT_CREATED"
        project_id, version_id = uuid4(), uuid4()

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                if cleaned_idem:
                    prev = conn.execute(
                        """
                        SELECT id, code, content_fingerprint
                        FROM projects
                        WHERE creation_idempotency_key = %s
                        """,
                        (cleaned_idem,),
                    ).fetchone()
                    if prev:
                        if prev.get("content_fingerprint") and prev["content_fingerprint"] != fingerprint:
                            raise ConfiguracionPMV1Error(
                                "Idempotency-Key ya fue utilizada para registrar un expediente distinto."
                            )
                        existing = self.get_project(prev["id"])
                        existing["duplicated"] = True
                        return existing

                dup_fp = conn.execute(
                    """
                    SELECT id, code
                    FROM projects
                    WHERE content_fingerprint = %s
                    LIMIT 1
                    """,
                    (fingerprint,),
                ).fetchone()
                if dup_fp:
                    raise ConfiguracionPMV1Error(
                        f"Solicitud de carga duplicada: ya existe un expediente idéntico registrado "
                        f"(código {dup_fp['code']}, id {dup_fp['id']}). No se generó ningún duplicado."
                    )

                explicit_code = (data.code or "").strip()
                if explicit_code:
                    dup_code = conn.execute(
                        "SELECT id, code FROM projects WHERE code = %s LIMIT 1",
                        (explicit_code,),
                    ).fetchone()
                    if dup_code:
                        raise ConfiguracionPMV1Error(
                            f"Ya existe un expediente registrado con el código '{explicit_code}' (id {dup_code['id']})."
                        )

                code = explicit_code or _allocate_expediente_code(conn)
                conn.execute(
                    """
                    INSERT INTO projects
                    (id, code, created_by_user_id, status, creation_idempotency_key, content_fingerprint)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (project_id, code, actor_id, project_status, cleaned_idem, fingerprint),
                )
                conn.execute(
                    """
                    INSERT INTO project_versions
                    (id, project_id, version_number, title, description,
                     location_description, proposed_land_use, territorial_data_origin,
                     latitude, longitude, coordinates_origin, coordinates_source,
                     estimated_budget_pen, beneficiaries_count, created_by_user_id)
                    VALUES (%s,%s,1,%s,%s,%s,%s,'declared',%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        version_id,
                        project_id,
                        data.title,
                        data.description or "",
                        data.location,
                        data.proposed_land_use,
                        data.latitude,
                        data.longitude,
                        data.coordinates_origin if data.latitude is not None else None,
                        data.coordinates_source if data.latitude is not None else None,
                        data.estimated_budget_pen,
                        data.beneficiaries_count,
                        actor_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO audit_events
                    (actor_user_id, action, entity_type, entity_id, details)
                    VALUES (%s,%s,'project',%s,%s)""",
                    (
                        actor_id,
                        audit_action,
                        project_id,
                        Jsonb(
                            {
                                "project_version_id": str(version_id),
                                "status": project_status,
                                "missing_fields": missing,
                                "content_fingerprint": fingerprint,
                            }
                        ),
                    ),
                )
        created = self.get_project(project_id)
        created["duplicated"] = False
        return created

    def update_project(
        self, project_id: UUID, data: PMV1ProjectUpdate, actor_id: UUID
    ) -> dict[str, Any]:
        """Completa o corrige un expediente en borrador o crea una nueva versión corregida (HU1.2)."""
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            p_row = conn.execute(
                "SELECT id, status FROM projects WHERE id = %s FOR UPDATE",
                (project_id,),
            ).fetchone()
            if not p_row:
                raise RecursoPMV1NoEncontradoError("Proyecto no encontrado")

            latest_pv = conn.execute(
                """
                SELECT id, version_number, title, description,
                       location_description AS location, proposed_land_use,
                       territorial_data_origin, latitude, longitude,
                       coordinates_origin, coordinates_source,
                       estimated_budget_pen, beneficiaries_count
                FROM project_versions
                WHERE project_id = %s
                ORDER BY version_number DESC
                LIMIT 1
                FOR UPDATE
                """,
                (project_id,),
            ).fetchone()
            if not latest_pv:
                raise RecursoPMV1NoEncontradoError("Versión de proyecto no encontrada")

            fields_set = data.model_fields_set
            merged = {
                "title": data.title if "title" in fields_set else latest_pv["title"],
                "description": (
                    (data.description or "")
                    if "description" in fields_set
                    else (latest_pv["description"] or "")
                ),
                "location": data.location if "location" in fields_set else latest_pv["location"],
                "proposed_land_use": (
                    data.proposed_land_use
                    if "proposed_land_use" in fields_set
                    else latest_pv["proposed_land_use"]
                ),
                "estimated_budget_pen": (
                    data.estimated_budget_pen
                    if "estimated_budget_pen" in fields_set
                    else latest_pv["estimated_budget_pen"]
                ),
                "beneficiaries_count": (
                    data.beneficiaries_count
                    if "beneficiaries_count" in fields_set
                    else latest_pv["beneficiaries_count"]
                ),
                "latitude": data.latitude if "latitude" in fields_set else latest_pv["latitude"],
                "longitude": data.longitude if "longitude" in fields_set else latest_pv["longitude"],
                "coordinates_origin": (
                    data.coordinates_origin
                    if "coordinates_origin" in fields_set
                    else latest_pv["coordinates_origin"]
                ),
                "coordinates_source": (
                    data.coordinates_source
                    if "coordinates_source" in fields_set
                    else latest_pv["coordinates_source"]
                ),
            }

            missing = self._missing_from_values(merged)
            if missing and not data.save_as_draft:
                raise ExpedientePMV1IncompletoError(missing)
            if data.save_as_draft:
                invalid_draft_fields = [
                    f
                    for f in missing
                    if f
                    in (
                        "title",
                        "estimated_budget_pen (> 0)",
                        "beneficiaries_count (> 0)",
                    )
                ]
                if invalid_draft_fields:
                    raise ExpedientePMV1IncompletoError(invalid_draft_fields)

            if not data.create_new_version and p_row["status"] not in ("draft", "ready"):
                raise ConfiguracionPMV1Error(
                    "El expediente ya inició evaluación o cuenta con revisión registrada; registre una nueva versión para modificarlo."
                )

            new_status = "draft" if missing else "ready"
            now = datetime.now(timezone.utc)
            lat = merged["latitude"]
            lon = merged["longitude"]
            coord_origin = merged["coordinates_origin"] if lat is not None else None
            coord_source = merged["coordinates_source"] if lat is not None else None

            if data.create_new_version:
                next_version = int(latest_pv["version_number"]) + 1
                target_version_id = uuid4()
                conn.execute(
                    """
                    INSERT INTO project_versions
                    (id, project_id, version_number, title, description,
                     location_description, proposed_land_use, territorial_data_origin,
                     latitude, longitude, coordinates_origin, coordinates_source,
                     estimated_budget_pen, beneficiaries_count, created_by_user_id, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        target_version_id,
                        project_id,
                        next_version,
                        merged["title"],
                        merged["description"],
                        merged["location"],
                        merged["proposed_land_use"],
                        latest_pv["territorial_data_origin"] or "declared",
                        lat,
                        lon,
                        coord_origin,
                        coord_source,
                        merged["estimated_budget_pen"],
                        merged["beneficiaries_count"],
                        actor_id,
                        now,
                    ),
                )
                audit_action = "PROJECT_VERSION_CREATED"
            else:
                target_version_id = latest_pv["id"]
                next_version = int(latest_pv["version_number"])
                conn.execute(
                    """
                    UPDATE project_versions
                    SET title = %s,
                        description = %s,
                        location_description = %s,
                        proposed_land_use = %s,
                        latitude = %s,
                        longitude = %s,
                        coordinates_origin = %s,
                        coordinates_source = %s,
                        estimated_budget_pen = %s,
                        beneficiaries_count = %s
                    WHERE id = %s
                    """,
                    (
                        merged["title"],
                        merged["description"],
                        merged["location"],
                        merged["proposed_land_use"],
                        lat,
                        lon,
                        coord_origin,
                        coord_source,
                        merged["estimated_budget_pen"],
                        merged["beneficiaries_count"],
                        target_version_id,
                    ),
                )
                audit_action = (
                    "PROJECT_DRAFT_COMPLETED"
                    if p_row["status"] == "draft" and new_status == "ready"
                    else "PROJECT_UPDATED"
                )

            conn.execute(
                "UPDATE projects SET status = %s, updated_at = %s WHERE id = %s",
                (new_status, now, project_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, %s, 'project', %s, %s)
                """,
                (
                    actor_id,
                    audit_action,
                    project_id,
                    Jsonb(
                        {
                            "project_version_id": str(target_version_id),
                            "version_number": next_version,
                            "previous_status": p_row["status"],
                            "new_status": new_status,
                            "missing_fields": missing,
                        }
                    ),
                ),
            )
        return self.get_project(project_id)

    def create_project_version(
        self,
        project_id: UUID,
        title: str,
        description: str,
        location_description: str,
        proposed_land_use: str,
        estimated_budget_pen: Decimal,
        beneficiaries_count: int,
        actor_id: UUID,
        territorial_data_origin: str = "declared",
        latitude: float | None = None,
        longitude: float | None = None,
        coordinates_origin: str | None = None,
        coordinates_source: str | None = None,
    ) -> dict[str, Any]:
        """Crea una nueva versión de un proyecto (ej. v2 replanteada/mitigada) y actualiza el estado almacenado a 'ready' (operación de escritura)."""
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            p_row = conn.execute(
                "SELECT id FROM projects WHERE id = %s",
                (project_id,),
            ).fetchone()
            if not p_row:
                raise RecursoPMV1NoEncontradoError("Proyecto no encontrado")

            max_v = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS max_v FROM project_versions WHERE project_id = %s",
                (project_id,),
            ).fetchone()["max_v"]
            next_version = max_v + 1
            version_id = uuid4()
            now = datetime.now(timezone.utc)

            conn.execute(
                """
                INSERT INTO project_versions
                (id, project_id, version_number, title, description,
                 location_description, proposed_land_use, territorial_data_origin,
                 latitude, longitude, coordinates_origin, coordinates_source,
                 estimated_budget_pen, beneficiaries_count, created_by_user_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    project_id,
                    next_version,
                    title,
                    description,
                    location_description,
                    proposed_land_use,
                    territorial_data_origin,
                    latitude,
                    longitude,
                    coordinates_origin if latitude is not None else None,
                    coordinates_source if latitude is not None else None,
                    estimated_budget_pen,
                    beneficiaries_count,
                    actor_id,
                    now,
                ),
            )
            # En la operación de escritura correspondiente, se actualiza el estado del proyecto a 'ready' para la versión nueva
            conn.execute(
                "UPDATE projects SET status = 'ready', updated_at = %s WHERE id = %s",
                (now, project_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'PROJECT_VERSION_CREATED', 'project_version', %s, %s)
                """,
                (
                    actor_id,
                    version_id,
                    Jsonb({"project_id": str(project_id), "version_number": next_version}),
                ),
            )
        return self.get_project(project_id)

    def get_project(self, project_id: UUID) -> dict[str, Any]:
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT p.id AS project_id, pv.id AS project_version_id, p.code,
                       pv.version_number, p.status, pv.title, pv.description,
                       pv.location_description AS location, pv.proposed_land_use,
                       pv.territorial_data_origin,
                       pv.latitude, pv.longitude, pv.coordinates_origin, pv.coordinates_source,
                       pv.estimated_budget_pen, pv.beneficiaries_count
                FROM projects p JOIN project_versions pv ON pv.project_id = p.id
                WHERE p.id=%s ORDER BY pv.version_number DESC LIMIT 1
                """,
                (project_id,),
            ).fetchone()
            if not row:
                raise RecursoPMV1NoEncontradoError("Proyecto no encontrado")
            checkpoint = RiskCheckpointEngine.get_active_checkpoint(conn, row["project_version_id"])
            pert_estimate = self._get_pert_estimate_with_conn(conn, row["project_version_id"])
            res = dict(row)
            if res.get("latitude") is not None:
                res["latitude"] = float(res["latitude"])
            if res.get("longitude") is not None:
                res["longitude"] = float(res["longitude"])

            # HU3.2 (Estrictamente de lectura): Determinar el estado efectivo para la versión vigente del expediente.
            # No ejecuta sentencias UPDATE en projects ni muta evaluaciones, checkpoints, auditoría ni fechas.
            if checkpoint is not None:
                # Caso 2: La versión vigente posee un punto de control activo de riesgo alto.
                res["status"] = "pending_review"
            elif res["status"] == "pending_review":
                # projects.status en base de datos figura en 'pending_review',
                # pero la versión vigente NO tiene punto de control activo registrado.
                is_subsequent = row["version_number"] > 1
                has_prior_checkpoint = False
                if is_subsequent:
                    has_prior_checkpoint = bool(
                        conn.execute(
                            """
                            SELECT 1 FROM evaluation_checkpoints ec
                            JOIN project_versions pv ON pv.id = ec.project_version_id
                            WHERE pv.project_id = %s
                              AND pv.version_number < %s
                              AND ec.status = 'pending_review'
                            LIMIT 1
                            """,
                            (project_id, row["version_number"]),
                        ).fetchone()
                    )

                # Verificar si la versión vigente tiene una evaluación en 'pending_review'
                current_eval_paused = bool(
                    conn.execute(
                        """
                        SELECT 1 FROM evaluations
                        WHERE project_version_id = %s AND status = 'pending_review'
                        LIMIT 1
                        """,
                        (row["project_version_id"],),
                    ).fetchone()
                )

                if is_subsequent and has_prior_checkpoint and not current_eval_paused:
                    # Caso 1: Versión 1 pausada y Versión 2 (o superior) mitigada/replanteada.
                    # El estado efectivo de la versión vigente se deriva de sus propias evaluaciones sin mutar la BD:
                    eval_row = conn.execute(
                        """
                        SELECT status FROM evaluations
                        WHERE project_version_id = %s
                        ORDER BY requested_at DESC LIMIT 1
                        """,
                        (row["project_version_id"],),
                    ).fetchone()
                    if eval_row and eval_row["status"] == "completed":
                        effective_status = "evaluated"
                    elif eval_row and eval_row["status"] in ("queued", "processing"):
                        effective_status = "evaluating"
                    else:
                        effective_status = "ready"

                    res["status"] = effective_status
                    checkpoint = None
                else:
                    # Caso 3: Inconsistencia de datos.
                    # El expediente figura en 'pending_review' (o evaluación pausada) sin registro de checkpoint
                    # activo para la versión vigente, y no corresponde a una versión posterior con pausa histórica.
                    # REGLA: No convertir automáticamente a 'ready' ni habilitar una aprobación sin revisión.
                    # Mantener estrictamente 'pending_review' e informar la inconsistencia de forma controlada.
                    res["status"] = "pending_review"
                    created_at_dt = row.get("updated_at") or row.get("created_at") or datetime.now(timezone.utc)
                    checkpoint = {
                        "checkpoint_id": None,
                        "project_id": project_id,
                        "project_version_id": row["project_version_id"],
                        "evaluation_id": None,
                        "checkpoint_type": "inconsistent_data_review",
                        "status": "pending_review",
                        "status_label": "Pendiente de revisión",
                        "reason": (
                            "Inconsistencia de control: El expediente figura en estado 'Pendiente de revisión' "
                            "sin registro de punto de control activo para la versión vigente. "
                            "Requiere aclaración técnica y auditoría antes de continuar."
                        ),
                        "cause": "Inconsistencia de control: Estado 'Pendiente de revisión' sin punto de control registrado",
                        "observation": (
                            "Observación pendiente de aclaración: Se requiere auditoría y comprobación técnica del expediente "
                            "para subsanar el registro antes de permitir cualquier aprobación o procesamiento."
                        ),
                        "risk_source": "inconsistent_state",
                        "risk_source_label": "Auditoría de consistencia de datos",
                        "risk_level": "high",
                        "risk_level_label": "Riesgo Alto",
                        "requires_human_review": True,
                        "allows_final_approval": False,
                        "details": {
                            "inconsistent": True,
                            "inconsistency_type": "pending_review_missing_checkpoint",
                            "message": "Expediente en estado 'pending_review' sin checkpoint activo.",
                        },
                        "created_at": created_at_dt,
                        "updated_at": created_at_dt,
                    }

            res["checkpoint"] = checkpoint
            res["pert_estimate"] = pert_estimate
            res["technical_assessment"] = self._get_technical_assessment_with_conn(conn, row["project_version_id"])
            res["economic_evaluation"] = self._get_latest_evaluation_for_version_with_conn(
                conn,
                row["project_version_id"],
                checkpoint=checkpoint,
            )
            res["zoning_precheck"] = self._get_latest_zoning_precheck_with_conn(
                conn, res
            )
            dispatches = self._get_dimension_dispatches_with_conn(conn, row["project_version_id"])
            res["dimension_dispatches"] = dispatches
            res["has_failed_dimensions"] = any(d["status"] == "failed" for d in dispatches)
            res["duplicated"] = False
            if res["has_failed_dimensions"] and res["status"] == "evaluated":
                res["status"] = "evaluating"
            return res

    _DIMENSION_NAMES: ClassVar[dict[str, str]] = {
        "economic": "Económica",
        "social": "Social",
        "environmental": "Ambiental",
        "technical": "Técnica",
        "legal": "Jurídica",
    }
    _DIMENSION_ORDER: ClassVar[tuple[str, ...]] = (
        "economic",
        "social",
        "environmental",
        "technical",
        "legal",
    )

    @classmethod
    def _get_dimension_dispatches_with_conn(
        cls, conn: psycopg.Connection[Any], project_version_id: UUID
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT dimension_code, project_version_id, status, status_label,
                   result_reference_id, summary, error_message, attempt_count,
                   dispatched_at, updated_at
            FROM evaluation_dimension_dispatches
            WHERE project_version_id = %s
            """,
            (project_version_id,),
        ).fetchall()
        by_code = {r["dimension_code"]: dict(r) for r in rows}
        ordered: list[dict[str, Any]] = []
        for code in cls._DIMENSION_ORDER:
            if code in by_code:
                item = by_code[code]
                item["dimension_name"] = cls._DIMENSION_NAMES[code]
                ordered.append(item)
        return ordered

    @staticmethod
    def _upsert_dimension_dispatch_with_conn(
        conn: psycopg.Connection[Any],
        *,
        evaluation_id: UUID,
        project_id: UUID,
        project_version_id: UUID,
        dimension_code: str,
        status: str,
        status_label: str,
        result_reference_id: UUID | None = None,
        summary: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
    ) -> None:
        conn.execute(
            """
            INSERT INTO evaluation_dimension_dispatches (
                id, evaluation_id, project_id, project_version_id, dimension_code,
                status, status_label, result_reference_id, summary, error_message,
                attempt_count, dispatched_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now(), now())
            ON CONFLICT (project_version_id, dimension_code) DO UPDATE SET
                evaluation_id = EXCLUDED.evaluation_id,
                status = EXCLUDED.status,
                status_label = EXCLUDED.status_label,
                result_reference_id = COALESCE(EXCLUDED.result_reference_id, evaluation_dimension_dispatches.result_reference_id),
                summary = COALESCE(EXCLUDED.summary, evaluation_dimension_dispatches.summary),
                error_message = EXCLUDED.error_message,
                attempt_count = CASE
                    WHEN %s THEN evaluation_dimension_dispatches.attempt_count + 1
                    ELSE evaluation_dimension_dispatches.attempt_count
                END,
                updated_at = now()
            """,
            (
                uuid4(),
                evaluation_id,
                project_id,
                project_version_id,
                dimension_code,
                status,
                status_label,
                result_reference_id,
                summary,
                error_message,
                increment_attempt,
            ),
        )


    def _get_pert_estimate_with_conn(
        self, conn: psycopg.Connection[Any], project_version_id: UUID
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT id, project_id, project_version_id, registered_by_user_id, time_unit,
                   optimistic_duration, most_likely_duration, pessimistic_duration,
                   expected_duration, standard_deviation, variance,
                   confidence_interval_low, confidence_interval_high,
                   formula_applied, disclaimer, explanation, created_at, updated_at
            FROM project_pert_estimates
            WHERE project_version_id = %s
            """,
            (project_version_id,),
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_pert_estimate(
        self, project_id: UUID, project_version_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """Recupera la estimación de duración PERT para la versión del expediente (HU1.9)."""
        project = self.get_project(project_id)
        target_version_id = UUID(str(project_version_id)) if project_version_id else project["project_version_id"]
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            if project_version_id is not None:
                self._verify_version_belongs_to_project(conn, project_id, target_version_id)
            return self._get_pert_estimate_with_conn(conn, target_version_id)

    def _get_technical_assessment_with_conn(
        self, conn: psycopg.Connection[Any], project_version_id: UUID
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT ta.id, ta.project_id, p.code AS project_code, ta.project_version_id, pv.version_number,
                   ta.status, ta.overall_result, ta.overall_result_label, ta.is_viable,
                   ta.is_preliminary_opinion, ta.disclaimer, ta.summary,
                   ta.criteria_summary, ta.findings_json, ta.technical_score,
                   ta.score_explanation, ta.dimension_conclusion, ta.dimension_justification,
                   ta.evaluated_at
            FROM technical_feasibility_assessments ta
            JOIN projects p ON p.id = ta.project_id
            JOIN project_versions pv ON pv.id = ta.project_version_id
            WHERE ta.project_version_id = %s
            """,
            (project_version_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "assessment_id": row["id"],
            "project_id": row["project_id"],
            "project_code": row["project_code"],
            "project_version_id": row["project_version_id"],
            "version_number": row["version_number"],
            "status": row["status"],
            "overall_result": row["overall_result"],
            "overall_result_label": row["overall_result_label"],
            "is_viable": row["is_viable"],
            "is_preliminary_opinion": row["is_preliminary_opinion"],
            "disclaimer": row["disclaimer"],
            "summary": row["summary"],
            "criteria_summary": row["criteria_summary"],
            "criteria": row["findings_json"],
            "technical_score": float(row["technical_score"]) if row["technical_score"] is not None else None,
            "score_explanation": row["score_explanation"],
            "dimension_conclusion": row["dimension_conclusion"],
            "dimension_justification": row["dimension_justification"],
            "evaluated_at": row["evaluated_at"],
            "duplicated": False,
        }

    def evaluate_technical_feasibility(
        self,
        project_id: UUID,
        actor_id: UUID,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Ejecuta y persiste la preevaluación de viabilidad técnica del expediente para su versión vigente (HU1.8)."""
        project = self.get_project(project_id)
        self._ensure_complete_project(project)
        project_version_id = project["project_version_id"]
        version_number = project["version_number"]
        cleaned_key = (idempotency_key or "").strip() or None

        if not _is_db_available(self.db_url):
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de viabilidad técnica no disponible: la base de datos no responde."
                )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            # 1. Comprobar Idempotency-Key
            if cleaned_key:
                prev = conn.execute(
                    """
                    SELECT ta.id, ta.project_id, p.code AS project_code, ta.project_version_id,
                           pv.version_number, ta.status, ta.overall_result, ta.overall_result_label,
                           ta.is_viable, ta.is_preliminary_opinion, ta.disclaimer, ta.summary,
                           ta.criteria_summary, ta.findings_json, ta.technical_score,
                           ta.score_explanation, ta.dimension_conclusion, ta.dimension_justification,
                           ta.evaluated_at, ta.idempotency_key
                    FROM technical_feasibility_assessments ta
                    JOIN projects p ON p.id = ta.project_id
                    JOIN project_versions pv ON pv.id = ta.project_version_id
                    WHERE ta.idempotency_key = %s
                    """,
                    (cleaned_key,),
                ).fetchone()
                if prev:
                    if prev["project_id"] != project_id:
                        raise ConfiguracionPMV1Error("Idempotency-Key ya fue utilizada para otro expediente")
                    return {
                        "assessment_id": prev["id"],
                        "project_id": prev["project_id"],
                        "project_code": prev["project_code"],
                        "project_version_id": prev["project_version_id"],
                        "version_number": prev["version_number"],
                        "status": prev["status"],
                        "overall_result": prev["overall_result"],
                        "overall_result_label": prev["overall_result_label"],
                        "is_viable": prev["is_viable"],
                        "is_preliminary_opinion": prev["is_preliminary_opinion"],
                        "disclaimer": prev["disclaimer"],
                        "summary": prev["summary"],
                        "criteria_summary": prev["criteria_summary"],
                        "criteria": prev["findings_json"],
                        "technical_score": float(prev["technical_score"]) if prev["technical_score"] is not None else None,
                        "score_explanation": prev["score_explanation"],
                        "dimension_conclusion": prev["dimension_conclusion"],
                        "dimension_justification": prev["dimension_justification"],
                        "evaluated_at": prev["evaluated_at"],
                        "duplicated": True,
                    }

            # 2. Leer parámetros técnicos registrados para esta versión
            param_rows = conn.execute(
                """
                SELECT id, project_id, project_version_id, criterion_code, criterion_name,
                       normative_reference, normative_version, has_sufficient_data,
                       parameter_name, parameter_value_numeric, parameter_unit, parameter_value_text,
                       applied_rule, supporting_document, source_origin, is_simulated
                FROM project_technical_parameters
                WHERE project_version_id = %s
                ORDER BY criterion_code ASC
                """,
                (project_version_id,),
            ).fetchall()

            # 3. Evaluar con el motor técnico
            assessment = TechnicalFeasibilityEngine.evaluate(
                project_id=project_id,
                project_code=project["code"],
                project_version_id=project_version_id,
                version_number=version_number,
                parameter_rows=[dict(r) for r in param_rows],
            )

            assessment_id = uuid4()
            effective_key = cleaned_key or f"tech-assessment-{assessment_id}"
            now = assessment["evaluated_at"]

            # 4. Insertar o actualizar la evaluación para esta versión
            saved_row = conn.execute(
                """
                INSERT INTO technical_feasibility_assessments
                (id, project_id, project_version_id, requested_by_user_id, idempotency_key,
                 status, overall_result, overall_result_label, is_viable, is_preliminary_opinion,
                 disclaimer, summary, criteria_summary, findings_json, technical_score,
                 score_explanation, dimension_conclusion, dimension_justification, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_version_id) DO UPDATE SET
                    idempotency_key = EXCLUDED.idempotency_key,
                    requested_by_user_id = EXCLUDED.requested_by_user_id,
                    status = EXCLUDED.status,
                    overall_result = EXCLUDED.overall_result,
                    overall_result_label = EXCLUDED.overall_result_label,
                    is_viable = EXCLUDED.is_viable,
                    is_preliminary_opinion = EXCLUDED.is_preliminary_opinion,
                    disclaimer = EXCLUDED.disclaimer,
                    summary = EXCLUDED.summary,
                    criteria_summary = EXCLUDED.criteria_summary,
                    findings_json = EXCLUDED.findings_json,
                    technical_score = EXCLUDED.technical_score,
                    score_explanation = EXCLUDED.score_explanation,
                    dimension_conclusion = EXCLUDED.dimension_conclusion,
                    dimension_justification = EXCLUDED.dimension_justification,
                    evaluated_at = EXCLUDED.evaluated_at
                RETURNING id
                """,
                (
                    assessment_id,
                    project_id,
                    project_version_id,
                    actor_id,
                    effective_key,
                    assessment["status"],
                    assessment["overall_result"],
                    assessment["overall_result_label"],
                    assessment["is_viable"],
                    assessment["is_preliminary_opinion"],
                    assessment["disclaimer"],
                    assessment["summary"],
                    Jsonb(assessment["criteria_summary"]),
                    Jsonb(assessment["criteria"]),
                    Decimal(str(assessment["technical_score"])) if assessment["technical_score"] is not None else None,
                    assessment["score_explanation"],
                    assessment["dimension_conclusion"],
                    assessment["dimension_justification"],
                    now,
                ),
            ).fetchone()
            actual_assessment_id = saved_row["id"] if saved_row else assessment_id

            # 5. Registrar evento de auditoría
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'TECHNICAL_ASSESSMENT_COMPLETED', 'technical_assessment', %s, %s)
                """,
                (
                    actor_id,
                    actual_assessment_id,
                    Jsonb(
                        {
                            "project_id": str(project_id),
                            "project_code": project["code"],
                            "project_version_id": str(project_version_id),
                            "overall_result": assessment["overall_result"],
                            "is_viable": assessment["is_viable"],
                            "technical_score": assessment["technical_score"],
                            "criteria_summary": assessment["criteria_summary"],
                        }
                    ),
                ),
            )

        return {
            "assessment_id": actual_assessment_id,
            **assessment,
            "duplicated": False,
        }

    def get_technical_feasibility(
        self, project_id: UUID, version_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """Obtiene la evaluación técnica de la versión vigente (o la especificada) de forma estrictamente de lectura."""
        if not _is_db_available(self.db_url):
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de viabilidad técnica no disponible: la base de datos no responde."
                )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            target_version_id = version_id
            if target_version_id is None:
                pv = conn.execute(
                    "SELECT id FROM project_versions WHERE project_id = %s ORDER BY version_number DESC LIMIT 1",
                    (project_id,),
                ).fetchone()
                if not pv:
                    raise RecursoPMV1NoEncontradoError("Expediente no encontrado")
                target_version_id = pv["id"]
            else:
                pv = conn.execute(
                    "SELECT 1 FROM project_versions WHERE id = %s AND project_id = %s",
                    (target_version_id, project_id),
                ).fetchone()
                if not pv:
                    raise VersionNoPertenecienteError(
                        "La versión especificada no pertenece al expediente indicado"
                    )

            return self._get_technical_assessment_with_conn(conn, target_version_id)

    def _verify_version_belongs_to_project(
        self, conn: psycopg.Connection[Any], project_id: UUID, version_id: UUID
    ) -> None:
        """Comprueba que project_version_id pertenece a project_id. Lanza VersionNoPertenecienteError si no."""
        row = conn.execute(
            "SELECT 1 FROM project_versions WHERE id = %s AND project_id = %s",
            (version_id, project_id),
        ).fetchone()
        if not row:
            raise VersionNoPertenecienteError(
                f"La versión {version_id} no pertenece al expediente {project_id}. "
                "Operación rechazada sin modificar ninguna estimación.",
                project_id=str(project_id),
                version_id=str(version_id),
            )

    def register_pert_estimate(
        self,
        project_id: UUID,
        actor_id: UUID,
        data: PertEstimateCreate | dict[str, Any],
    ) -> dict[str, Any]:
        """Calcula y persiste la estimación de duración PERT e incertidumbre vinculada a la versión (HU1.9)."""
        project = self.get_project(project_id)
        if isinstance(data, dict):
            req_version_id = data.get("project_version_id")
            opt = data.get("optimistic_duration")
            ml = data.get("most_likely_duration")
            pess = data.get("pessimistic_duration")
            t_unit = data.get("time_unit", "meses")
        else:
            req_version_id = data.project_version_id
            opt = data.optimistic_duration
            ml = data.most_likely_duration
            pess = data.pessimistic_duration
            t_unit = data.time_unit

        version_id = UUID(str(req_version_id)) if req_version_id else project["project_version_id"]

        a, m, b, unit = PertDurationEngine.validate_inputs(
            optimistic=opt,
            most_likely=ml,
            pessimistic=pess,
            time_unit=t_unit,
        )

        calc = PertDurationEngine.calculate(
            optimistic=a,
            most_likely=m,
            pessimistic=b,
            time_unit=unit,
        )

        estimate_id = uuid4()
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            self._verify_version_belongs_to_project(conn, project_id, version_id)
            row = conn.execute(
                """
                INSERT INTO project_pert_estimates
                (id, project_id, project_version_id, registered_by_user_id, time_unit,
                 optimistic_duration, most_likely_duration, pessimistic_duration,
                 expected_duration, standard_deviation, variance,
                 confidence_interval_low, confidence_interval_high,
                 formula_applied, disclaimer, explanation, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (project_version_id) DO UPDATE SET
                    registered_by_user_id = EXCLUDED.registered_by_user_id,
                    time_unit = EXCLUDED.time_unit,
                    optimistic_duration = EXCLUDED.optimistic_duration,
                    most_likely_duration = EXCLUDED.most_likely_duration,
                    pessimistic_duration = EXCLUDED.pessimistic_duration,
                    expected_duration = EXCLUDED.expected_duration,
                    standard_deviation = EXCLUDED.standard_deviation,
                    variance = EXCLUDED.variance,
                    confidence_interval_low = EXCLUDED.confidence_interval_low,
                    confidence_interval_high = EXCLUDED.confidence_interval_high,
                    formula_applied = EXCLUDED.formula_applied,
                    disclaimer = EXCLUDED.disclaimer,
                    explanation = EXCLUDED.explanation,
                    updated_at = now()
                RETURNING id, project_id, project_version_id, registered_by_user_id, time_unit,
                          optimistic_duration, most_likely_duration, pessimistic_duration,
                          expected_duration, standard_deviation, variance,
                          confidence_interval_low, confidence_interval_high,
                          formula_applied, disclaimer, explanation, created_at, updated_at
                """,
                (
                    estimate_id,
                    project_id,
                    version_id,
                    actor_id,
                    unit,
                    calc["optimistic_duration"],
                    calc["most_likely_duration"],
                    calc["pessimistic_duration"],
                    calc["expected_duration"],
                    calc["standard_deviation"],
                    calc["variance"],
                    calc["confidence_interval_low"],
                    calc["confidence_interval_high"],
                    calc["formula_applied"],
                    calc["disclaimer"],
                    calc["explanation"],
                ),
            ).fetchone()

            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'PERT_ESTIMATE_REGISTERED', 'project_pert_estimate', %s, %s)
                """,
                (
                    actor_id,
                    row["id"],
                    Jsonb(
                        {
                            "project_id": str(project_id),
                            "project_version_id": str(version_id),
                            "time_unit": unit,
                            "a": str(calc["optimistic_duration"]),
                            "m": str(calc["most_likely_duration"]),
                            "b": str(calc["pessimistic_duration"]),
                            "expected": str(calc["expected_duration"]),
                            "std_dev": str(calc["standard_deviation"]),
                        }
                    ),
                ),
            )

        return dict(row)

    def _verify_planner_actor(self, actor_id: UUID) -> None:
        """Verifica que el usuario tenga rol de Gerente/Planificador (PLANNER) o Administrador (ADMIN) en auth_db (HU3.4)."""
        raw_auth = self.auth_db_url or os.getenv("AUTH_DB_URL") or get_settings().auth_db_url
        auth_dsn = _dsn(raw_auth)
        with psycopg.connect(auth_dsn, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT r.code
                FROM users u
                JOIN user_roles ur ON u.id = ur.user_id
                JOIN roles r ON ur.role_id = r.id
                WHERE u.id = %s AND u.is_active = TRUE
                """,
                (actor_id,),
            ).fetchall()
            roles = {r["code"] for r in rows}
            if not (roles & {"PLANNER", "PLANNING_MANAGER", "ADMIN", "SUPERADMIN"}):
                raise AccesoDenegadoError(
                    f"El usuario {actor_id} no cuenta con perfil de Gerente de Planeamiento o Administrador. "
                    "Se requiere rol PLANNING_MANAGER, PLANNER o ADMIN para autorizar la reanudación del checkpoint."
                )

    def resume_project_checkpoint(
        self,
        project_id: UUID,
        actor_id: UUID,
        justification: str,
        conditions: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Reanuda la evaluación desde un punto de control HITL persistente autorizado por Gerencia (HU3.4).

        Cumple estrictamente los criterios de aceptación:
        - CA-HU3.4-01: Con pausa persistida y decisión válida de un responsable autorizado, continúa desde el
          checkpoint, conserva los resultados anteriores y no repite etapas ya completadas.
        - CA-HU3.4-02: Sin autorización válida o con un usuario sin permiso, rechaza la reanudación y conserva 'Pendiente de revisión'.
        - CA-HU3.4-03: Si hubo cambios sustanciales o una versión nueva del expediente, no reutiliza la aprobación anterior.
        """
        cleaned_key = idempotency_key.strip()
        if not cleaned_key:
            raise ConfiguracionPMV1Error("Idempotency-Key es obligatorio")

        cleaned_justification = justification.strip() if justification else ""
        if len(cleaned_justification) < 5:
            raise ConfiguracionPMV1Error("La justificación de reanudación debe tener al menos 5 caracteres")

        cleaned_conditions = conditions.strip() if conditions and conditions.strip() else None

        # CA-HU3.4-02: Verificar que el usuario tenga rol autorizado
        self._verify_planner_actor(actor_id)

        project = self.get_project(project_id)
        version_id = project["project_version_id"]

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            # 1. Comprobación de idempotencia estricta
            prev = conn.execute(
                """
                SELECT id AS checkpoint_id, project_id, project_version_id, evaluation_id, status,
                       resolved_by_user_id, resolution_motive, resolution_conditions, resolved_at, idempotency_key
                FROM evaluation_checkpoints
                WHERE idempotency_key = %s
                """,
                (cleaned_key,),
            ).fetchone()

            if prev:
                if prev["project_id"] != project_id or prev["project_version_id"] != version_id:
                    raise ConfiguracionPMV1Error("Idempotency-Key ya fue utilizada para otro expediente o versión")
                if prev["resolution_motive"] != cleaned_justification:
                    raise ConfiguracionPMV1Error("Idempotency-Key no puede ser reutilizada con justificación distinta")
                return {
                    "checkpoint_id": prev["checkpoint_id"],
                    "project_id": prev["project_id"],
                    "project_version_id": prev["project_version_id"],
                    "evaluation_id": prev["evaluation_id"],
                    "status": "resolved",
                    "status_label": "Reanudado / Resuelto",
                    "resolved_by_user_id": prev["resolved_by_user_id"],
                    "resolution_motive": prev["resolution_motive"],
                    "resolution_conditions": prev["resolution_conditions"],
                    "resolved_at": prev["resolved_at"],
                    "project_status": project["status"],
                    "duplicated": True,
                    "message": "Evaluación ya reanudada previamente con esta clave de idempotencia.",
                }

            # 2. Buscar checkpoint para la versión vigente
            cp_row = conn.execute(
                """
                SELECT id AS checkpoint_id, project_id, project_version_id, evaluation_id, status,
                       reason, risk_source, risk_level, details, resolved_by_user_id, resolution_motive,
                       resolution_conditions, resolved_at
                FROM evaluation_checkpoints
                WHERE project_version_id = %s AND checkpoint_type = 'high_risk_pause'
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (version_id,),
            ).fetchone()

            if not cp_row:
                raise RecursoPMV1NoEncontradoError(
                    f"No existe un punto de control (checkpoint) para la versión vigente ({version_id}) del expediente."
                )

            if cp_row["status"] == "resolved":
                raise ConfiguracionPMV1Error(
                    "El punto de control ya ha sido resuelto o reanudado previamente. "
                    "No se puede reanudar nuevamente sin un nuevo punto de control activo."
                )

            if cp_row["status"] != "pending_review":
                raise ConfiguracionPMV1Error(
                    f"El punto de control se encuentra en estado '{cp_row['status']}', no en 'pending_review'."
                )

            now = datetime.now(timezone.utc)
            checkpoint_id = cp_row["checkpoint_id"]
            evaluation_id = cp_row["evaluation_id"]

            # HU3.3: Evaluar la ULTIMA decision registrada para este checkpoint, proyecto y version (orden determinista)
            latest_dec = conn.execute(
                """
                SELECT *
                FROM checkpoint_review_decisions
                WHERE checkpoint_id = %s
                  AND project_id = %s
                  AND project_version_id = %s
                ORDER BY decided_at DESC, created_at DESC, id DESC
                LIMIT 1
                """,
                (checkpoint_id, project_id, version_id),
            ).fetchone()

            if not latest_dec:
                raise ConfiguracionPMV1Error(
                    "Debe registrar previamente una aprobación de continuación firmada (HU3.3) "
                    "para este punto de control y versión antes de reanudar la evaluación."
                )

            if (
                latest_dec["checkpoint_id"] != checkpoint_id
                or latest_dec["project_id"] != project_id
                or latest_dec["project_version_id"] != version_id
            ):
                raise ConfiguracionPMV1Error(
                    "La constancia de decisión no corresponde al punto de control, proyecto o versión vigente."
                )

            if latest_dec["decision"] != "approve_continue":
                raise ConfiguracionPMV1Error(
                    "La última decisión registrada para este punto de control solicita corrección; "
                    "la pausa permanece activa hasta registrar una nueva aprobación de continuación."
                )

            verified_dec = self._serialize_checkpoint_decision_row(
                latest_dec,
                version_number=int(project.get("version_number") or 1),
                checkpoint_status=cp_row["status"],
            )
            if verified_dec["verification_status"] != "valid":
                raise ConfiguracionPMV1Error(
                    "La constancia de aprobación más reciente falló la verificación de integridad SHA-256; "
                    "no se autoriza la reanudación."
                )

            linked_decision_id = latest_dec["id"]

            # 3. Actualizar checkpoint a resolved vinculando decision_id
            conn.execute(
                """
                UPDATE evaluation_checkpoints
                SET status = 'resolved',
                    resolved_by_user_id = %s,
                    resolution_motive = %s,
                    resolution_conditions = %s,
                    resolved_at = %s,
                    idempotency_key = %s,
                    decision_id = %s,
                    updated_at = %s
                WHERE id = %s AND status = 'pending_review'
                """,
                (
                    actor_id,
                    cleaned_justification,
                    cleaned_conditions,
                    now,
                    cleaned_key,
                    linked_decision_id,
                    now,
                    checkpoint_id,
                ),
            )

            # 4. CA-HU3.4-01: Conservar resultados anteriores y continuar procesamiento de la etapa pendiente
            target_eval_id = evaluation_id
            if not target_eval_id:
                ev_find = conn.execute(
                    """
                    SELECT id FROM evaluations
                    WHERE project_version_id = %s
                    ORDER BY requested_at DESC LIMIT 1
                    """,
                    (version_id,),
                ).fetchone()
                if ev_find:
                    target_eval_id = ev_find["id"]

            if target_eval_id:
                # Comprobar si economic_result_projections ya existe
                proj_exists = conn.execute(
                    "SELECT 1 FROM economic_result_projections WHERE evaluation_id = %s",
                    (target_eval_id,),
                ).fetchone()

                if not proj_exists:
                    # Procesar la etapa pendiente exactamente una vez
                    ev_data = conn.execute(
                        """
                        SELECT e.id AS evaluation_id, e.criteria_version_id,
                               pv.estimated_budget_pen, pv.beneficiaries_count,
                               cv.excellent_cost_pen, cv.unacceptable_cost_pen
                        FROM evaluations e
                        JOIN project_versions pv ON pv.id = e.project_version_id
                        LEFT JOIN criteria_versions cv ON cv.id = e.criteria_version_id
                        WHERE e.id = %s
                        """,
                        (target_eval_id,),
                    ).fetchone()

                    if ev_data:
                        excellent = ev_data.get("excellent_cost_pen")
                        unacceptable = ev_data.get("unacceptable_cost_pen")
                        if excellent is None or unacceptable is None:
                            act_cv = conn.execute(
                                "SELECT id, excellent_cost_pen, unacceptable_cost_pen FROM criteria_versions WHERE status = 'active' LIMIT 1"
                            ).fetchone()
                            if act_cv:
                                excellent = act_cv["excellent_cost_pen"]
                                unacceptable = act_cv["unacceptable_cost_pen"]
                                conn.execute(
                                    "UPDATE evaluations SET criteria_version_id = %s WHERE id = %s",
                                    (act_cv["id"], target_eval_id),
                                )

                        budget = Decimal(str(ev_data["estimated_budget_pen"]))
                        beneficiaries = int(ev_data["beneficiaries_count"])
                        exc_dec = Decimal(str(excellent))
                        unacc_dec = Decimal(str(unacceptable))

                        cost, score = EconomicProcessor.calculate(budget, beneficiaries, exc_dec, unacc_dec)
                        assessment_id = uuid4()
                        explanation = (
                            f"Costo por beneficiario = S/ {budget} / {beneficiaries} = S/ {cost}. "
                            f"Umbrales: excelente S/ {exc_dec}, inaceptable S/ {unacc_dec}; score {score}/100. "
                            "Procesado tras reanudación autorizada de checkpoint."
                        )
                        conn.execute(
                            """
                            INSERT INTO economic_result_projections
                            (evaluation_id, economic_assessment_id, cost_per_beneficiary_pen, score_0_100,
                             status, explanation, algorithm_version, received_at)
                            VALUES (%s, %s, %s, %s, 'completed', %s, 'economic-cost-per-beneficiary-v1', %s)
                            ON CONFLICT (evaluation_id) DO UPDATE SET
                                cost_per_beneficiary_pen = EXCLUDED.cost_per_beneficiary_pen,
                                score_0_100 = EXCLUDED.score_0_100,
                                status = 'completed',
                                explanation = EXCLUDED.explanation,
                                received_at = EXCLUDED.received_at
                            """,
                            (target_eval_id, assessment_id, cost, score, explanation, now),
                        )

                # Declarar completed únicamente cuando exista su resultado en economic_result_projections
                has_proj = conn.execute(
                    "SELECT 1 FROM economic_result_projections WHERE evaluation_id = %s",
                    (target_eval_id,),
                ).fetchone()

                if has_proj:
                    conn.execute(
                        """
                        UPDATE evaluations
                        SET status = 'completed', finished_at = %s, error_message = NULL
                        WHERE id = %s
                        """,
                        (now, target_eval_id),
                    )

            if target_eval_id and not has_proj:
                raise ConfiguracionPMV1Error(
                    "No se pudo completar la etapa económica pendiente; el expediente continúa en revisión."
                )
            next_project_status = "evaluated" if target_eval_id else "ready"
            conn.execute(
                "UPDATE projects SET status = %s, updated_at = %s WHERE id = %s",
                (next_project_status, now, project_id),
            )

            # 5. Persistir auditoría de la decisión
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'CHECKPOINT_RESUMED', 'evaluation_checkpoint', %s, %s)
                """,
                (
                    actor_id,
                    checkpoint_id,
                    Jsonb({
                        "project_id": str(project_id),
                        "project_version_id": str(version_id),
                        "evaluation_id": str(evaluation_id) if evaluation_id else None,
                        "resolution_motive": cleaned_justification,
                        "resolution_conditions": cleaned_conditions,
                        "resumed_at": now.isoformat(),
                    }),
                ),
            )

        return {
            "checkpoint_id": checkpoint_id,
            "project_id": project_id,
            "project_version_id": version_id,
            "evaluation_id": evaluation_id,
            "status": "resolved",
            "status_label": "Reanudado / Resuelto",
            "resolved_by_user_id": actor_id,
            "resolution_motive": cleaned_justification,
            "resolution_conditions": cleaned_conditions,
            "resolved_at": now,
            "project_status": next_project_status,
            "duplicated": False,
            "message": "Evaluación reanudada exitosamente desde el punto de control conservando resultados previos.",
        }

    def get_project_checkpoint(self, project_id: UUID) -> dict[str, Any] | None:
        """Obtiene el punto de control persistente (pendiente o resuelto) para la versión vigente del proyecto (HU3.1 / HU3.4)."""
        project = self.get_project(project_id)
        if project.get("checkpoint"):
            return project["checkpoint"]
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            return RiskCheckpointEngine.get_latest_checkpoint(conn, project["project_version_id"])

    def _serialize_checkpoint_decision_row(
        self,
        row: dict[str, Any],
        *,
        version_number: int,
        checkpoint_status: str,
        duplicated: bool = False,
    ) -> dict[str, Any]:
        expected_canonical, recomputed_hash = _compute_checkpoint_decision_sha256(
            decision_id=row["id"],
            checkpoint_id=row["checkpoint_id"],
            project_id=row["project_id"],
            project_version_id=row["project_version_id"],
            decision=row["decision"],
            justification=row["justification"],
            conditions=row.get("conditions"),
            decided_by_user_id=row["decided_by_user_id"],
            decided_by_name=row.get("decided_by_name"),
            decided_by_email=row.get("decided_by_email"),
            decided_at=row["decided_at"],
        )
        stored_hash = str(row["integrity_hash_sha256"]).strip()
        stored_canonical = row.get("canonical_payload")
        canonical_matches = isinstance(stored_canonical, dict) and stored_canonical == expected_canonical
        is_valid = hmac.compare_digest(recomputed_hash, stored_hash) and canonical_matches
        decision_labels = {
            "approve_continue": "Aprobación para continuar evaluación",
            "request_correction": "Corrección solicitada (pausa vigente)",
        }
        return {
            "decision_id": row["id"],
            "checkpoint_id": row["checkpoint_id"],
            "project_id": row["project_id"],
            "project_version_id": row["project_version_id"],
            "version_number": version_number,
            "decision": row["decision"],
            "decision_label": decision_labels.get(row["decision"], row["decision"]),
            "justification": row["justification"],
            "conditions": row.get("conditions"),
            "decided_by_user_id": row["decided_by_user_id"],
            "decided_by_name": row.get("decided_by_name"),
            "decided_by_email": row.get("decided_by_email"),
            "decided_at": row["decided_at"],
            "idempotency_key": row["idempotency_key"],
            "integrity_hash_sha256": stored_hash,
            "recomputed_hash_sha256": recomputed_hash,
            "verification_status": "valid" if is_valid else "failed",
            "verification_label": "Válida (integridad verificada)" if is_valid else "Fallida (alteración detectada)",
            "prototype_notice": HU33_PROTOTYPE_NOTICE,
            "checkpoint_status": checkpoint_status,
            "duplicated": duplicated,
        }

    def register_checkpoint_decision(
        self,
        *,
        project_id: UUID,
        actor_id: UUID,
        actor_name: str | None,
        actor_email: str | None,
        decision: str,
        justification: str,
        conditions: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Registra una decisión autenticada y con huella SHA-256 sobre un checkpoint pendiente (HU3.3)."""
        cleaned_key = idempotency_key.strip()
        if not cleaned_key:
            raise ConfiguracionPMV1Error("Idempotency-Key es obligatorio")

        cleaned_decision = decision.strip().lower()
        if cleaned_decision not in ("approve_continue", "request_correction"):
            raise ConfiguracionPMV1Error("Decisión inválida. Use 'approve_continue' o 'request_correction'.")

        cleaned_justification = justification.strip() if justification else ""
        if len(cleaned_justification) < 5:
            raise ConfiguracionPMV1Error("El fundamento de la decisión debe tener al menos 5 caracteres.")

        cleaned_conditions = conditions.strip() if conditions and conditions.strip() else None
        if cleaned_decision == "request_correction" and not cleaned_conditions:
            raise ConfiguracionPMV1Error(
                "Al solicitar una corrección debe especificar las observaciones o correcciones requeridas."
            )

        project = self.get_project(project_id)
        version_id = project["project_version_id"]
        version_number = int(project.get("version_number") or 1)

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            prev = conn.execute(
                """
                SELECT d.*, c.status AS cp_status
                FROM checkpoint_review_decisions d
                JOIN evaluation_checkpoints c ON c.id = d.checkpoint_id
                WHERE d.idempotency_key = %s
                """,
                (cleaned_key,),
            ).fetchone()
            if prev:
                if prev["project_id"] != project_id or prev["project_version_id"] != version_id:
                    raise ConfiguracionPMV1Error("Idempotency-Key ya fue utilizada para otro expediente o versión.")
                if (
                    prev["decision"] != cleaned_decision
                    or prev["justification"] != cleaned_justification
                    or (prev.get("conditions") or None) != cleaned_conditions
                ):
                    raise ConfiguracionPMV1Error(
                        "Idempotency-Key no puede reutilizarse con decisión, fundamento o condiciones distintas."
                    )
                return self._serialize_checkpoint_decision_row(
                    prev,
                    version_number=version_number,
                    checkpoint_status=prev["cp_status"],
                    duplicated=True,
                )

            cp_row = conn.execute(
                """
                SELECT id, status
                FROM evaluation_checkpoints
                WHERE project_version_id = %s
                  AND checkpoint_type = 'high_risk_pause'
                ORDER BY created_at DESC
                LIMIT 1
                FOR UPDATE
                """,
                (version_id,),
            ).fetchone()

            if not cp_row:
                raise RecursoPMV1NoEncontradoError(
                    f"No existe un punto de control (checkpoint) para la versión vigente ({version_id})."
                )
            if cp_row["status"] != "pending_review":
                raise ConfiguracionPMV1Error(
                    f"El punto de control de la versión vigente está en estado '{cp_row['status']}', "
                    "no en 'pending_review'."
                )

            decision_id = uuid4()
            decided_at = datetime.now(timezone.utc)
            canonical_payload, integrity_hash = _compute_checkpoint_decision_sha256(
                decision_id=decision_id,
                checkpoint_id=cp_row["id"],
                project_id=project_id,
                project_version_id=version_id,
                decision=cleaned_decision,
                justification=cleaned_justification,
                conditions=cleaned_conditions,
                decided_by_user_id=actor_id,
                decided_by_name=actor_name,
                decided_by_email=actor_email,
                decided_at=decided_at,
            )

            inserted = conn.execute(
                """
                INSERT INTO checkpoint_review_decisions
                (id, checkpoint_id, project_id, project_version_id, decision, justification,
                 conditions, decided_by_user_id, decided_by_name, decided_by_email, decided_at,
                 idempotency_key, canonical_payload, integrity_hash_sha256)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    decision_id,
                    cp_row["id"],
                    project_id,
                    version_id,
                    cleaned_decision,
                    cleaned_justification,
                    cleaned_conditions,
                    actor_id,
                    actor_name,
                    actor_email,
                    decided_at,
                    cleaned_key,
                    Jsonb(canonical_payload),
                    integrity_hash,
                ),
            ).fetchone()

            conn.execute(
                """
                INSERT INTO audit_events (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'CHECKPOINT_DECISION_RECORDED', 'checkpoint_decision', %s, %s)
                """,
                (
                    actor_id,
                    decision_id,
                    Jsonb({
                        "project_id": str(project_id),
                        "project_version_id": str(version_id),
                        "checkpoint_id": str(cp_row["id"]),
                        "decision": cleaned_decision,
                    }),
                ),
            )

            return self._serialize_checkpoint_decision_row(
                inserted,
                version_number=version_number,
                checkpoint_status=cp_row["status"],
                duplicated=False,
            )

    def list_checkpoint_decisions(self, project_id: UUID) -> dict[str, Any]:
        """Consulta y verifica en solo lectura la integridad SHA-256 de las decisiones de la versión vigente (HU3.3)."""
        project = self.get_project(project_id)
        version_id = project["project_version_id"]
        version_number = int(project.get("version_number") or 1)

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT d.*, c.status AS cp_status
                FROM checkpoint_review_decisions d
                JOIN evaluation_checkpoints c ON c.id = d.checkpoint_id
                WHERE d.project_id = %s AND d.project_version_id = %s
                ORDER BY d.decided_at DESC, d.created_at DESC, d.id DESC
                """,
                (project_id, version_id),
            ).fetchall()

        decisions = [
            self._serialize_checkpoint_decision_row(
                r,
                version_number=version_number,
                checkpoint_status=r["cp_status"],
            )
            for r in rows
        ]
        return {
            "project_id": project_id,
            "project_version_id": version_id,
            "version_number": version_number,
            "decisions": decisions,
            "prototype_notice": HU33_PROTOTYPE_NOTICE,
        }

    def _resolve_actor_names(self, actor_ids: list[UUID]) -> dict[UUID, str]:
        """Consulta en solo lectura los nombres reales en auth_db sin inventar cuentas inexistentes."""
        unique_ids = list({aid for aid in actor_ids if aid is not None})
        if not unique_ids:
            return {}
        raw_auth = self.auth_db_url or os.getenv("AUTH_DB_URL") or get_settings().auth_db_url
        auth_dsn = _dsn(raw_auth)
        try:
            if not _is_db_available(auth_dsn):
                return {}
            with psycopg.connect(auth_dsn, row_factory=dict_row) as conn:
                rows = conn.execute(
                    "SELECT id, full_name FROM users WHERE id = ANY(%s)",
                    (unique_ids,),
                ).fetchall()
            return {r["id"]: str(r["full_name"]) for r in rows if r.get("full_name")}
        except Exception:
            return {}

    def list_auditable_projects(self) -> dict[str, Any]:
        """Lista los expedientes disponibles para consulta de auditoria en solo lectura (HU5.3)."""
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT p.id AS project_id, p.code, p.status, p.created_at, p.updated_at,
                       latest.title, latest.version_number AS current_version_number,
                       (SELECT COUNT(*) FROM project_versions pv2 WHERE pv2.project_id = p.id) AS versions_count
                FROM projects p
                JOIN LATERAL (
                    SELECT title, version_number
                    FROM project_versions pv
                    WHERE pv.project_id = p.id
                    ORDER BY pv.version_number DESC
                    LIMIT 1
                ) latest ON TRUE
                ORDER BY p.updated_at DESC, p.created_at DESC, p.code ASC
                """
            ).fetchall()
        return {"projects": [dict(r) for r in rows]}

    def get_project_history(self, project_id: UUID) -> dict[str, Any]:
        """Reconstruye en solo lectura el historial cronologico completo de un expediente (HU5.3)."""
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            proj = conn.execute(
                "SELECT id, code, status, created_by_user_id, created_at, updated_at FROM projects WHERE id = %s",
                (project_id,),
            ).fetchone()
            if not proj:
                raise RecursoPMV1NoEncontradoError("Expediente no encontrado.")

            version_rows = conn.execute(
                """
                SELECT id, project_id, version_number, title, estimated_budget_pen,
                       beneficiaries_count, created_by_user_id, created_at
                FROM project_versions
                WHERE project_id = %s
                ORDER BY version_number ASC, created_at ASC
                """,
                (project_id,),
            ).fetchall()
            if not version_rows:
                raise RecursoPMV1NoEncontradoError("El expediente no tiene versiones registradas.")

            version_ids = [v["id"] for v in version_rows]
            version_num_by_id = {v["id"]: int(v["version_number"]) for v in version_rows}
            latest_v = version_rows[-1]

            eval_rows = conn.execute(
                """
                SELECT e.id, e.project_version_id, e.requested_by_user_id, e.status,
                       e.requested_at, e.started_at, e.finished_at, e.error_message,
                       cv.version_number AS criteria_version_number,
                       erp.score_0_100, erp.cost_per_beneficiary_pen, erp.received_at AS projection_received_at
                FROM evaluations e
                LEFT JOIN criteria_versions cv ON cv.id = e.criteria_version_id
                LEFT JOIN economic_result_projections erp ON erp.evaluation_id = e.id
                WHERE e.project_version_id = ANY(%s)
                ORDER BY e.requested_at ASC
                """,
                (version_ids,),
            ).fetchall()

            social_rows = conn.execute(
                """
                SELECT id, project_version_id, requested_by_user_id, status, social_score, summary, evaluated_at
                FROM social_accessibility_assessments
                WHERE project_version_id = ANY(%s)
                ORDER BY evaluated_at ASC
                """,
                (version_ids,),
            ).fetchall()

            env_rows = conn.execute(
                """
                SELECT id, project_version_id, assessed_by_user_id, status, risk_level,
                       environmental_score, favorable, summary, evaluated_at
                FROM environmental_assessments
                WHERE project_version_id = ANY(%s)
                ORDER BY evaluated_at ASC
                """,
                (version_ids,),
            ).fetchall()

            tech_rows = conn.execute(
                """
                SELECT id, project_version_id, requested_by_user_id, status, overall_result,
                       overall_result_label, technical_score, summary, evaluated_at
                FROM technical_feasibility_assessments
                WHERE project_version_id = ANY(%s)
                ORDER BY evaluated_at ASC
                """,
                (version_ids,),
            ).fetchall()

            legal_rows = conn.execute(
                """
                SELECT id, project_version_id, status, summary, verifiable_citations_count,
                       unverifiable_citations_count, has_legal_alert, legal_alert_norm,
                       is_resolved, evaluated_at
                FROM legal_assessments
                WHERE project_id = %s AND (project_version_id = ANY(%s) OR project_version_id IS NULL)
                ORDER BY evaluated_at ASC
                """,
                (project_id, version_ids),
            ).fetchall()

            legal_res_rows = conn.execute(
                """
                SELECT id, project_version_id, resolved_by_user_id, resolution_action,
                       justification, evidence_reference, resolved_at
                FROM legal_alert_resolutions
                WHERE project_id = %s
                ORDER BY resolved_at ASC
                """,
                (project_id,),
            ).fetchall()

            zoning_rows = conn.execute(
                """
                SELECT id, project_version_id, requested_by_user_id, status, compatible,
                       source_document, source_locator, limitations, requested_at, reviewed_at
                FROM zoning_review_requests
                WHERE project_version_id = ANY(%s)
                ORDER BY requested_at ASC
                """,
                (version_ids,),
            ).fetchall()

            pert_rows = conn.execute(
                """
                SELECT id, project_version_id, registered_by_user_id, expected_duration,
                       standard_deviation, time_unit, created_at
                FROM project_pert_estimates
                WHERE project_id = %s
                ORDER BY created_at ASC
                """,
                (project_id,),
            ).fetchall()

            geo_rows = conn.execute(
                """
                SELECT id, project_version_id, layer_type, layer_name, data_origin,
                       availability_status, created_by_user_id, created_at
                FROM project_geospatial_layers
                WHERE project_version_id = ANY(%s)
                ORDER BY created_at ASC
                """,
                (version_ids,),
            ).fetchall()

            cp_rows = conn.execute(
                """
                SELECT id, project_version_id, checkpoint_type, status, reason, risk_source,
                       risk_level, resolved_by_user_id, resolution_motive, resolution_conditions,
                       resolved_at, created_at
                FROM evaluation_checkpoints
                WHERE project_id = %s
                ORDER BY created_at ASC
                """,
                (project_id,),
            ).fetchall()

            cp_decision_rows = conn.execute(
                """
                SELECT d.*, c.status AS cp_status
                FROM checkpoint_review_decisions d
                JOIN evaluation_checkpoints c ON c.id = d.checkpoint_id
                WHERE d.project_id = %s
                ORDER BY d.decided_at ASC, d.created_at ASC
                """,
                (project_id,),
            ).fetchall()

            approval_rows = conn.execute(
                """
                SELECT id, project_version_id, decision, justification, conditions,
                       decided_by_user_id, decided_at
                FROM human_approvals
                WHERE project_id = %s
                ORDER BY decided_at ASC
                """,
                (project_id,),
            ).fetchall()

            candidate_entity_ids = (
                [project_id]
                + list(version_ids)
                + [r["id"] for r in eval_rows]
                + [r["id"] for r in social_rows]
                + [r["id"] for r in env_rows]
                + [r["id"] for r in tech_rows]
                + [r["id"] for r in legal_rows]
                + [r["id"] for r in legal_res_rows]
                + [r["id"] for r in zoning_rows]
                + [r["id"] for r in pert_rows]
                + [r["id"] for r in cp_rows]
                + [r["id"] for r in cp_decision_rows]
                + [r["id"] for r in approval_rows]
            )
            audit_rows = conn.execute(
                """
                SELECT id, actor_user_id, action, entity_type, entity_id, details, occurred_at
                FROM audit_events
                WHERE entity_id = ANY(%s)
                   OR details->>'project_id' = %s
                ORDER BY occurred_at ASC, id ASC
                """,
                (candidate_entity_ids, str(project_id)),
            ).fetchall()

        # Recolectar todos los UUID de actores para consulta unica a auth_db (sin hashes ni secretos)
        actor_ids: list[UUID] = [proj["created_by_user_id"]]
        for r in version_rows:
            actor_ids.append(r["created_by_user_id"])
        for r in eval_rows:
            actor_ids.append(r["requested_by_user_id"])
        for r in social_rows:
            actor_ids.append(r["requested_by_user_id"])
        for r in env_rows:
            actor_ids.append(r["assessed_by_user_id"])
        for r in tech_rows:
            actor_ids.append(r["requested_by_user_id"])
        for r in legal_res_rows:
            actor_ids.append(r["resolved_by_user_id"])
        for r in zoning_rows:
            actor_ids.append(r["requested_by_user_id"])
        for r in pert_rows:
            actor_ids.append(r["registered_by_user_id"])
        for r in geo_rows:
            actor_ids.append(r["created_by_user_id"])
        for r in cp_rows:
            if r.get("resolved_by_user_id"):
                actor_ids.append(r["resolved_by_user_id"])
        for r in cp_decision_rows:
            actor_ids.append(r["decided_by_user_id"])
        for r in approval_rows:
            actor_ids.append(r["decided_by_user_id"])
        for r in audit_rows:
            if r.get("actor_user_id"):
                actor_ids.append(r["actor_user_id"])

        actor_names = self._resolve_actor_names(actor_ids)

        timeline: list[tuple[datetime, int, str, dict[str, Any]]] = []

        def _add(ts: datetime, priority: int, item: dict[str, Any]) -> None:
            timeline.append((ts, priority, item["event_id"], item))

        for v in version_rows:
            vid = v["id"]
            vnum = int(v["version_number"])
            _add(
                v["created_at"],
                10,
                {
                    "event_id": f"version:{vid}",
                    "event_type": "VERSION_CREATED",
                    "category": "version",
                    "title": f"Creación de versión v{vnum}" if vnum > 1 else "Registro inicial del expediente (v1)",
                    "source": "Registro de versiones del expediente",
                    "occurred_at": v["created_at"],
                    "project_version_id": vid,
                    "version_number": vnum,
                    "actor_user_id": v["created_by_user_id"],
                    "actor_name": actor_names.get(v["created_by_user_id"]),
                    "result": f"Versión v{vnum}",
                    "detail": (
                        f"{v['title']} · Presupuesto: "
                        + (
                            f"S/ {float(v['estimated_budget_pen']):,.2f}"
                            if v.get("estimated_budget_pen") is not None
                            else "Pendiente"
                        )
                        + " · Beneficiarios est.: "
                        + (
                            f"{int(v['beneficiaries_count']):,}"
                            if v.get("beneficiaries_count") is not None
                            else "Pendiente"
                        )
                    ),
                },
            )

        for g in geo_rows:
            vid = g["project_version_id"]
            _add(
                g["created_at"],
                20,
                {
                    "event_id": f"geo:{g['id']}",
                    "event_type": "GEOSPATIAL_LAYER_REGISTERED",
                    "category": "territorial",
                    "title": f"Capa geoespacial registrada ({g['layer_name']})",
                    "source": "Capas geoespaciales",
                    "occurred_at": g["created_at"],
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid),
                    "actor_user_id": g["created_by_user_id"],
                    "actor_name": actor_names.get(g["created_by_user_id"]),
                    "result": f"{g['layer_type']} · {g['availability_status']}",
                    "detail": f"Origen: {g['data_origin']}",
                },
            )

        for p_est in pert_rows:
            vid = p_est["project_version_id"]
            _add(
                p_est["created_at"],
                25,
                {
                    "event_id": f"pert:{p_est['id']}",
                    "event_type": "PERT_ESTIMATE",
                    "category": "evaluation",
                    "title": "Estimación de duración PERT",
                    "source": "Estimación de plazos PERT",
                    "occurred_at": p_est["created_at"],
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid),
                    "actor_user_id": p_est["registered_by_user_id"],
                    "actor_name": actor_names.get(p_est["registered_by_user_id"]),
                    "result": f"{float(p_est['expected_duration']):.2f} {p_est['time_unit']}",
                    "detail": f"Desviación estándar: {float(p_est['standard_deviation']):.2f} {p_est['time_unit']}",
                },
            )

        for z in zoning_rows:
            vid = z["project_version_id"]
            ts = z.get("reviewed_at") or z["requested_at"]
            compat = (
                "Compatible" if z["compatible"] is True else ("No compatible" if z["compatible"] is False else "Pendiente de revisión")
            )
            doc_txt = f"{z['source_document']} ({z['source_locator']})" if z.get("source_document") else z["limitations"]
            _add(
                ts,
                30,
                {
                    "event_id": f"zoning:{z['id']}",
                    "event_type": "ZONING_REVIEW",
                    "category": "review",
                    "title": "Revisión de zonificación territorial",
                    "source": "Revisión de zonificación",
                    "occurred_at": ts,
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid),
                    "actor_user_id": z["requested_by_user_id"],
                    "actor_name": actor_names.get(z["requested_by_user_id"]),
                    "result": f"{z['status']} · {compat}",
                    "detail": str(doc_txt),
                },
            )

        for s in social_rows:
            vid = s["project_version_id"]
            score_txt = f"{float(s['social_score']):.2f} pts" if s.get("social_score") is not None else "Sin puntaje"
            _add(
                s["evaluated_at"],
                35,
                {
                    "event_id": f"social:{s['id']}",
                    "event_type": "SOCIAL_ASSESSMENT",
                    "category": "evaluation",
                    "title": "Evaluación social y equidad territorial",
                    "source": "Evaluación social",
                    "occurred_at": s["evaluated_at"],
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid),
                    "actor_user_id": s["requested_by_user_id"],
                    "actor_name": actor_names.get(s["requested_by_user_id"]),
                    "result": f"{s['status']} · {score_txt}",
                    "detail": str(s["summary"]),
                },
            )

        for env in env_rows:
            vid = env["project_version_id"]
            score_txt = f"{float(env['environmental_score']):.2f} pts" if env.get("environmental_score") is not None else "Sin puntaje"
            _add(
                env["evaluated_at"],
                40,
                {
                    "event_id": f"env:{env['id']}",
                    "event_type": "ENVIRONMENTAL_ASSESSMENT",
                    "category": "evaluation",
                    "title": "Evaluación ambiental",
                    "source": "Evaluación ambiental",
                    "occurred_at": env["evaluated_at"],
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid),
                    "actor_user_id": env["assessed_by_user_id"],
                    "actor_name": actor_names.get(env["assessed_by_user_id"]),
                    "result": f"{env['status']} · Riesgo {env['risk_level']} · {score_txt}",
                    "detail": str(env.get("risk_observation") or env["summary"]),
                },
            )

        for tech in tech_rows:
            vid = tech["project_version_id"]
            score_txt = f"{float(tech['technical_score']):.2f} pts" if tech.get("technical_score") is not None else "Sin puntaje"
            _add(
                tech["evaluated_at"],
                45,
                {
                    "event_id": f"tech:{tech['id']}",
                    "event_type": "TECHNICAL_ASSESSMENT",
                    "category": "evaluation",
                    "title": "Evaluación de viabilidad técnica",
                    "source": "Evaluación técnica",
                    "occurred_at": tech["evaluated_at"],
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid),
                    "actor_user_id": tech["requested_by_user_id"],
                    "actor_name": actor_names.get(tech["requested_by_user_id"]),
                    "result": f"{tech['overall_result_label']} · {score_txt}",
                    "detail": str(tech["summary"]),
                },
            )

        consumed_audit_ids: set[UUID] = set()
        for leg in legal_rows:
            vid = leg.get("project_version_id")
            leg_ts = leg["evaluated_at"]
            matched_eval_ae = None
            best_diff = 5.0
            for ae in audit_rows:
                if ae["id"] in consumed_audit_ids:
                    continue
                if ae["action"] == "LEGAL_ASSESSMENT_EVALUATED":
                    diff = abs((ae["occurred_at"] - leg_ts).total_seconds())
                    if diff <= best_diff:
                        best_diff = diff
                        matched_eval_ae = ae
            if matched_eval_ae is not None:
                consumed_audit_ids.add(matched_eval_ae["id"])
            for ae in audit_rows:
                if ae["id"] not in consumed_audit_ids and ae["action"] == "LEGAL_PRECHECK_REQUIRES_REVIEW":
                    if abs((ae["occurred_at"] - leg_ts).total_seconds()) <= 5.0:
                        consumed_audit_ids.add(ae["id"])
            leg_actor_id = matched_eval_ae.get("actor_user_id") if matched_eval_ae else None
            _add(
                leg_ts,
                50,
                {
                    "event_id": f"legal:{leg['id']}",
                    "event_type": "LEGAL_ASSESSMENT",
                    "category": "evaluation",
                    "title": "Evaluación jurídica",
                    "source": "Evaluación jurídica",
                    "occurred_at": leg_ts,
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid) if vid else None,
                    "actor_user_id": leg_actor_id,
                    "actor_name": actor_names.get(leg_actor_id) if leg_actor_id else None,
                    "result": (
                        f"{leg['status']} ({leg['verifiable_citations_count']} cita(s) verificable(s))"
                        + (" · Alerta legal" if leg.get("has_legal_alert") else "")
                    ),
                    "detail": str(leg["summary"]),
                },
            )

        for lr in legal_res_rows:
            vid = lr["project_version_id"]
            _add(
                lr["resolved_at"],
                55,
                {
                    "event_id": f"legalres:{lr['id']}",
                    "event_type": "LEGAL_ALERT_RESOLVED",
                    "category": "review",
                    "title": f"Resolución de alerta legal ({lr['resolution_action']})",
                    "source": "Resoluciones de alerta legal",
                    "occurred_at": lr["resolved_at"],
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid),
                    "actor_user_id": lr["resolved_by_user_id"],
                    "actor_name": actor_names.get(lr["resolved_by_user_id"]),
                    "result": str(lr["resolution_action"]),
                    "detail": str(lr["justification"]),
                },
            )

        for ev in eval_rows:
            vid = ev["project_version_id"]
            vnum = version_num_by_id.get(vid)
            c_ver = f"criterios v{ev['criteria_version_number']}" if ev.get("criteria_version_number") else "criterios"
            _add(
                ev["requested_at"],
                60,
                {
                    "event_id": f"evalreq:{ev['id']}",
                    "event_type": "EVALUATION_REQUESTED",
                    "category": "evaluation",
                    "title": f"Solicitud de evaluación económica ({c_ver})",
                    "source": "Evaluaciones económicas",
                    "occurred_at": ev["requested_at"],
                    "project_version_id": vid,
                    "version_number": vnum,
                    "actor_user_id": ev["requested_by_user_id"],
                    "actor_name": actor_names.get(ev["requested_by_user_id"]),
                    "result": str(ev["status"]),
                    "detail": str(ev.get("error_message") or f"Evaluación registrada con {c_ver}."),
                },
            )
            if ev.get("projection_received_at") is not None and ev.get("score_0_100") is not None:
                _add(
                    ev["projection_received_at"],
                    80,
                    {
                        "event_id": f"evaldone:{ev['id']}",
                        "event_type": "ECONOMIC_EVALUATION_COMPLETED",
                        "category": "evaluation",
                        "title": "Resultado de evaluación económica registrado",
                        "source": "Proyección económica",
                        "occurred_at": ev["projection_received_at"],
                        "project_version_id": vid,
                        "version_number": vnum,
                        "actor_user_id": None,
                        "actor_name": None,
                        "result": f"{float(ev['score_0_100']):.2f} / 100 pts",
                        "detail": f"Costo por beneficiario: S/ {float(ev['cost_per_beneficiary_pen']):,.2f}",
                    },
                )

        for cp in cp_rows:
            vid = cp["project_version_id"]
            vnum = version_num_by_id.get(vid)
            _add(
                cp["created_at"],
                65,
                {
                    "event_id": f"cp_pause:{cp['id']}",
                    "event_type": "CHECKPOINT_PAUSED",
                    "category": "checkpoint",
                    "title": "Pausa en punto de control por riesgo alto",
                    "source": "Puntos de control",
                    "occurred_at": cp["created_at"],
                    "project_version_id": vid,
                    "version_number": vnum,
                    "actor_user_id": None,
                    "actor_name": None,
                    "result": f"Riesgo {cp['risk_level']} ({cp['risk_source']})",
                    "detail": str(cp["reason"]),
                },
            )
            if cp.get("resolved_at") is not None:
                _add(
                    cp["resolved_at"],
                    75,
                    {
                        "event_id": f"cp_resume:{cp['id']}",
                        "event_type": "CHECKPOINT_RESUMED",
                        "category": "resume",
                        "title": "Reanudación de evaluación desde punto de control",
                        "source": "Reanudación de punto de control",
                        "occurred_at": cp["resolved_at"],
                        "project_version_id": vid,
                        "version_number": vnum,
                        "actor_user_id": cp.get("resolved_by_user_id"),
                        "actor_name": actor_names.get(cp["resolved_by_user_id"]) if cp.get("resolved_by_user_id") else None,
                        "result": "Reanudado / Resuelto",
                        "detail": (
                            str(cp.get("resolution_motive") or "")
                            + (f" · Condiciones: {cp['resolution_conditions']}" if cp.get("resolution_conditions") else "")
                        ),
                    },
                )

        for dec in cp_decision_rows:
            vid = dec["project_version_id"]
            vnum = version_num_by_id.get(vid, 1)
            serialized = self._serialize_checkpoint_decision_row(
                dec,
                version_number=vnum,
                checkpoint_status=dec["cp_status"],
            )
            uid = dec["decided_by_user_id"]
            in_dir = uid in actor_names
            resolved_name = actor_names.get(uid) if in_dir else None
            _add(
                dec["decided_at"],
                70,
                {
                    "event_id": f"cp_dec:{dec['id']}",
                    "event_type": "CHECKPOINT_DECISION",
                    "category": "decision",
                    "title": serialized["decision_label"],
                    "source": "Constancia de decisión sobre punto de control",
                    "occurred_at": dec["decided_at"],
                    "project_version_id": vid,
                    "version_number": vnum,
                    "actor_user_id": uid,
                    "actor_name": resolved_name,
                    "actor_in_directory": in_dir,
                    "record_id": dec["id"],
                    "decision_kind": "checkpoint_decision",
                    "decision_code": str(dec["decision"]),
                    "justification": str(dec["justification"]),
                    "conditions": dec.get("conditions"),
                    "checkpoint_id": dec["checkpoint_id"],
                    "checkpoint_status": str(dec["cp_status"]),
                    "has_integrity_hash": bool(serialized["integrity_hash_sha256"]),
                    "result": serialized["verification_label"],
                    "detail": (
                        str(dec["justification"])
                        + (f" · Condiciones/corrección: {dec['conditions']}" if dec.get("conditions") else "")
                    ),
                    "integrity_hash_sha256": serialized["integrity_hash_sha256"],
                    "verification_status": serialized["verification_status"],
                    "verification_label": serialized["verification_label"],
                    "prototype_notice": serialized["prototype_notice"],
                },
            )

        for ap in approval_rows:
            vid = ap["project_version_id"]
            vnum = version_num_by_id.get(vid)
            uid = ap["decided_by_user_id"]
            in_dir = uid in actor_names
            decision_es = {"approved": "Aprobado", "rejected": "Rechazado", "observed": "Observado"}.get(
                ap["decision"], ap["decision"]
            )
            _add(
                ap["decided_at"],
                90,
                {
                    "event_id": f"approval:{ap['id']}",
                    "event_type": "HUMAN_APPROVAL",
                    "category": "approval",
                    "title": f"Dictamen final registrado ({decision_es})",
                    "source": "Dictámenes finales",
                    "occurred_at": ap["decided_at"],
                    "project_version_id": vid,
                    "version_number": vnum,
                    "actor_user_id": uid,
                    "actor_name": actor_names.get(uid) if in_dir else None,
                    "actor_in_directory": in_dir,
                    "record_id": ap["id"],
                    "decision_kind": "final_approval",
                    "decision_code": str(ap["decision"]),
                    "justification": str(ap["justification"]),
                    "conditions": ap.get("conditions"),
                    "checkpoint_id": None,
                    "checkpoint_status": None,
                    "has_integrity_hash": False,
                    "result": decision_es,
                    "detail": (
                        str(ap["justification"])
                        + (f" · Condiciones: {ap['conditions']}" if ap.get("conditions") else "")
                    ),
                    "integrity_hash_sha256": None,
                    "verification_status": None,
                    "verification_label": "Verificación de integridad no disponible para este registro",
                    "prototype_notice": (
                        "Este dictamen final no dispone de constancia criptográfica SHA-256 en el registro actual; "
                        "no equivale a firma digital certificada con validez jurídica."
                    ),
                },
            )

        # Eventos de audit_events complementarios (excluyendo duplicados ya reconstruidos arriba)
        covered_actions = {
            "PROJECT_CREATED",
            "PROJECT_VERSION_CREATED",
            "EVALUATION_QUEUED",
            "EVALUATION_COMPLETED",
            "EVALUATION_PAUSED_HIGH_RISK",
            "CHECKPOINT_DECISION_RECORDED",
            "CHECKPOINT_RESUMED",
            "PROJECT_HUMAN_APPROVAL_RECORDED",
            "SOCIAL_ACCESSIBILITY_EVALUATED",
            "ENVIRONMENTAL_ASSESSMENT_EVALUATED",
            "TECHNICAL_ASSESSMENT_COMPLETED",
            "PERT_ESTIMATE_REGISTERED",
            "ZONING_REVIEW_REQUESTED",
            "LEGAL_ALERT_RESOLVED",
            "GEOSPATIAL_LAYER_REGISTERED",
        }
        for ae in audit_rows:
            if ae["id"] in consumed_audit_ids or ae["action"] in covered_actions:
                continue
            details = ae.get("details") or {}
            vid_str = details.get("project_version_id")
            vid = UUID(vid_str) if vid_str else None
            uid = ae.get("actor_user_id")
            action_key = str(ae["action"])
            title_map = {
                "LEGAL_ASSESSMENT_EVALUATED": "Evaluación jurídica ejecutada (registro histórico en bitácora)",
                "LEGAL_PRECHECK_REQUIRES_REVIEW": "Prechequeo normativo con observación para revisión",
            }
            if action_key == "LEGAL_ASSESSMENT_EVALUATED":
                result_txt = f"{details.get('total_findings', 0)} hallazgo(s) · {details.get('verifiable_count', 0)} cita(s) verificable(s)"
                detail_txt = (
                    "Con alerta legal."
                    if details.get("has_legal_alert")
                    else "Evaluación jurídica registrada en bitácora."
                )
            elif action_key == "LEGAL_PRECHECK_REQUIRES_REVIEW":
                result_txt = "Requiere revisión"
                detail_txt = "Prechequeo legal ejecutado durante el análisis normativo."
            else:
                result_txt = "Registro de bitácora"
                detail_txt = str(details.get("reason") or details.get("status") or "Evento registrado en bitácora.")
            _add(
                ae["occurred_at"],
                95,
                {
                    "event_id": f"audit:{ae['id']}",
                    "event_type": action_key,
                    "category": "audit",
                    "title": title_map.get(action_key, "Evento complementario de bitácora"),
                    "source": "Bitácora de auditoría",
                    "occurred_at": ae["occurred_at"],
                    "project_version_id": vid,
                    "version_number": version_num_by_id.get(vid) if vid else None,
                    "actor_user_id": uid,
                    "actor_name": actor_names.get(uid) if uid else None,
                    "result": result_txt,
                    "detail": detail_txt,
                },
            )

        timeline.sort(key=lambda item: (item[0], item[1], item[2]))

        versions_summary = [
            {
                "project_version_id": v["id"],
                "version_number": int(v["version_number"]),
                "title": v["title"],
                "estimated_budget_pen": v["estimated_budget_pen"],
                "beneficiaries_count": (
                    int(v["beneficiaries_count"])
                    if v.get("beneficiaries_count") is not None
                    else None
                ),
                "created_by_user_id": v["created_by_user_id"],
                "created_by_name": actor_names.get(v["created_by_user_id"]),
                "created_at": v["created_at"],
            }
            for v in version_rows
        ]

        return {
            "project_id": proj["id"],
            "code": proj["code"],
            "current_title": latest_v["title"],
            "current_status": proj["status"],
            "current_version_id": latest_v["id"],
            "current_version_number": int(latest_v["version_number"]),
            "versions": versions_summary,
            "events": [item[3] for item in timeline],
            "historical_limitations": [
                "Las evaluaciones jurídicas, técnicas y estimaciones PERT conservan un registro único por versión si no se emitieron eventos previos adicionales.",
                "El registro de evaluación jurídica no almacena columna directa de autor; se muestra el actor únicamente cuando consta en eventos de auditoría o si se crea una nueva versión.",
                "Los dictámenes finales de aprobación no guardan huella SHA-256 en el esquema vigente; su verificación criptográfica de integridad no está disponible.",
                "La verificación de integridad SHA-256 sobre decisiones de puntos de control detecta alteraciones internas del contenido, pero no equivale a firma digital certificada con validez jurídica.",
            ],
            "read_only": True,
            "generated_at": datetime.now(timezone.utc),
        }

    def verify_decision_integrity(
        self,
        *,
        project_id: UUID,
        project_version_id: UUID,
        decision_id: UUID,
    ) -> dict[str, Any]:
        """Comprueba en vivo (solo lectura) la integridad SHA-256 de una decisión perteneciente al expediente y versión."""
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        checked_at = datetime.now(timezone.utc)
        prototype_notice = (
            "Constancia de integridad SHA-256 del prototipo académico SIPRIM: detecta alteraciones del contenido "
            "respecto a la huella almacenada, pero no constituye firma digital certificada con validez jurídica."
        )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            cp_dec = conn.execute(
                """
                SELECT d.*,
                       c.status AS cp_status,
                       p.code AS project_code,
                       v.version_number,
                       v.title AS project_title
                FROM checkpoint_review_decisions d
                JOIN evaluation_checkpoints c ON c.id = d.checkpoint_id
                JOIN projects p ON p.id = d.project_id
                JOIN project_versions v ON v.id = d.project_version_id AND v.project_id = p.id
                WHERE d.id = %s AND d.project_id = %s AND d.project_version_id = %s
                """,
                (decision_id, project_id, project_version_id),
            ).fetchone()

            if cp_dec is not None:
                expected_canonical, recomputed_hash = _compute_checkpoint_decision_sha256(
                    decision_id=cp_dec["id"],
                    checkpoint_id=cp_dec["checkpoint_id"],
                    project_id=cp_dec["project_id"],
                    project_version_id=cp_dec["project_version_id"],
                    decision=cp_dec["decision"],
                    justification=cp_dec["justification"],
                    conditions=cp_dec.get("conditions"),
                    decided_by_user_id=cp_dec["decided_by_user_id"],
                    decided_by_name=cp_dec.get("decided_by_name"),
                    decided_by_email=cp_dec.get("decided_by_email"),
                    decided_at=cp_dec["decided_at"],
                )
                stored_hash = str(cp_dec["integrity_hash_sha256"] or "").strip()
                stored_canonical = cp_dec.get("canonical_payload")
                canonical_matches = isinstance(stored_canonical, dict) and stored_canonical == expected_canonical
                is_valid = bool(stored_hash) and hmac.compare_digest(recomputed_hash, stored_hash) and canonical_matches

                uid = cp_dec["decided_by_user_id"]
                actor_names = self._resolve_actor_names([uid])
                in_dir = uid in actor_names
                decision_labels = {
                    "approve_continue": "Aprobación para continuar evaluación",
                    "request_correction": "Corrección solicitada (pausa vigente)",
                }
                return {
                    "project_id": cp_dec["project_id"],
                    "project_code": cp_dec["project_code"],
                    "project_title": cp_dec["project_title"],
                    "project_version_id": cp_dec["project_version_id"],
                    "version_number": int(cp_dec["version_number"]),
                    "decision_id": cp_dec["id"],
                    "decision_kind": "checkpoint_decision",
                    "checkpoint_id": cp_dec["checkpoint_id"],
                    "checkpoint_status": str(cp_dec["cp_status"]),
                    "decision": str(cp_dec["decision"]),
                    "decision_label": decision_labels.get(cp_dec["decision"], str(cp_dec["decision"])),
                    "justification": str(cp_dec["justification"]),
                    "conditions": cp_dec.get("conditions"),
                    "decided_by_user_id": uid,
                    "decided_by_name": actor_names.get(uid) if in_dir else None,
                    "actor_in_directory": in_dir,
                    "decided_at": cp_dec["decided_at"],
                    "has_integrity_hash": True,
                    "integrity_status": "intact" if is_valid else "tampered",
                    "integrity_label": "Íntegra" if is_valid else "Alteración detectada",
                    "stored_hash_sha256": stored_hash,
                    "recomputed_hash_sha256": recomputed_hash,
                    "canonical_payload_matches": canonical_matches,
                    "checked_at": checked_at,
                    "prototype_notice": prototype_notice,
                    "read_only": True,
                }

            ap = conn.execute(
                """
                SELECT a.*,
                       p.code AS project_code,
                       v.version_number,
                       v.title AS project_title
                FROM human_approvals a
                JOIN projects p ON p.id = a.project_id
                JOIN project_versions v ON v.id = a.project_version_id AND v.project_id = p.id
                WHERE a.id = %s AND a.project_id = %s AND a.project_version_id = %s
                """,
                (decision_id, project_id, project_version_id),
            ).fetchone()

            if ap is not None:
                uid = ap["decided_by_user_id"]
                actor_names = self._resolve_actor_names([uid])
                in_dir = uid in actor_names
                decision_es = {"approved": "Aprobado", "rejected": "Rechazado", "observed": "Observado"}.get(
                    ap["decision"], str(ap["decision"])
                )
                return {
                    "project_id": ap["project_id"],
                    "project_code": ap["project_code"],
                    "project_title": ap["project_title"],
                    "project_version_id": ap["project_version_id"],
                    "version_number": int(ap["version_number"]),
                    "decision_id": ap["id"],
                    "decision_kind": "final_approval",
                    "checkpoint_id": None,
                    "checkpoint_status": None,
                    "decision": str(ap["decision"]),
                    "decision_label": f"Dictamen final registrado ({decision_es})",
                    "justification": str(ap["justification"]),
                    "conditions": ap.get("conditions"),
                    "decided_by_user_id": uid,
                    "decided_by_name": actor_names.get(uid) if in_dir else None,
                    "actor_in_directory": in_dir,
                    "decided_at": ap["decided_at"],
                    "has_integrity_hash": False,
                    "integrity_status": "unavailable",
                    "integrity_label": "Verificación de integridad no disponible para este registro",
                    "stored_hash_sha256": None,
                    "recomputed_hash_sha256": None,
                    "canonical_payload_matches": None,
                    "checked_at": checked_at,
                    "prototype_notice": (
                        "Este dictamen final no dispone de constancia criptográfica SHA-256 en el registro actual; "
                        "no se marca como íntegro ni equivale a una firma digital certificada con validez jurídica."
                    ),
                    "read_only": True,
                }

        raise RecursoPMV1NoEncontradoError(
            "No se encontró la decisión solicitada para el expediente y versión indicados."
        )

    def get_project_by_code(self, code: str) -> dict[str, Any]:
        normalized = code.strip()
        if len(normalized) < 3:
            raise RecursoPMV1NoEncontradoError("Código de expediente no válido")
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT id FROM projects WHERE upper(code) = upper(%s)",
                (normalized,),
            ).fetchone()
        if not row:
            raise RecursoPMV1NoEncontradoError(
                f"No existe expediente con código {normalized}"
            )
        return self.get_project(row["id"])

    def validate_project(
        self, project_id: UUID, version_id: UUID | None = None
    ) -> dict[str, Any]:
        if version_id is None:
            project = self.get_project(project_id)
            target_version_id = project["project_version_id"]
            target_version_number = project["version_number"]
            values = project
        else:
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                pv_row = conn.execute(
                    """
                    SELECT id AS project_version_id, project_id, version_number,
                           title, description, location_description AS location,
                           proposed_land_use, estimated_budget_pen, beneficiaries_count
                    FROM project_versions
                    WHERE id = %s
                    """,
                    (version_id,),
                ).fetchone()
            if not pv_row:
                raise RecursoPMV1NoEncontradoError("Versión del expediente no encontrada")
            if pv_row["project_id"] != project_id:
                raise VersionNoPertenecienteError(
                    "La versión indicada no pertenece al expediente consultado.",
                    project_id=str(project_id),
                    version_id=str(version_id),
                )
            target_version_id = pv_row["project_version_id"]
            target_version_number = pv_row["version_number"]
            values = dict(pv_row)

        missing = self._missing_from_values(values)
        missing_labels = ExpedientePMV1IncompletoError.format_labels(missing)
        complete = not missing
        return {
            "complete": complete,
            "status": "ready" if complete else "incomplete",
            "missing_fields": missing,
            "missing_field_labels": missing_labels,
            "project_version_id": target_version_id,
            "version_number": target_version_number,
            "message": (
                "Expediente completo"
                if complete
                else f"Complete los datos obligatorios antes de evaluar: {', '.join(missing_labels)}."
            ),
        }

    def _execute_specialist_dimension(
        self,
        *,
        dimension_code: str,
        evaluation_id: UUID,
        project_id: UUID,
        project_code: str,
        project_version_id: UUID,
        version_number: int,
        actor_id: UUID,
    ) -> dict[str, Any]:
        """Ejecuta de forma aislada y trazable un especialista (social, ambiental, técnico o jurídico) para project_version_id."""
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            if dimension_code == "social":
                rows = conn.execute(
                    """
                    SELECT id, project_id, project_code, category, indicator_name,
                           metric_value, metric_unit, has_sufficient_data, source_name,
                           data_origin, description,
                           (data_origin = 'simulated') AS is_simulated
                    FROM territorial_social_indicators
                    WHERE project_id = %s OR project_code = %s
                    ORDER BY created_at ASC
                    """,
                    (project_id, project_code),
                ).fetchall()
                equity_rows = conn.execute(
                    """
                    SELECT id, project_id, project_code, dimension, has_sufficient_data,
                           target_group, aggregated_population, aggregated_percentage,
                           equity_criterion, declared_benefit, verifiable_data,
                           has_favorable_evidence, score_effect_pts, effect_type,
                           effect_explanation, source_name, data_origin, description,
                           (data_origin = 'simulated') AS is_simulated
                    FROM project_demographic_equity_records
                    WHERE project_id = %s OR project_code = %s
                    ORDER BY dimension ASC
                    """,
                    (project_id, project_code),
                ).fetchall()
                assessment = SocialAccessibilityEngine.evaluate(
                    project_id=project_id,
                    project_code=project_code,
                    project_version_id=project_version_id,
                    indicator_rows=[dict(r) for r in rows],
                    equity_rows=[dict(r) for r in equity_rows],
                )
                idem = f"coord-{evaluation_id}-social-{project_version_id}"
                existing_soc = conn.execute(
                    """
                    SELECT id FROM social_accessibility_assessments
                    WHERE project_version_id = %s
                    ORDER BY evaluated_at DESC LIMIT 1
                    """,
                    (project_version_id,),
                ).fetchone()
                if existing_soc:
                    assessment_id = existing_soc["id"]
                    conn.execute(
                        """
                        UPDATE social_accessibility_assessments
                        SET status = %s, summary = %s, results_json = %s,
                            equity_json = %s, social_score = %s,
                            score_explanation = %s, evaluated_at = %s
                        WHERE id = %s
                        """,
                        (
                            assessment["status"],
                            assessment["summary"],
                            Jsonb({"categories": assessment["categories"], "equity": assessment.get("equity")}),
                            Jsonb(assessment["equity"]) if assessment.get("equity") else None,
                            assessment.get("social_score"),
                            assessment.get("score_explanation"),
                            assessment["evaluated_at"],
                            assessment_id,
                        ),
                    )
                else:
                    assessment_id = uuid4()
                    conn.execute(
                        """
                        INSERT INTO social_accessibility_assessments
                        (id, project_id, project_version_id, requested_by_user_id, idempotency_key,
                         status, summary, results_json, equity_json, social_score, score_explanation, evaluated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            assessment_id,
                            project_id,
                            project_version_id,
                            actor_id,
                            idem,
                            assessment["status"],
                            assessment["summary"],
                            Jsonb({"categories": assessment["categories"], "equity": assessment.get("equity")}),
                            Jsonb(assessment["equity"]) if assessment.get("equity") else None,
                            assessment.get("social_score"),
                            assessment.get("score_explanation"),
                            assessment["evaluated_at"],
                        ),
                    )
                raw_st = str(assessment.get("status") or "").lower()
                if raw_st in ("complete", "completed", "verified", "evaluated") and assessment.get("social_score") is not None:
                    d_status, d_label = "completed", "Completado"
                else:
                    d_status, d_label = "insufficient_data", "Información insuficiente"
                return {"status": d_status, "status_label": d_label, "result_reference_id": assessment_id, "summary": assessment["summary"]}

            if dimension_code == "environmental":
                rows = conn.execute(
                    """
                    SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                           environmental_component, declared_impacts, declared_measures,
                           verifiable_evidence, proximity_to_water_meters,
                           is_in_protected_or_risk_area, source_name, data_origin, description
                    FROM project_environmental_records
                    WHERE project_version_id = %s
                    ORDER BY created_at ASC
                    """,
                    (project_version_id,),
                ).fetchall()
                if not rows and version_number == 1:
                    rows = conn.execute(
                        """
                        SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                               environmental_component, declared_impacts, declared_measures,
                               verifiable_evidence, proximity_to_water_meters,
                               is_in_protected_or_risk_area, source_name, data_origin, description
                        FROM project_environmental_records
                        WHERE (project_id = %s OR project_code = %s) AND project_version_id IS NULL
                        ORDER BY created_at ASC
                        """,
                        (project_id, project_code),
                    ).fetchall()
                green_rows = conn.execute(
                    """
                    SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                           total_project_area_m2, green_area_m2, green_area_percentage,
                           delimitation_description, declared_affectation, verifiable_affectation,
                           affectation_type, protection_measures, has_sufficient_measures,
                           score_effect_pts, effect_type, calculation_explanation,
                           source_name, data_origin, description,
                           (data_origin = 'simulated') AS is_simulated
                    FROM project_green_area_records
                    WHERE project_version_id = %s
                    ORDER BY created_at ASC
                    """,
                    (project_version_id,),
                ).fetchall()
                if not green_rows and version_number == 1:
                    green_rows = conn.execute(
                        """
                        SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                               total_project_area_m2, green_area_m2, green_area_percentage,
                               delimitation_description, declared_affectation, verifiable_affectation,
                               affectation_type, protection_measures, has_sufficient_measures,
                               score_effect_pts, effect_type, calculation_explanation,
                               source_name, data_origin, description,
                               (data_origin = 'simulated') AS is_simulated
                        FROM project_green_area_records
                        WHERE (project_id = %s OR project_code = %s) AND project_version_id IS NULL
                        ORDER BY created_at ASC
                        """,
                        (project_id, project_code),
                    ).fetchall()
                assessment = EnvironmentalAssessmentEngine.evaluate(
                    project_id=project_id,
                    project_code=project_code,
                    project_version_id=project_version_id,
                    records=[dict(r) for r in rows],
                    green_records=[dict(g) for g in green_rows],
                )
                idem = f"coord-{evaluation_id}-env-{project_version_id}"
                existing_env = conn.execute(
                    """
                    SELECT id FROM environmental_assessments
                    WHERE project_version_id = %s
                    ORDER BY evaluated_at DESC LIMIT 1
                    """,
                    (project_version_id,),
                ).fetchone()
                if existing_env:
                    assessment_id = existing_env["id"]
                    conn.execute(
                        """
                        UPDATE environmental_assessments
                        SET status = %s, risk_level = %s, risk_observation = %s,
                            requires_human_review = %s, favorable = %s, summary = %s,
                            declared_vs_evidence_json = %s, impacts_json = %s,
                            mitigations_json = %s, green_area_json = %s,
                            environmental_score = %s, score_explanation = %s, evaluated_at = %s
                        WHERE id = %s
                        """,
                        (
                            assessment["status"],
                            assessment["risk_level"],
                            assessment["risk_observation"],
                            assessment["requires_human_review"],
                            assessment["favorable"],
                            assessment["summary"],
                            Jsonb(assessment["declared_vs_evidence"]),
                            Jsonb(assessment["impacts"]),
                            Jsonb(assessment["mitigations"]),
                            Jsonb(assessment["green_area"]) if assessment.get("green_area") else None,
                            assessment.get("environmental_score"),
                            assessment.get("score_explanation"),
                            assessment["evaluated_at"],
                            assessment_id,
                        ),
                    )
                else:
                    assessment_id = uuid4()
                    conn.execute(
                        """
                        INSERT INTO environmental_assessments
                        (id, project_id, project_version_id, assessed_by_user_id, idempotency_key,
                         status, risk_level, risk_observation, requires_human_review, favorable,
                         summary, declared_vs_evidence_json, impacts_json, mitigations_json,
                         green_area_json, environmental_score, score_explanation, evaluated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            assessment_id,
                            project_id,
                            project_version_id,
                            actor_id,
                            idem,
                            assessment["status"],
                            assessment["risk_level"],
                            assessment["risk_observation"],
                            assessment["requires_human_review"],
                            assessment["favorable"],
                            assessment["summary"],
                            Jsonb(assessment["declared_vs_evidence"]),
                            Jsonb(assessment["impacts"]),
                            Jsonb(assessment["mitigations"]),
                            Jsonb(assessment["green_area"]) if assessment.get("green_area") else None,
                            assessment.get("environmental_score"),
                            assessment.get("score_explanation"),
                            assessment["evaluated_at"],
                        ),
                    )
                risk = str(assessment.get("risk_level") or "").lower()
                if risk == "high" and assessment.get("requires_human_review"):
                    risk_info = RiskCheckpointEngine.detect_high_risk_condition(conn, project_version_id)
                    if risk_info:
                        RiskCheckpointEngine.create_or_get_checkpoint(
                            conn,
                            project_id=project_id,
                            project_version_id=project_version_id,
                            evaluation_id=evaluation_id,
                            risk_info=risk_info,
                        )
                    return {
                        "status": "pending_review",
                        "status_label": "Pendiente de revisión (HITL)",
                        "result_reference_id": assessment_id,
                        "summary": assessment.get("risk_observation") or assessment["summary"],
                    }
                if risk == "insufficient" or assessment.get("status") == "insufficient" or assessment.get("environmental_score") is None:
                    return {
                        "status": "insufficient_data",
                        "status_label": "Información insuficiente",
                        "result_reference_id": assessment_id,
                        "summary": assessment.get("risk_observation") or assessment["summary"],
                    }
                return {
                    "status": "completed",
                    "status_label": "Completado",
                    "result_reference_id": assessment_id,
                    "summary": assessment["summary"],
                }

            if dimension_code == "technical":
                param_rows = conn.execute(
                    """
                    SELECT id, project_id, project_version_id, criterion_code, criterion_name,
                           normative_reference, normative_version, has_sufficient_data,
                           parameter_name, parameter_value_numeric, parameter_unit, parameter_value_text,
                           applied_rule, supporting_document, source_origin, is_simulated
                    FROM project_technical_parameters
                    WHERE project_version_id = %s
                    ORDER BY criterion_code ASC
                    """,
                    (project_version_id,),
                ).fetchall()
                assessment = TechnicalFeasibilityEngine.evaluate(
                    project_id=project_id,
                    project_code=project_code,
                    project_version_id=project_version_id,
                    version_number=version_number,
                    parameter_rows=[dict(r) for r in param_rows],
                )
                assessment_id = uuid4()
                idem = f"coord-{evaluation_id}-tech-{project_version_id}"
                saved_row = conn.execute(
                    """
                    INSERT INTO technical_feasibility_assessments
                    (id, project_id, project_version_id, requested_by_user_id, idempotency_key,
                     status, overall_result, overall_result_label, is_viable, is_preliminary_opinion,
                     disclaimer, summary, criteria_summary, findings_json, technical_score,
                     score_explanation, dimension_conclusion, dimension_justification, evaluated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_version_id) DO UPDATE SET
                        requested_by_user_id = EXCLUDED.requested_by_user_id,
                        status = EXCLUDED.status,
                        overall_result = EXCLUDED.overall_result,
                        overall_result_label = EXCLUDED.overall_result_label,
                        is_viable = EXCLUDED.is_viable,
                        is_preliminary_opinion = EXCLUDED.is_preliminary_opinion,
                        disclaimer = EXCLUDED.disclaimer,
                        summary = EXCLUDED.summary,
                        criteria_summary = EXCLUDED.criteria_summary,
                        findings_json = EXCLUDED.findings_json,
                        technical_score = EXCLUDED.technical_score,
                        score_explanation = EXCLUDED.score_explanation,
                        dimension_conclusion = EXCLUDED.dimension_conclusion,
                        dimension_justification = EXCLUDED.dimension_justification,
                        evaluated_at = EXCLUDED.evaluated_at
                    RETURNING id
                    """,
                    (
                        assessment_id,
                        project_id,
                        project_version_id,
                        actor_id,
                        idem,
                        assessment["status"],
                        assessment["overall_result"],
                        assessment["overall_result_label"],
                        assessment["is_viable"],
                        assessment["is_preliminary_opinion"],
                        assessment["disclaimer"],
                        assessment["summary"],
                        Jsonb(assessment["criteria_summary"]),
                        Jsonb(assessment["criteria"]),
                        Decimal(str(assessment["technical_score"])) if assessment["technical_score"] is not None else None,
                        assessment["score_explanation"],
                        assessment["dimension_conclusion"],
                        assessment["dimension_justification"],
                        assessment["evaluated_at"],
                    ),
                ).fetchone()
                actual_id = saved_row["id"] if saved_row else assessment_id
                ov = str(assessment.get("overall_result") or "").lower()
                if ov == "informacion_insuficiente" or assessment.get("technical_score") is None:
                    return {
                        "status": "insufficient_data",
                        "status_label": "Información insuficiente",
                        "result_reference_id": actual_id,
                        "summary": assessment["dimension_conclusion"],
                    }
                if ov in ("observado", "pendiente_revision"):
                    return {
                        "status": "requires_review",
                        "status_label": assessment.get("overall_result_label") or "Requiere revisión",
                        "result_reference_id": actual_id,
                        "summary": assessment["dimension_conclusion"],
                    }
                return {
                    "status": "completed",
                    "status_label": "Completado",
                    "result_reference_id": actual_id,
                    "summary": assessment["dimension_conclusion"],
                }

            if dimension_code == "legal":
                assessment, resolution_row = self._compute_legal_assessment_with_conn(
                    conn, project_id, {"code": project_code}, project_version_id
                )
                assessment_id = UUID(assessment["assessment_id"])
                saved_row = conn.execute(
                    """
                    INSERT INTO legal_assessments
                    (id, project_id, project_code, project_version_id, status, summary, findings_json,
                     total_findings, verifiable_citations_count, unverifiable_citations_count,
                     requires_human_review, has_legal_alert, legal_alert_motive, legal_alert_norm,
                     is_resolved, resolved_by_user_id, resolved_at, resolution_justification, evaluated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, project_version_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        summary = EXCLUDED.summary,
                        findings_json = EXCLUDED.findings_json,
                        total_findings = EXCLUDED.total_findings,
                        verifiable_citations_count = EXCLUDED.verifiable_citations_count,
                        unverifiable_citations_count = EXCLUDED.unverifiable_citations_count,
                        requires_human_review = EXCLUDED.requires_human_review,
                        has_legal_alert = EXCLUDED.has_legal_alert,
                        legal_alert_motive = EXCLUDED.legal_alert_motive,
                        legal_alert_norm = EXCLUDED.legal_alert_norm,
                        is_resolved = EXCLUDED.is_resolved,
                        resolved_by_user_id = EXCLUDED.resolved_by_user_id,
                        resolved_at = EXCLUDED.resolved_at,
                        resolution_justification = EXCLUDED.resolution_justification,
                        evaluated_at = EXCLUDED.evaluated_at
                    RETURNING id
                    """,
                    (
                        assessment_id,
                        project_id,
                        project_code,
                        project_version_id,
                        assessment["status"],
                        assessment["summary"],
                        Jsonb(assessment["findings"]),
                        assessment["total_findings"],
                        assessment["verifiable_citations_count"],
                        assessment["unverifiable_citations_count"],
                        assessment["requires_human_review"],
                        assessment["has_legal_alert"],
                        assessment.get("legal_alert_motive"),
                        assessment.get("legal_alert_norm"),
                        assessment.get("is_resolved", False),
                        resolution_row["resolved_by_user_id"] if resolution_row else None,
                        resolution_row["resolved_at"] if resolution_row else None,
                        assessment.get("resolution_justification"),
                        assessment["evaluated_at"],
                    ),
                ).fetchone()
                actual_id = saved_row["id"] if saved_row else assessment_id
                st = str(assessment.get("status") or "").lower()
                if st == "insufficient":
                    return {
                        "status": "insufficient_data",
                        "status_label": "Información insuficiente",
                        "result_reference_id": actual_id,
                        "summary": assessment["summary"],
                    }
                if st == "verified":
                    return {
                        "status": "completed",
                        "status_label": "Completado",
                        "result_reference_id": actual_id,
                        "summary": assessment["summary"],
                    }
                return {
                    "status": "requires_review",
                    "status_label": assessment.get("status_label") or "Requiere revisión",
                    "result_reference_id": actual_id,
                    "summary": assessment["summary"],
                }

        raise ConfiguracionPMV1Error(f"Especialista desconocido: {dimension_code}")

    def _reconcile_evaluation_state_with_conn(
        self,
        conn: psycopg.Connection[Any],
        *,
        project_id: UUID,
        project_version_id: UUID,
        evaluation_id: UUID,
    ) -> dict[str, Any]:
        dispatches = self._get_dimension_dispatches_with_conn(conn, project_version_id)
        has_failed = any(d["status"] == "failed" for d in dispatches)
        has_checkpoint = RiskCheckpointEngine.has_pending_checkpoint(conn, project_version_id)
        econ_row = conn.execute(
            "SELECT status FROM evaluations WHERE id = %s",
            (evaluation_id,),
        ).fetchone()
        econ_status = econ_row["status"] if econ_row else "queued"

        if has_checkpoint or econ_status == "pending_review":
            conn.execute(
                "UPDATE projects SET status = 'pending_review', updated_at = now() WHERE id = %s",
                (project_id,),
            )
        elif has_failed:
            conn.execute(
                "UPDATE projects SET status = 'evaluating', updated_at = now() WHERE id = %s",
                (project_id,),
            )
        elif econ_status == "completed" and not has_failed:
            conn.execute(
                "UPDATE projects SET status = 'evaluated', updated_at = now() WHERE id = %s",
                (project_id,),
            )
        return {
            "dimension_dispatches": dispatches,
            "has_failed_dimensions": has_failed,
        }

    def _dispatch_multidimensional_specialists(
        self,
        *,
        evaluation_id: UUID,
        project_id: UUID,
        project_code: str,
        project_version_id: UUID,
        version_number: int,
        actor_id: UUID,
        economic_payload: dict[str, Any] | None,
        economic_event_id: UUID | None,
        dimensions_to_run: set[str] | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        target_dims = dimensions_to_run if dimensions_to_run is not None else set(self._DIMENSION_ORDER)

        # 1. Ejecutar los 4 especialistas no económicos sobre la misma project_version_id
        for dim_code in ("social", "environmental", "technical", "legal"):
            if dim_code not in target_dims:
                continue
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                self._upsert_dimension_dispatch_with_conn(
                    conn,
                    evaluation_id=evaluation_id,
                    project_id=project_id,
                    project_version_id=project_version_id,
                    dimension_code=dim_code,
                    status="processing",
                    status_label="En proceso",
                    error_message=None,
                    increment_attempt=increment_attempt,
                )
            try:
                outcome = self._execute_specialist_dimension(
                    dimension_code=dim_code,
                    evaluation_id=evaluation_id,
                    project_id=project_id,
                    project_code=project_code,
                    project_version_id=project_version_id,
                    version_number=version_number,
                    actor_id=actor_id,
                )
                with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                    self._upsert_dimension_dispatch_with_conn(
                        conn,
                        evaluation_id=evaluation_id,
                        project_id=project_id,
                        project_version_id=project_version_id,
                        dimension_code=dim_code,
                        status=outcome["status"],
                        status_label=outcome["status_label"],
                        result_reference_id=outcome.get("result_reference_id"),
                        summary=outcome.get("summary"),
                        error_message=None,
                        increment_attempt=False,
                    )
            except Exception as exc:
                with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                    self._upsert_dimension_dispatch_with_conn(
                        conn,
                        evaluation_id=evaluation_id,
                        project_id=project_id,
                        project_version_id=project_version_id,
                        dimension_code=dim_code,
                        status="failed",
                        status_label="Fallido",
                        error_message=f"Falla en especialista {self._DIMENSION_NAMES[dim_code]}: {exc}"[:400],
                        increment_attempt=False,
                    )

        # 2. Despachar especialista económico sobre la misma project_version_id
        publication_pending = False
        if "economic" in target_dims and economic_payload is not None:
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                self._upsert_dimension_dispatch_with_conn(
                    conn,
                    evaluation_id=evaluation_id,
                    project_id=project_id,
                    project_version_id=project_version_id,
                    dimension_code="economic",
                    status="processing",
                    status_label="En proceso",
                    error_message=None,
                    increment_attempt=increment_attempt,
                )
            if self.dispatcher is None:
                publication_pending = True
                with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                    if economic_event_id is not None:
                        conn.execute(
                            "UPDATE outbox_events SET publish_attempts=publish_attempts+1 WHERE id=%s",
                            (economic_event_id,),
                        )
                    self._upsert_dimension_dispatch_with_conn(
                        conn,
                        evaluation_id=evaluation_id,
                        project_id=project_id,
                        project_version_id=project_version_id,
                        dimension_code="economic",
                        status="pending",
                        status_label="Pendiente (en cola)",
                        summary="Despacho diferido en outbox hasta disponibilidad del broker.",
                        error_message=None,
                        increment_attempt=False,
                    )
            else:
                try:
                    self.dispatcher.dispatch_economic(economic_payload)
                    with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                        if economic_event_id is not None:
                            conn.execute(
                                "UPDATE outbox_events SET published_at=now(), publish_attempts=publish_attempts+1 WHERE id=%s",
                                (economic_event_id,),
                            )
                        ev_now = conn.execute(
                            """
                            SELECT e.status, r.economic_assessment_id, r.cost_per_beneficiary_pen, r.score_0_100
                            FROM evaluations e
                            LEFT JOIN economic_result_projections r ON r.evaluation_id = e.id
                            WHERE e.id = %s
                            """,
                            (evaluation_id,),
                        ).fetchone()
                        if ev_now and ev_now["status"] == "completed" and ev_now.get("economic_assessment_id"):
                            self._upsert_dimension_dispatch_with_conn(
                                conn,
                                evaluation_id=evaluation_id,
                                project_id=project_id,
                                project_version_id=project_version_id,
                                dimension_code="economic",
                                status="completed",
                                status_label="Completado",
                                result_reference_id=ev_now["economic_assessment_id"],
                                summary=(
                                    f"Costo por beneficiario S/ {ev_now['cost_per_beneficiary_pen']} "
                                    f"({ev_now['score_0_100']}/100)."
                                ),
                                error_message=None,
                                increment_attempt=False,
                            )
                        elif ev_now and ev_now["status"] == "pending_review":
                            self._upsert_dimension_dispatch_with_conn(
                                conn,
                                evaluation_id=evaluation_id,
                                project_id=project_id,
                                project_version_id=project_version_id,
                                dimension_code="economic",
                                status="pending_review",
                                status_label="Pendiente de revisión (HITL)",
                                summary="Evaluación económica detenida en punto de control de riesgo alto.",
                                error_message=None,
                                increment_attempt=False,
                            )
                        else:
                            self._upsert_dimension_dispatch_with_conn(
                                conn,
                                evaluation_id=evaluation_id,
                                project_id=project_id,
                                project_version_id=project_version_id,
                                dimension_code="economic",
                                status="processing",
                                status_label="En proceso",
                                summary="Tarea económica encolada para la versión vigente.",
                                error_message=None,
                                increment_attempt=False,
                            )
                except Exception as exc:
                    publication_pending = True
                    with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                        if economic_event_id is not None:
                            conn.execute(
                                "UPDATE outbox_events SET publish_attempts=publish_attempts+1 WHERE id=%s",
                                (economic_event_id,),
                            )
                        self._upsert_dimension_dispatch_with_conn(
                            conn,
                            evaluation_id=evaluation_id,
                            project_id=project_id,
                            project_version_id=project_version_id,
                            dimension_code="economic",
                            status="failed",
                            status_label="Fallido",
                            error_message=f"Falla en especialista Económica: {exc}"[:400],
                            increment_attempt=False,
                        )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            rec = self._reconcile_evaluation_state_with_conn(
                conn,
                project_id=project_id,
                project_version_id=project_version_id,
                evaluation_id=evaluation_id,
            )
            return {
                "publication_pending": publication_pending,
                "dimension_dispatches": rec["dimension_dispatches"],
                "has_failed_dimensions": rec["has_failed_dimensions"],
            }

    def request_evaluation(
        self, project_version_id: UUID, actor_id: UUID, idempotency_key: str
    ) -> dict[str, Any]:
        if not idempotency_key.strip():
            raise ConfiguracionPMV1Error("Idempotency-Key es obligatorio")
        retry_payload: dict[str, Any] | None = None
        retry_dims: set[str] = set()
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                previous = conn.execute(
                    """SELECT e.id AS evaluation_id, e.status, e.project_version_id,
                              o.id AS event_id, o.payload,
                              (o.id IS NOT NULL AND o.published_at IS NULL) AS publication_pending
                       FROM evaluations e
                       LEFT JOIN outbox_events o ON o.aggregate_id=e.id
                       WHERE e.idempotency_key=%s""",
                    (idempotency_key,),
                ).fetchone()
                if previous:
                    previous = dict(previous)
                    pv_id_prev = previous["project_version_id"]
                    previous["duplicated"] = True
                    dispatches = self._get_dimension_dispatches_with_conn(conn, pv_id_prev)
                    previous["dimension_dispatches"] = dispatches
                    previous["has_failed_dimensions"] = any(d["status"] == "failed" for d in dispatches)
                    if previous["status"] == "pending_review":
                        previous["event_id"] = None
                        previous["publication_pending"] = False
                        previous["status_label"] = "Pendiente de revisión"
                        previous["checkpoint"] = RiskCheckpointEngine.get_active_checkpoint(
                            conn, pv_id_prev
                        )
                    else:
                        previous["status_label"] = None
                        previous["checkpoint"] = None
                    previous.pop("payload", None)
                    return previous

                snapshot = conn.execute(
                    """
                    SELECT pv.id AS project_version_id, pv.project_id, p.code AS project_code,
                           pv.version_number, pv.title, pv.description, pv.location_description AS location,
                           pv.proposed_land_use, pv.estimated_budget_pen,
                           pv.beneficiaries_count, cv.id AS criteria_version_id,
                           cv.version_number AS criteria_version_number,
                           cv.excellent_cost_pen, cv.unacceptable_cost_pen
                    FROM project_versions pv
                    JOIN projects p ON p.id = pv.project_id
                    CROSS JOIN criteria_versions cv
                    WHERE pv.id=%s AND cv.status='active'
                    """,
                    (project_version_id,),
                ).fetchone()
                if not snapshot:
                    raise RecursoPMV1NoEncontradoError(
                        "Version de proyecto o criterios activos no encontrados"
                    )

                missing = self._missing_from_values(dict(snapshot))
                if missing:
                    raise ExpedientePMV1IncompletoError(missing)

                # Evitar evaluaciones duplicadas sobre la misma versión (aun con nueva Idempotency-Key):
                existing_for_ver = conn.execute(
                    """
                    SELECT e.id AS evaluation_id, e.status, e.project_version_id,
                           o.id AS event_id, o.payload,
                           (o.id IS NOT NULL AND o.published_at IS NULL) AS publication_pending
                    FROM evaluations e
                    LEFT JOIN outbox_events o ON o.aggregate_id = e.id
                    WHERE e.project_version_id = %s
                    ORDER BY e.requested_at DESC
                    LIMIT 1
                    """,
                    (project_version_id,),
                ).fetchone()
                if existing_for_ver:
                    dispatches = self._get_dimension_dispatches_with_conn(conn, project_version_id)
                    if dispatches or existing_for_ver["event_id"] is not None:
                        failed_dims = {d["dimension_code"] for d in dispatches if d["status"] == "failed"}
                        if not failed_dims:
                            existing_dict = dict(existing_for_ver)
                            existing_dict["duplicated"] = True
                            existing_dict["dimension_dispatches"] = dispatches
                            existing_dict["has_failed_dimensions"] = False
                            if existing_dict["status"] == "pending_review":
                                existing_dict["event_id"] = None
                                existing_dict["publication_pending"] = False
                                existing_dict["status_label"] = "Pendiente de revisión"
                                existing_dict["checkpoint"] = RiskCheckpointEngine.get_active_checkpoint(
                                    conn, project_version_id
                                )
                            else:
                                existing_dict["status_label"] = None
                                existing_dict["checkpoint"] = None
                            existing_dict.pop("payload", None)
                            return existing_dict
                        # Recuperación segura de únicamente las dimensiones fallidas sin duplicar las exitosas
                        retry_dims = failed_dims
                        retry_payload = {
                            "evaluation_id": existing_for_ver["evaluation_id"],
                            "event_id": existing_for_ver["event_id"],
                            "payload": existing_for_ver["payload"],
                            "status": existing_for_ver["status"],
                            "snapshot": dict(snapshot),
                        }

                if retry_payload is None:
                    # Comprobar si existe condición de riesgo alto configurada para revisión humana (CA-HU3.1-01)
                    risk_info = RiskCheckpointEngine.detect_high_risk_condition(conn, project_version_id)
                    if risk_info:
                        evaluation_id = uuid4()
                        conn.execute(
                            """INSERT INTO evaluations
                            (id, project_version_id, criteria_version_id, requested_by_user_id,
                             status, idempotency_key, error_message) VALUES (%s,%s,%s,%s,'pending_review',%s,%s)""",
                            (
                                evaluation_id,
                                project_version_id,
                                snapshot["criteria_version_id"],
                                actor_id,
                                idempotency_key,
                                risk_info["reason"],
                            ),
                        )
                        checkpoint = RiskCheckpointEngine.create_or_get_checkpoint(
                            conn,
                            project_id=snapshot["project_id"],
                            project_version_id=project_version_id,
                            evaluation_id=evaluation_id,
                            risk_info=risk_info,
                        )
                        for dim_code in self._DIMENSION_ORDER:
                            self._upsert_dimension_dispatch_with_conn(
                                conn,
                                evaluation_id=evaluation_id,
                                project_id=snapshot["project_id"],
                                project_version_id=project_version_id,
                                dimension_code=dim_code,
                                status="pending_review",
                                status_label="Pendiente de revisión (HITL)",
                                summary="Distribución detenida preventivamente por riesgo alto en punto de control (HU3.1).",
                            )
                        dispatches = self._get_dimension_dispatches_with_conn(conn, project_version_id)
                        return {
                            "evaluation_id": evaluation_id,
                            "event_id": None,
                            "project_version_id": project_version_id,
                            "status": "pending_review",
                            "status_label": "Pendiente de revisión",
                            "duplicated": False,
                            "publication_pending": False,
                            "checkpoint": checkpoint,
                            "dimension_dispatches": dispatches,
                            "has_failed_dimensions": False,
                        }

                    evaluation_id, event_id = uuid4(), uuid4()
                    payload = {
                        "event_id": str(event_id),
                        "event_type": "EconomicEvaluationRequested",
                        "evaluation_id": str(evaluation_id),
                        "project_version_id": str(snapshot["project_version_id"]),
                        "criteria_version_id": str(snapshot["criteria_version_id"]),
                        "budget_pen": str(snapshot["estimated_budget_pen"]),
                        "beneficiaries_count": snapshot["beneficiaries_count"],
                        "excellent_cost_pen": str(snapshot["excellent_cost_pen"]),
                        "unacceptable_cost_pen": str(snapshot["unacceptable_cost_pen"]),
                        "criteria_version_number": snapshot["criteria_version_number"],
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "correlation_id": str(evaluation_id),
                        "schema_version": 1,
                    }
                    conn.execute(
                        """INSERT INTO evaluations
                        (id, project_version_id, criteria_version_id, requested_by_user_id,
                         status, idempotency_key) VALUES (%s,%s,%s,%s,'queued',%s)""",
                        (
                            evaluation_id,
                            project_version_id,
                            snapshot["criteria_version_id"],
                            actor_id,
                            idempotency_key,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO outbox_events (id,event_type,aggregate_id,payload) VALUES (%s,'EconomicEvaluationRequested',%s,%s)",
                        (event_id, evaluation_id, Jsonb(payload)),
                    )
                    for dim_code in self._DIMENSION_ORDER:
                        self._upsert_dimension_dispatch_with_conn(
                            conn,
                            evaluation_id=evaluation_id,
                            project_id=snapshot["project_id"],
                            project_version_id=project_version_id,
                            dimension_code=dim_code,
                            status="pending",
                            status_label="Pendiente",
                            summary="En cola para distribución automática.",
                        )
                    conn.execute(
                        "UPDATE projects SET status='evaluating', updated_at=now() WHERE id=%s",
                        (snapshot["project_id"],),
                    )
                    conn.execute(
                        """INSERT INTO audit_events
                        (actor_user_id,action,entity_type,entity_id,details)
                        VALUES (%s,'EVALUATION_QUEUED','evaluation',%s,%s)""",
                        (
                            actor_id,
                            evaluation_id,
                            Jsonb({
                                "event_id": str(event_id),
                                "project_version_id": str(project_version_id),
                                "distributed_dimensions": list(self._DIMENSION_ORDER),
                            }),
                        ),
                    )

        if retry_payload is not None:
            snap = retry_payload["snapshot"]
            sync_res = self._dispatch_multidimensional_specialists(
                evaluation_id=retry_payload["evaluation_id"],
                project_id=snap["project_id"],
                project_code=snap["project_code"],
                project_version_id=project_version_id,
                version_number=snap["version_number"],
                actor_id=actor_id,
                economic_payload=retry_payload["payload"],
                economic_event_id=retry_payload["event_id"],
                dimensions_to_run=retry_dims,
                increment_attempt=True,
            )
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                cp = RiskCheckpointEngine.get_active_checkpoint(conn, project_version_id)
                ev_cur = conn.execute("SELECT status FROM evaluations WHERE id=%s", (retry_payload["evaluation_id"],)).fetchone()
            return {
                "evaluation_id": retry_payload["evaluation_id"],
                "event_id": retry_payload["event_id"],
                "project_version_id": project_version_id,
                "status": ev_cur["status"] if ev_cur else retry_payload["status"],
                "status_label": None,
                "duplicated": True,
                "publication_pending": sync_res["publication_pending"],
                "checkpoint": cp,
                "dimension_dispatches": sync_res["dimension_dispatches"],
                "has_failed_dimensions": sync_res["has_failed_dimensions"],
            }

        sync_res = self._dispatch_multidimensional_specialists(
            evaluation_id=evaluation_id,
            project_id=snapshot["project_id"],
            project_code=snapshot["project_code"],
            project_version_id=project_version_id,
            version_number=snapshot["version_number"],
            actor_id=actor_id,
            economic_payload=payload,
            economic_event_id=event_id,
            dimensions_to_run=set(self._DIMENSION_ORDER),
            increment_attempt=False,
        )
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            cp = RiskCheckpointEngine.get_active_checkpoint(conn, project_version_id)
            ev_cur = conn.execute("SELECT status FROM evaluations WHERE id=%s", (evaluation_id,)).fetchone()
            cur_status = ev_cur["status"] if ev_cur else "queued"

        return {
            "evaluation_id": evaluation_id,
            "event_id": None if cur_status == "pending_review" else event_id,
            "project_version_id": project_version_id,
            "status": "pending_review" if cur_status == "pending_review" else "queued",
            "status_label": "Pendiente de revisión" if cur_status == "pending_review" else None,
            "duplicated": False,
            "publication_pending": sync_res["publication_pending"],
            "checkpoint": cp,
            "dimension_dispatches": sync_res["dimension_dispatches"],
            "has_failed_dimensions": sync_res["has_failed_dimensions"],
        }

    def retry_failed_dimensions(
        self,
        project_id: UUID,
        actor_id: UUID,
        version_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Recuperación segura: reintenta únicamente dimensiones en estado 'failed' o 'pending' para la versión indicada."""
        project = self.get_project(project_id)
        target_vid = version_id or project["project_version_id"]
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            self._verify_version_belongs_to_project(conn, project_id, target_vid)
            pv_row = conn.execute(
                """
                SELECT pv.id AS project_version_id, pv.project_id, pv.version_number,
                       pv.title, pv.description, pv.location_description AS location,
                       pv.proposed_land_use, pv.estimated_budget_pen, pv.beneficiaries_count
                FROM project_versions pv WHERE pv.id = %s
                """,
                (target_vid,),
            ).fetchone()
            if not pv_row:
                raise RecursoPMV1NoEncontradoError("Versión no encontrada")
            missing = self._missing_from_values(dict(pv_row))
            if missing:
                raise ExpedientePMV1IncompletoError(missing)
            if RiskCheckpointEngine.has_pending_checkpoint(conn, target_vid):
                raise ConfiguracionPMV1Error(
                    "El expediente se encuentra detenido en un punto de control activo (HU3.1); resuelva la revisión antes de reintentar."
                )
            ev_row = conn.execute(
                """
                SELECT e.id AS evaluation_id, o.id AS event_id, o.payload
                FROM evaluations e
                LEFT JOIN outbox_events o ON o.aggregate_id = e.id
                WHERE e.project_version_id = %s
                ORDER BY e.requested_at DESC
                LIMIT 1
                """,
                (target_vid,),
            ).fetchone()
            if not ev_row:
                raise RecursoPMV1NoEncontradoError(
                    "No existe una distribución previa para reintentar en esta versión."
                )
            dispatches = self._get_dimension_dispatches_with_conn(conn, target_vid)
            target_dims = {d["dimension_code"] for d in dispatches if d["status"] in ("failed", "pending")}

        if target_dims:
            self._dispatch_multidimensional_specialists(
                evaluation_id=ev_row["evaluation_id"],
                project_id=project_id,
                project_code=project["code"],
                project_version_id=target_vid,
                version_number=pv_row["version_number"],
                actor_id=actor_id,
                economic_payload=ev_row["payload"],
                economic_event_id=ev_row["event_id"],
                dimensions_to_run=target_dims,
                increment_attempt=True,
            )
        return self.get_evaluation(ev_row["evaluation_id"])

    @staticmethod
    def _build_evaluation_payload(
        row: dict[str, Any],
        checkpoint: dict[str, Any] | None,
        dimension_dispatches: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result = None
        if row["status"] != "pending_review" and checkpoint is None and row.get("economic_assessment_id"):
            socioeconomic = SocioeconomicReturnEngine.evaluate(
                budget_pen=row.get("estimated_budget_pen"),
                beneficiaries_count=row.get("beneficiaries_count"),
                socioeconomic_inputs=None,
            )
            result = {
                "economic_assessment_id": row["economic_assessment_id"],
                "cost_per_beneficiary_pen": row["cost_per_beneficiary_pen"],
                "score_0_100": row["score_0_100"],
                "explanation": row["explanation"],
                "algorithm_version": row["algorithm_version"],
                "indicator_name": "Costo por beneficiario",
                "currency": "PEN",
                "formula": "presupuesto / beneficiarios",
                "rounding_rule": "2 decimales (ROUND_HALF_UP)",
                "budget_pen": row.get("estimated_budget_pen"),
                "beneficiaries_count": row.get("beneficiaries_count"),
                **socioeconomic,
            }
        dispatches = dimension_dispatches or []
        has_failed = any(d["status"] == "failed" for d in dispatches)
        status_label = (
            "Pendiente de revisión"
            if row["status"] == "pending_review"
            else (
                "Completada con falla en especialista"
                if (row["status"] == "completed" and has_failed)
                else (
                    "Completada"
                    if row["status"] == "completed"
                    else (
                        "En procesamiento"
                        if row["status"] == "processing"
                        else ("En cola" if row["status"] == "queued" else row["status"])
                    )
                )
            )
        )
        return {
            "evaluation_id": row["evaluation_id"],
            "project_version_id": row["project_version_id"],
            "criteria_version_id": row["criteria_version_id"],
            "status": row["status"],
            "status_label": status_label,
            "checkpoint": checkpoint,
            "result": result,
            "dimension_dispatches": dispatches,
            "has_failed_dimensions": has_failed,
        }

    def _get_latest_evaluation_for_version_with_conn(
        self,
        conn: psycopg.Connection[Any],
        project_version_id: UUID,
        checkpoint: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            """
            SELECT e.id AS evaluation_id, e.project_version_id, e.criteria_version_id, e.status,
                   pv.estimated_budget_pen, pv.beneficiaries_count,
                   r.economic_assessment_id, r.cost_per_beneficiary_pen, r.score_0_100,
                   r.explanation, r.algorithm_version
            FROM evaluations e
            JOIN project_versions pv ON pv.id = e.project_version_id
            LEFT JOIN economic_result_projections r ON r.evaluation_id = e.id
            WHERE e.project_version_id = %s
            ORDER BY e.requested_at DESC
            LIMIT 1
            """,
            (project_version_id,),
        ).fetchone()
        if not row:
            return None
        dispatches = self._get_dimension_dispatches_with_conn(conn, project_version_id)
        return self._build_evaluation_payload(dict(row), checkpoint, dispatches)

    def get_evaluation(self, evaluation_id: UUID) -> dict[str, Any]:
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                SELECT e.id AS evaluation_id, e.project_version_id, e.criteria_version_id, e.status,
                       pv.estimated_budget_pen, pv.beneficiaries_count,
                       r.economic_assessment_id, r.cost_per_beneficiary_pen, r.score_0_100,
                       r.explanation, r.algorithm_version
                FROM evaluations e
                JOIN project_versions pv ON pv.id = e.project_version_id
                LEFT JOIN economic_result_projections r ON r.evaluation_id = e.id
                WHERE e.id = %s
                """,
                (evaluation_id,),
            ).fetchone()
            if not row:
                raise RecursoPMV1NoEncontradoError("Evaluacion no encontrada")
            checkpoint = RiskCheckpointEngine.get_active_checkpoint(conn, row["project_version_id"])
            dispatches = self._get_dimension_dispatches_with_conn(conn, row["project_version_id"])
            return self._build_evaluation_payload(dict(row), checkpoint, dispatches)

    def record_legal_abstention(self, project_id: UUID, actor_id: UUID) -> dict[str, Any]:
        """Mantiene compatibilidad con legal_precheck y devuelve el informe jurídico enriquecido con citas (HU2.2)."""
        self.get_project(project_id)
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            conn.execute(
                """INSERT INTO audit_events
                (actor_user_id,action,entity_type,entity_id,details)
                VALUES (%s,'LEGAL_PRECHECK_REQUIRES_REVIEW','project',%s,%s)""",
                (actor_id, project_id, Jsonb({"reason": "legal_assessment_performed"})),
            )
        assessment = self.get_legal_assessment(project_id)
        obs = [
            f"{f['title']}: {f['citation']['limitation_note']}"
            for f in assessment["findings"]
            if not f["citation"]["is_verifiable"]
        ] or [f["description"] for f in assessment["findings"]]
        return {
            "project_id": project_id,
            "status": assessment["status"],
            "complies": False if any(f.get("complies") is False for f in assessment["findings"]) else None,
            "sources": [
                f"{f['citation']['document_name']} ({f['citation']['version']}) - {f['citation']['article_or_section'] or 'Cita no verificable'}"
                for f in assessment["findings"]
            ],
            "requires_review": assessment["requires_human_review"],
            "observations": obs,
            "assessment": assessment,
            "findings": assessment["findings"],
        }

    @staticmethod
    def _compute_legal_assessment_with_conn(
        conn: psycopg.Connection[Any],
        project_id: UUID,
        project: dict[str, Any],
        version_id: UUID | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        finding_rows = conn.execute(
            """
            SELECT * FROM project_legal_findings
            WHERE project_id = %s AND (project_version_id = %s OR project_version_id IS NULL)
            ORDER BY created_at ASC
            """,
            (project_id, version_id),
        ).fetchall()

        resolution_row = conn.execute(
            """
            SELECT * FROM legal_alert_resolutions
            WHERE project_id = %s AND project_version_id = %s
            ORDER BY resolved_at DESC LIMIT 1
            """,
            (project_id, version_id),
        ).fetchone()

        chunk_ids = [r["normative_chunk_id"] for r in finding_rows if r.get("normative_chunk_id")]
        chunks_by_id: dict[str, dict[str, Any]] = {}
        if chunk_ids:
            chunks = conn.execute(
                "SELECT * FROM normative_documents WHERE id = ANY(%s)",
                (chunk_ids,),
            ).fetchall()
            chunks_by_id = {str(c["id"]): dict(c) for c in chunks}

        assessment = LegalAssessmentEngine.evaluate(
            project_id=project_id,
            project_code=project["code"],
            project_version_id=version_id,
            finding_rows=[dict(r) for r in finding_rows],
            chunks_by_id=chunks_by_id,
        )

        if resolution_row and assessment["has_legal_alert"]:
            assessment["has_legal_alert"] = False
            assessment["is_resolved"] = True
            assessment["resolution_justification"] = resolution_row["justification"]

            all_findings_comply = bool(finding_rows and all(r.get("complies") for r in finding_rows))
            assessment["status"] = (
                "verified"
                if (all_findings_comply and assessment["unverifiable_citations_count"] == 0)
                else "requires_review"
            )
            assessment["status_label"] = (
                "Alerta Legal Levantada / Citas Verificadas"
                if assessment["status"] == "verified"
                else "Alerta Legal Levantada / Requiere revisión de cumplimiento o citas"
            )
            assessment["summary"] += (
                f" [ALERTA LEVANTADA: {resolution_row['resolution_action']} por Asesor Jurídico. "
                f"Justificación: {resolution_row['justification']}]"
            )
        return assessment, resolution_row

    def evaluate_legal_assessment(
        self,
        project_id: UUID,
        actor_id: UUID,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Ejecuta y persiste la evaluación jurídica del expediente con citas comprobables (HU2.2/HU2.3)."""
        project = self.get_project(project_id)
        self._ensure_complete_project(project)
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            v_row = conn.execute(
                "SELECT id FROM project_versions WHERE project_id = %s ORDER BY version_number DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            version_id = v_row["id"] if v_row else None

            assessment, resolution_row = self._compute_legal_assessment_with_conn(
                conn, project_id, project, version_id
            )

            assessment_id = UUID(assessment["assessment_id"])
            row_ins = conn.execute(
                """
                INSERT INTO legal_assessments
                (id, project_id, project_code, project_version_id, status, summary, findings_json,
                 total_findings, verifiable_citations_count, unverifiable_citations_count,
                 requires_human_review, has_legal_alert, legal_alert_motive, legal_alert_norm,
                 is_resolved, resolved_by_user_id, resolved_at, resolution_justification, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, project_version_id) DO UPDATE SET
                    project_version_id = EXCLUDED.project_version_id,
                    status = EXCLUDED.status,
                    summary = EXCLUDED.summary,
                    findings_json = EXCLUDED.findings_json,
                    total_findings = EXCLUDED.total_findings,
                    verifiable_citations_count = EXCLUDED.verifiable_citations_count,
                    unverifiable_citations_count = EXCLUDED.unverifiable_citations_count,
                    requires_human_review = EXCLUDED.requires_human_review,
                    has_legal_alert = EXCLUDED.has_legal_alert,
                    legal_alert_motive = EXCLUDED.legal_alert_motive,
                    legal_alert_norm = EXCLUDED.legal_alert_norm,
                    is_resolved = EXCLUDED.is_resolved,
                    resolved_by_user_id = EXCLUDED.resolved_by_user_id,
                    resolved_at = EXCLUDED.resolved_at,
                    resolution_justification = EXCLUDED.resolution_justification,
                    evaluated_at = EXCLUDED.evaluated_at
                RETURNING id
                """,
                (
                    assessment_id,
                    project_id,
                    project["code"],
                    version_id,
                    assessment["status"],
                    assessment["summary"],
                    Jsonb(assessment["findings"]),
                    assessment["total_findings"],
                    assessment["verifiable_citations_count"],
                    assessment["unverifiable_citations_count"],
                    assessment["requires_human_review"],
                    assessment["has_legal_alert"],
                    assessment.get("legal_alert_motive"),
                    assessment.get("legal_alert_norm"),
                    assessment.get("is_resolved", False),
                    resolution_row["resolved_by_user_id"] if resolution_row else None,
                    resolution_row["resolved_at"] if resolution_row else None,
                    assessment.get("resolution_justification"),
                    assessment["evaluated_at"],
                ),
            ).fetchone()
            if row_ins:
                assessment["assessment_id"] = str(row_ins["id"])

            conn.execute(
                """INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'LEGAL_ASSESSMENT_EVALUATED', 'project', %s, %s)""",
                (
                    actor_id,
                    project_id,
                    Jsonb({
                        "project_code": project["code"],
                        "total_findings": assessment["total_findings"],
                        "verifiable_count": assessment["verifiable_citations_count"],
                        "unverifiable_count": assessment["unverifiable_citations_count"],
                        "has_legal_alert": assessment["has_legal_alert"],
                    }),
                ),
            )
            assessment["procurement_verification"] = self._get_procurement_verification_with_conn(
                conn,
                project_id=project_id,
                project_version_id=version_id,
                version_number=project.get("version_number", 1),
            )

            return assessment

    def get_legal_assessment(self, project_id: UUID) -> dict[str, Any]:
        """Recupera la evaluación jurídica del expediente o la evalúa si no existe."""
        project = self.get_project(project_id)
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            current_v_id = project.get("project_version_id")
            row = conn.execute(
                """
                SELECT * FROM legal_assessments
                WHERE project_id = %s
                ORDER BY (project_version_id = %s) DESC NULLS LAST, evaluated_at DESC
                LIMIT 1
                """,
                (project_id, current_v_id),
            ).fetchone()
            if row:
                # Aislamiento por versiones: si la versión vigente no coincide, reevaluar limpiamente o mostrar vista previa si es borrador
                if row.get("project_version_id") and current_v_id and row["project_version_id"] != current_v_id:
                    if self._missing_from_values(project):
                        assessment, _ = self._compute_legal_assessment_with_conn(
                            conn, project_id, project, current_v_id
                        )
                        assessment["procurement_verification"] = self._get_procurement_verification_with_conn(
                            conn,
                            project_id=project_id,
                            project_version_id=current_v_id,
                            version_number=project.get("version_number", 1),
                        )
                        return assessment
                    return self.evaluate_legal_assessment(project_id, UUID("10000000-0000-4000-a000-000000000004"))

                status = row["status"]
                if status == "alerta_legal":
                    status_label = "Alerta Legal: Incompatibilidad Normativa/Territorial"
                elif status == "verified":
                    status_label = "Citas normativas verificadas"
                elif status == "insufficient":
                    status_label = "Información jurídica insuficiente"
                else:
                    status_label = "Requiere revisión humana"

                proc_ver = self._get_procurement_verification_with_conn(
                    conn,
                    project_id=project_id,
                    project_version_id=row.get("project_version_id") or current_v_id,
                    version_number=project.get("version_number", 1),
                )
                return {
                    "assessment_id": str(row["id"]),
                    "project_id": str(row["project_id"]),
                    "project_code": row["project_code"],
                    "project_version_id": str(row["project_version_id"]) if row.get("project_version_id") else None,
                    "status": status,
                    "status_label": status_label,
                    "summary": row["summary"],
                    "findings": row["findings_json"],
                    "total_findings": row["total_findings"],
                    "verifiable_citations_count": row["verifiable_citations_count"],
                    "unverifiable_citations_count": row["unverifiable_citations_count"],
                    "requires_human_review": row["requires_human_review"],
                    "has_legal_alert": bool(row.get("has_legal_alert", False)),
                    "legal_alert_motive": row.get("legal_alert_motive"),
                    "legal_alert_norm": row.get("legal_alert_norm"),
                    "is_resolved": bool(row.get("is_resolved", False)),
                    "resolution_justification": row.get("resolution_justification"),
                    "evaluated_at": row["evaluated_at"],
                    "is_simulated_corpus": any(
                        isinstance(f, dict) and f.get("citation", {}).get("is_simulated", False)
                        for f in (row["findings_json"] or [])
                    ),
                    "procurement_verification": proc_ver,
                }

            if self._missing_from_values(project):
                assessment, _ = self._compute_legal_assessment_with_conn(
                    conn, project_id, project, current_v_id
                )
                assessment["procurement_verification"] = self._get_procurement_verification_with_conn(
                    conn,
                    project_id=project_id,
                    project_version_id=current_v_id,
                    version_number=project.get("version_number", 1),
                )
                return assessment

        return self.evaluate_legal_assessment(project_id, UUID("10000000-0000-4000-a000-000000000004"))

    ALLOWED_LEGAL_RESOLUTION_ACTIONS = {
        "falso_positivo",
        "subsanado_en_expediente",
        "excepcion_fundada",
        "riesgo_asumido",
    }

    ACTION_NORMALIZATION = {
        "falso_positivo": "falso_positivo",
        "falso positivo": "falso_positivo",
        "subsanado_en_expediente": "subsanado_en_expediente",
        "subsanado": "subsanado_en_expediente",
        "subsanación": "subsanado_en_expediente",
        "subsanacion": "subsanado_en_expediente",
        "excepcion_fundada": "excepcion_fundada",
        "excepcion": "excepcion_fundada",
        "excepción fundada": "excepcion_fundada",
        "riesgo_asumido": "riesgo_asumido",
        "riesgo normativo asumido": "riesgo_asumido",
    }

    def _verify_legal_resolution_actor(self, actor_id: UUID) -> None:
        """Verifica que el actor tenga rol LEGAL_ADVISOR, ADMIN o SUPERADMIN en auth_db."""
        raw_auth = self.auth_db_url or os.getenv("AUTH_DB_URL") or get_settings().auth_db_url
        auth_dsn = _dsn(raw_auth)
        with psycopg.connect(auth_dsn, row_factory=dict_row) as conn:
            rows = conn.execute(
                """
                SELECT r.code
                FROM users u
                JOIN user_roles ur ON u.id = ur.user_id
                JOIN roles r ON ur.role_id = r.id
                WHERE u.id = %s
                """,
                (actor_id,),
            ).fetchall()
            roles = {r["code"] for r in rows}
            if not (roles & {"LEGAL_ADVISOR", "ADMIN", "SUPERADMIN"}):
                raise AccesoDenegadoError(
                    f"El usuario {actor_id} no cuenta con perfil de Asesor Jurídico o Administrador autorizado. "
                    "Se requiere rol LEGAL_ADVISOR o ADMIN."
                )

    def resolve_legal_alert(
        self,
        project_id: UUID,
        actor_id: UUID,
        resolution_action: str,
        justification: str,
        idempotency_key: str,
        evidence_reference: str | None = None,
    ) -> dict[str, Any]:
        """Permite al Asesor Jurídico o Administrador levantar formalmente una alerta legal activa (HU2.3)."""
        cleaned_key = idempotency_key.strip()
        if not cleaned_key:
            raise ConfiguracionPMV1Error("Idempotency-Key es obligatorio")

        cleaned_action = resolution_action.strip()
        normalized_action = self.ACTION_NORMALIZATION.get(cleaned_action.lower())
        if not normalized_action:
            for k, norm_k in self.ACTION_NORMALIZATION.items():
                if k in cleaned_action.lower():
                    normalized_action = norm_k
                    break
        if not normalized_action:
            raise ConfiguracionPMV1Error(
                f"Acción de resolución '{cleaned_action}' no válida. "
                "Las decisiones permitidas son: 'falso_positivo', 'subsanado_en_expediente', "
                "'excepcion_fundada', 'riesgo_asumido'."
            )

        cleaned_justification = justification.strip()
        if len(cleaned_justification) < 5:
            raise ConfiguracionPMV1Error("La justificación debe tener al menos 5 caracteres")

        cleaned_evidence = evidence_reference.strip() if evidence_reference and evidence_reference.strip() else None

        # HU2.3 Corrección: subsanado_en_expediente exige obligatoriamente referencia probatoria
        if normalized_action == "subsanado_en_expediente":
            if not cleaned_evidence or len(cleaned_evidence) < 5:
                raise ConfiguracionPMV1Error(
                    "La decisión 'subsanado_en_expediente' exige obligatoriamente una referencia documental "
                    "o probatoria ('evidence_reference') con al menos 5 caracteres que sustente la subsanación del expediente."
                )

        # Exigir usuario con rol autorizado
        self._verify_legal_resolution_actor(actor_id)

        project = self.get_project(project_id)
        version_id = project["project_version_id"]

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            # 1. Comprobación estricta de idempotencia
            prev = conn.execute(
                "SELECT * FROM legal_alert_resolutions WHERE idempotency_key = %s",
                (cleaned_key,),
            ).fetchone()
            if prev:
                if prev["project_id"] != project_id or prev["project_version_id"] != version_id:
                    raise ConfiguracionPMV1Error("Idempotency-Key ya fue utilizada para otro expediente o versión")
                if (
                    prev["resolution_action"] != normalized_action
                    or prev["justification"] != cleaned_justification
                ):
                    raise ConfiguracionPMV1Error("Idempotency-Key no puede ser reutilizada con datos de resolución distintos")
                return {
                    "resolution_id": prev["id"],
                    "project_id": prev["project_id"],
                    "project_version_id": prev["project_version_id"],
                    "resolved_by_user_id": prev["resolved_by_user_id"],
                    "resolution_action": prev["resolution_action"],
                    "justification": prev["justification"],
                    "evidence_reference": prev.get("evidence_reference"),
                    "resolved_at": prev["resolved_at"],
                    "duplicated": True,
                }

            # 2. Solo permite resolver una alerta activa de la versión vigente.
            current_assessment = conn.execute(
                """
                SELECT id, status, has_legal_alert, is_resolved, summary, unverifiable_citations_count
                FROM legal_assessments
                WHERE project_id = %s AND project_version_id = %s
                """,
                (project_id, version_id),
            ).fetchone()

            if (
                not current_assessment
                or not current_assessment.get("has_legal_alert")
                or current_assessment.get("is_resolved", False)
            ):
                raise ConfiguracionPMV1Error(
                    f"No existe una Alerta Legal activa sin resolver en la versión vigente del expediente. "
                    "Operación rechazada."
                )

            # 3. Registrar la decisión humana POR SEPARADO con usuario, fecha, versión, acción, justificación y evidencia
            resolution_id = uuid4()
            now = datetime.now(timezone.utc)
            conn.execute(
                """
                INSERT INTO legal_alert_resolutions
                (id, project_id, project_version_id, resolved_by_user_id, resolution_action, justification, evidence_reference, resolved_at, idempotency_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    resolution_id,
                    project_id,
                    version_id,
                    actor_id,
                    normalized_action,
                    cleaned_justification,
                    cleaned_evidence,
                    now,
                    cleaned_key,
                ),
            )

            # 4. Conservar hallazgos históricos intactos: NO se muta project_legal_findings a complies=TRUE.

            # 5. Limitar actualización de estado exclusivamente a la versión correcta:
            # HU2.3 Corrección: Una justificación escrita no acredita la subsanación sin una nueva evaluación
            # de los datos corregidos. Levantar la alerta deja el caso en 'requires_review' sin declararlo 'verified'.
            new_status = "requires_review"

            conn.execute(
                """
                UPDATE legal_assessments
                SET status = %s,
                    has_legal_alert = FALSE,
                    is_resolved = TRUE,
                    resolved_by_user_id = %s,
                    resolved_at = %s,
                    resolution_justification = %s,
                    summary = summary || ' [ALERTA LEVANTADA (' || %s || '): ' || %s || ']'
                WHERE project_id = %s AND project_version_id = %s
                """,
                (
                    new_status,
                    actor_id,
                    now,
                    cleaned_justification,
                    normalized_action,
                    cleaned_justification,
                    project_id,
                    version_id,
                ),
            )

            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'LEGAL_ALERT_RESOLVED', 'project', %s, %s)
                """,
                (
                    actor_id,
                    project_id,
                    Jsonb({
                        "project_code": project["code"],
                        "resolution_action": normalized_action,
                        "justification": cleaned_justification,
                        "evidence_reference": cleaned_evidence,
                        "project_version_id": str(version_id),
                    }),
                ),
            )

        return {
            "resolution_id": resolution_id,
            "project_id": project_id,
            "project_version_id": version_id,
            "resolved_by_user_id": actor_id,
            "resolution_action": normalized_action,
            "justification": cleaned_justification,
            "evidence_reference": cleaned_evidence,
            "resolved_at": now,
            "duplicated": False,
        }

    # ------------------------------------------------------------------
    # HU1.10: Verificar Ley de Contrataciones Públicas (Ley N.° 32069)
    # ------------------------------------------------------------------

    @staticmethod
    def _fetch_procurement_chunks_with_conn(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT id, document_name, short_code, version, topic, content,
                   in_force, has_alert, data_origin,
                   official_url, consulted_at, amendments_summary, effective_from
            FROM normative_documents
            WHERE short_code IN ('LEY_32069', 'REG_32069', 'LEY_30225', 'LEY_26850', 'LEY_32069_HIST')
               OR id LIKE 'LEY32069%%'
               OR id LIKE 'DS009-2025%%'
               OR id IN ('LEY30225-HIST', 'LEY26850-HIST')
            ORDER BY in_force DESC, id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def _serialize_procurement_verification_row(
        row: dict[str, Any], *, version_number: int, duplicated: bool = False
    ) -> dict[str, Any]:
        con = row.get("consulted_at")
        return {
            "verification_id": row["id"],
            "project_id": row["project_id"],
            "project_version_id": row["project_version_id"],
            "version_number": version_number,
            "status": row["status"],
            "status_label": row["status_label"],
            "preliminary_compliant": row["preliminary_compliant"],
            "requires_human_review": bool(row["requires_human_review"]),
            "certified_infraction": bool(row["certified_infraction"]),
            "sent_to_legal_advisor": bool(row["sent_to_legal_advisor"]),
            "summary": row["summary"],
            "affected_requirement": row.get("affected_requirement"),
            "observation_explanation": row.get("observation_explanation"),
            "missing_fields": row.get("missing_fields_json") or [],
            "examined_requirements": row.get("examined_requirements_json") or [],
            "normative_sources": row.get("normative_sources_json") or [],
            "superseded_sources_excluded": row.get("superseded_sources_excluded_json") or [],
            "advisory_review_status": row.get("advisory_review_status") or "pending",
            "advisory_reviewer_id": row.get("advisory_reviewer_id"),
            "advisory_review_notes": row.get("advisory_review_notes"),
            "advisory_reviewed_at": row.get("advisory_reviewed_at"),
            "consulted_at": con.isoformat() if hasattr(con, "isoformat") else (str(con) if con else ProcurementVerificationEngine.CONSULTED_AT),
            "duplicated": duplicated,
            "requested_at": row["requested_at"],
        }

    def _get_procurement_verification_with_conn(
        self,
        conn: psycopg.Connection[Any],
        *,
        project_id: UUID,
        project_version_id: UUID | None,
        version_number: int,
    ) -> dict[str, Any] | None:
        if not project_version_id:
            return None
        row = conn.execute(
            """
            SELECT * FROM procurement_verification_requests
            WHERE project_id = %s AND project_version_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (project_id, project_version_id),
        ).fetchone()
        if not row:
            return None
        return self._serialize_procurement_verification_row(dict(row), version_number=version_number)

    def _resolve_target_version(
        self, conn: psycopg.Connection[Any], project_id: UUID, version_id: UUID | None
    ) -> dict[str, Any]:
        if version_id:
            v_row = conn.execute(
                """
                SELECT id, version_number, estimated_budget_pen
                FROM project_versions
                WHERE id = %s AND project_id = %s
                """,
                (version_id, project_id),
            ).fetchone()
        else:
            v_row = conn.execute(
                """
                SELECT id, version_number, estimated_budget_pen
                FROM project_versions
                WHERE project_id = %s
                ORDER BY version_number DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()
        if not v_row:
            raise RecursoPMV1NoEncontradoError("Versión de expediente no encontrada para el proyecto")
        return dict(v_row)

    def get_procurement_antecedents(
        self, project_id: UUID, version_id: UUID | None = None
    ) -> dict[str, Any]:
        """Consulta antecedentes contractuales estructurados y fuentes vigentes de Ley N.° 32069."""
        self.get_project(project_id)
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            ver = self._resolve_target_version(conn, project_id, version_id)
            rec = conn.execute(
                """
                SELECT * FROM project_procurement_records
                WHERE project_id = %s AND project_version_id = %s
                """,
                (project_id, ver["id"]),
            ).fetchone()
            all_chunks = self._fetch_procurement_chunks_with_conn(conn)

        active_sources: list[dict[str, Any]] = []
        hist_sources: list[dict[str, Any]] = []
        for c in all_chunks:
            s = ProcurementVerificationEngine._serialize_source(c)
            ver_str = str(c.get("version") or "").lower()
            is_stale = str(c["id"]).startswith("LEY32069-") and ("2024-v1" in ver_str or "histor" in ver_str)
            if c.get("in_force") and c.get("data_origin") in ("public", "official") and not is_stale:
                active_sources.append(s)
            else:
                hist_sources.append(s)

        if not rec:
            return {
                "project_id": project_id,
                "project_version_id": ver["id"],
                "version_number": ver["version_number"],
                "has_record": False,
                "data_origin": "declared",
                "active_normative_sources": active_sources,
                "historical_normative_sources": hist_sources,
            }

        return {
            "project_id": project_id,
            "project_version_id": ver["id"],
            "version_number": ver["version_number"],
            "has_record": True,
            "contracting_regime": rec.get("contracting_regime"),
            "contract_object": rec.get("contract_object"),
            "contracting_phase": rec.get("contracting_phase"),
            "selection_procedure": rec.get("selection_procedure"),
            "requirement_defined": rec.get("requirement_defined"),
            "in_cmn_and_viability": rec.get("in_cmn_and_viability"),
            "budget_certification_declared": rec.get("budget_certification_declared"),
            "pladicop_registration_planned": rec.get("pladicop_registration_planned"),
            "no_fractionation_declared": rec.get("no_fractionation_declared"),
            "supporting_document_ref": rec.get("supporting_document_ref"),
            "contractual_summary": rec.get("contractual_summary"),
            "data_origin": rec.get("data_origin") or "declared",
            "updated_at": rec.get("updated_at"),
            "active_normative_sources": active_sources,
            "historical_normative_sources": hist_sources,
        }

    def upsert_procurement_antecedents(
        self,
        *,
        project_id: UUID,
        actor_id: UUID,
        data: ProcurementAntecedentUpsert,
        version_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Registra o actualiza antecedentes contractuales estrictamente para la versión indicada."""
        project = self.get_project(project_id)
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            ver = self._resolve_target_version(conn, project_id, version_id)
            rec_id = uuid4()
            conn.execute(
                """
                INSERT INTO project_procurement_records (
                    id, project_id, project_version_id, contracting_regime, contract_object,
                    contracting_phase, selection_procedure, requirement_defined, in_cmn_and_viability,
                    budget_certification_declared, pladicop_registration_planned, no_fractionation_declared,
                    supporting_document_ref, contractual_summary, data_origin, updated_by_user_id, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (project_version_id) DO UPDATE SET
                    contracting_regime = EXCLUDED.contracting_regime,
                    contract_object = EXCLUDED.contract_object,
                    contracting_phase = EXCLUDED.contracting_phase,
                    selection_procedure = EXCLUDED.selection_procedure,
                    requirement_defined = EXCLUDED.requirement_defined,
                    in_cmn_and_viability = EXCLUDED.in_cmn_and_viability,
                    budget_certification_declared = EXCLUDED.budget_certification_declared,
                    pladicop_registration_planned = EXCLUDED.pladicop_registration_planned,
                    no_fractionation_declared = EXCLUDED.no_fractionation_declared,
                    supporting_document_ref = EXCLUDED.supporting_document_ref,
                    contractual_summary = EXCLUDED.contractual_summary,
                    data_origin = EXCLUDED.data_origin,
                    updated_by_user_id = EXCLUDED.updated_by_user_id,
                    updated_at = now()
                """,
                (
                    rec_id,
                    project_id,
                    ver["id"],
                    data.contracting_regime,
                    data.contract_object,
                    data.contracting_phase,
                    data.selection_procedure,
                    data.requirement_defined,
                    data.in_cmn_and_viability,
                    data.budget_certification_declared,
                    data.pladicop_registration_planned,
                    data.no_fractionation_declared,
                    data.supporting_document_ref,
                    data.contractual_summary,
                    data.data_origin,
                    actor_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'PROCUREMENT_ANTECEDENTS_UPDATED', 'project', %s, %s)
                """,
                (
                    actor_id,
                    project_id,
                    Jsonb({
                        "project_code": project["code"],
                        "project_version_id": str(ver["id"]),
                        "version_number": ver["version_number"],
                        "contracting_regime": data.contracting_regime,
                        "contract_object": data.contract_object,
                        "selection_procedure": data.selection_procedure,
                    }),
                ),
            )

        return self.get_procurement_antecedents(project_id, version_id=ver["id"])

    def get_procurement_verification(
        self, project_id: UUID, version_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """Consulta en modo solo lectura la última verificación contractual HU1.10 de la versión."""
        self.get_project(project_id)
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            ver = self._resolve_target_version(conn, project_id, version_id)
            return self._get_procurement_verification_with_conn(
                conn,
                project_id=project_id,
                project_version_id=ver["id"],
                version_number=ver["version_number"],
            )

    def request_procurement_verification(
        self,
        *,
        project_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        version_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Ejecuta la verificación contractual contra Ley N.° 32069 y D.S. N.° 009-2025-EF (HU1.10)."""
        cleaned_key = idempotency_key.strip()
        if not cleaned_key:
            raise ConfiguracionPMV1Error("Idempotency-Key es obligatorio")
        project = self.get_project(project_id)
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            ver = self._resolve_target_version(conn, project_id, version_id)
            previous = conn.execute(
                """
                SELECT * FROM procurement_verification_requests
                WHERE idempotency_key = %s AND project_id = %s AND project_version_id = %s
                """,
                (cleaned_key, project_id, ver["id"]),
            ).fetchone()
            if previous:
                return self._serialize_procurement_verification_row(
                    dict(previous),
                    version_number=ver["version_number"],
                    duplicated=True,
                )

            antecedent_row = conn.execute(
                """
                SELECT * FROM project_procurement_records
                WHERE project_id = %s AND project_version_id = %s
                """,
                (project_id, ver["id"]),
            ).fetchone()
            all_chunks = self._fetch_procurement_chunks_with_conn(conn)

            project_for_version = {**project, "estimated_budget_pen": ver.get("estimated_budget_pen")}
            evaluated = ProcurementVerificationEngine.evaluate(
                project=project_for_version,
                antecedent=dict(antecedent_row) if antecedent_row else None,
                all_chunks=all_chunks,
            )

            ver_id = uuid4()
            now = evaluated["evaluated_at"]
            conn.execute(
                """
                INSERT INTO procurement_verification_requests (
                    id, project_id, project_version_id, requested_by_user_id, idempotency_key,
                    status, status_label, preliminary_compliant, requires_human_review,
                    certified_infraction, sent_to_legal_advisor, summary,
                    affected_requirement, observation_explanation,
                    missing_fields_json, examined_requirements_json,
                    normative_sources_json, superseded_sources_excluded_json,
                    advisory_review_status, consulted_at, requested_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ver_id,
                    project_id,
                    ver["id"],
                    actor_id,
                    cleaned_key,
                    evaluated["status"],
                    evaluated["status_label"],
                    evaluated["preliminary_compliant"],
                    evaluated["requires_human_review"],
                    False,
                    evaluated["sent_to_legal_advisor"],
                    evaluated["summary"],
                    evaluated.get("affected_requirement"),
                    evaluated.get("observation_explanation"),
                    Jsonb(evaluated["missing_fields"]),
                    Jsonb(evaluated["examined_requirements"]),
                    Jsonb(evaluated["normative_sources"]),
                    Jsonb(evaluated["superseded_sources_excluded"]),
                    evaluated["advisory_review_status"],
                    evaluated["consulted_at"],
                    now,
                ),
            )
            conn.execute(
                """
                UPDATE legal_assessments
                SET procurement_verification_json = %s
                WHERE project_id = %s AND project_version_id = %s
                """,
                (
                    Jsonb({
                        "verification_id": str(ver_id),
                        "status": evaluated["status"],
                        "status_label": evaluated["status_label"],
                        "preliminary_compliant": evaluated["preliminary_compliant"],
                        "sent_to_legal_advisor": evaluated["sent_to_legal_advisor"],
                        "affected_requirement": evaluated.get("affected_requirement"),
                        "summary": evaluated["summary"],
                    }),
                    project_id,
                    ver["id"],
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'PROCUREMENT_VERIFICATION_EXECUTED', 'project', %s, %s)
                """,
                (
                    actor_id,
                    project_id,
                    Jsonb({
                        "project_code": project["code"],
                        "project_version_id": str(ver["id"]),
                        "status": evaluated["status"],
                        "sent_to_legal_advisor": evaluated["sent_to_legal_advisor"],
                        "affected_requirement": evaluated.get("affected_requirement"),
                    }),
                ),
            )
            saved = conn.execute(
                "SELECT * FROM procurement_verification_requests WHERE id = %s",
                (ver_id,),
            ).fetchone()

        return self._serialize_procurement_verification_row(
            dict(saved),
            version_number=ver["version_number"],
            duplicated=False,
        )

    def review_procurement_observation(
        self,
        *,
        project_id: UUID,
        actor_id: UUID,
        advisory_review_status: str,
        advisory_review_notes: str,
        version_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Registra la validación jurídica humana (LEGAL_ADVISOR) sobre observaciones HU1.10."""
        self._verify_legal_resolution_actor(actor_id)
        project = self.get_project(project_id)
        if not _is_db_available(self.db_url):
            raise ServicioNoDisponibleError("Base de datos no disponible")

        cleaned_notes = advisory_review_notes.strip()
        if len(cleaned_notes) < 5:
            raise ConfiguracionPMV1Error(
                "Debe consignar una fundamentación jurídica mínima de 5 caracteres."
            )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            ver = self._resolve_target_version(conn, project_id, version_id)
            target = conn.execute(
                """
                SELECT * FROM procurement_verification_requests
                WHERE project_id = %s AND project_version_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id, ver["id"]),
            ).fetchone()
            if not target:
                raise RecursoPMV1NoEncontradoError(
                    "No existe verificación contractual previa para la versión vigente del expediente."
                )

            now = datetime.now(timezone.utc)
            updated = conn.execute(
                """
                UPDATE procurement_verification_requests
                SET advisory_review_status = %s,
                    advisory_reviewer_id = %s,
                    advisory_review_notes = %s,
                    advisory_reviewed_at = %s,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """,
                (
                    advisory_review_status,
                    actor_id,
                    cleaned_notes,
                    now,
                    now,
                    target["id"],
                ),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'PROCUREMENT_ADVISORY_REVIEW_RECORDED', 'project', %s, %s)
                """,
                (
                    actor_id,
                    project_id,
                    Jsonb({
                        "project_code": project["code"],
                        "project_version_id": str(ver["id"]),
                        "verification_id": str(target["id"]),
                        "advisory_review_status": advisory_review_status,
                    }),
                ),
            )

        return self._serialize_procurement_verification_row(
            dict(updated),
            version_number=ver["version_number"],
            duplicated=False,
        )

    _PDM_OFFICIAL_URL: ClassVar[str] = (
        "https://www.gob.pe/institucion/munihuancayo/normas-legales/838304-636-2020-mph-om"
    )
    _PDM_MAPS_URL: ClassVar[str] = (
        "https://www.gob.pe/institucion/munihuancayo/informes-publicaciones/7983599-planos-del-pdm-huancayo-2017-2037"
    )
    _PDM_AUTHORITY: ClassVar[str] = "Municipalidad Provincial de Huancayo (MPH)"

    # Registro controlado en servidor de capas GIS oficialmente verificadas (SHA-256 de geometría canónica).
    # En el repositorio no existe aún una capa GIS vectorial oficial de zonificación del PDM Huancayo
    # (los planos en gob.pe están en PDF); por ello este registro inicia vacío y NINGUNA capa declarada por
    # un usuario mediante POST /geospatial-layers pasa por oficial aunque indique data_origin='public'.
    _VERIFIED_OFFICIAL_ZONING_LAYERS: ClassVar[dict[str, dict[str, Any]]] = {}

    # Huellas verificadas de límites político-administrativos (INEI ig_distrito UBIGEO 120114),
    # útiles solo como referencia distrital, jamás como zonificación predial.
    _VERIFIED_ADMIN_BOUNDARY_SHA256S: ClassVar[frozenset[str]] = frozenset(
        {
            "71591b5a5491e102f8cc81084fcde8e84564685352b3b2538fcde8e9fe64928b",
            "dc265829f26fd8724d19169648a180a0bc35e1b7baad8e62cdecea5e374c96b2",
        }
    )

    # Catálogo verificado en gob.pe sobre vigencia base y modificaciones parciales del PDM 2017-2037 (El Tambo / Provincia).
    # Ninguna modificación parcial desactiva la O.M. N.° 636-2020-MPH/OM para todo el distrito.
    _PDM_MODIFICATION_ORDINANCES: ClassVar[tuple[dict[str, Any], ...]] = (
        {
            "ordinance": "O.M. N.° 636-2020-MPH/OM",
            "publication_date": "2020-05-08",
            "relation_to_base": "Norma base vigente que aprueba PAT y PDM Huancayo 2017-2037",
            "scope_type": "base_metropolitana",
            "scope_summary": "Provincia de Huancayo (incluye El Tambo), salvo predios o tramos con modificación parcial expresa.",
            "scope_keywords": (),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/838304-636-2020-mph-om",
        },
        {
            "ordinance": "O.M. N.° 669-2021-MPH/OM",
            "publication_date": "2021-10-20",
            "relation_to_base": "Modificación al PDM 2017-2037 (alcance reglamentario/metropolitano)",
            "scope_type": "rectificacion_reglamento",
            "scope_summary": "Modificación general al PDM 2017-2037 publicada sin delimitación vectorial abierta.",
            "scope_keywords": (),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/5947318-669-2021-mph-om",
        },
        {
            "ordinance": "O.M. N.° 743-2023-MPH/CM",
            "publication_date": "2023-08-21",
            "relation_to_base": "Modifica O.M. N.° 636-MPH/CM (PDM 2017-2037)",
            "scope_type": "rectificacion_reglamento",
            "scope_summary": "Modificación de disposiciones del PDM 2017-2037 (sin shapefile consolidado público).",
            "scope_keywords": (),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/4582484-743-2023-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 753-2023-MPH/CM",
            "publication_date": "2023-12-13",
            "relation_to_base": "Rectifica errores materiales de O.M. N.° 636-MPH/CM y sus reglamentos",
            "scope_type": "rectificacion_reglamento",
            "scope_summary": "Rectificación reglamentaria y de errores materiales del PDM Huancayo.",
            "scope_keywords": (),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/4953099-753-2023-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 761-2024-MPH/CM",
            "publication_date": "2024-04-08",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (diseño vial)",
            "scope_type": "tramo_vial",
            "scope_summary": "Av. Ferrocarril, tramo Av. Circunvalación (El Tambo) hasta Av. Centenario (San Jerónimo).",
            "scope_keywords": ("ferrocarril", "circunvalacion"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/5466458-761-2024-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 765-2024-MPH",
            "publication_date": "2024-06-24",
            "relation_to_base": "Modificación parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Paraje Virgen de Cocharcas, Anexo de Saños Chico, El Tambo (reducción de ZRP-P a RDM).",
            "scope_keywords": ("virgen de cocharcas", "sanos chico"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/5847506-765-2024-mph",
        },
        {
            "ordinance": "O.M. N.° 777-2024-MPH/CM",
            "publication_date": "2024-11-06",
            "relation_to_base": "Rectificación por error material de O.M. N.° 636-MPH/CM y reglamentos",
            "scope_type": "rectificacion_reglamento",
            "scope_summary": "Corrección reglamentaria aplicable al texto/cuadros del PDM y sus reglamentos.",
            "scope_keywords": (),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/6245405-777-2024-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 778-2024-MPH/CM",
            "publication_date": "2024-12-02",
            "relation_to_base": "Actualización de Sistema Vial de O.M. N.° 636-MPH/CM (vía colectora específica)",
            "scope_type": "tramo_vial",
            "scope_summary": "Vía Colectora Jr. Daniel Alcides Carrión, AA.HH. La Victoria, El Tambo.",
            "scope_keywords": ("daniel alcides carrion", "la victoria"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/6282115-778-2024-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 779-2024-MPH/CM",
            "publication_date": "2024-12-20",
            "relation_to_base": "Subsanación de error material en PDM 2017-2037 y reglamentos",
            "scope_type": "rectificacion_reglamento",
            "scope_summary": "Subsanación general de errores materiales en el PDM 2017-2037 y reglamentos.",
            "scope_keywords": (),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/6369203-779-2024-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 787-2025-MPH/CM",
            "publication_date": "2025-04-04",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio 'Asoc. Sucesión Flores Cerrón', sector Saños Chico, El Tambo (ZRP-P a RDM).",
            "scope_keywords": ("flores cerron", "sanos chico"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/6669653-787-2025-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 789-2025-MPH/CM",
            "publication_date": "2025-04-16",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio en inmediaciones de Vía Arterial A1-II (Av. Túpac Amaru y Av. Unión), Anexo Hualahoyo, El Tambo (PU a RDM).",
            "scope_keywords": ("tupac amaru", "av. union", "hualahoyo", "hualaoyo"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/6736082-789-2025-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 793-2025-MPH/CM",
            "publication_date": "2025-07-07",
            "relation_to_base": "Rectificación de error material de O.M. N.° 636-MPH/CM",
            "scope_type": "rectificacion_reglamento",
            "scope_summary": "Rectificación reglamentaria/documental del PDM 2017-2037.",
            "scope_keywords": (),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/6946063-793-2025-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 800-2025-MPH/CM",
            "publication_date": "2025-09-15",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio Universidad Continental S.A.C. en Jr. Carhuaquero y Prolongación Hilarión, El Tambo (OU a E3).",
            "scope_keywords": ("carhuaquero", "hilarion", "continental"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/7278067-800-2025-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 818-2025-MPH/CM",
            "publication_date": "2025-12-19",
            "relation_to_base": "Modificación parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio ubicado en Av. José Carlos Mariátegui, El Tambo (ZRP-P a CZ).",
            "scope_keywords": ("mariategui",),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/7573970-818-2025-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 829-2026-MPH/CM",
            "publication_date": "2026-02-25",
            "relation_to_base": "Modificación parcial de O.M. N.° 636-MPH/CM (lote específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Lote ubicado en Calle Real y Sebastián Lorente, distrito de El Tambo.",
            "scope_keywords": ("calle real", "sebastian lorente", "lorente"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/7922811-829-2026-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 834-2026-MPH/CM",
            "publication_date": "2026-04-15",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio ubicado en Anexo de Cullpa Baja, distrito de El Tambo (ZRP-P a RDM).",
            "scope_keywords": ("cullpa",),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/8070875-834-2026-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 836-2026-MPH/CM",
            "publication_date": "2026-05-13",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio ubicado en Av. Progreso, distrito de El Tambo (E1 Educación Básica a Comercio Vecinal CV y RDM).",
            "scope_keywords": ("av. progreso", "progreso"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/8165508-836-2026-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 838-2026-MPH/CM",
            "publication_date": "2026-05-13",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio en Vía Arterial A1-II (Av. Túpac Amaru S/N) y Circuito Ecológico Turístico (Av. General Mercado Jarrín), Anexo de Hualahoyo, El Tambo (PU a RDM).",
            "scope_keywords": ("tupac amaru", "mercado jarrin", "hualahoyo"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/8165556-838-2026-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 839-2026-MPH/CM",
            "publication_date": "2026-05-13",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (predio específico)",
            "scope_type": "predio_especifico",
            "scope_summary": "Predio en calle proyectada S/N adyacente a Av. Mariátegui y Av. Francisca de la Calle, El Tambo (ZRP-P a RDM).",
            "scope_keywords": ("mariategui", "francisca de la calle"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/8165564-839-2026-mph-cm",
        },
        {
            "ordinance": "O.M. N.° 851-2026-MPH/CM",
            "publication_date": "2026-11-02",
            "relation_to_base": "Actualización parcial de O.M. N.° 636-MPH/CM (diseño vial específico)",
            "scope_type": "tramo_vial",
            "scope_summary": "Modificación de Diseño Vial de Av. Mariscal Castilla (tramo: La Victoria - Av. Mariátegui), distrito de El Tambo.",
            "scope_keywords": ("mariscal castilla", "la victoria", "mariategui"),
            "official_url": "https://www.gob.pe/institucion/munihuancayo/normas-legales/8821705-851-2026-mph-cm",
        },
    )

    @staticmethod
    def _canonical_geojson_sha256(geojson_obj: Any) -> str:
        return ZoningSourceValidator.canonical_geojson_sha256(geojson_obj)

    @classmethod
    def _resolve_pdm_amendments_for_location(
        cls, location: str | None
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Devuelve todas las normas del PDM clasificadas por alcance sobre la ubicación sin desactivar la base."""
        loc_norm = cls._normalize_use_text(location)
        amendments: list[dict[str, str]] = []
        potentially_affected: list[dict[str, str]] = []

        for item in cls._PDM_MODIFICATION_ORDINANCES:
            stype = item["scope_type"]
            keywords = item.get("scope_keywords") or ()
            keyword_hit = bool(keywords) and any(kw in loc_norm for kw in keywords)
            if stype == "base_metropolitana":
                app_status = "base_vigente"
            elif stype == "rectificacion_reglamento":
                app_status = "alcance_general_reglamentario"
            elif keyword_hit:
                app_status = "posible_coincidencia_pendiente_catastro"
            else:
                app_status = "otro_predio_sector_el_tambo"

            entry = {
                "ordinance": item["ordinance"],
                "publication_date": item["publication_date"],
                "relation_to_base": item["relation_to_base"],
                "scope_type": stype,
                "scope_summary": item["scope_summary"],
                "applicability_status": app_status,
                "official_url": item["official_url"],
            }
            amendments.append(entry)
            if app_status == "posible_coincidencia_pendiente_catastro":
                potentially_affected.append(entry)

        return amendments, potentially_affected

    @classmethod
    def _match_geojson_feature(
        cls, geojson_obj: Any, lon: float, lat: float
    ) -> dict[str, Any] | None:
        return ZoningSourceValidator.lookup_point(geojson_obj, lon, lat)

    @staticmethod
    def _normalize_use_text(text: str | None) -> str:
        raw = (text or "").strip().lower()
        for src, dst in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n")):
            raw = raw.replace(src, dst)
        return raw

    @classmethod
    def _fetch_pdm_document_evidence(
        cls, conn: psycopg.Connection[Any]
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT id, document_name, short_code, version, topic, content, in_force, data_origin
            FROM normative_documents
            WHERE short_code = 'PDU_EL_TAMBO' AND in_force = TRUE
            ORDER BY id ASC
            """
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "document": r["document_name"],
                "version": r["version"],
                "locator": f"{r['id']} · {r['topic']}",
                "excerpt": str(r["content"])[:260],
                "is_spatial_classification": False,
                "authority": cls._PDM_AUTHORITY,
                "official_url": cls._PDM_OFFICIAL_URL,
                "in_force": bool(r["in_force"]),
                "data_origin": r.get("data_origin"),
            })
        return out

    @classmethod
    def _collect_zoning_risk_notices(
        cls,
        conn: psycopg.Connection[Any],
        project: dict[str, Any],
    ) -> str | None:
        """Detecta avisos de riesgo hídrico/ambiental o áreas verdes sin umbral ni polígono oficial."""
        pv_id = project["project_version_id"]
        notices: list[str] = []

        env_rec = conn.execute(
            """
            SELECT environmental_component, verifiable_evidence, proximity_to_water_meters,
                   is_in_protected_or_risk_area, source_name, data_origin
            FROM project_environmental_records
            WHERE (
                project_version_id = %s
                OR (project_version_id IS NULL AND project_id = %s)
            )
              AND has_sufficient_data = TRUE
              AND (
                  is_in_protected_or_risk_area = TRUE
                  OR (proximity_to_water_meters IS NOT NULL AND proximity_to_water_meters < 50)
              )
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (pv_id, project["project_id"]),
        ).fetchone()
        if env_rec:
            prox = env_rec.get("proximity_to_water_meters")
            prox_txt = f"{prox} m" if prox is not None else "no especificada"
            notices.append(
                f"Alerta de riesgo ambiental/hídrico para revisión humana ({env_rec['environmental_component']}, "
                f"origen '{env_rec.get('data_origin')}', distancia declarada a agua: {prox_txt}, fuente: {env_rec.get('source_name')}): "
                "O.M. N.° 636-2020-MPH/OM (PDUPDM-04) exige respetar zonas no mitigables y fajas marginales, pero NO fija un umbral "
                "uniforme de 50 m ni acredita delimitación ANA sin Resolución Directoral/polígono oficial; requiere revisión técnica "
                "y jurídica sin certificar compatibilidad ni incompatibilidad de zonificación."
            )

        green_rec = conn.execute(
            """
            SELECT affectation_type, verifiable_affectation, source_name, data_origin
            FROM project_green_area_records
            WHERE (project_version_id = %s OR (project_version_id IS NULL AND (project_id = %s OR project_code = %s)))
              AND has_sufficient_data = TRUE
              AND affectation_type IN ('loss', 'encroachment', 'incompatible_conversion', 'perdida', 'superposicion', 'afectacion_no_mitigada')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (pv_id, project["project_id"], project["code"]),
        ).fetchone()
        if green_rec:
            notices.append(
                f"Riesgo reportado sobre áreas verdes/espacio público (tipo '{green_rec['affectation_type']}', "
                f"origen '{green_rec.get('data_origin')}', fuente: {green_rec.get('source_name')}): "
                "contrastar en revisión jurídica y municipal contra PDUPDM-06 sin emitir dictamen zonal sin polígono oficial."
            )

        return " ".join(notices) if notices else None

    @classmethod
    def _evaluate_verified_zone_against_pdm_sections(
        cls,
        *,
        zone_code: str,
        proposed_norm: str,
        pdm_by_id: dict[str, dict[str, Any]],
        verified_entry: dict[str, Any],
    ) -> dict[str, Any] | None:
        """
        Evalúa únicamente reglas vinculadas explícitamente a secciones normativas vigentes en corpus.
        No utiliza diccionarios heurísticos de abreviaturas urbanas (RDM/RDA/CZ/I-1/OU/E/H) si no consta
        el Índice de Usos oficial parametrizado en repositorio.
        """
        z_upper = zone_code.strip().upper()
        # Regla explícita en PDUPDM-04 (O.M. 636-2020-MPH/OM): zonas de riesgo no mitigable o faja marginal
        if z_upper in ("ZRNM", "FAJA_MARGINAL") and "PDUPDM-04" in pdm_by_id:
            ref = pdm_by_id["PDUPDM-04"]
            return {
                "compatible": False,
                "section_id": "PDUPDM-04",
                "section_ref": ref,
                "reason": "verified_non_mitigable_risk_zone",
            }

        # Reglas explícitas en PDUPDM-01 y PDUPDM-06 sobre Zona de Recreación Pública (ZRP / ZRP-P / ZRP-A)
        if z_upper in ("ZRP", "ZRP-P", "ZRP-A") and "PDUPDM-01" in pdm_by_id and "PDUPDM-06" in pdm_by_id:
            private_commercial_tokens = ("comerci", "vivienda", "habitacion", "industr", "edificacion privada", "centro comercial")
            green_recreation_tokens = ("recrea", "parque", "area verde", "plaza", "alameda", "arboriz")
            if any(tok in proposed_norm for tok in private_commercial_tokens):
                return {
                    "compatible": False,
                    "section_id": "PDUPDM-06",
                    "section_ref": pdm_by_id["PDUPDM-06"],
                    "reason": "verified_zrp_intangible_incompatibility",
                }
            if any(tok in proposed_norm for tok in green_recreation_tokens):
                return {
                    "compatible": True,
                    "section_id": "PDUPDM-01",
                    "section_ref": pdm_by_id["PDUPDM-01"],
                    "reason": "verified_zrp_recreation_compatible",
                }

        # Matriz oficial explícita solo si la capa verificada incluye sección e Índice de Usos oficial aprobado
        official_matrix = verified_entry.get("official_use_rules") or []
        for rule in official_matrix:
            section_id = str(rule.get("section_id") or "")
            if section_id not in pdm_by_id:
                continue
            incomp = [cls._normalize_use_text(str(x)) for x in (rule.get("incompatible_uses") or [])]
            comp = [cls._normalize_use_text(str(x)) for x in (rule.get("compatible_uses") or [])]
            if any(tok and (tok in proposed_norm or proposed_norm in tok) for tok in incomp):
                return {
                    "compatible": False,
                    "section_id": section_id,
                    "section_ref": pdm_by_id[section_id],
                    "reason": "verified_official_matrix_incompatibility",
                }
            if any(tok and (tok in proposed_norm or proposed_norm in tok) for tok in comp):
                return {
                    "compatible": True,
                    "section_id": section_id,
                    "section_ref": pdm_by_id[section_id],
                    "reason": "verified_official_matrix_compatibility",
                }

        # Para abreviaturas como RDM/RDA/CZ/OU/I-1/E/H sin Índice de Usos oficial cargado, abstenemos.
        return None

    _SYNTHETIC_ZONING_MARKER: ClassVar[str] = "[DATOS SINTÉTICOS DE PRUEBA TÉCNICA]"

    @classmethod
    def _build_zoning_response_from_row(
        cls,
        conn: psycopg.Connection[Any],
        zrow: dict[str, Any],
        project: dict[str, Any],
        *,
        duplicated: bool = False,
        extra_evidence: list[dict[str, Any]] | None = None,
        environmental_risk_notice: str | None = None,
    ) -> dict[str, Any]:
        compatible = zrow.get("compatible")
        status_val = zrow["status"]
        limitations = zrow["limitations"]
        amendments, _ = cls._resolve_pdm_amendments_for_location(project.get("location"))
        if environmental_risk_notice is None:
            environmental_risk_notice = cls._collect_zoning_risk_notices(conn, project)

        is_synthetic_data = (
            cls._SYNTHETIC_ZONING_MARKER in str(limitations or "")
            or cls._SYNTHETIC_ZONING_MARKER in str(zrow.get("source_document") or "")
        )
        outside_cov = "fuera de la cobertura territorial" in str(limitations or "").lower()
        coverage_status = (
            "inside_coverage"
            if compatible is not None
            else ("outside_coverage" if outside_cov else "not_available")
        )
        provenance_mode = (
            "synthetic_rehearsal"
            if is_synthetic_data
            else ("verified_official" if compatible is not None else "unverified_abstention")
        )

        if compatible is True:
            status_label = (
                "Compatibilidad preliminar sustentada (SOLO PRUEBA TÉCNICA SINTÉTICA)"
                if is_synthetic_data
                else "Compatibilidad preliminar sustentada"
            )
            conclusion = (
                (f"{cls._SYNTHETIC_ZONING_MARKER} Resultado generado únicamente con polígono ficticio de ensayo técnico; NO acredita compatibilidad territorial real. " if is_synthetic_data else "")
                + f"Compatibilidad preliminar favorable (sujeta a conformidad municipal de GDU) "
                f"según {zrow.get('source_document')} ({zrow.get('source_version')}, {zrow.get('source_locator')})."
            )
            alerts: list[str] = [
                (
                    "ENSAYO SINTÉTICO (NO OFICIAL): demuestra el mecanismo espacial con polígono ficticio; no sustituye la fuente oficial de la GDU (MPH)."
                    if is_synthetic_data
                    else "Conclusión preliminar basada en fuente territorial oficialmente verificada; el certificado definitivo corresponde a la Gerencia de Desarrollo Urbano (MPH)."
                )
            ]
            flagged = False
        elif compatible is False:
            status_label = (
                "Posible incompatibilidad · Derivado a revisión jurídica (PRUEBA SINTÉTICA)"
                if is_synthetic_data
                else "Posible incompatibilidad · Derivado a revisión jurídica"
            )
            conclusion = (
                (f"{cls._SYNTHETIC_ZONING_MARKER} Observación generada únicamente con polígono ficticio de ensayo técnico. " if is_synthetic_data else "")
                + f"Posible incompatibilidad territorial detectada según "
                f"{zrow.get('source_document')} ({zrow.get('source_version')}, {zrow.get('source_locator')}). "
                "Requiere revisión humana del responsable jurídico; no certifica infracción automática."
            )
            alerts = [
                (
                    "ENSAYO SINTÉTICO (NO OFICIAL): posible incompatibilidad detectada en prueba técnica y señalada para revisión jurídica humana (sin certificar infracción)."
                    if is_synthetic_data
                    else "Posible incompatibilidad PDU/PDM registrada y señalada para revisión jurídica humana (sin certificar infracción automática)."
                )
            ]
            flagged = True
        else:
            status_label = (
                "Fuera de cobertura territorial · Información insuficiente"
                if outside_cov
                else "Requiere revisión / Información insuficiente"
            )
            conclusion = (
                "Información insuficiente por coordenadas fuera de la cobertura espacial de la capa zonal consultada; no se declara compatibilidad."
                if outside_cov
                else (
                    "No se declara compatibilidad ni incompatibilidad de zonificación: no existe capa GIS vectorial oficial "
                    "verificada ni verificación catastral frente a modificaciones parciales del PDM Huancayo."
                )
            )
            alerts = [
                "Las coordenadas del proyecto están fuera del ámbito cubierto por la fuente espacial consultada; requiere verificación municipal."
                if outside_cov
                else "No se puede concluir compatibilidad ni incompatibilidad sin capa GIS oficial verificada independientemente, sección normativa aplicable y validación de modificaciones parciales sobre el predio."
            ]
            if environmental_risk_notice:
                alerts.append(environmental_risk_notice)
            flagged = False

        evidence: list[dict[str, Any]] = []
        if zrow.get("source_document") and zrow.get("source_locator") and zrow.get("source_excerpt"):
            evidence.append({
                "document": zrow["source_document"],
                "version": zrow.get("source_version") or "2020-OM636",
                "locator": zrow["source_locator"],
                "excerpt": zrow["source_excerpt"],
                "is_spatial_classification": compatible is not None,
                "authority": (
                    "ENSAYO TÉCNICO SINTÉTICO (NO OFICIAL)" if is_synthetic_data else cls._PDM_AUTHORITY
                ),
                "official_url": None if is_synthetic_data else cls._PDM_OFFICIAL_URL,
                "in_force": None if is_synthetic_data else True,
                "data_origin": "simulated" if is_synthetic_data else "public",
            })
        if extra_evidence is None:
            extra_evidence = cls._fetch_pdm_document_evidence(conn)
        seen_locs = {e["locator"] for e in evidence}
        for doc_ev in extra_evidence:
            if doc_ev["locator"] not in seen_locs:
                evidence.append(doc_ev)
                seen_locs.add(doc_ev["locator"])

        return {
            "review_id": zrow["id"],
            "project_id": project["project_id"],
            "project_version_id": project["project_version_id"],
            "status": status_val,
            "status_label": status_label,
            "compatible": compatible,
            "preliminary_conclusion": conclusion,
            "flagged_for_legal_review": flagged,
            "location": project.get("location") or "",
            "proposed_land_use": project.get("proposed_land_use") or "",
            "territorial_data_origin": project.get("territorial_data_origin") or "declared",
            "coverage_status": coverage_status,
            "is_synthetic_data": is_synthetic_data,
            "provenance_mode": provenance_mode,
            "base_ordinance_url": cls._PDM_OFFICIAL_URL,
            "normative_amendments": amendments,
            "environmental_risk_notice": environmental_risk_notice,
            "evidence": evidence,
            "alerts": alerts,
            "limitations": limitations,
            "requires_human_review": True,
            "duplicated": duplicated,
        }

    def _get_latest_zoning_precheck_with_conn(
        self, conn: psycopg.Connection[Any], project: dict[str, Any]
    ) -> dict[str, Any] | None:
        zrow = conn.execute(
            """
            SELECT id, project_version_id, status, compatible,
                   source_document, source_version, source_locator, source_excerpt, limitations
            FROM zoning_review_requests
            WHERE project_version_id = %s
            ORDER BY requested_at DESC
            LIMIT 1
            """,
            (project["project_version_id"],),
        ).fetchone()
        if not zrow:
            return None
        return self._build_zoning_response_from_row(conn, dict(zrow), project, duplicated=False)

    def get_zoning_precheck(
        self, project_id: UUID, version_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """Consulta en solo lectura el último precheck de zonificación para la versión vigente o indicada (HU1.11)."""
        project = self.get_project(project_id)
        target_vid = version_id or project["project_version_id"]
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            if version_id is not None:
                self._verify_version_belongs_to_project(conn, project_id, target_vid)
                if target_vid != project["project_version_id"]:
                    pv = conn.execute(
                        """
                        SELECT location_description AS location, proposed_land_use, territorial_data_origin
                        FROM project_versions WHERE id = %s
                        """,
                        (target_vid,),
                    ).fetchone()
                    project = {
                        **project,
                        "project_version_id": target_vid,
                        "location": pv["location"] if pv else project.get("location"),
                        "proposed_land_use": pv["proposed_land_use"] if pv else project.get("proposed_land_use"),
                        "territorial_data_origin": (
                            pv["territorial_data_origin"] if pv else project.get("territorial_data_origin")
                        ),
                    }
            return self._get_latest_zoning_precheck_with_conn(conn, project)

    def _evaluate_zoning_evidence_with_conn(
        self, conn: psycopg.Connection[Any], project: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Evalúa compatibilidad PDU/PDM con compuerta estricta de procedencia oficial verificable (HU1.11):
        1. Ninguna geometría cargada por un usuario con data_origin='public' o texto libre se acepta como oficial.
        2. Límites administrativos (INEI) jamás clasifican usos de suelo predial.
        3. Modificaciones parciales aplicables al ámbito/predio impiden dictamen si no puede descartarse su alcance.
        4. Umbrales no sustentados en secciones oficiales (como 50 m de distancia a agua) y abreviaturas sin Índice
           de Usos oficial generan aviso de riesgo o revisión humana sin certificar compatibilidad ni incompatibilidad.
        """
        pv_id = project["project_version_id"]
        doc_evidence = self._fetch_pdm_document_evidence(conn)
        pdm_by_id = {ev["locator"].split(" · ", 1)[0]: ev for ev in doc_evidence}
        proposed_norm = self._normalize_use_text(project.get("proposed_land_use"))
        _, potentially_affected_mods = self._resolve_pdm_amendments_for_location(project.get("location"))
        risk_notice = self._collect_zoning_risk_notices(conn, project)

        lat = project.get("latitude")
        lon = project.get("longitude")
        coord_source = (project.get("coordinates_source") or "").strip()
        has_coords = lat is not None and lon is not None and len(coord_source) >= 3
        is_simulated_origin = (
            project.get("territorial_data_origin") == "simulated"
            or project.get("coordinates_origin") == "simulated"
        )

        # Revisar capas registradas en la versión vigente aplicando verificación criptográfica/institucional de servidor
        ignored_admin_boundary = False
        ignored_unverified_user_layer = False
        ignored_simulated_layer = False
        blocked_by_amendment_scope = False
        blocked_by_missing_rule_section = False
        blocked_by_pending_risk = False
        outside_official_coverage = False
        outside_synthetic_coverage = False
        amendment_conflict_names: list[str] = []

        synthetic_meta: OfficialZoningSourceMetadata | None = None
        synthetic_sha256s: set[str] = set()
        if self.enable_synthetic_zoning_rehearsal:
            synthetic_meta, synthetic_fc = build_synthetic_rehearsal_zoning_package()
            ZoningSourceValidator.validate_package(synthetic_meta, synthetic_fc)
            synthetic_sha256s.add(ZoningSourceValidator.canonical_geojson_sha256(synthetic_fc))
            synthetic_sha256s.add(
                ZoningSourceValidator.canonical_geojson_sha256(synthetic_fc["features"][0]["geometry"])
            )

        allow_simulated_rehearsal = self.enable_synthetic_zoning_rehearsal
        if has_coords and (not is_simulated_origin or allow_simulated_rehearsal):
            layers = conn.execute(
                """
                SELECT id, layer_name, meaning, source_name, data_origin, geojson_data
                FROM project_geospatial_layers
                WHERE project_version_id = %s
                  AND layer_type = 'territorial_zone'
                  AND availability_status = 'available'
                  AND geojson_data IS NOT NULL
                ORDER BY created_at DESC
                """,
                (pv_id,),
            ).fetchall()

            for layer in layers:
                combined_meta = f"{layer['layer_name']} {layer['source_name']} {layer['meaning']}".lower()
                geo_sha256 = self._canonical_geojson_sha256(layer["geojson_data"])

                if geo_sha256 in self._VERIFIED_ADMIN_BOUNDARY_SHA256S or any(
                    tok in combined_meta for tok in ("ig_distrito", "inei", "limite administrativo", "limite distrital")
                ):
                    ignored_admin_boundary = True
                    continue

                is_synth_match = (
                    allow_simulated_rehearsal
                    and layer["data_origin"] == "simulated"
                    and synthetic_meta is not None
                    and geo_sha256 in synthetic_sha256s
                )
                if layer["data_origin"] in ("simulated", "declared") and not is_synth_match:
                    ignored_simulated_layer = True
                    continue

                # Compuerta de procedencia independiente en servidor:
                # Ni data_origin='public'/'official_cadastre' ni el nombre de una ordenanza escrito en source_name/layer_name
                # acreditan por sí solos autenticidad oficial; se exige coincidencia verificada en el registro institucional del servidor.
                if is_synth_match and synthetic_meta is not None:
                    matched_props = ZoningSourceValidator.lookup_point(
                        layer["geojson_data"], float(lon), float(lat)
                    )
                    if matched_props is None:
                        outside_synthetic_coverage = True
                        continue
                    zone_code_syn = str(matched_props.get("zone_code") or "ZRP").strip().upper()
                    verified_entry = {
                        "zone_code": zone_code_syn,
                        "effective_date": synthetic_meta.effective_date,
                        "official_source_name": f"{self._SYNTHETIC_ZONING_MARKER} {synthetic_meta.document_title}",
                        "source_document": f"{self._SYNTHETIC_ZONING_MARKER} {synthetic_meta.document_title}",
                        "source_version": synthetic_meta.version,
                        "is_synthetic": True,
                    }
                else:
                    verified_entry = self._VERIFIED_OFFICIAL_ZONING_LAYERS.get(geo_sha256)
                    if not verified_entry:
                        ignored_unverified_user_layer = True
                        continue
                    if self._match_geojson_feature(layer["geojson_data"], float(lon), float(lat)) is None:
                        outside_official_coverage = True
                        continue

                entry_is_synthetic = bool(verified_entry.get("is_synthetic"))

                # Verificar si existen modificaciones parciales posteriores cuyo ámbito/predio pueda afectar esta ubicación
                layer_cutoff = str(verified_entry.get("effective_date") or "2020-05-08")
                unresolved_mods = [
                    m for m in potentially_affected_mods if m["publication_date"] > layer_cutoff
                ]
                if unresolved_mods:
                    blocked_by_amendment_scope = True
                    amendment_conflict_names = [m["ordinance"] for m in unresolved_mods]
                    continue

                zone_code = str(verified_entry.get("zone_code") or "").strip().upper()
                rule_outcome = self._evaluate_verified_zone_against_pdm_sections(
                    zone_code=zone_code,
                    proposed_norm=proposed_norm,
                    pdm_by_id=pdm_by_id,
                    verified_entry=verified_entry,
                )
                if rule_outcome is None:
                    blocked_by_missing_rule_section = True
                    continue

                section_ref = rule_outcome["section_ref"]
                official_doc = str(verified_entry.get("source_document") or section_ref["document"])
                official_ver = str(verified_entry.get("source_version") or section_ref["version"])
                synth_prefix = f"{self._SYNTHETIC_ZONING_MARKER} " if entry_is_synthetic else ""
                if rule_outcome["compatible"] is False:
                    return {
                        "status": "evidence_available",
                        "compatible": False,
                        "source_document": official_doc,
                        "source_version": official_ver,
                        "source_locator": f"Zona {zone_code} · {section_ref['locator']}",
                        "source_excerpt": (
                            f"{synth_prefix}Coordenadas ({lat:.5f}, {lon:.5f}) dentro de zona '{zone_code}' ({verified_entry['official_source_name']}); "
                            f"uso propuesto '{project['proposed_land_use']}' presenta posible incompatibilidad con {rule_outcome['section_id']}. "
                            + section_ref["excerpt"]
                        )[:500],
                        "limitations": (
                            f"{synth_prefix}Posible incompatibilidad detectada sobre capa {'sintética de ensayo técnico (NO OFICIAL)' if entry_is_synthetic else 'verificada'} "
                            f"({verified_entry['official_source_name']}, zona '{zone_code}'). "
                            "Señalada para revisión jurídica humana; no certifica automáticamente una infracción."
                        ),
                        "reason": rule_outcome["reason"],
                        "extra_evidence": doc_evidence[:2],
                        "environmental_risk_notice": risk_notice,
                    }

                if rule_outcome["compatible"] is True and risk_notice:
                    # Riesgo hídrico/ambiental pendiente de verificación ANA/SINAGERD impide declarar compatibilidad favorable
                    blocked_by_pending_risk = True
                    continue

                return {
                    "status": "evidence_available",
                    "compatible": True,
                    "source_document": official_doc,
                    "source_version": official_ver,
                    "source_locator": f"{section_ref['locator']} · Zona {zone_code}",
                    "source_excerpt": (
                        f"{synth_prefix}Coordenadas ({lat:.5f}, {lon:.5f}) dentro de zona '{zone_code}' ({verified_entry['official_source_name']}); "
                        f"compatible preliminarmente con '{project['proposed_land_use']}' según {rule_outcome['section_id']}. "
                        + section_ref["excerpt"]
                    )[:500],
                    "limitations": (
                        f"{synth_prefix}Compatibilidad preliminar sustentada con polígono sintético de prueba técnica (NO OFICIAL ni acredita uso de suelo real)."
                        if entry_is_synthetic
                        else (
                            "Compatibilidad preliminar sustentada con capa institucional verificada y O.M. N.° 636-2020-MPH/OM; "
                            "sujeta a verificación predial y certificado definitivo de la Gerencia de Desarrollo Urbano (MPH)."
                        )
                    ),
                    "reason": rule_outcome["reason"],
                    "extra_evidence": doc_evidence[:2],
                    "environmental_risk_notice": risk_notice,
                }

        # Abstención estricta (CA-HU1.11-03): no emitir compatibilidad ni incompatibilidad si falta procedencia verificada,
        # delimitación frente a modificaciones parciales, cobertura espacial o regla normativa sustentada en sección oficial.
        gap_reasons: list[str] = []
        if not has_coords:
            gap_reasons.append("el expediente no cuenta con coordenadas geográficas verificables del predio")
        if is_simulated_origin and not allow_simulated_rehearsal:
            gap_reasons.append("los datos territoriales del expediente tienen origen simulado")
        if outside_official_coverage or outside_synthetic_coverage:
            gap_reasons.append(
                f"las coordenadas ({lat}, {lon}) se encuentran fuera de la cobertura territorial de la capa zonal consultada"
            )
        if ignored_admin_boundary:
            gap_reasons.append(
                "el polígono cargado corresponde solo al límite administrativo distrital (INEI ig_distrito UBIGEO 120114), no a zonificación predial"
            )
        if ignored_simulated_layer:
            gap_reasons.append("las capas zonales registradas tienen origen simulado o meramente declarado")
        if ignored_unverified_user_layer:
            gap_reasons.append(
                "la geometría cargada por el usuario carece de verificación oficial independiente (indicar data_origin='public' o escribir el nombre de una ordenanza en source_name/layer_name no acredita autenticidad cartográfica)"
            )
        if blocked_by_amendment_scope:
            gap_reasons.append(
                "no puede determinarse si modificaciones parciales posteriores a O.M. N.° 636-2020-MPH/OM "
                f"({', '.join(amendment_conflict_names)}) alcanzan al predio exacto evaluado"
            )
        elif potentially_affected_mods:
            names = ", ".join(m["ordinance"] for m in potentially_affected_mods[:4])
            gap_reasons.append(
                f"la ubicación mencionada podría coincidir con modificaciones parciales por predio/tramo del PDM ({names}), "
                "cuyo perímetro exacto requiere cotejo catastral municipal sin afectar al resto del distrito"
            )
        if blocked_by_pending_risk:
            gap_reasons.append(
                "existen alertas ambientales o de proximidad hídrica pendientes de verificación técnica (sin certificar compatibilidad)"
            )
        if blocked_by_missing_rule_section:
            gap_reasons.append(
                "la clasificación zonal detectada carece de Índice de Usos oficial parametrizado en repositorio para esa categoría"
            )
        if not doc_evidence:
            gap_reasons.append("no existen fragmentos vigentes de PDU/PDM en normative_documents")
        if not self._VERIFIED_OFFICIAL_ZONING_LAYERS and not outside_synthetic_coverage:
            gap_reasons.append(
                "no se dispone de una capa GIS vectorial oficial verificada de polígonos de zonificación del PDM Huancayo "
                "(los planos publicados en gob.pe por la MPH se encuentran en PDF)"
            )

        synth_lim_prefix = f"{self._SYNTHETIC_ZONING_MARKER} " if outside_synthetic_coverage else ""
        limitations = (
            synth_lim_prefix
            + "Información insuficiente para declarar compatibilidad ni incompatibilidad espacial de zonificación: "
            + "; ".join(gap_reasons)
            + ". La norma base vigente es O.M. N.° 636-2020-MPH/OM (no desactivada globalmente), complementada por "
            "modificaciones parciales por ámbito o predio específico registradas con enlace verificable. "
            + (f"Aviso de riesgo territorial: {risk_notice} " if risk_notice else "")
            + "La solicitud no equivale a conformidad territorial ni certifica infracción."
        )
        return {
            "status": "requires_review",
            "compatible": None,
            "source_document": None,
            "source_version": None,
            "source_locator": None,
            "source_excerpt": None,
            "limitations": limitations,
            "reason": (
                "outside_spatial_zoning_coverage"
                if (outside_official_coverage or outside_synthetic_coverage)
                else "missing_verifiable_spatial_zoning_layer"
            ),
            "extra_evidence": doc_evidence[:2],
            "environmental_risk_notice": risk_notice,
        }

    def request_zoning_precheck(
        self, project_id: UUID, actor_id: UUID, idempotency_key: str
    ) -> dict[str, Any]:
        """Evalúa o abstiene compatibilidad PDU/PDM con sustento territorial versionado (HU1.11)."""

        if not idempotency_key.strip():
            raise ConfiguracionPMV1Error("Idempotency-Key es obligatorio")
        project = self.get_project(project_id)
        missing = [
            field
            for field in ("location", "proposed_land_use")
            if not project.get(field)
            or (isinstance(project.get(field), str) and not project[field].strip())
        ]
        if missing:
            raise ExpedientePMV1IncompletoError(missing)

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                previous = conn.execute(
                    """
                    SELECT id, project_version_id, status, compatible,
                           source_document, source_version, source_locator, source_excerpt, limitations
                    FROM zoning_review_requests WHERE idempotency_key=%s
                    """,
                    (idempotency_key,),
                ).fetchone()
                if previous:
                    if previous["project_version_id"] != project["project_version_id"]:
                        raise ConfiguracionPMV1Error(
                            "Idempotency-Key ya fue utilizada para otro expediente"
                        )
                    return self._build_zoning_response_from_row(
                        conn, dict(previous), project, duplicated=True
                    )

                eval_res = self._evaluate_zoning_evidence_with_conn(conn, project)
                review_id = uuid4()
                conn.execute(
                    """
                    INSERT INTO zoning_review_requests
                    (id, project_version_id, requested_by_user_id, idempotency_key,
                     status, compatible, source_document, source_version,
                     source_locator, source_excerpt, limitations)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        review_id,
                        project["project_version_id"],
                        actor_id,
                        idempotency_key,
                        eval_res["status"],
                        eval_res["compatible"],
                        eval_res["source_document"],
                        eval_res["source_version"],
                        eval_res["source_locator"],
                        eval_res["source_excerpt"],
                        eval_res["limitations"],
                    ),
                )
                audit_action = (
                    "ZONING_INCOMPATIBILITY_FLAGGED"
                    if eval_res["compatible"] is False
                    else "ZONING_REVIEW_REQUESTED"
                )
                conn.execute(
                    """INSERT INTO audit_events
                    (actor_user_id,action,entity_type,entity_id,details)
                    VALUES (%s,%s,'zoning_review',%s,%s)""",
                    (
                        actor_id,
                        audit_action,
                        review_id,
                        Jsonb(
                            {
                                "project_id": str(project_id),
                                "project_version_id": str(
                                    project["project_version_id"]
                                ),
                                "status": eval_res["status"],
                                "compatible": eval_res["compatible"],
                                "source_document": eval_res["source_document"],
                                "source_locator": eval_res["source_locator"],
                                "reason": eval_res["reason"],
                            }
                        ),
                    ),
                )
                inserted_row = {
                    "id": review_id,
                    "project_version_id": project["project_version_id"],
                    "status": eval_res["status"],
                    "compatible": eval_res["compatible"],
                    "source_document": eval_res["source_document"],
                    "source_version": eval_res["source_version"],
                    "source_locator": eval_res["source_locator"],
                    "source_excerpt": eval_res["source_excerpt"],
                    "limitations": eval_res["limitations"],
                }
                return self._build_zoning_response_from_row(
                    conn,
                    inserted_row,
                    project,
                    duplicated=False,
                    extra_evidence=eval_res.get("extra_evidence"),
                    environmental_risk_notice=eval_res.get("environmental_risk_notice"),
                )



    @staticmethod
    def _serialize_normative_chunk(item: dict[str, Any]) -> dict[str, Any]:
        eff = item.get("effective_from")
        con = item.get("consulted_at")
        return {
            "id": str(item["id"]),
            "document_name": str(item.get("document_name") or ""),
            "short_code": str(item.get("short_code") or ""),
            "version": str(item.get("version") or ""),
            "topic": str(item.get("topic") or ""),
            "content": str(item.get("content") or ""),
            "in_force": bool(item.get("in_force", False)),
            "has_alert": bool(item.get("has_alert", False)),
            "data_origin": str(item.get("data_origin") or "simulated"),
            "relevance_score": float(item.get("relevance_score", 0.0)),
            "location": item.get("location"),
            "official_url": item.get("official_url"),
            "consulted_at": con.isoformat() if hasattr(con, "isoformat") else (str(con) if con else None),
            "amendments_summary": item.get("amendments_summary"),
            "effective_from": eff.isoformat() if hasattr(eff, "isoformat") else (str(eff) if eff else None),
        }

    @staticmethod
    def _is_superseded_or_inactive_chunk(item: dict[str, Any]) -> bool:
        if not bool(item.get("in_force", False)):
            return True
        cid = str(item.get("id") or "")
        ver = str(item.get("version") or "").lower()
        if cid.startswith("LEY32069-") and ("2024-v1" in ver or "histor" in ver):
            return True
        return False

    def search_normatives(
        self,
        query: str,
        actor_id: UUID,
        document_filter: str | None = None,
        only_in_force: bool = True,
        limit: int = 5,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Busca automaticamente fragmentos normativos versionados para el Asesor Juridico (HU2.1)."""
        cleaned_query = query.strip()
        if len(cleaned_query) < 2:
            raise ConfiguracionPMV1Error("La consulta debe tener al menos 2 caracteres")

        search_id = uuid4()
        is_demo_fallback = False
        historical_excluded: list[dict[str, Any]] = []

        def _partition_and_score(candidate_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            keywords = NormativeSearchEngine.extract_keywords(cleaned_query)
            active_scored: list[dict[str, Any]] = []
            hist_scored: list[dict[str, Any]] = []
            for raw in candidate_rows:
                item = dict(raw)
                is_hist = self._is_superseded_or_inactive_chunk(item)
                if only_in_force and is_hist:
                    score_h = NormativeSearchEngine.score_chunk(item, cleaned_query, keywords)
                    if score_h > 0 and NormativeSearchEngine.is_relevant(item, cleaned_query, keywords):
                        item["relevance_score"] = score_h
                        item["location"] = NormativeSearchEngine.resolve_location(item)
                        item["in_force"] = False
                        hist_scored.append(self._serialize_normative_chunk(item))
                    continue

                score = NormativeSearchEngine.score_chunk(item, cleaned_query, keywords)
                if score > 0 and NormativeSearchEngine.is_relevant(item, cleaned_query, keywords):
                    item["relevance_score"] = score
                    item["location"] = NormativeSearchEngine.resolve_location(item)
                    active_scored.append(self._serialize_normative_chunk(item))

            active_scored.sort(key=lambda x: (x["relevance_score"], x["has_alert"]), reverse=True)
            hist_scored.sort(key=lambda x: x["relevance_score"], reverse=True)
            return active_scored[:limit], hist_scored[:limit]

        try:
            if not _is_db_available(self.db_url):
                raise psycopg.OperationalError("Database unreachable")
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                duplicated = False
                if idempotency_key and idempotency_key.strip():
                    prev = conn.execute(
                        "SELECT id, query FROM normative_search_logs WHERE idempotency_key = %s",
                        (idempotency_key.strip(),),
                    ).fetchone()
                    if prev:
                        if prev["query"].strip().lower() != cleaned_query.lower():
                            raise ConfiguracionPMV1Error(
                                "Idempotency-Key ya fue utilizada para otra consulta"
                            )
                        duplicated = True

                sql = (
                    "SELECT id, document_name, short_code, version, topic, content, "
                    "in_force, has_alert, data_origin, official_url, consulted_at, "
                    "amendments_summary, effective_from FROM normative_documents WHERE 1=1"
                )
                params: list[Any] = []
                if document_filter and document_filter.strip():
                    f = f"%{document_filter.strip()}%"
                    sql += " AND (document_name ILIKE %s OR short_code ILIKE %s)"
                    params.extend([f, f])

                rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
                results, historical_excluded = _partition_and_score(rows)

                alerts_found = []
                for r in results:
                    if r.get("has_alert"):
                        alerts_found.append(
                            f"Alerta legal detectada en {r['id']} ({r['topic']}): {r['content']}"
                        )

                if not duplicated:
                    conn.execute(
                        """INSERT INTO normative_search_logs
                        (id, actor_user_id, query, document_filter, results_count, idempotency_key)
                        VALUES (%s, %s, %s, %s, %s, %s)""",
                        (
                            search_id,
                            actor_id,
                            cleaned_query,
                            document_filter.strip() if document_filter else None,
                            len(results),
                            idempotency_key.strip() if idempotency_key else None,
                        ),
                    )
                    conn.execute(
                        """INSERT INTO audit_events
                        (actor_user_id, action, entity_type, entity_id, details)
                        VALUES (%s, 'NORMATIVE_SEARCH_PERFORMED', 'normative_search', %s, %s)""",
                        (
                            actor_id,
                            search_id,
                            Jsonb(
                                {
                                    "query": cleaned_query,
                                    "document_filter": document_filter.strip()
                                    if document_filter
                                    else None,
                                    "only_in_force": only_in_force,
                                    "results_count": len(results),
                                    "historical_excluded_count": len(historical_excluded),
                                    "alerts_count": len(alerts_found),
                                }
                            ),
                        ),
                    )
        except psycopg.OperationalError as err:
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de normativa no disponible: la base de datos no responde."
                ) from err
            is_demo_fallback = True
            fallback_rows = _load_fallback_seed_documents()
            if document_filter and document_filter.strip():
                df_lower = document_filter.strip().lower()
                fallback_rows = [
                    r for r in fallback_rows
                    if df_lower in r["document_name"].lower() or df_lower in r["short_code"].lower()
                ]
            results, historical_excluded = _partition_and_score(fallback_rows)

            alerts_found = []
            for r in results:
                if r.get("has_alert"):
                    alerts_found.append(
                        f"Alerta legal detectada en {r['id']} ({r['topic']}): {r['content']}"
                    )
            duplicated = False

        if is_demo_fallback:
            disclaimer = (
                "MODO DEMOSTRACIÓN (Base de datos no disponible): resultados obtenidos desde el corpus semilla "
                "local de demostración; NO representan una consulta vigente en base de datos ni emiten dictamen jurídico."
            )
        else:
            disclaimer = (
                "Consulta ejecutada sobre el corpus normativo indexado en base de datos. "
                "La búsqueda automática asiste al Asesor Jurídico mostrando norma, versión y fragmento verificable, "
                "sin emitir dictámenes vinculantes ni sustituir su análisis profesional."
            )

        return {
            "query": cleaned_query,
            "document_filter": document_filter.strip() if document_filter else None,
            "total_results": len(results),
            "results": results,
            "historical_references_excluded": historical_excluded,
            "alerts_found": alerts_found,
            "disclaimer": disclaimer,
            "requires_human_review": bool(alerts_found),
            "duplicated": duplicated,
            "is_demo_fallback": is_demo_fallback,
        }

    def list_normative_documents(self) -> list[dict[str, Any]]:
        try:
            if not _is_db_available(self.db_url):
                raise psycopg.OperationalError("Database unreachable")
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                rows = conn.execute(
                    """
                    SELECT short_code, document_name, version, in_force, data_origin,
                           COUNT(*)::int AS chunks_count,
                           MIN(official_url) AS official_url,
                           MAX(consulted_at)::text AS consulted_at,
                           MAX(effective_from)::text AS effective_from,
                           MAX(amendments_summary) AS amendments_summary
                    FROM normative_documents
                    GROUP BY short_code, document_name, version, in_force, data_origin
                    ORDER BY in_force DESC, short_code ASC, version DESC
                    """
                ).fetchall()
            return [dict(r) for r in rows]
        except psycopg.OperationalError as err:
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de normativa no disponible: la base de datos no responde."
                ) from err
            docs = _load_fallback_seed_documents()
            grouped: dict[tuple[str, str, str, bool, str], int] = {}
            for d in docs:
                k = (d["short_code"], d["document_name"], d["version"], d["in_force"], d["data_origin"])
                grouped[k] = grouped.get(k, 0) + 1
            res = []
            for (code, name, ver, in_f, origin), count in sorted(grouped.items(), key=lambda x: (x[0][0], x[0][2])):
                res.append({
                    "short_code": code,
                    "document_name": name,
                    "version": ver,
                    "in_force": in_f,
                    "data_origin": origin,
                    "chunks_count": count,
                    "official_url": None,
                    "consulted_at": None,
                    "effective_from": None,
                    "amendments_summary": "Modo demostración local (sin conexión a BD).",
                })
            return res

    def get_unified_assessment_summary(
        self, project_id: UUID, version_id: UUID | None = None
    ) -> dict[str, Any]:
        """Genera el resumen unificado de evaluacion multidimensional (HU1.12).

        Cumple estrictamente los criterios de aceptacion:
        - CA-HU1.12-01: Reune los resultados de las cinco dimensiones (economica, social, ambiental,
          tecnica y juridica) en una sola vista con la conclusion y justificacion de la version vigente.
        - CA-HU1.12-02: Expone la puntuacion global consolidada con sus puntuaciones de entrada, pesos
          y formula explicita y reproducible a partir de la version activa de criterios.
        - CA-HU1.12-03: Si alguna dimension sigue pendiente, fallo o carece de datos indispensables,
          identifica la dimension afectada y no presenta una evaluacion incompleta como resultado aprobado.
        - Rigor normativo: Respeta distinciones de datos simulados (HU1.8) y citas no verificables (HU2.2),
          y bloquea aprobacion si existen alertas legales activas (HU2.3) o checkpoints pausados (HU3.1/HU3.4).
        """
        project = self.get_project(project_id)
        current_version_id = version_id or project["project_version_id"]

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            # Obtener datos de la version consultada
            v_row = conn.execute(
                """
                SELECT id, version_number, title, description, location_description,
                       proposed_land_use, estimated_budget_pen, beneficiaries_count
                FROM project_versions
                WHERE id = %s AND project_id = %s
                """,
                (current_version_id, project_id),
            ).fetchone()

            if not v_row:
                raise RecursoPMV1NoEncontradoError("Version del expediente no encontrada")

            version_number = v_row["version_number"]
            dimensions: list[dict[str, Any]] = []
            missing_dimensions: list[str] = []
            blocking_reasons: list[str] = []

            # ---------------------------------------------------------
            # 1. Dimension Economica
            # ---------------------------------------------------------
            econ_row = conn.execute(
                """
                SELECT e.id AS evaluation_id, e.status, e.error_message, e.requested_at,
                       r.cost_per_beneficiary_pen, r.score_0_100, r.explanation, r.algorithm_version
                FROM evaluations e
                LEFT JOIN economic_result_projections r ON r.evaluation_id = e.id
                WHERE e.project_version_id = %s
                ORDER BY e.requested_at DESC
                LIMIT 1
                """,
                (current_version_id,),
            ).fetchone()

            if not econ_row:
                econ_status = "pending"
                econ_label = "Pendiente de evaluacion"
                econ_conclusion = "Evaluacion economica no ejecutada."
                econ_justification = "No se ha solicitado la proyeccion de costo por beneficiario para esta version del expediente."
                econ_score = None
                econ_eval_at = None
                econ_has_blocker = False
                econ_blocker_msg = None
                missing_dimensions.append("Economica")
            elif econ_row["status"] == "pending_review":
                econ_status = "blocked"
                econ_label = "Detenido en punto de control"
                econ_conclusion = "Evaluacion economica detenida por riesgo alto en punto de control HITL."
                econ_justification = econ_row.get("error_message") or "Detencion preventiva conforme a HU3.1."
                econ_score = None
                econ_eval_at = econ_row["requested_at"]
                econ_has_blocker = True
                econ_blocker_msg = "Pausa en punto de control"
                missing_dimensions.append("Economica (Detenida por HITL)")
                blocking_reasons.append("Dimension economica detenida por punto de control de riesgo alto.")
            elif econ_row["status"] in ("error", "failed"):
                econ_status = "failed"
                econ_label = "Evaluacion fallida"
                econ_conclusion = "La evaluacion economica genero un error."
                econ_justification = econ_row.get("error_message") or "Error en calculo de costo por beneficiario."
                econ_score = None
                econ_eval_at = econ_row["requested_at"]
                econ_has_blocker = True
                econ_blocker_msg = "La evaluacion economica fallo y debe reejecutarse."
                missing_dimensions.append("Economica (Fallida)")
                blocking_reasons.append("Dimension economica fallida: requiere correccion y nueva ejecucion (CA-HU1.12-03).")
            elif econ_row["status"] != "completed" or econ_row.get("score_0_100") is None:
                econ_status = "pending"
                econ_label = "En proceso" if econ_row["status"] in ("queued", "processing") else "Pendiente de cierre"
                econ_conclusion = "Evaluacion economica aun en curso o sin proyeccion de puntaje disponible."
                econ_justification = (
                    f"Estado actual de la evaluacion economica: '{econ_row['status']}'. "
                    "Aun no cuenta con puntaje consolidado en economic_result_projections."
                )
                econ_score = None
                econ_eval_at = econ_row["requested_at"]
                econ_has_blocker = False
                econ_blocker_msg = None
                missing_dimensions.append("Economica (Pendiente)")
            else:
                econ_score = float(econ_row["score_0_100"])
                cost_pen = econ_row.get("cost_per_beneficiary_pen")
                cost_str = f"S/. {cost_pen:.2f}" if cost_pen is not None else "No determinado"
                econ_status = "completed"
                econ_label = "Completada"
                econ_conclusion = (
                    f"Costo por beneficiario: {cost_str}. Puntuacion normalizada: {econ_score:.1f}/100."
                )
                econ_justification = econ_row.get("explanation") or "Proyeccion de costo por beneficiario conforme a umbrales vigentes."
                econ_eval_at = econ_row["requested_at"]
                econ_has_blocker = False
                econ_blocker_msg = None

            dimensions.append({
                "dimension_code": "economic",
                "dimension_name": "Economica",
                "status": econ_status,
                "status_label": econ_label,
                "conclusion": econ_conclusion,
                "justification": econ_justification,
                "score": econ_score,
                "is_simulated": False,
                "warning": None,
                "has_blocker": econ_has_blocker,
                "blocker_message": econ_blocker_msg,
                "evaluated_at": econ_eval_at,
                "details": {
                    "cost_per_beneficiary_pen": float(econ_row["cost_per_beneficiary_pen"])
                    if econ_row and econ_row.get("cost_per_beneficiary_pen") is not None
                    else None
                },
            })

            # ---------------------------------------------------------
            # 2. Dimension Social
            # ---------------------------------------------------------
            soc_row = conn.execute(
                """
                SELECT id, status, summary, results_json, equity_json, social_score, score_explanation, evaluated_at
                FROM social_accessibility_assessments
                WHERE project_version_id = %s
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (current_version_id,),
            ).fetchone()

            if not soc_row:
                soc_status = "pending"
                soc_label = "Pendiente de evaluacion"
                soc_conclusion = "Evaluacion de accesibilidad social no ejecutada."
                soc_justification = "No se registran indicadores de proximidad a servicios sociales (salud, educacion, transporte) para esta version."
                soc_score = None
                soc_eval_at = None
                soc_simulated = False
                soc_warning = None
                soc_has_blocker = False
                soc_blocker_msg = None
                missing_dimensions.append("Social")
            else:
                soc_score = float(soc_row["social_score"]) if soc_row.get("social_score") is not None else None
                soc_simulated = bool(
                    (
                        soc_row.get("results_json")
                        and any(r.get("is_simulated") for r in soc_row["results_json"] if isinstance(r, dict))
                    )
                    or (isinstance(soc_row.get("equity_json"), dict) and soc_row["equity_json"].get("is_simulated"))
                )
                soc_warning = (
                    "Datos de accesibilidad calculados a partir de fuentes simuladas (RENAES/MINEDU)"
                    if soc_simulated
                    else None
                )
                raw_soc_status = str(soc_row.get("status") or "").strip().lower()
                soc_conclusion = soc_row.get("summary") or "Evaluacion social registrada."
                soc_justification = (
                    soc_row.get("score_explanation")
                    or soc_row.get("summary")
                    or "Analisis de cobertura y equidad social territorial."
                )
                soc_eval_at = soc_row["evaluated_at"]

                # SocialAccessibilityEngine guarda 'complete'; registros historicos pueden usar 'completed'/'verified'/'evaluated'.
                if raw_soc_status in ("complete", "verified", "evaluated", "completed") and soc_score is not None:
                    soc_status = "completed"
                    soc_label = "Completada"
                    soc_has_blocker = False
                    soc_blocker_msg = None
                elif raw_soc_status == "insufficient" or soc_score is None:
                    soc_status = "failed"
                    soc_label = "Informacion insuficiente"
                    soc_has_blocker = True
                    soc_blocker_msg = "Datos de accesibilidad o equidad social insuficientes (HU1.4/HU1.5)."
                    soc_warning = soc_warning or "Falta cobertura social minima para validar el expediente."
                    missing_dimensions.append("Social (Informacion insuficiente)")
                    blocking_reasons.append(f"Dimension social con informacion insuficiente: {soc_conclusion}")
                else:
                    soc_status = "warning"
                    soc_label = "Parcial / Requiere verificacion" if raw_soc_status == "partial" else "Requiere revision"
                    soc_has_blocker = True
                    soc_blocker_msg = "Dimension social incompleta o parcial: faltan indicadores indispensables de cobertura o equidad."
                    soc_warning = soc_warning or "Evaluacion social parcial: no acredita cobertura completa en todas las categorias (HU1.4/HU1.5)."
                    missing_dimensions.append("Social (Parcial / Incompleta)")
                    blocking_reasons.append(
                        f"Dimension social parcial o incompleta ({raw_soc_status or 'sin estado'}): {soc_conclusion}"
                    )

            dimensions.append({
                "dimension_code": "social",
                "dimension_name": "Social",
                "status": soc_status,
                "status_label": soc_label,
                "conclusion": soc_conclusion,
                "justification": soc_justification,
                "score": soc_score,
                "is_simulated": soc_simulated,
                "warning": soc_warning,
                "has_blocker": soc_has_blocker,
                "blocker_message": soc_blocker_msg,
                "evaluated_at": soc_eval_at,
                "details": {"social_score": soc_score},
            })

            # ---------------------------------------------------------
            # 3. Dimension Ambiental
            # ---------------------------------------------------------
            env_row = conn.execute(
                """
                SELECT id, status, risk_level, risk_observation, requires_human_review, favorable,
                       summary, declared_vs_evidence_json, impacts_json, green_area_json, environmental_score, evaluated_at
                FROM environmental_assessments
                WHERE project_version_id = %s
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (current_version_id,),
            ).fetchone()

            if not env_row:
                env_status = "pending"
                env_label = "Pendiente de evaluacion"
                env_conclusion = "Evaluacion de impacto ambiental territorial no ejecutada."
                env_justification = "No se registran antecedentes ambientales ni verificacion de faja marginal hidrica para esta version."
                env_score = None
                env_eval_at = None
                env_has_blocker = False
                env_blocker_msg = None
                env_simulated = False
                env_warning = None
                missing_dimensions.append("Ambiental")
            else:
                env_score = float(env_row["environmental_score"]) if env_row.get("environmental_score") is not None else None
                risk = env_row.get("risk_level", "low")
                req_rev = env_row.get("requires_human_review", False)
                env_eval_at = env_row["evaluated_at"]
                green_json = env_row.get("green_area_json")
                green_insufficient = isinstance(green_json, dict) and (green_json.get("has_sufficient_data") is False)

                env_simulated = bool(
                    (env_row.get("declared_vs_evidence_json") and env_row["declared_vs_evidence_json"].get("is_simulated"))
                    or (green_json and green_json.get("is_simulated"))
                )
                env_warning = "Datos ambientales calculados a partir de capas cartograficas simuladas" if env_simulated else None

                has_pending_cp = RiskCheckpointEngine.has_pending_checkpoint(conn, current_version_id)
                env_has_blocker = (risk == "high" and req_rev and has_pending_cp)

                if risk == "insufficient" or env_row.get("status") == "insufficient":
                    env_status = "failed"
                    env_label = "Informacion insuficiente"
                    env_conclusion = "Informacion ambiental insuficiente para dictaminar."
                    env_justification = env_row.get("risk_observation") or "Faltan datos cartograficos o declarados indispensables."
                    env_has_blocker = True
                    env_blocker_msg = "Datos ambientales indispensables no provistos."
                    missing_dimensions.append("Ambiental (Informacion insuficiente)")
                    blocking_reasons.append(
                        f"Dimension ambiental con informacion insuficiente (CA-HU1.12-03): {env_justification}"
                    )
                elif env_has_blocker:
                    env_status = "blocked"
                    env_label = "Alerta de Riesgo Alto (HITL)"
                    env_conclusion = f"Riesgo ambiental territorial ALTO detectado. {env_row.get('summary', '')}"
                    env_justification = env_row.get("risk_observation") or "Requiere autorizacion y levantamiento de pausa en punto de control (HU3.1/HU3.4)."
                    env_blocker_msg = "Riesgo ambiental territorial alto activo sin resolver (HU3.1)."
                    blocking_reasons.append(f"Dimension ambiental con riesgo alto pendiente de revision humana: {env_justification}")
                elif env_score is None or green_insufficient:
                    env_status = "failed"
                    env_label = "Informacion insuficiente"
                    env_conclusion = (
                        f"Nivel de riesgo ambiental: {risk.upper()}, pero faltan datos o puntaje indispensable "
                        "de areas verdes/conservacion (HU1.7)."
                    )
                    env_justification = (
                        env_row.get("risk_observation")
                        or env_row.get("summary")
                        or "Faltan datos indispensables para calcular el puntaje ambiental."
                    )
                    env_has_blocker = True
                    env_blocker_msg = "Faltan datos o puntaje ambiental indispensable (HU1.7/CA-HU1.12-03)."
                    missing_dimensions.append("Ambiental (Sin datos o puntaje de conservacion)")
                    blocking_reasons.append(
                        "Dimension ambiental sin datos suficientes de conservacion/areas verdes o sin puntaje cuantitativo (HU1.7/CA-HU1.12-03)."
                    )
                else:
                    env_status = "completed" if risk in ("low", "moderate") or not has_pending_cp else "warning"
                    env_label = "Completada / Favorable" if env_row.get("favorable") else f"Completada ({risk.capitalize()})"
                    env_conclusion = f"Nivel de riesgo ambiental: {risk.upper()}. {env_row.get('summary', '')}"
                    env_justification = env_row.get("risk_observation") or env_row.get("summary") or "Impactos ambientales y medidas de mitigacion verificados."
                    env_blocker_msg = None

            dimensions.append({
                "dimension_code": "environmental",
                "dimension_name": "Ambiental",
                "status": env_status,
                "status_label": env_label,
                "conclusion": env_conclusion,
                "justification": env_justification,
                "score": env_score,
                "is_simulated": env_simulated,
                "warning": env_warning,
                "has_blocker": env_has_blocker,
                "blocker_message": env_blocker_msg,
                "evaluated_at": env_eval_at,
                "details": {"risk_level": env_row.get("risk_level") if env_row else None},
            })

            # ---------------------------------------------------------
            # 4. Dimension Tecnica (HU1.8)
            # ---------------------------------------------------------
            tech_row = conn.execute(
                """
                SELECT id, status, overall_result, overall_result_label, is_viable,
                       technical_score, score_explanation, dimension_conclusion, dimension_justification,
                       findings_json, criteria_summary, evaluated_at
                FROM technical_feasibility_assessments
                WHERE project_version_id = %s
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (current_version_id,),
            ).fetchone()

            if not tech_row:
                tech_status = "pending"
                tech_label = "Pendiente de evaluacion"
                tech_conclusion = "Evaluacion de viabilidad tecnica no ejecutada."
                tech_justification = "No se ha procesado el analisis de diseno geometrico, pendientes ni capacidad portante (HU1.8)."
                tech_score = None
                tech_eval_at = None
                tech_simulated = False
                tech_warning = None
                tech_has_blocker = False
                tech_blocker_msg = None
                missing_dimensions.append("Tecnica")
            else:
                tech_score = float(tech_row["technical_score"]) if tech_row.get("technical_score") is not None else None
                tech_simulated = bool(
                    tech_row.get("findings_json")
                    and any(c.get("is_simulated") for c in tech_row["findings_json"] if isinstance(c, dict))
                )
                tech_warning = (
                    "Datos de viabilidad calculados a partir de expediente tecnico simulado (HU1.8); requiere verificacion en campo."
                    if tech_simulated
                    else None
                )
                overall_result = str(tech_row.get("overall_result") or "").strip().lower()
                tech_conclusion = tech_row["dimension_conclusion"]
                tech_justification = tech_row["dimension_justification"]
                tech_eval_at = tech_row["evaluated_at"]

                if overall_result == "cumple" and tech_score is not None:
                    tech_status = "completed"
                    tech_label = "Viable (Cumple)"
                    tech_has_blocker = False
                    tech_blocker_msg = None
                elif overall_result == "observado":
                    tech_status = "warning"
                    tech_label = "Observado"
                    tech_has_blocker = True
                    tech_blocker_msg = "Viabilidad tecnica observada: requiere subsanacion antes de aprobacion definitiva (HU1.8)."
                    missing_dimensions.append("Tecnica (Observado)")
                    blocking_reasons.append(
                        f"Dimension tecnica en estado 'observado' (no viable): {tech_conclusion}"
                    )
                elif overall_result == "informacion_insuficiente":
                    tech_status = "failed"
                    tech_label = "Informacion insuficiente"
                    tech_has_blocker = True
                    tech_blocker_msg = "Datos tecnicos indispensables no provistos (HU1.8)."
                    missing_dimensions.append("Tecnica (Informacion insuficiente)")
                    blocking_reasons.append(
                        f"Dimension tecnica con informacion insuficiente (CA-HU1.12-03): {tech_conclusion}"
                    )
                else:
                    tech_status = "failed"
                    tech_label = tech_row.get("overall_result_label") or "No viable"
                    tech_has_blocker = True
                    tech_blocker_msg = "Viabilidad tecnica no cumplida."
                    missing_dimensions.append(f"Tecnica ({tech_label})")
                    blocking_reasons.append(
                        f"Dimension tecnica no viable ({tech_label}): {tech_conclusion}"
                    )

            dimensions.append({
                "dimension_code": "technical",
                "dimension_name": "Tecnica",
                "status": tech_status,
                "status_label": tech_label,
                "conclusion": tech_conclusion,
                "justification": tech_justification,
                "score": tech_score,
                "is_simulated": tech_simulated,
                "warning": tech_warning,
                "has_blocker": tech_has_blocker,
                "blocker_message": tech_blocker_msg,
                "evaluated_at": tech_eval_at,
                "details": {"technical_score": tech_score},
            })

            # ---------------------------------------------------------
            # 5. Dimension Juridica (HU2.2 & HU2.3)
            # ---------------------------------------------------------
            legal_row = conn.execute(
                """
                SELECT id, status, summary, verifiable_citations_count, unverifiable_citations_count,
                       requires_human_review, has_legal_alert, legal_alert_motive, legal_alert_norm,
                       is_resolved, resolution_justification, evaluated_at, findings_json
                FROM legal_assessments
                WHERE project_id = %s AND project_version_id = %s
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (project_id, current_version_id),
            ).fetchone()

            if not legal_row:
                legal_status = "pending"
                legal_label = "Pendiente de evaluacion"
                legal_conclusion = "Evaluacion juridica y contraste normativo no ejecutada."
                legal_justification = "No se ha emitido dictamen normativo ni cotejo de citas juridicas para esta version."
                legal_eval_at = None
                legal_has_blocker = False
                legal_blocker_msg = None
                legal_warning = None
                missing_dimensions.append("Juridica")
            else:
                has_active_alert = bool(
                    (legal_row["status"] == "alerta_legal" or legal_row.get("has_legal_alert"))
                    and not legal_row.get("is_resolved", False)
                )
                unverifiable = legal_row.get("unverifiable_citations_count", 0)
                legal_eval_at = legal_row["evaluated_at"]
                legal_warning = None

                if has_active_alert:
                    legal_status = "blocked"
                    legal_label = "Alerta Legal Activa"
                    motive = legal_row.get("legal_alert_motive") or "Incompatibilidad territorial"
                    norm = legal_row.get("legal_alert_norm") or "Plan urbano"
                    legal_conclusion = f"ALERTA LEGAL (HU2.3): Incompatibilidad normativa/territorial con {norm}."
                    legal_justification = f"Motivo: {motive}. Conforme a HU2.3, se bloquea la aprobacion y priorizacion definitiva."
                    legal_has_blocker = True
                    legal_blocker_msg = f"Alerta Legal activa: {motive} [{norm}]."
                    blocking_reasons.append(f"Dimension juridica con Alerta Legal activa sin resolver (HU2.3): {motive}")
                elif unverifiable > 0 or legal_row.get("requires_human_review") or legal_row.get("status") != "verified":
                    legal_status = "warning"
                    legal_label = "Requiere revision humana"
                    legal_conclusion = legal_row.get("summary") or "Revision juridica con citas no comprobables o dictamen pendiente."
                    legal_justification = (
                        f"Se detectaron {unverifiable} cita(s) no verificable(s) (HU2.2); requiere cotejo manual por el Asesor Juridico."
                        if unverifiable > 0
                        else f"Evaluacion juridica en estado '{legal_row.get('status')}' pendiente de subsanacion o dictamen formal."
                    )
                    legal_has_blocker = True
                    legal_blocker_msg = "Dimension juridica requiere revision humana formal (HU2.2/HU1.12)."
                    legal_warning = "Contiene citas no comprobables contra normativa oficial o dictamen pendiente."
                    missing_dimensions.append("Juridica (Requiere revision humana)")
                    blocking_reasons.append(
                        f"Dimension juridica en estado '{legal_row.get('status')}' por citas normativas no verificables o dictamen legal pendiente (HU2.2/HU1.12)."
                    )
                else:
                    legal_status = "completed"
                    legal_label = "Citas normativas verificadas"
                    legal_conclusion = legal_row.get("summary") or "Citas juridicas verificadas."
                    legal_justification = f"Todas las citas ({legal_row.get('verifiable_citations_count', 0)}) fueron debidamente cotejadas contra el corpus normativo vigente."
                    legal_has_blocker = False
                    legal_blocker_msg = None
                    legal_warning = None

            # HU1.10: Si la verificación contractual vigente detectó posible incompatibilidad sin validación compatible de Asesoría Jurídica,
            # se bloquea la aprobación definitiva hasta que sea revisada o subsanada.
            proc_ver_row = conn.execute(
                """
                SELECT status, advisory_review_status, affected_requirement
                FROM procurement_verification_requests
                WHERE project_id = %s AND project_version_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (project_id, current_version_id),
            ).fetchone()
            if (
                proc_ver_row
                and proc_ver_row["status"] == "possible_incompatibility"
                and proc_ver_row["advisory_review_status"] in ("pending", "confirmed_incompatibility", "subsanacion_requerida")
            ):
                req_txt = proc_ver_row.get("affected_requirement") or "Ley N.° 32069"
                legal_has_blocker = True
                legal_blocker_msg = (
                    f"Posible incompatibilidad contractual (HU1.10) en '{req_txt}' "
                    f"(revisión jurídica: {proc_ver_row['advisory_review_status']})."
                )
                if legal_status == "completed":
                    legal_status = "warning"
                    legal_label = "Observación contractual pendiente (HU1.10)"
                    missing_dimensions.append("Juridica (Observacion contractual HU1.10)")
                blocking_reasons.append(legal_blocker_msg)

            dimensions.append({
                "dimension_code": "legal",
                "dimension_name": "Juridica",
                "status": legal_status,
                "status_label": legal_label,
                "conclusion": legal_conclusion,
                "justification": legal_justification,
                "score": None,  # Dimension juridica es cualitativa y de control normativo
                "is_simulated": False,
                "warning": legal_warning,
                "has_blocker": legal_has_blocker,
                "blocker_message": legal_blocker_msg,
                "evaluated_at": legal_eval_at,
                "details": {
                    "verifiable_citations_count": legal_row.get("verifiable_citations_count", 0) if legal_row else 0,
                    "unverifiable_citations_count": legal_row.get("unverifiable_citations_count", 0) if legal_row else 0,
                },
            })

            # Integrar estados observables de distribución (HU1.1) si alguna dimensión falló o sigue en proceso
            dispatch_rows = self._get_dimension_dispatches_with_conn(conn, current_version_id)
            dispatch_by_code = {d["dimension_code"]: d for d in dispatch_rows}
            for dim in dimensions:
                d_row = dispatch_by_code.get(dim["dimension_code"])
                if not d_row:
                    continue
                if d_row["status"] == "failed":
                    dim["status"] = "failed"
                    dim["status_label"] = "Evaluacion fallida"
                    dim["conclusion"] = f"Error en especialista {dim['dimension_name']}."
                    dim["justification"] = d_row.get("error_message") or "Error durante ejecución del especialista; requiere recuperación."
                    dim["score"] = None
                    dim["has_blocker"] = True
                    dim["blocker_message"] = d_row.get("error_message") or "Especialista fallido; requiere reintento."
                    if dim["dimension_code"] == "economic":
                        econ_score = None
                    elif dim["dimension_code"] == "social":
                        soc_score = None
                    elif dim["dimension_code"] == "environmental":
                        env_score = None
                    elif dim["dimension_code"] == "technical":
                        tech_score = None
                    tag = f"{dim['dimension_name']} (Fallida)"
                    if tag not in missing_dimensions and dim["dimension_name"] not in missing_dimensions:
                        missing_dimensions.append(tag)
                    blocking_reasons.append(
                        f"Dimension {dim['dimension_name'].lower()} fallida (CA-HU1.1-02 / CA-HU1.12-03): {dim['justification']}"
                    )
                elif d_row["status"] == "processing" and dim["status"] == "pending":
                    dim["status"] = "pending"
                    dim["status_label"] = "En proceso"
                    dim["conclusion"] = f"Especialista {dim['dimension_name']} en ejecución para esta versión."

            # ---------------------------------------------------------
            # 6. Puntuacion Global Explicable (CA-HU1.12-02 & CA-HU1.12-03)
            # ---------------------------------------------------------
            crit_row = conn.execute(
                """
                SELECT id, version_number FROM criteria_versions
                WHERE status = 'active'
                ORDER BY activated_at DESC LIMIT 1
                """
            ).fetchone()

            crit_weights_rows = []
            if crit_row:
                crit_weights_rows = conn.execute(
                    """
                    SELECT criterion_code, weight_percent
                    FROM criterion_weights
                    WHERE criteria_version_id = %s
                    ORDER BY criterion_code ASC
                    """,
                    (crit_row["id"],),
                ).fetchall()

            raw_weights = {r["criterion_code"]: float(r["weight_percent"]) for r in crit_weights_rows}

            # Normalizar codigos de criterios soportados
            def _norm_code(c: str) -> str:
                cu = c.upper().strip()
                if cu in ("ECONOMIC", "ECONOMICA", "ECONOMICO"):
                    return "ECONOMIC"
                if cu in ("SOCIAL", "SOCIALES"):
                    return "SOCIAL"
                if cu in ("ENVIRONMENTAL", "AMBIENTAL", "MEDIOAMBIENTAL"):
                    return "ENVIRONMENTAL"
                if cu in ("TECHNICAL", "TECNICA", "TECNICO"):
                    return "TECHNICAL"
                return cu

            weights_dict = {_norm_code(k): v for k, v in raw_weights.items()}

            input_scores_dict: dict[str, float] = {}
            if econ_score is not None:
                input_scores_dict["ECONOMIC"] = round(econ_score, 2)
            if soc_score is not None:
                input_scores_dict["SOCIAL"] = round(soc_score, 2)
            if env_score is not None:
                input_scores_dict["ENVIRONMENTAL"] = round(env_score, 2)
            if tech_score is not None:
                input_scores_dict["TECHNICAL"] = round(tech_score, 2)

            is_complete = (len(missing_dimensions) == 0)

            global_score_val = None
            is_score_calculated = False
            formula_text = ""
            explanation_text = ""
            score_blocker_reason: str | None = None

            if not crit_row or not weights_dict:
                if not crit_row:
                    formula_text = "No existe una version activa de criterios y pesos normativos configurada."
                    explanation_text = "No se puede calcular puntuacion global explicable sin una version activa de criterios."
                    score_blocker_reason = (
                        "No existe una version activa de criterios y pesos normativos configurada "
                        "para calcular la puntuacion global valida (CA-HU1.12-02 / CA-HU1.12-03)."
                    )
                else:
                    formula_text = (
                        f"La version activa de criterios v{crit_row['version_number']} no tiene pesos configurados."
                    )
                    explanation_text = "No se puede calcular puntuacion global explicable sin ponderaciones activas."
                    score_blocker_reason = (
                        f"La version activa de criterios v{crit_row['version_number']} no tiene pesos asignados "
                        "para calcular una puntuacion global valida (CA-HU1.12-02 / CA-HU1.12-03)."
                    )
            elif not is_complete:
                missing_str = ", ".join(missing_dimensions)
                formula_text = f"Puntuacion no calculable: faltan dimensiones indispensables ({missing_str})."
                explanation_text = (
                    f"Conforme a CA-HU1.12-03, una evaluacion parcial no produce puntuacion global definitiva. "
                    f"Dimensiones pendientes o no viables: {missing_str}."
                )
            else:
                unscored_criteria = [
                    code for code, weight in weights_dict.items()
                    if weight > 0 and code not in input_scores_dict
                ]
                if unscored_criteria:
                    missing_scores_str = ", ".join(unscored_criteria)
                    formula_text = f"Puntuacion no calculable: faltan puntuaciones cuantitativas para criterios ponderados ({missing_scores_str})."
                    explanation_text = "Se requiere registrar puntuacion valida para todos los criterios con peso asignado."
                    score_blocker_reason = (
                        f"Falta puntuacion cuantitativa valida para criterio(s) ponderado(s) ({missing_scores_str}); "
                        "no es posible aprobar sin puntuacion global valida (CA-HU1.12-02 / CA-HU1.12-03)."
                    )
                else:
                    weighted_codes = [
                        code for code, weight in weights_dict.items()
                        if weight > 0 and code in input_scores_dict
                    ]
                    weighted_sum = sum(
                        input_scores_dict[code] * (weights_dict[code] / 100.0)
                        for code in weighted_codes
                    )
                    total_weight = sum(weights_dict[code] for code in weighted_codes)
                    if total_weight > 0:
                        calc_score = round(weighted_sum * (100.0 / total_weight), 2)
                        global_score_val = calc_score
                        is_score_calculated = True

                        terms = [
                            f"[{code}: {input_scores_dict[code]:.1f} * {weights_dict[code]:.1f}%]"
                            for code in weighted_codes
                        ]
                        formula_text = f"({' + '.join(terms)}) / {total_weight:.1f}% = {calc_score:.2f} pts"
                        explanation_text = (
                            f"Puntuacion global consolidada calculada a partir de la version activa de criterios v{crit_row['version_number']}. "
                            f"Ponderacion aplicada sobre {len(terms)} criterio(s) cuantitativo(s)."
                        )
                    else:
                        formula_text = "Puntuacion no calculable: la suma de pesos activos para dimensiones cuantitativas evaluadas es 0%."
                        explanation_text = "Configure pesos mayores a 0% para al menos una dimension cuantitativa evaluada."
                        score_blocker_reason = (
                            "La suma de pesos cuantitativos en la politica activa de criterios es 0%; "
                            "no es posible aprobar sin una puntuacion global calculable (CA-HU1.12-02 / CA-HU1.12-03)."
                        )

            if is_complete and (not is_score_calculated or global_score_val is None):
                blocking_reasons.append(
                    score_blocker_reason
                    or "No se dispone de una puntuacion global valida bajo la politica activa de criterios (CA-HU1.12-02 / CA-HU1.12-03)."
                )

            has_blockers = (len(blocking_reasons) > 0)
            if has_blockers:
                summary_status = "blocked"
                summary_status_label = (
                    "Bloqueado: puntuacion global no calculable"
                    if is_complete and not is_score_calculated and score_blocker_reason and len(blocking_reasons) == 1
                    else "Bloqueado por alertas o punto de control"
                )
            elif not is_complete:
                summary_status = "incomplete"
                summary_status_label = "Evaluacion multidimensional incompleta"
            else:
                summary_status = "completed"
                summary_status_label = "Evaluacion multidimensional completa"

            allows_final_approval = bool(
                is_complete
                and not has_blockers
                and is_score_calculated
                and global_score_val is not None
            )

            resumen_parts = [
                f"Expediente {project['code']} (version {version_number}): "
                f"Dimensiones finalizadas: {5 - len(missing_dimensions)}/5."
            ]
            if global_score_val is not None:
                resumen_parts.append(f"Puntuacion consolidada: {global_score_val:.2f}/100.")
            if has_blockers:
                resumen_parts.append(f"Requiere resolver: {'; '.join(blocking_reasons)}.")
            elif not is_complete:
                resumen_parts.append(f"Pendiente de completar: {', '.join(missing_dimensions)}.")
            else:
                resumen_parts.append("Expediente con evaluacion completa y expedita para dictamen humano formal.")

            summary_text = " ".join(resumen_parts)

            return {
                "project_id": project_id,
                "project_code": project["code"],
                "project_version_id": current_version_id,
                "version_number": version_number,
                "is_complete": is_complete,
                "status": summary_status,
                "status_label": summary_status_label,
                "summary_text": summary_text,
                "dimensions": dimensions,
                "missing_dimensions": missing_dimensions,
                "global_score": {
                    "score": global_score_val,
                    "is_calculated": is_score_calculated,
                    "formula": formula_text,
                    "criteria_version_id": crit_row["id"] if crit_row else None,
                    "criteria_version_number": crit_row["version_number"] if crit_row else None,
                    "weights": weights_dict,
                    "input_scores": input_scores_dict,
                    "explanation": explanation_text,
                },
                "allows_final_approval": allows_final_approval,
                "blocking_reasons": blocking_reasons,
                "generated_at": datetime.now(timezone.utc),
            }

    def get_project_map_ranking(self) -> dict[str, Any]:
        """Ranking determinista de proyectos comparables en mapa (HU4.1).

        Reutiliza get_unified_assessment_summary (HU1.12) para puntuacion, pesos y formula,
        y excluye del orden numerico expedientes incompletos, bloqueados o evaluados con
        una version de criterios distinta a la vigente. Nunca inventa coordenadas.
        """
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            active_crit = conn.execute(
                """
                SELECT id, version_number
                FROM criteria_versions
                WHERE status = 'active'
                LIMIT 1
                """
            ).fetchone()

            project_rows = conn.execute(
                """
                SELECT DISTINCT ON (p.id)
                       p.id AS project_id,
                       p.code,
                       p.status AS db_status,
                       pv.id AS project_version_id,
                       pv.version_number,
                       pv.title,
                       pv.location_description AS location,
                       pv.estimated_budget_pen,
                       pv.beneficiaries_count,
                       pv.latitude,
                       pv.longitude,
                       pv.coordinates_origin,
                       pv.coordinates_source,
                       (
                           SELECT e.criteria_version_id
                           FROM evaluations e
                           WHERE e.project_version_id = pv.id
                           ORDER BY e.requested_at DESC
                           LIMIT 1
                       ) AS eval_criteria_version_id,
                       (
                           SELECT COUNT(*)::int
                           FROM project_geospatial_layers gl
                           WHERE gl.project_version_id = pv.id
                             AND gl.availability_status = 'available'
                       ) AS geospatial_layers_count
                FROM projects p
                JOIN project_versions pv ON pv.project_id = p.id
                ORDER BY p.id, pv.version_number DESC
                """
            ).fetchall()

        comparable_candidates: list[dict[str, Any]] = []
        excluded_projects: list[dict[str, Any]] = []

        active_crit_id = active_crit["id"] if active_crit else None
        active_crit_num = active_crit["version_number"] if active_crit else None

        for row in project_rows:
            pid = row["project_id"]
            vid = row["project_version_id"]
            lat = float(row["latitude"]) if row.get("latitude") is not None else None
            lon = float(row["longitude"]) if row.get("longitude") is not None else None
            coord_source = (row.get("coordinates_source") or "").strip() or None
            has_coords = (lat is not None and lon is not None and coord_source is not None)

            summary = self.get_unified_assessment_summary(pid, version_id=vid)
            gscore = summary["global_score"]

            exclusion_code: str | None = None
            exclusion_reason: str | None = None

            if active_crit_id is None:
                exclusion_code = "no_active_criteria"
                exclusion_reason = "No existe una version activa de criterios y pesos normativos."
            elif summary["status"] == "blocked":
                exclusion_code = "blocked"
                exclusion_reason = "; ".join(summary["blocking_reasons"]) or "Expediente bloqueado por alerta legal o punto de control HITL."
            elif not summary["is_complete"] or not gscore["is_calculated"] or gscore["score"] is None:
                exclusion_code = "incomplete"
                missing = ", ".join(summary["missing_dimensions"])
                exclusion_reason = (
                    f"Evaluacion multidimensional incompleta ({missing}). "
                    "No se asigna puntaje ni posicion ficticia."
                    if missing
                    else gscore.get("explanation") or "Puntuacion global no calculable."
                )
            elif (
                row.get("estimated_budget_pen") is None
                or row.get("beneficiaries_count") is None
            ):
                exclusion_code = "incomplete"
                exclusion_reason = "Borrador sin presupuesto o beneficiarios completos; no comparable en ranking."
            elif (
                row.get("eval_criteria_version_id") is not None
                and row["eval_criteria_version_id"] != active_crit_id
            ):
                exclusion_code = "criteria_version_mismatch"
                exclusion_reason = (
                    f"Evaluacion economica ejecutada con una version de criterios distinta a la vigente (v{active_crit_num}). "
                    "Requiere reevaluacion para ser comparable."
                )

            if exclusion_code is not None:
                excluded_projects.append({
                    "project_id": pid,
                    "project_version_id": vid,
                    "code": row["code"],
                    "version_number": row["version_number"],
                    "title": row["title"],
                    "status": summary["status"],
                    "exclusion_code": exclusion_code,
                    "exclusion_reason": exclusion_reason,
                    "missing_dimensions": summary["missing_dimensions"],
                    "blocking_reasons": summary["blocking_reasons"],
                    "has_coordinates": has_coords,
                    "latitude": lat,
                    "longitude": lon,
                })
                continue

            comparable_candidates.append({
                "project_id": pid,
                "project_version_id": vid,
                "code": row["code"],
                "version_number": row["version_number"],
                "title": row["title"],
                "status": summary["status"],
                "location": row["location"],
                "estimated_budget_pen": row["estimated_budget_pen"],
                "beneficiaries_count": row["beneficiaries_count"],
                "global_score": float(gscore["score"]),
                "formula": gscore["formula"],
                "explanation": gscore["explanation"],
                "criteria_version_number": gscore["criteria_version_number"] or active_crit_num,
                "weights": gscore["weights"],
                "input_scores": gscore["input_scores"],
                "has_coordinates": has_coords,
                "latitude": lat if has_coords else None,
                "longitude": lon if has_coords else None,
                "coordinates_origin": row.get("coordinates_origin") if has_coords else None,
                "coordinates_source": coord_source if has_coords else None,
                "allows_final_approval": summary["allows_final_approval"],
                "geospatial_layers_count": int(row.get("geospatial_layers_count") or 0),
            })

        comparable_candidates.sort(
            key=lambda item: (
                -item["global_score"],
                -item["beneficiaries_count"],
                float(item["estimated_budget_pen"]),
                item["code"],
                str(item["project_id"]),
            )
        )

        score_counts: dict[float, int] = {}
        for item in comparable_candidates:
            s = item["global_score"]
            score_counts[s] = score_counts.get(s, 0) + 1

        ranked_projects: list[dict[str, Any]] = []
        for idx, item in enumerate(comparable_candidates, start=1):
            tie_reason = None
            if score_counts.get(item["global_score"], 0) > 1:
                tie_reason = (
                    f"Empate en {item['global_score']:.2f} pts; ordenado deterministamente por "
                    f"beneficiarios ({item['beneficiaries_count']:,}), presupuesto (S/ {float(item['estimated_budget_pen']):,.2f}) y codigo ({item['code']})."
                )
            ranked_projects.append({
                "rank_position": idx,
                **item,
                "tie_break_reason": tie_reason,
            })

        mapped_projects = [item for item in ranked_projects if item["has_coordinates"]]
        unmapped_projects = [item for item in ranked_projects if not item["has_coordinates"]]

        return {
            "is_recommendation": True,
            "recommendation_notice": (
                "Recomendacion tecnica preliminar de priorizacion basada en criterios vigentes (HU1.12). "
                "No constituye aprobacion municipal ni reemplaza el dictamen formal de las autoridades."
            ),
            "criteria_version_id": active_crit_id,
            "criteria_version_number": active_crit_num,
            "total_projects": len(project_rows),
            "comparable_count": len(ranked_projects),
            "mapped_count": len(mapped_projects),
            "unmapped_count": len(unmapped_projects),
            "excluded_count": len(excluded_projects),
            "ranked_projects": ranked_projects,
            "mapped_projects": mapped_projects,
            "unmapped_projects": unmapped_projects,
            "excluded_projects": excluded_projects,
            "generated_at": datetime.now(timezone.utc),
        }

    _GEO_LAYER_LABELS = {
        "influence_area": "Area de influencia",
        "beneficiary_coverage": "Cobertura de beneficiarios (agregada)",
        "territorial_zone": "Zona territorial",
    }

    def register_geospatial_layer(
        self,
        project_id: UUID,
        data: GeospatialLayerCreate,
        actor_id: UUID,
    ) -> dict[str, Any]:
        """Registra una capa geoespacial versionada con geometria y fuente verificable (HU4.2)."""
        project = self.get_project(project_id)
        version_id = project["project_version_id"]
        layer_id = uuid4()
        now = datetime.now(timezone.utc)
        gtype = data.geojson_data["type"] if data.geojson_data else None

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            conn.execute(
                """
                INSERT INTO project_geospatial_layers (
                    id, project_version_id, layer_type, layer_name, meaning,
                    source_name, data_origin, availability_status, geometry_type,
                    geojson_data, aggregate_beneficiaries_count, created_by_user_id, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    layer_id,
                    version_id,
                    data.layer_type,
                    data.layer_name,
                    data.meaning,
                    data.source_name,
                    data.data_origin,
                    data.availability_status,
                    gtype,
                    Jsonb(data.geojson_data) if data.geojson_data is not None else None,
                    data.aggregate_beneficiaries_count,
                    actor_id,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO audit_events (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'GEOSPATIAL_LAYER_REGISTERED', 'project_version', %s, %s)
                """,
                (
                    actor_id,
                    version_id,
                    Jsonb({
                        "layer_id": str(layer_id),
                        "layer_type": data.layer_type,
                        "source_name": data.source_name,
                        "availability_status": data.availability_status,
                    }),
                ),
            )

        return {
            "layer_id": layer_id,
            "project_version_id": version_id,
            "layer_type": data.layer_type,
            "layer_type_label": self._GEO_LAYER_LABELS.get(data.layer_type, data.layer_type),
            "layer_name": data.layer_name,
            "meaning": data.meaning,
            "source_name": data.source_name,
            "data_origin": data.data_origin,
            "availability_status": data.availability_status,
            "geometry_type": gtype,
            "geojson_data": data.geojson_data,
            "aggregate_beneficiaries_count": data.aggregate_beneficiaries_count,
            "created_at": now,
        }

    def get_project_geospatial_layers(
        self,
        project_id: UUID,
        version_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Consulta las capas geoespaciales de la version vigente sin inventar geometrias (HU4.2)."""
        project = self.get_project(project_id)
        target_version_id = version_id or project["project_version_id"]

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            v_row = conn.execute(
                "SELECT id, version_number FROM project_versions WHERE id = %s AND project_id = %s",
                (target_version_id, project_id),
            ).fetchone()
            if not v_row:
                raise RecursoPMV1NoEncontradoError("Version del expediente no encontrada")

            rows = conn.execute(
                """
                SELECT id AS layer_id, project_version_id, layer_type, layer_name,
                       meaning, source_name, data_origin, availability_status,
                       geometry_type, geojson_data, aggregate_beneficiaries_count, created_at
                FROM project_geospatial_layers
                WHERE project_version_id = %s
                ORDER BY created_at ASC, layer_name ASC
                """,
                (target_version_id,),
            ).fetchall()

        layers: list[dict[str, Any]] = []
        has_available = False
        for r in rows:
            is_avail = r["availability_status"] == "available" and r.get("geojson_data") is not None
            if is_avail:
                has_available = True
            layers.append({
                "layer_id": r["layer_id"],
                "project_version_id": r["project_version_id"],
                "layer_type": r["layer_type"],
                "layer_type_label": self._GEO_LAYER_LABELS.get(r["layer_type"], r["layer_type"]),
                "layer_name": r["layer_name"],
                "meaning": r["meaning"],
                "source_name": r["source_name"],
                "data_origin": r["data_origin"],
                "availability_status": r["availability_status"],
                "geometry_type": r["geometry_type"] if is_avail else None,
                "geojson_data": r["geojson_data"] if is_avail else None,
                "aggregate_beneficiaries_count": r["aggregate_beneficiaries_count"],
                "created_at": r["created_at"],
            })

        empty_msg = None
        if not has_available:
            empty_msg = (
                "Sin geometria geoespacial verificada para esta version del expediente; "
                "no se dibuja ningun area de influencia ficticia."
            )

        return {
            "project_id": project_id,
            "project_code": project["code"],
            "project_version_id": target_version_id,
            "version_number": v_row["version_number"],
            "has_available_geometry": has_available,
            "layers": layers,
            "empty_message": empty_msg,
            "privacy_notice": (
                "Las capas geoespaciales muestran unicamente geometrias territoriales y conteos agregados; "
                "no exponen datos personales de beneficiarios."
            ),
        }

    def export_prioritization_ranking_pdf(self) -> tuple[bytes, str]:
        """Exporta el informe preliminar de priorizacion en PDF en memoria (HU4.3).

        Reutiliza exactamente get_project_map_ranking() y consulta en modo solo lectura
        las evidencias normativas verificables y el estado de revision humana por version.
        """
        ranking = self.get_project_map_ranking()
        all_items = list(ranking["ranked_projects"]) + list(ranking["excluded_projects"])
        version_ids = [item["project_version_id"] for item in all_items]
        project_ids = [item["project_id"] for item in all_items]

        evidences_by_version: dict[str, list[dict[str, str]]] = {}
        human_review_by_version: dict[str, dict[str, Any]] = {}

        if version_ids:
            with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
                norm_rows = conn.execute(
                    """
                    SELECT id, document_name, short_code, version, data_origin, in_force
                    FROM normative_documents
                    """
                ).fetchall()

                def _classify_zoning_source(
                    source_doc: str, source_ver: str, source_loc: str, source_exc: str, limitations: str
                ) -> tuple[str, str]:
                    combined = f"{source_doc} {source_ver} {source_loc} {source_exc} {limitations}".lower()
                    demo_markers = ("[demo]", "simulado", "simulated", "ficticio", "dato demo", "prueba")
                    if any(m in combined for m in demo_markers):
                        return "simulated", "Dato demo o simulado en zonificación; pendiente de cotejo con documento fuente."
                    doc_norm = source_doc.strip().lower()
                    for nr in norm_rows:
                        n_name = str(nr.get("document_name") or "").strip().lower()
                        n_code = str(nr.get("short_code") or "").strip().lower()
                        if (n_name and (n_name in doc_norm or doc_norm in n_name)) or (n_code and n_code in doc_norm):
                            origin = str(nr.get("data_origin") or "simulated")
                            if origin in ("public", "official") and nr.get("in_force"):
                                return "verified", f"Respaldado en corpus normativo ({nr['id']}, origen {origin})."
                            return "simulated", f"Fuente normativa con origen '{origin}' o no vigente; pendiente de cotejo."
                    return "unlinked", "Referencia de zonificación registrada sin documento fuente vinculado en corpus normativo; pendiente de cotejo."

                # 1. Evidencia de zonificacion (HU1.11) contrastada contra corpus normativo
                zoning_rows = conn.execute(
                    """
                    SELECT project_version_id, source_document, source_version, source_locator, source_excerpt, limitations
                    FROM zoning_review_requests
                    WHERE project_version_id = ANY(%s)
                      AND NULLIF(btrim(source_document), '') IS NOT NULL
                      AND NULLIF(btrim(source_version), '') IS NOT NULL
                      AND NULLIF(btrim(source_locator), '') IS NOT NULL
                      AND NULLIF(btrim(source_excerpt), '') IS NOT NULL
                    ORDER BY requested_at DESC
                    """,
                    (version_ids,),
                ).fetchall()
                for zr in zoning_rows:
                    vid = str(zr["project_version_id"])
                    v_status, v_note = _classify_zoning_source(
                        str(zr["source_document"]),
                        str(zr["source_version"]),
                        str(zr["source_locator"]),
                        str(zr["source_excerpt"]),
                        str(zr.get("limitations") or ""),
                    )
                    evidences_by_version.setdefault(vid, []).append({
                        "document": str(zr["source_document"]),
                        "version": str(zr["source_version"]),
                        "locator": str(zr["source_locator"]),
                        "excerpt": str(zr["source_excerpt"])[:180],
                        "verification_status": v_status,
                        "verification_note": v_note,
                    })

                # 2. Hallazgos legales con referencia precisa (HU2.2) - solo verificable si enlaza a normative_documents public/official
                finding_rows = conn.execute(
                    """
                    SELECT f.project_id, f.project_version_id, f.normative_chunk_id,
                           f.custom_article_or_section, f.title, f.notes,
                           n.id AS norm_id, n.document_name AS norm_title, n.version AS norm_version,
                           n.content AS norm_content, n.data_origin AS norm_origin, n.in_force AS norm_in_force
                    FROM project_legal_findings f
                    LEFT JOIN normative_documents n ON n.id = f.normative_chunk_id
                    WHERE f.project_id = ANY(%s)
                      AND f.has_precise_reference = TRUE
                    ORDER BY f.created_at DESC
                    """,
                    (project_ids,),
                ).fetchall()
                latest_vid_by_pid = {str(it["project_id"]): str(it["project_version_id"]) for it in all_items}
                for fr in finding_rows:
                    vid = str(fr["project_version_id"] or latest_vid_by_pid.get(str(fr["project_id"]), ""))
                    if not vid:
                        continue
                    has_linked_doc = bool(fr.get("normative_chunk_id") and fr.get("norm_id"))
                    norm_origin = str(fr.get("norm_origin") or "")
                    has_doc_fields = bool(
                        str(fr.get("norm_title") or "").strip()
                        and str(fr.get("norm_version") or "").strip()
                        and str(fr.get("norm_content") or "").strip()
                    )
                    if not has_linked_doc or not has_doc_fields:
                        v_status = "unlinked"
                        v_note = "Referencia registrada sin documento fuente vinculado en normative_documents; pendiente de cotejo."
                        doc_name = fr.get("title") or "Referencia legal sin documento fuente"
                        doc_ver = "Sin versión verificada"
                        locator = fr.get("custom_article_or_section") or "Sin localizador verificado"
                        excerpt = fr.get("notes") or fr.get("title") or ""
                    elif norm_origin not in ("public", "official"):
                        v_status = "simulated"
                        v_note = f"Documento vinculado con origen '{norm_origin}' (no oficial/público); pendiente de cotejo."
                        doc_name = fr["norm_title"]
                        doc_ver = fr["norm_version"]
                        locator = fr.get("custom_article_or_section") or str(fr["norm_id"])
                        excerpt = fr["norm_content"]
                    else:
                        v_status = "verified"
                        v_note = f"Respaldado por {fr['norm_id']} (origen {norm_origin})."
                        doc_name = fr["norm_title"]
                        doc_ver = fr["norm_version"]
                        locator = fr.get("custom_article_or_section") or str(fr["norm_id"])
                        excerpt = fr["norm_content"]

                    evidences_by_version.setdefault(vid, []).append({
                        "document": str(doc_name),
                        "version": str(doc_ver),
                        "locator": str(locator),
                        "excerpt": str(excerpt)[:180],
                        "verification_status": v_status,
                        "verification_note": v_note,
                    })

                # 3. Checkpoints HITL, aprobaciones humanas y estado legal por version
                cp_rows = conn.execute(
                    """
                    SELECT DISTINCT ON (project_version_id)
                           project_version_id, status, reason, resolved_at
                    FROM evaluation_checkpoints
                    WHERE project_version_id = ANY(%s)
                    ORDER BY project_version_id, created_at DESC
                    """,
                    (version_ids,),
                ).fetchall()
                cp_by_vid = {str(r["project_version_id"]): r for r in cp_rows}

                appr_rows = conn.execute(
                    """
                    SELECT DISTINCT ON (project_version_id)
                           project_version_id, decision, justification, decided_at
                    FROM human_approvals
                    WHERE project_version_id = ANY(%s)
                    ORDER BY project_version_id, decided_at DESC
                    """,
                    (version_ids,),
                ).fetchall()
                appr_by_vid = {str(r["project_version_id"]): r for r in appr_rows}

                legal_rows = conn.execute(
                    """
                    SELECT DISTINCT ON (project_version_id)
                           project_version_id, status, requires_human_review, has_legal_alert, is_resolved,
                           verifiable_citations_count, unverifiable_citations_count
                    FROM legal_assessments
                    WHERE project_version_id = ANY(%s)
                    ORDER BY project_version_id, evaluated_at DESC
                    """,
                    (version_ids,),
                ).fetchall()
                legal_by_vid = {str(r["project_version_id"]): r for r in legal_rows}

                for item in all_items:
                    vid = str(item["project_version_id"])
                    parts: list[str] = []
                    cp = cp_by_vid.get(vid)
                    if cp:
                        if cp["status"] == "pending_review":
                            parts.append(f"Punto de control HITL pendiente de revision ({cp['reason']})")
                        else:
                            parts.append("Punto de control HITL resuelto")
                    lg = legal_by_vid.get(vid)
                    if lg:
                        if (lg["status"] == "alerta_legal" or lg.get("has_legal_alert")) and not lg.get("is_resolved"):
                            parts.append("Alerta legal activa sin resolver")
                        elif lg.get("requires_human_review") or lg["status"] == "requires_review":
                            parts.append("Revision juridica humana pendiente")
                        else:
                            parts.append(f"Evaluacion juridica {lg['status']} ({lg.get('verifiable_citations_count', 0)} cita(s) verificable(s))")
                    else:
                        parts.append("Evaluacion juridica pendiente")

                    ap = appr_by_vid.get(vid)
                    if ap:
                        parts.append(f"Dictamen humano formal: {ap['decision']} ({ap['decided_at'].strftime('%Y-%m-%d')})")
                    else:
                        parts.append("Sin dictamen humano formal emitido (recomendacion no vinculante)")

                    human_review_by_version[vid] = {"summary": " · ".join(parts)}

        pdf_bytes = build_prioritization_pdf(
            ranking=ranking,
            evidences_by_version=evidences_by_version,
            human_review_by_version=human_review_by_version,
        )
        ts = ranking["generated_at"].strftime("%Y%m%d-%H%M%S")
        return pdf_bytes, f"siprim-ranking-priorizacion-{ts}.pdf"

    def verify_prioritization_allowed(self, project_id: UUID) -> dict[str, Any]:
        """Control de precondiciones para la futura priorización definitiva (HU2.3 / HU3.1 / HU1.12).

        Verifica que el expediente no tenga Alerta Legal activa sin resolver,
        que no tenga puntos de control (checkpoints) en pausa/revisión humana pendiente,
        y que se encuentre debidamente evaluado.
        """
        project = self.get_project(project_id)
        version_id = project["project_version_id"]
        blocking_reasons: list[str] = []

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            # 1. Control HU2.3: Alerta Legal activa
            legal_row = conn.execute(
                """
                SELECT status, has_legal_alert, legal_alert_motive, legal_alert_norm, is_resolved
                FROM legal_assessments
                WHERE project_id = %s AND project_version_id = %s
                ORDER BY evaluated_at DESC LIMIT 1
                """,
                (project_id, version_id),
            ).fetchone()
            if legal_row and (legal_row["status"] == "alerta_legal" or legal_row.get("has_legal_alert")) and not legal_row.get("is_resolved", False):
                motive = legal_row.get("legal_alert_motive") or "Incompatibilidad territorial o normativa"
                norm = legal_row.get("legal_alert_norm") or "Normativa territorial"
                blocking_reasons.append(
                    f"Alerta Legal activa por incompatibilidad territorial/normativa (HU2.3): {motive} [{norm}]. "
                    "Se requiere resolución formal de Asesoría Jurídica antes de la priorización definitiva."
                )

            # 2. Control HU3.1 / HU3.4: Checkpoint HITL pendiente
            if RiskCheckpointEngine.has_pending_checkpoint(conn, version_id):
                active_cp = RiskCheckpointEngine.get_active_checkpoint(conn, version_id)
                reason = active_cp["reason"] if active_cp else "Punto de control de riesgo alto activo"
                blocking_reasons.append(
                    f"Punto de control de riesgo alto en estado 'Pendiente de revisión' (HU3.1/HU3.4): {reason}. "
                    "Se requiere autorización y reanudación por Gerencia antes de la priorización definitiva."
                )

        allows_prioritization = len(blocking_reasons) == 0
        return {
            "project_id": str(project_id),
            "project_version_id": str(version_id),
            "project_code": project["code"],
            "allows_prioritization": allows_prioritization,
            "blocking_reasons": blocking_reasons,
        }

    def submit_human_approval(
        self,
        project_id: UUID,
        actor_id: UUID,
        decision: str,
        justification: str,
        conditions: str | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Registra el dictamen humano formal sobre un proyecto tras la evaluacion tecnica."""
        cleaned_key = idempotency_key.strip()
        if not cleaned_key:
            raise ConfiguracionPMV1Error("Idempotency-Key es obligatorio")

        cleaned_decision = decision.strip().lower()
        if cleaned_decision not in ("approved", "rejected", "observed"):
            raise ConfiguracionPMV1Error("Dictamen invalido. Debe ser 'approved', 'rejected' u 'observed'")

        cleaned_justification = justification.strip()
        if len(cleaned_justification) < 5:
            raise ConfiguracionPMV1Error("La justificacion debe tener al menos 5 caracteres")

        cleaned_conditions = conditions.strip() if conditions and conditions.strip() else None
        if cleaned_decision == "observed" and not cleaned_conditions:
            raise ConfiguracionPMV1Error("El dictamen 'observed' requiere especificar condiciones u observaciones")

        project = self.get_project(project_id)
        if project["status"] == "pending_review" or project.get("checkpoint") is not None:
            raise ConfiguracionPMV1Error(
                "El expediente se encuentra en estado 'Pendiente de revisión' en su versión vigente. "
                "Conforme a HU3.1, no se puede emitir una aprobación final mientras la revisión humana esté pendiente."
            )

        if cleaned_decision == "approved":
            unified_summary = self.get_unified_assessment_summary(
                project_id=project_id,
                version_id=project["project_version_id"],
            )
            if not unified_summary["allows_final_approval"]:
                reasons = unified_summary.get("blocking_reasons") or [
                    "La evaluación multidimensional está incompleta o contiene dimensiones no conformes."
                ]
                raise ConfiguracionPMV1Error(
                    "Aprobación final bloqueada por el resumen unificado multidimensional (HU1.12): "
                    + " ".join(str(reason) for reason in reasons)
                )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            if RiskCheckpointEngine.has_pending_checkpoint(conn, project["project_version_id"]):
                raise ConfiguracionPMV1Error(
                    "El expediente se encuentra en estado 'Pendiente de revisión' por punto de control de riesgo alto en su versión vigente. "
                    "Conforme a HU3.1, no se puede emitir una aprobación final mientras la revisión humana esté pendiente."
                )

            # HU2.3: Bloqueo de aprobación definitiva si existe Alerta Legal activa en la versión vigente
            legal_row = conn.execute(
                """
                SELECT status, has_legal_alert, legal_alert_motive, legal_alert_norm, is_resolved
                FROM legal_assessments
                WHERE project_id = %s AND (project_version_id = %s OR project_version_id IS NULL)
                """,
                (project_id, project["project_version_id"]),
            ).fetchone()
            if legal_row and (legal_row["status"] == "alerta_legal" or legal_row.get("has_legal_alert")) and not legal_row.get("is_resolved", False):
                if cleaned_decision == "approved":
                    motive = legal_row.get("legal_alert_motive") or "Incompatibilidad territorial o normativa"
                    norm = legal_row.get("legal_alert_norm") or "Normativa territorial"
                    raise ConfiguracionPMV1Error(
                        f"Aprobación final y priorización bloqueadas por Alerta Legal activa (HU2.3): "
                        f"{motive} [{norm}]. "
                        "El expediente no puede avanzar a aprobación definitiva ni a priorización hasta que la alerta sea resuelta o levantada por Asesoría Jurídica."
                    )

            # HU1.12 / HU2.2: Bloqueo si la dimensión jurídica está en requires_review (citas no verificables o dictamen pendiente)
            if legal_row and legal_row["status"] == "requires_review":
                if cleaned_decision == "approved":
                    raise ConfiguracionPMV1Error(
                        "Aprobación final bloqueada: la dimensión jurídica se encuentra en estado 'requires_review' "
                        "debido a citas normativas no verificables o dictamen legal pendiente (HU2.2/HU1.12). "
                        "Conforme a CA-HU1.12-03, una evaluación incompleta o en revisión no puede habilitar la aprobación definitiva."
                    )

        if project["status"] not in ("evaluated", "approved", "rejected", "observed"):
            raise ConfiguracionPMV1Error(
                "El proyecto debe haber completado la evaluacion tecnica antes de la aprobacion humana formal"
            )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            prev = conn.execute(
                """
                SELECT id AS approval_id, project_id, project_version_id, decision,
                       justification, conditions, decided_by_user_id, idempotency_key, decided_at
                FROM human_approvals WHERE idempotency_key = %s
                """,
                (cleaned_key,),
            ).fetchone()
            if prev:
                if prev["project_id"] != project_id:
                    raise ConfiguracionPMV1Error("Idempotency-Key ya fue utilizada para otro expediente")
                return {**dict(prev), "duplicated": True}

            approval_id = uuid4()
            now = datetime.now(timezone.utc)
            conn.execute(
                """
                INSERT INTO human_approvals
                (id, project_id, project_version_id, decision, justification,
                 conditions, decided_by_user_id, idempotency_key, decided_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    approval_id,
                    project_id,
                    project["project_version_id"],
                    cleaned_decision,
                    cleaned_justification,
                    cleaned_conditions,
                    actor_id,
                    cleaned_key,
                    now,
                ),
            )
            conn.execute(
                "UPDATE projects SET status = %s, updated_at = %s WHERE id = %s",
                (cleaned_decision, now, project_id),
            )
            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'PROJECT_HUMAN_APPROVAL_RECORDED', 'human_approval', %s, %s)
                """,
                (
                    actor_id,
                    approval_id,
                    Jsonb(
                        {
                            "project_id": str(project_id),
                            "project_version_id": str(project["project_version_id"]),
                            "decision": cleaned_decision,
                            "justification": cleaned_justification,
                            "conditions": cleaned_conditions,
                        }
                    ),
                ),
            )

        return {
            "approval_id": approval_id,
            "project_id": project_id,
            "project_version_id": project["project_version_id"],
            "decision": cleaned_decision,
            "justification": cleaned_justification,
            "conditions": cleaned_conditions,
            "decided_by_user_id": actor_id,
            "idempotency_key": cleaned_key,
            "decided_at": now,
            "duplicated": False,
        }

    def get_human_approval(self, project_id: UUID) -> dict[str, Any]:
        """Obtiene el estado de la compuerta humana y el ultimo dictamen del proyecto."""
        project = self.get_project(project_id)
        checkpoint = project.get("checkpoint")
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            if not checkpoint:
                checkpoint = RiskCheckpointEngine.get_active_checkpoint(conn, project["project_version_id"])
            latest = conn.execute(
                """
                SELECT id AS approval_id, project_id, project_version_id, decision,
                       justification, conditions, decided_by_user_id, idempotency_key, decided_at
                FROM human_approvals
                WHERE project_id = %s AND project_version_id = %s
                ORDER BY decided_at DESC
                LIMIT 1
                """,
                (project_id, project["project_version_id"]),
            ).fetchone()

        legal_row = None
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            legal_row = conn.execute(
                """
                SELECT status, has_legal_alert, legal_alert_motive, legal_alert_norm, is_resolved
                FROM legal_assessments
                WHERE project_id = %s AND (project_version_id = %s OR project_version_id IS NULL)
                """,
                (project_id, project["project_version_id"]),
            ).fetchone()

        has_legal_alert = bool(
            legal_row and (legal_row["status"] == "alerta_legal" or legal_row.get("has_legal_alert"))
            and not legal_row.get("is_resolved", False)
        )
        legal_alert_motive = legal_row.get("legal_alert_motive") if has_legal_alert else None
        legal_alert_norm = legal_row.get("legal_alert_norm") if has_legal_alert else None

        # HU1.12 / HU2.2: Una dimensión jurídica en 'requires_review' bloquea la aprobación final
        legal_requires_review = bool(
            legal_row and legal_row["status"] == "requires_review" and not has_legal_alert
        )

        unified_summary = self.get_unified_assessment_summary(
            project_id=project_id,
            version_id=project["project_version_id"],
        )

        is_paused = (checkpoint is not None) or (project["status"] == "pending_review")
        is_evaluated = project["status"] in ("evaluated", "approved", "rejected", "observed") and not is_paused

        blocking_reasons: list[str] = []
        if is_paused:
            blocking_reasons.append(checkpoint["reason"] if checkpoint else "Expediente detenido en punto de control por riesgo alto")
        if has_legal_alert:
            blocking_reasons.append(f"Alerta Legal activa (HU2.3): {legal_alert_motive} [{legal_alert_norm}]")
        if legal_requires_review:
            blocking_reasons.append("Dimensión jurídica en estado 'requires_review' por citas no verificables o dictamen pendiente (HU2.2/HU1.12)")

        for reason in unified_summary.get("blocking_reasons", []):
            if reason not in blocking_reasons:
                blocking_reasons.append(reason)

        allows_final_approval = (
            not is_paused
            and not has_legal_alert
            and not legal_requires_review
            and bool(unified_summary.get("allows_final_approval"))
        )
        requires_human_approval = project["status"] == "evaluated" and latest is None and allows_final_approval

        return {
            "project_id": project_id,
            "project_version_id": project["project_version_id"],
            "project_status": project["status"],
            "is_evaluated": is_evaluated,
            "requires_human_approval": requires_human_approval,
            "is_paused": is_paused,
            "pause_reason": checkpoint["reason"] if checkpoint else None,
            "checkpoint": checkpoint,
            "current_approval": dict(latest) if latest else None,
            "has_legal_alert": has_legal_alert,
            "legal_alert_motive": legal_alert_motive,
            "legal_alert_norm": legal_alert_norm,
            "allows_final_approval": allows_final_approval,
            "blocking_reasons": blocking_reasons,
        }

    def evaluate_social_accessibility(
        self,
        project_id: UUID,
        actor_id: UUID,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Evalúa la accesibilidad territorial en salud, educación y transporte (HU1.4)."""
        project = self.get_project(project_id)
        self._ensure_complete_project(project)
        cleaned_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None

        if not _is_db_available(self.db_url):
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de accesibilidad social no disponible: la base de datos no responde."
                )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            if cleaned_key:
                prev = conn.execute(
                    """
                    SELECT id, project_id, project_version_id, status, summary, results_json,
                           equity_json, social_score, score_explanation, evaluated_at, idempotency_key
                    FROM social_accessibility_assessments
                    WHERE idempotency_key = %s
                    """,
                    (cleaned_key,),
                ).fetchone()
                if prev:
                    if prev["project_id"] != project_id:
                        raise ConfiguracionPMV1Error("Idempotency-Key ya fue utilizada para otro expediente")
                    return {
                        "project_id": prev["project_id"],
                        "project_code": project["code"],
                        "project_version_id": prev["project_version_id"],
                        "status": prev["status"],
                        "summary": prev["summary"],
                        "categories": prev["results_json"]["categories"],
                        "equity": prev.get("equity_json") or prev["results_json"].get("equity"),
                        "social_score": float(prev["social_score"]) if prev.get("social_score") is not None else None,
                        "score_explanation": prev.get("score_explanation"),
                        "evaluated_at": prev["evaluated_at"],
                        "duplicated": True,
                    }

            rows = conn.execute(
                """
                SELECT id, project_id, project_code, category, indicator_name,
                       metric_value, metric_unit, has_sufficient_data, source_name,
                       data_origin, description,
                       (data_origin = 'simulated') AS is_simulated
                FROM territorial_social_indicators
                WHERE project_id = %s OR project_code = %s
                ORDER BY created_at ASC
                """,
                (project_id, project["code"]),
            ).fetchall()

            equity_rows = conn.execute(
                """
                SELECT id, project_id, project_code, dimension, has_sufficient_data,
                       target_group, aggregated_population, aggregated_percentage,
                       equity_criterion, declared_benefit, verifiable_data,
                       has_favorable_evidence, score_effect_pts, effect_type,
                       effect_explanation, source_name, data_origin, description,
                       (data_origin = 'simulated') AS is_simulated
                FROM project_demographic_equity_records
                WHERE project_id = %s OR project_code = %s
                ORDER BY dimension ASC
                """,
                (project_id, project["code"]),
            ).fetchall()

            assessment = SocialAccessibilityEngine.evaluate(
                project_id=project_id,
                project_code=project["code"],
                project_version_id=project.get("project_version_id"),
                indicator_rows=[dict(r) for r in rows],
                equity_rows=[dict(r) for r in equity_rows],
            )

            assessment_id = uuid4()
            effective_key = cleaned_key or f"social-assessment-{assessment_id}"
            now = assessment["evaluated_at"]

            conn.execute(
                """
                INSERT INTO social_accessibility_assessments
                (id, project_id, project_version_id, requested_by_user_id, idempotency_key,
                 status, summary, results_json, equity_json, social_score, score_explanation, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    assessment_id,
                    project_id,
                    project["project_version_id"],
                    actor_id,
                    effective_key,
                    assessment["status"],
                    assessment["summary"],
                    Jsonb({"categories": assessment["categories"], "equity": assessment.get("equity")}),
                    Jsonb(assessment["equity"]) if assessment.get("equity") else None,
                    assessment.get("social_score"),
                    assessment.get("score_explanation"),
                    now,
                ),
            )

            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'SOCIAL_ACCESSIBILITY_EVALUATED', 'social_assessment', %s, %s)
                """,
                (
                    actor_id,
                    assessment_id,
                    Jsonb(
                        {
                            "project_id": str(project_id),
                            "project_code": project["code"],
                            "status": assessment["status"],
                            "categories_count": len(assessment["categories"]),
                        }
                    ),
                ),
            )

        return {
            **assessment,
            "duplicated": False,
        }

    def get_social_accessibility(self, project_id: UUID) -> dict[str, Any]:
        """Obtiene la última evaluación de accesibilidad social o genera el análisis si aún no fue guardado."""
        project = self.get_project(project_id)

        if not _is_db_available(self.db_url):
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de accesibilidad social no disponible: la base de datos no responde."
                )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            latest = conn.execute(
                """
                SELECT id, project_id, project_version_id, status, summary, results_json,
                       equity_json, social_score, score_explanation, evaluated_at
                FROM social_accessibility_assessments
                WHERE project_id = %s
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()

            if latest:
                return {
                    "project_id": latest["project_id"],
                    "project_code": project["code"],
                    "project_version_id": latest["project_version_id"],
                    "status": latest["status"],
                    "summary": latest["summary"],
                    "categories": latest["results_json"]["categories"],
                    "equity": latest.get("equity_json") or latest["results_json"].get("equity"),
                    "social_score": float(latest["social_score"]) if latest.get("social_score") is not None else None,
                    "score_explanation": latest.get("score_explanation"),
                    "evaluated_at": latest["evaluated_at"],
                    "duplicated": False,
                }

            rows = conn.execute(
                """
                SELECT id, project_id, project_code, category, indicator_name,
                       metric_value, metric_unit, has_sufficient_data, source_name,
                       data_origin, description,
                       (data_origin = 'simulated') AS is_simulated
                FROM territorial_social_indicators
                WHERE project_id = %s OR project_code = %s
                ORDER BY created_at ASC
                """,
                (project_id, project["code"]),
            ).fetchall()

            equity_rows = conn.execute(
                """
                SELECT id, project_id, project_code, dimension, has_sufficient_data,
                       target_group, aggregated_population, aggregated_percentage,
                       equity_criterion, declared_benefit, verifiable_data,
                       has_favorable_evidence, score_effect_pts, effect_type,
                       effect_explanation, source_name, data_origin, description,
                       (data_origin = 'simulated') AS is_simulated
                FROM project_demographic_equity_records
                WHERE project_id = %s OR project_code = %s
                ORDER BY dimension ASC
                """,
                (project_id, project["code"]),
            ).fetchall()

            assessment = SocialAccessibilityEngine.evaluate(
                project_id=project_id,
                project_code=project["code"],
                project_version_id=project.get("project_version_id"),
                indicator_rows=[dict(r) for r in rows],
                equity_rows=[dict(r) for r in equity_rows],
            )
            return {
                **assessment,
                "duplicated": False,
            }

    def evaluate_environmental_assessment(
        self,
        project_id: UUID,
        actor_id: UUID,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Evalúa el impacto ambiental territorial del proyecto (HU1.6)."""
        project = self.get_project(project_id)
        self._ensure_complete_project(project)
        cleaned_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None

        if not _is_db_available(self.db_url):
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de evaluación ambiental no disponible: la base de datos no responde."
                )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
            if cleaned_key:
                prev = conn.execute(
                    """
                    SELECT id, project_id, project_version_id, status, risk_level,
                           risk_observation, requires_human_review, favorable, summary,
                           declared_vs_evidence_json, impacts_json, mitigations_json,
                           green_area_json, environmental_score, score_explanation,
                           evaluated_at, idempotency_key
                    FROM environmental_assessments
                    WHERE idempotency_key = %s
                    """,
                    (cleaned_key,),
                ).fetchone()
                if prev:
                    if prev["project_id"] != project_id:
                        raise ConfiguracionPMV1Error("Idempotency-Key ya fue utilizada para otro expediente")
                    return {
                        "project_id": prev["project_id"],
                        "project_code": project["code"],
                        "project_version_id": prev["project_version_id"],
                        "status": prev["status"],
                        "risk_level": prev["risk_level"],
                        "risk_level_label": {
                            "low": "Riesgo Bajo",
                            "moderate": "Riesgo Moderado",
                            "high": "Riesgo Alto",
                            "insufficient": "Información insuficiente",
                        }.get(prev["risk_level"], prev["risk_level"]),
                        "risk_observation": prev["risk_observation"],
                        "requires_human_review": prev["requires_human_review"],
                        "favorable": prev["favorable"],
                        "favorable_label": "Favorable condicionado a cumplimiento de mitigaciones" if prev["favorable"] is True else (
                            "No favorable - Requiere revisión técnica ambiental humana obligatoria" if prev["risk_level"] == "high" else (
                                "Desfavorable: puntaje ambiental insuficiente por afectación no mitigada de áreas verdes" if prev["favorable"] is False else
                                "Sin conclusión favorable: información insuficiente"
                            )
                        ),
                        "summary": prev["summary"],
                        "declared_vs_evidence": prev["declared_vs_evidence_json"],
                        "impacts": prev["impacts_json"],
                        "mitigations": prev["mitigations_json"],
                        "green_area": prev.get("green_area_json"),
                        "environmental_score": float(prev["environmental_score"]) if prev.get("environmental_score") is not None else None,
                        "score_explanation": prev.get("score_explanation"),
                        "evaluated_at": prev["evaluated_at"],
                        "duplicated": True,
                    }

            pv_id = project.get("project_version_id")
            ver_num = project.get("version_number", 1)

            rows = conn.execute(
                """
                SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                       environmental_component, declared_impacts, declared_measures,
                       verifiable_evidence, proximity_to_water_meters,
                       is_in_protected_or_risk_area, source_name, data_origin, description
                FROM project_environmental_records
                WHERE project_version_id = %s
                ORDER BY created_at ASC
                """,
                (pv_id,),
            ).fetchall()
            if not rows and ver_num == 1:
                rows = conn.execute(
                    """
                    SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                           environmental_component, declared_impacts, declared_measures,
                           verifiable_evidence, proximity_to_water_meters,
                           is_in_protected_or_risk_area, source_name, data_origin, description
                    FROM project_environmental_records
                    WHERE (project_id = %s OR project_code = %s) AND project_version_id IS NULL
                    ORDER BY created_at ASC
                    """,
                    (project_id, project["code"]),
                ).fetchall()

            green_rows = conn.execute(
                """
                SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                       total_project_area_m2, green_area_m2, green_area_percentage,
                       delimitation_description, declared_affectation, verifiable_affectation,
                       affectation_type, protection_measures, has_sufficient_measures,
                       score_effect_pts, effect_type, calculation_explanation,
                       source_name, data_origin, description,
                       (data_origin = 'simulated') AS is_simulated
                FROM project_green_area_records
                WHERE project_version_id = %s
                ORDER BY created_at ASC
                """,
                (pv_id,),
            ).fetchall()
            if not green_rows and ver_num == 1:
                green_rows = conn.execute(
                    """
                    SELECT id, project_id, project_version_id, project_code, has_sufficient_data,
                           total_project_area_m2, green_area_m2, green_area_percentage,
                           delimitation_description, declared_affectation, verifiable_affectation,
                           affectation_type, protection_measures, has_sufficient_measures,
                           score_effect_pts, effect_type, calculation_explanation,
                           source_name, data_origin, description,
                           (data_origin = 'simulated') AS is_simulated
                    FROM project_green_area_records
                    WHERE (project_id = %s OR project_code = %s) AND project_version_id IS NULL
                    ORDER BY created_at ASC
                    """,
                    (project_id, project["code"]),
                ).fetchall()

            assessment = EnvironmentalAssessmentEngine.evaluate(
                project_id=project_id,
                project_code=project["code"],
                project_version_id=project.get("project_version_id"),
                records=[dict(r) for r in rows],
                green_records=[dict(g) for g in green_rows],
            )

            assessment_id = uuid4()
            effective_key = cleaned_key or f"env-assessment-{assessment_id}"
            now = assessment["evaluated_at"]

            conn.execute(
                """
                INSERT INTO environmental_assessments
                (id, project_id, project_version_id, assessed_by_user_id, idempotency_key,
                 status, risk_level, risk_observation, requires_human_review, favorable,
                 summary, declared_vs_evidence_json, impacts_json, mitigations_json,
                 green_area_json, environmental_score, score_explanation, evaluated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    assessment_id,
                    project_id,
                    project["project_version_id"],
                    actor_id,
                    effective_key,
                    assessment["status"],
                    assessment["risk_level"],
                    assessment["risk_observation"],
                    assessment["requires_human_review"],
                    assessment["favorable"],
                    assessment["summary"],
                    Jsonb(assessment["declared_vs_evidence"]),
                    Jsonb(assessment["impacts"]),
                    Jsonb(assessment["mitigations"]),
                    Jsonb(assessment["green_area"]) if assessment.get("green_area") else None,
                    assessment.get("environmental_score"),
                    assessment.get("score_explanation"),
                    now,
                ),
            )

            conn.execute(
                """
                INSERT INTO audit_events
                (actor_user_id, action, entity_type, entity_id, details)
                VALUES (%s, 'ENVIRONMENTAL_ASSESSMENT_EVALUATED', 'environmental_assessment', %s, %s)
                """,
                (
                    actor_id,
                    assessment_id,
                    Jsonb(
                        {
                            "project_id": str(project_id),
                            "project_code": project["code"],
                            "risk_level": assessment["risk_level"],
                            "requires_human_review": assessment["requires_human_review"],
                            "favorable": assessment["favorable"],
                        }
                    ),
                ),
            )

            checkpoint = None
            if (
                assessment.get("risk_level") == "high"
                and assessment.get("requires_human_review")
            ):
                # HU3.1: el hallazgo ambiental debe detener el flujo en el mismo
                # acto que lo registra. Esperar a una solicitud económica posterior
                # dejaba disponibles otras etapas y no constituía una pausa real.
                risk_info = RiskCheckpointEngine.detect_high_risk_condition(
                    conn, project["project_version_id"]
                )
                if risk_info:
                    checkpoint = RiskCheckpointEngine.create_or_get_checkpoint(
                        conn,
                        project_id=project_id,
                        project_version_id=project["project_version_id"],
                        evaluation_id=None,
                        risk_info=risk_info,
                    )

        return {
            **assessment,
            "checkpoint": checkpoint,
            "duplicated": False,
        }

    def get_environmental_assessment(self, project_id: UUID) -> dict[str, Any]:
        """Obtiene la última evaluación ambiental o genera el análisis preliminar si aún no fue guardado."""
        project = self.get_project(project_id)

        if not _is_db_available(self.db_url):
            if not self.enable_demo_fallback:
                raise ServicioNoDisponibleError(
                    "Servicio de evaluación ambiental no disponible: la base de datos no responde."
                )

        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            latest = conn.execute(
                """
                SELECT id, project_id, project_version_id, status, risk_level,
                       risk_observation, requires_human_review, favorable, summary,
                       declared_vs_evidence_json, impacts_json, mitigations_json,
                       green_area_json, environmental_score, score_explanation, evaluated_at
                FROM environmental_assessments
                WHERE project_id = %s
                ORDER BY evaluated_at DESC
                LIMIT 1
                """,
                (project_id,),
            ).fetchone()

            if latest:
                return {
                    "project_id": latest["project_id"],
                    "project_code": project["code"],
                    "project_version_id": latest["project_version_id"],
                    "status": latest["status"],
                    "risk_level": latest["risk_level"],
                    "risk_level_label": {
                        "low": "Riesgo Bajo",
                        "moderate": "Riesgo Moderado",
                        "high": "Riesgo Alto",
                        "insufficient": "Información insuficiente",
                    }.get(latest["risk_level"], latest["risk_level"]),
                    "risk_observation": latest["risk_observation"],
                    "requires_human_review": latest["requires_human_review"],
                    "favorable": latest["favorable"],
                    "favorable_label": "Favorable condicionado a cumplimiento de mitigaciones" if latest["favorable"] is True else (
                        "No favorable - Requiere revisión técnica ambiental humana obligatoria" if latest["risk_level"] == "high" else (
                            "Desfavorable: puntaje ambiental insuficiente por afectación no mitigada de áreas verdes" if latest["favorable"] is False else
                            "Sin conclusión favorable: información insuficiente"
                        )
                    ),
                    "summary": latest["summary"],
                    "declared_vs_evidence": latest["declared_vs_evidence_json"],
                    "impacts": latest["impacts_json"],
                    "mitigations": latest["mitigations_json"],
                    "green_area": latest.get("green_area_json"),
                    "environmental_score": float(latest["environmental_score"]) if latest.get("environmental_score") is not None else None,
                    "score_explanation": latest.get("score_explanation"),
                    "evaluated_at": latest["evaluated_at"],
                    "duplicated": False,
                }

            rows = conn.execute(
                """
                SELECT id, project_id, project_code, has_sufficient_data,
                       environmental_component, declared_impacts, declared_measures,
                       verifiable_evidence, proximity_to_water_meters,
                       is_in_protected_or_risk_area, source_name, data_origin, description
                FROM project_environmental_records
                WHERE project_id = %s OR project_code = %s
                ORDER BY created_at ASC
                """,
                (project_id, project["code"]),
            ).fetchall()

            green_rows = conn.execute(
                """
                SELECT id, project_id, project_code, has_sufficient_data,
                       total_project_area_m2, green_area_m2, green_area_percentage,
                       delimitation_description, declared_affectation, verifiable_affectation,
                       affectation_type, protection_measures, has_sufficient_measures,
                       score_effect_pts, effect_type, calculation_explanation,
                       source_name, data_origin, description,
                       (data_origin = 'simulated') AS is_simulated
                FROM project_green_area_records
                WHERE project_id = %s OR project_code = %s
                ORDER BY created_at ASC
                """,
                (project_id, project["code"]),
            ).fetchall()

            assessment = EnvironmentalAssessmentEngine.evaluate(
                project_id=project_id,
                project_code=project["code"],
                project_version_id=project.get("project_version_id"),
                records=[dict(r) for r in rows],
                green_records=[dict(g) for g in green_rows],
            )
            return {
                **assessment,
                "duplicated": False,
            }


class SocialAccessibilityEngine:
    """Motor de evaluación social territorial: accesibilidad (HU1.4) y equidad demográfica (HU1.5).

    Garantiza el cumplimiento estricto de los criterios de aceptación:
    - CA-HU1.4-01 / CA-HU1.4-02: Presenta por separado salud, educación y transporte.
    - CA-HU1.5-01: Muestra cómo los indicadores agregados de género y edad influyen en el resultado social.
    - CA-HU1.5-02: Cuando falten datos fiables, señala la limitación, no infiere proporciones y no asigna
      resultado favorable sin evidencia (efecto no determinado / None, nunca 0.0 pts ni ventaja automática).
    """

    CANONICAL_CATEGORIES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("salud", "Accesibilidad a Salud"),
        ("educacion", "Accesibilidad a Educación"),
        ("transporte", "Accesibilidad a Transporte"),
    )

    @classmethod
    def build_category_item(
        cls,
        category: str,
        category_label: str,
        indicator_row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not indicator_row or not indicator_row.get("has_sufficient_data") or indicator_row.get("metric_value") is None:
            source = indicator_row.get("source_name") if indicator_row else "Registro Catastral Municipal El Tambo (Sin cobertura)"
            data_origin = indicator_row.get("data_origin") if indicator_row else "simulated"
            is_sim = indicator_row.get("is_simulated", True) if indicator_row else True
            return {
                "category": category,
                "category_label": category_label,
                "status": "insufficient",
                "has_sufficient_data": False,
                "indicator": indicator_row.get("indicator_name") if indicator_row else f"Indicador territorial de {category}",
                "value": None,
                "unit": None,
                "source": source,
                "data_origin": data_origin,
                "is_simulated": is_sim,
                "message": "Información insuficiente",
            }

        val = float(indicator_row["metric_value"])
        unit = indicator_row["metric_unit"]
        ind_name = indicator_row["indicator_name"]
        source = indicator_row["source_name"]
        data_origin = indicator_row["data_origin"]
        is_sim = indicator_row.get("is_simulated", True)
        desc = indicator_row.get("description") or f"{ind_name}: {val} {unit}"

        return {
            "category": category,
            "category_label": category_label,
            "status": "sufficient",
            "has_sufficient_data": True,
            "indicator": ind_name,
            "value": val,
            "unit": unit,
            "source": source,
            "data_origin": data_origin,
            "is_simulated": is_sim,
            "message": desc,
        }

    @classmethod
    def _evaluate_dimension(
        cls,
        dim_key: Literal["genero", "edad"],
        dim_label: str,
        row: dict[str, Any] | None,
    ) -> dict[str, Any]:
        default_target = "Mujeres y niñas del área de influencia" if dim_key == "genero" else "Niñez (<12) y adultos mayores (≥65)"
        default_criterion = (
            "Priorización de equipamiento seguro y reducción de brechas con enfoque de género"
            if dim_key == "genero"
            else "Acceso universal y equipamiento adaptado para niñez y personas adultas mayores"
        )
        default_source = "Catastro Municipal El Tambo (Sin cobertura demográfica desagregada)"

        if not row or not row.get("has_sufficient_data") or row.get("aggregated_percentage") is None:
            return {
                "dimension": dim_key,
                "dimension_label": dim_label,
                "target_group": row.get("target_group") if row else default_target,
                "has_sufficient_data": False,
                "aggregated_population": None,
                "aggregated_percentage": None,
                "equity_criterion": row.get("equity_criterion") if row else default_criterion,
                "declared_benefit": row.get("declared_benefit") if row else None,
                "verifiable_data": None,
                "has_favorable_evidence": False,
                "score_effect_pts": None,
                "effect_type": "indeterminado",
                "effect_explanation": (
                    f"Información insuficiente: no se dispone de datos demográficos fiables para {dim_label.lower()}. "
                    "Conforme a la regla de precaución (CA-HU1.5-02), no se infieren proporciones ni se otorga bonificación sin evidencia."
                ),
                "source_name": row.get("source_name") if row else default_source,
                "data_origin": row.get("data_origin", "simulated") if row else "simulated",
                "is_simulated": True,
            }

        pop = int(row["aggregated_population"]) if row.get("aggregated_population") is not None else None
        pct = float(row["aggregated_percentage"])
        pts = float(row["score_effect_pts"]) if row.get("score_effect_pts") is not None else 0.0
        effect_t = str(row.get("effect_type", "neutro"))
        fav_ev = bool(row.get("has_favorable_evidence", False))
        expl = row.get("effect_explanation") or f"Efecto {effect_t} ({pts:+.1f} pts) en {dim_label.lower()}."

        return {
            "dimension": dim_key,
            "dimension_label": dim_label,
            "target_group": row.get("target_group") or default_target,
            "has_sufficient_data": True,
            "aggregated_population": pop,
            "aggregated_percentage": pct,
            "equity_criterion": row.get("equity_criterion") or default_criterion,
            "declared_benefit": row.get("declared_benefit"),
            "verifiable_data": row.get("verifiable_data"),
            "has_favorable_evidence": fav_ev,
            "score_effect_pts": pts,
            "effect_type": effect_t,
            "effect_explanation": expl,
            "source_name": row.get("source_name") or default_source,
            "data_origin": row.get("data_origin", "simulated"),
            "is_simulated": row.get("data_origin", "simulated") == "simulated",
        }

    @classmethod
    def _evaluate_demographic_equity(
        cls,
        equity_rows: list[dict[str, Any]] | None,
        base_score: float | None,
    ) -> tuple[dict[str, Any], float | None, str]:
        rows_by_dim = {str(r["dimension"]).lower(): r for r in (equity_rows or [])}
        gender_item = cls._evaluate_dimension("genero", "Equidad de Género", rows_by_dim.get("genero"))
        age_item = cls._evaluate_dimension("edad", "Equidad de Edad", rows_by_dim.get("edad"))

        has_sufficient_data = gender_item["has_sufficient_data"] and age_item["has_sufficient_data"]
        any_sufficient = gender_item["has_sufficient_data"] or age_item["has_sufficient_data"]

        if has_sufficient_data:
            g_pts = gender_item["score_effect_pts"] or 0.0
            a_pts = age_item["score_effect_pts"] or 0.0
            equity_pts = g_pts + a_pts
            summary_eq = (
                f"Evaluación de equidad completa: género ({gender_item['effect_type']}, {g_pts:+.1f} pts) "
                f"y edad ({age_item['effect_type']}, {a_pts:+.1f} pts)."
            )
        elif any_sufficient:
            equity_pts = None
            dim_ok = "género" if gender_item["has_sufficient_data"] else "edad"
            dim_missing = "edad" if not age_item["has_sufficient_data"] else "género"
            summary_eq = (
                f"Evaluación de equidad parcial: datos suficientes en {dim_ok}, pero información insuficiente en {dim_missing}. "
                "No se infieren proporciones ni ventaja automática para el grupo faltante (CA-HU1.5-02)."
            )
        else:
            equity_pts = None
            summary_eq = (
                "Información insuficiente en género y edad: no se dispone de datos demográficos fiables. "
                "Conforme a CA-HU1.5-02, no se infieren proporciones ni se otorga bonificación automática sin evidencia."
            )

        equity_dict = {
            "has_sufficient_data": has_sufficient_data,
            "gender": gender_item,
            "age": age_item,
            "equity_score_pts": equity_pts,
            "effect_summary": summary_eq,
        }

        if base_score is None or not any_sufficient:
            final_social_score = None
            score_expl = (
                "Puntaje social no determinado: información insuficiente en accesibilidad territorial o datos demográficos "
                "de equidad para ponderación cuantitativa (CA-HU1.4-02 / CA-HU1.5-02)."
            )
        else:
            g_pts = gender_item["score_effect_pts"] if gender_item["score_effect_pts"] is not None else 0.0
            a_pts = age_item["score_effect_pts"] if age_item["score_effect_pts"] is not None else 0.0
            final_social_score = min(100.0, max(0.0, base_score + g_pts + a_pts))

            g_desc = f"{gender_item['score_effect_pts']:+.1f} pts" if gender_item["score_effect_pts"] is not None else "No determinado"
            a_desc = f"{age_item['score_effect_pts']:+.1f} pts" if age_item["score_effect_pts"] is not None else "No determinado"

            score_expl = (
                f"Puntaje social preliminar: {final_social_score:.1f}/100 pts "
                f"(Base accesibilidad: {base_score:.1f} pts, Género: {g_desc}, Edad: {a_desc}). "
                "Regla de demostración para el prototipo PMV2 (HU1.5)."
            )

        return equity_dict, final_social_score, score_expl

    @classmethod
    def evaluate(
        cls,
        project_id: UUID,
        project_code: str,
        project_version_id: UUID | None,
        indicator_rows: list[dict[str, Any]],
        equity_rows: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        rows_by_category = {str(r["category"]).lower(): r for r in indicator_rows}
        categories_output: list[dict[str, Any]] = []

        insufficient_count = 0
        for cat_key, cat_label in cls.CANONICAL_CATEGORIES:
            row = rows_by_category.get(cat_key)
            item = cls.build_category_item(cat_key, cat_label, row)
            if not item["has_sufficient_data"]:
                insufficient_count += 1
            categories_output.append(item)

        if insufficient_count == 0:
            base_score = 70.0
            cat_summary = "Análisis de accesibilidad social completo (salud, educación y transporte)."
        elif insufficient_count == len(cls.CANONICAL_CATEGORIES):
            base_score = None
            cat_summary = "Información insuficiente: no se dispone de datos de cobertura para salud, educación o transporte."
        else:
            base_score = 50.0
            cat_summary = (
                f"Análisis de accesibilidad social parcial: {len(cls.CANONICAL_CATEGORIES) - insufficient_count} "
                f"categoría(s) con datos suficientes y {insufficient_count} con Información insuficiente."
            )

        if equity_rows is not None:
            equity_dict, social_score, score_expl = cls._evaluate_demographic_equity(
                equity_rows=equity_rows,
                base_score=base_score,
            )
            # Estado global integrado con equidad
            if insufficient_count == 0 and equity_dict["has_sufficient_data"]:
                status = "complete"
                summary = f"{cat_summary} Se integraron indicadores demográficos de género y edad con criterios de equidad."
            elif insufficient_count == len(cls.CANONICAL_CATEGORIES) and not equity_dict["has_sufficient_data"]:
                status = "insufficient"
                summary = "Información insuficiente en accesibilidad y equidad social: no se emite resultado favorable por defecto."
            else:
                status = "partial"
                summary = f"{cat_summary} {equity_dict['effect_summary']}"
        else:
            equity_dict = None
            social_score = base_score
            score_expl = (
                f"Puntaje Base de Accesibilidad Territorial: {base_score:.1f} pts."
                if base_score is not None
                else "Información insuficiente de accesibilidad."
            )
            if insufficient_count == 0:
                status = "complete"
                summary = cat_summary
            elif insufficient_count == len(cls.CANONICAL_CATEGORIES):
                status = "insufficient"
                summary = cat_summary
            else:
                status = "partial"
                summary = cat_summary

        return {
            "project_id": project_id,
            "project_code": project_code,
            "project_version_id": project_version_id,
            "status": status,
            "summary": summary,
            "categories": categories_output,
            "equity": equity_dict,
            "social_score": social_score,
            "score_explanation": score_expl,
            "evaluated_at": datetime.now(timezone.utc),
        }


class EnvironmentalAssessmentEngine:
    """Motor de evaluación de impacto ambiental territorial (HU1.6).

    Cumple estrictamente los criterios de aceptación:
    - CA-HU1.6-01: Muestra impactos identificados, nivel de riesgo estimado, sustento y
      posibles mitigaciones, diferenciando lo declarado en el expediente de la evidencia verificable.
    - CA-HU1.6-02: Ante posible impacto alto, registra una observación de riesgo que requiere
      consideración humana obligatoria antes de cualquier decisión favorable (prohibiendo aprobación automática).
    - CA-HU1.6-03: Si faltan datos ambientales esenciales, muestra 'Información insuficiente'
      y nunca una conclusión favorable por defecto (favorable=None/False).
    """

    @classmethod
    def _evaluate_green_area(
        cls,
        green_records: list[dict[str, Any]] | None,
        base_score: float | None,
        is_high_risk: bool,
    ) -> tuple[dict[str, Any], float | None, str]:
        g_rec = green_records[0] if green_records else None
        if not g_rec or not g_rec.get("has_sufficient_data"):
            green_dict = {
                "has_sufficient_data": False,
                "total_project_area_m2": None,
                "green_area_m2": None,
                "green_area_percentage": None,
                "delimitation_description": None,
                "declared_affectation": None,
                "verifiable_affectation": None,
                "affectation_type": None,
                "affectation_type_label": "No determinada: información insuficiente",
                "protection_measures": None,
                "has_sufficient_measures": False,
                "score_effect_pts": None,
                "effect_type": "indeterminado",
                "calculation_explanation": (
                    "Información insuficiente: no se dispone de superficie, delimitación o sustento suficiente de áreas verdes. "
                    "Conforme a la regla de precaución (CA-HU1.7-02), no se concede ventaja ambiental ni bonificación automática."
                ),
                "source_name": g_rec.get("source_name") if g_rec else "Catastro Municipal El Tambo (Sin catastro verde ni delimitación)",
                "data_origin": "simulated",
                "is_simulated": True,
            }
            return (
                green_dict,
                None,
                "Puntaje ambiental no determinado: información de áreas verdes insuficiente para ponderación cuantitativa (CA-HU1.7-02)."
            )

        total_area = float(g_rec.get("total_project_area_m2") or 0.0)
        green_m2 = float(g_rec.get("green_area_m2") or 0.0)
        green_pct = float(g_rec.get("green_area_percentage") or ((green_m2 / total_area * 100.0) if total_area > 0 else 0.0))
        delimitation = g_rec.get("delimitation_description") or "Delimitación perimétrica no especificada"
        declared_aff = g_rec.get("declared_affectation") or "No declarada"
        verifiable_aff = g_rec.get("verifiable_affectation") or "Sin hallazgos cartográficos"
        aff_type = g_rec.get("affectation_type") or "indeterminada"
        measures = g_rec.get("protection_measures") or "No especificadas"
        suff_measures = bool(g_rec.get("has_sufficient_measures", True))

        aff_labels = {
            "conservacion_positiva": "Conservación activa / Aumento de área verde",
            "afectacion_no_mitigada": "Afectación negativa sin mitigación suficiente",
            "afectacion_mitigada": "Intervención con medidas de mitigación",
            "sin_afectacion": "Sin afectación a cobertura vegetal",
            "indeterminada": "Afectación no determinada",
        }
        aff_label = aff_labels.get(aff_type, aff_type)

        if base_score is None:
            score_effect_pts = None
            effect_type = "indeterminado"
            calc_expl = "Información ambiental base insuficiente: no es posible computar puntaje consolidado."
            explanation = calc_expl
            final_score = None
        else:
            if aff_type in ("conservacion_positiva", "sin_afectacion") and suff_measures:
                score_effect_pts = 15.0
                effect_type = "bonus"
                calc_expl = (
                    f"Bonificación ambiental (+15.0 pts): el proyecto conserva y protege {green_m2:.2f} m² "
                    f"({green_pct:.1f}% del predio) de áreas verdes con delimitación verificada y medidas de protección suficientes."
                )
            elif aff_type == "afectacion_no_mitigada" or (aff_type == "afectacion_mitigada" and not suff_measures):
                score_effect_pts = -20.0
                effect_type = "penalizacion"
                calc_expl = (
                    f"Deducción ambiental (-20.0 pts): el proyecto genera afectación/reducción de {green_m2:.2f} m² "
                    f"de área verde sin medidas de compensación suficientes ({measures})."
                )
            else:
                score_effect_pts = 0.0
                effect_type = "neutro"
                calc_expl = (
                    f"Efecto neutro (0.0 pts): la intervención sobre {green_m2:.2f} m² de área verde "
                    "cuenta con medidas de reposición y compensación equivalentes acreditadas."
                )

            final_score = max(0.0, min(100.0, base_score + score_effect_pts))
            if is_high_risk:
                explanation = (
                    f"Puntaje ambiental preliminar: {final_score:.1f}/100 pts (Base de riesgo alto: {base_score:.1f} pts, {calc_expl}). "
                    "ALERTA DE RIESGO PREVALENTE: La valoración de áreas verdes no convalida ni convierte en favorable el expediente "
                    "ante la presencia de RIESGO AMBIENTAL ALTO (faja marginal fluvial intangible). "
                    "La aprobación automática queda prohibida (CA-HU1.6-02 / CA-HU1.7-01)."
                )
            else:
                explanation = (
                    f"Puntaje ambiental preliminar: {final_score:.1f}/100 pts (Base ambiental: {base_score:.1f} pts, {calc_expl}). "
                    "Regla de demostración para el prototipo PMV2 (HU1.7)."
                )

        green_dict = {
            "has_sufficient_data": True,
            "total_project_area_m2": total_area,
            "green_area_m2": green_m2,
            "green_area_percentage": green_pct,
            "delimitation_description": delimitation,
            "declared_affectation": declared_aff,
            "verifiable_affectation": verifiable_aff,
            "affectation_type": aff_type,
            "affectation_type_label": aff_label,
            "protection_measures": measures,
            "has_sufficient_measures": suff_measures,
            "score_effect_pts": score_effect_pts,
            "effect_type": effect_type,
            "calculation_explanation": calc_expl,
            "source_name": g_rec.get("source_name") or "Dataset territorial simulado SIPRIM El Tambo - Gerencia de Gestión Ambiental MPH (Simulado)",
            "data_origin": g_rec.get("data_origin", "simulated"),
            "is_simulated": g_rec.get("data_origin", "simulated") == "simulated",
        }
        return green_dict, final_score, explanation

    @classmethod
    def evaluate(
        cls,
        project_id: UUID,
        project_code: str,
        project_version_id: UUID | None,
        records: list[dict[str, Any]],
        green_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)

        if not records or any(not r.get("has_sufficient_data") for r in records):
            rec = records[0] if records else None
            declared_vs_evidence = {
                "declared_by_applicant": rec.get("declared_impacts") if rec else None,
                "declared_measures": rec.get("declared_measures") if rec else None,
                "verifiable_evidence": None,
                "evidence_source": rec.get("source_name") if rec else "Catastro Municipal El Tambo (Sin cobertura)",
                "is_simulated": True,
                "proximity_to_water_meters": None,
                "is_in_protected_or_risk_area": None,
            }
            green_item, env_score, score_expl = cls._evaluate_green_area(
                green_records=green_records,
                base_score=None,
                is_high_risk=False,
            )
            return {
                "project_id": project_id,
                "project_code": project_code,
                "project_version_id": project_version_id,
                "status": "insufficient",
                "risk_level": "insufficient",
                "risk_level_label": "Información insuficiente",
                "risk_observation": None,
                "requires_human_review": False,
                "favorable": None,
                "favorable_label": "Sin conclusión favorable: información insuficiente",
                "summary": "Información insuficiente: el expediente no cuenta con datos ambientales esenciales, Declaración de Impacto Ambiental (DIA) ni línea base territorial de demostración disponible.",
                "declared_vs_evidence": declared_vs_evidence,
                "impacts": [],
                "mitigations": [],
                "green_area": green_item,
                "environmental_score": env_score,
                "score_explanation": score_expl,
                "evaluated_at": now,
            }

        is_high_risk = False
        high_risk_reasons = []
        impacts: list[dict[str, Any]] = []
        mitigations: list[dict[str, Any]] = []

        all_declared_impacts = []
        all_declared_measures = []
        all_verifiable_evidence = []
        sources = set()
        min_water_dist: float | None = None
        in_protected_area = False

        for r in records:
            if r.get("declared_impacts"):
                all_declared_impacts.append(r["declared_impacts"])
            if r.get("declared_measures"):
                all_declared_measures.append(r["declared_measures"])
            if r.get("verifiable_evidence"):
                all_verifiable_evidence.append(r["verifiable_evidence"])
            if r.get("source_name"):
                sources.add(r["source_name"])

            prox = r.get("proximity_to_water_meters")
            if prox is not None:
                prox_val = float(prox)
                if min_water_dist is None or prox_val < min_water_dist:
                    min_water_dist = prox_val
                if prox_val < 50.0:
                    is_high_risk = True
                    high_risk_reasons.append(f"Distancia crítica a cuerpo de agua o faja marginal ({prox_val:.1f} m < 50 m reglamentarios).")

            if r.get("is_in_protected_or_risk_area"):
                in_protected_area = True
                is_high_risk = True
                high_risk_reasons.append("Ubicación en zona de riesgo alto/no mitigable o faja marginal intangible según PDM Huancayo / CENEPRED.")

            comp = r.get("environmental_component", "integral")
            if "hidrico" in comp or in_protected_area:
                impacts.append({
                    "component": "recurso_hidrico",
                    "component_label": "Recurso Hídrico y Faja Marginal",
                    "description": "Riesgo de contaminación por vertimientos, escorrentía con contaminantes y alteración del régimen fluvial.",
                    "severity": "alta" if is_high_risk else "moderada",
                    "substantiation": r.get("verifiable_evidence") or "Proximidad a cauce o faja marginal fluvial.",
                    "mitigations": [
                        "Reubicación fuera de la faja marginal intangible según PDM Huancayo",
                        "Instalación de trampas de grasa y desarenadores certificados",
                        "Plan de contingencia obligatorio ante crecidas fluviales"
                    ]
                })
                mitigations.append({
                    "component": "recurso_hidrico",
                    "measure": "Delimitación estricta y retiro obligatorio del cauce fluvial según plano PDM Huancayo.",
                    "type": "preventiva",
                    "origin": "requerimiento_tecnico"
                })
            else:
                impacts.append({
                    "component": "aire_acustico",
                    "component_label": "Emisiones Atmosféricas y Ruido",
                    "description": "Generación temporal de polvo y niveles sonoros diurnos durante obras civiles preliminares.",
                    "severity": "moderada",
                    "substantiation": r.get("verifiable_evidence") or "Obra en zona urbana consolidada.",
                    "mitigations": [
                        "Aspersión periódica de agua para control de polvo",
                        "Cerco perimétrico con malla antipolvo",
                        "Horario de trabajo diurno controlado (08:00 - 17:00 h)"
                    ]
                })
                impacts.append({
                    "component": "suelo_residuos",
                    "component_label": "Gestión de Residuos de Demolición",
                    "description": "Generación de escombros y material excedente de obra civil.",
                    "severity": "baja",
                    "substantiation": "Residuos inertes de concreto y afirmado.",
                    "mitigations": [
                        "Transporte cubierto hacia escombrera autorizada municipal",
                        "Segregación en obra de materiales reutilizables"
                    ]
                })
                mitigations.append({
                    "component": "aire_acustico",
                    "measure": "Aspersión diaria y cerco perimétrico con malla antipolvo.",
                    "type": "preventiva",
                    "origin": "propuesta_expediente"
                })
                mitigations.append({
                    "component": "suelo_residuos",
                    "measure": "Disposición final exclusiva en escombrera autorizada por la Municipalidad de El Tambo.",
                    "type": "correctiva",
                    "origin": "requerimiento_tecnico"
                })

        source_str = " | ".join(sorted(sources)) if sources else "Dataset ambiental simulado SIPRIM El Tambo (Simulado)"
        declared_vs_evidence = {
            "declared_by_applicant": " // ".join(all_declared_impacts) if all_declared_impacts else None,
            "declared_measures": " // ".join(all_declared_measures) if all_declared_measures else None,
            "verifiable_evidence": " // ".join(all_verifiable_evidence) if all_verifiable_evidence else None,
            "evidence_source": source_str,
            "is_simulated": True,
            "proximity_to_water_meters": min_water_dist,
            "is_in_protected_or_risk_area": in_protected_area,
        }

        if is_high_risk:
            risk_observation = (
                "OBSERVACIÓN DE RIESGO AMBIENTAL ALTO: " + " ".join(high_risk_reasons) +
                " Se prohíbe la aprobación automática. Requiere consideración técnica ambiental humana obligatoria antes de cualquier decisión favorable."
            )
            green_item, env_score, score_expl = cls._evaluate_green_area(
                green_records=green_records,
                base_score=25.0,
                is_high_risk=True,
            )
            return {
                "project_id": project_id,
                "project_code": project_code,
                "project_version_id": project_version_id,
                "status": "sufficient",
                "risk_level": "high",
                "risk_level_label": "Riesgo Alto",
                "risk_observation": risk_observation,
                "requires_human_review": True,
                "favorable": None,
                "favorable_label": "No favorable - Requiere revisión técnica ambiental humana obligatoria",
                "summary": "Alerta ambiental: se identificó un nivel de riesgo ambiental ALTO. El expediente requiere pronunciamiento técnico humano formal antes de una decisión favorable.",
                "declared_vs_evidence": declared_vs_evidence,
                "impacts": impacts,
                "mitigations": mitigations,
                "green_area": green_item,
                "environmental_score": env_score,
                "score_explanation": score_expl,
                "evaluated_at": now,
            }

        green_item, env_score, score_expl = cls._evaluate_green_area(
            green_records=green_records,
            base_score=70.0,
            is_high_risk=False,
        )
        is_favorable = True
        fav_label = "Favorable condicionado a cumplimiento de mitigaciones"
        summary_text = "Evaluación ambiental con riesgo MODERADO: impactos de obra mitigables mediante las medidas ambientales programadas."
        if env_score is not None and env_score < 60.0:
            is_favorable = False
            fav_label = "Desfavorable: puntaje ambiental insuficiente por afectación no mitigada de áreas verdes"
            summary_text = (
                f"Evaluación ambiental DESFAVORABLE: puntaje de {env_score:.1f}/100 pts inferior al umbral aprobatorio (60.0 pts) "
                "debido a la afectación no mitigada de áreas verdes."
            )

        return {
            "project_id": project_id,
            "project_code": project_code,
            "project_version_id": project_version_id,
            "status": "sufficient",
            "risk_level": "moderate",
            "risk_level_label": "Riesgo Moderado",
            "risk_observation": None,
            "requires_human_review": False,
            "favorable": is_favorable,
            "favorable_label": fav_label,
            "summary": summary_text,
            "declared_vs_evidence": declared_vs_evidence,
            "impacts": impacts,
            "mitigations": mitigations,
            "green_area": green_item,
            "environmental_score": env_score,
            "score_explanation": score_expl,
            "evaluated_at": now,
        }


class ApprovedSocioeconomicMethodCalculator(Protocol):
    """Puerto de extensión para incorporar el método oficial de retorno socioeconómico cuando sea aprobado."""

    method_name: str
    method_version: str
    formula: str
    required_fields: tuple[str, ...]

    def compute(
        self,
        *,
        budget_pen: Decimal,
        beneficiaries_count: int,
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        ...


class SocioeconomicReturnEngine:
    """
    Motor controlado para retorno socioeconómico (HU1.3 · CA-HU1.3-02 / CA-HU1.3-03).
    Por defecto el cálculo está DESHABILITADO (`_APPROVED_METHOD = None`) porque no existe aún
    un método institucional aprobado en SIPRIM; jamás inventa cifras ni sustituye por un ROI genérico.
    """

    DEFAULT_MISSING_REQUIREMENTS: ClassVar[tuple[str, ...]] = (
        "Método de retorno socioeconómico aprobado formalmente (p. ej., VAN social, TIR social o ratio B/C)",
        "Flujos anuales de beneficios sociales monetizados y fuentes trazables",
        "Flujos anuales de costos de operación y mantenimiento (O&M)",
        "Horizonte de evaluación del proyecto (años)",
        "Tasa social de descuento vigente aprobada (MEF / Invierte.pe)",
    )
    _APPROVED_METHOD: ClassVar[ApprovedSocioeconomicMethodCalculator | None] = None

    @classmethod
    def evaluate(
        cls,
        *,
        budget_pen: Decimal | None,
        beneficiaries_count: int | None,
        socioeconomic_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        missing_base: list[str] = []
        if budget_pen is None or budget_pen <= 0:
            missing_base.append("Presupuesto estimado (> 0)")
        if beneficiaries_count is None or beneficiaries_count <= 0:
            missing_base.append("Cantidad de beneficiarios (> 0)")

        calculator = cls._APPROVED_METHOD
        if calculator is None:
            return {
                "socioeconomic_return": None,
                "socioeconomic_return_status": "not_calculable",
                "socioeconomic_return_label": "No calculable",
                "socioeconomic_return_reason": (
                    "No existe un metodo aprobado de retorno socioeconomico (p. ej. VAN/TIR/B-C) "
                    "ni datos trazables de beneficios monetizados, costos de operacion/mantenimiento "
                    "y tasa social de descuento en el expediente; no se inventa ningun valor."
                ),
                "socioeconomic_return_integration_status": "disabled_pending_method_approval",
                "socioeconomic_return_missing_requirements": [
                    *missing_base,
                    *cls.DEFAULT_MISSING_REQUIREMENTS,
                ],
                "socioeconomic_return_method": None,
                "socioeconomic_return_variables": {},
                "socioeconomic_return_sources": [],
            }

        data = socioeconomic_inputs or {}
        missing_inputs = [*missing_base]
        for req in calculator.required_fields:
            val = data.get(req)
            if val is None or (isinstance(val, (list, dict, str)) and not val):
                missing_inputs.append(req)
        sources = [
            str(s).strip()
            for s in (data.get("sources") or [])
            if isinstance(s, str) and s.strip()
        ]
        if not sources:
            missing_inputs.append("sources (fuentes verificables de beneficios y costos)")

        if missing_inputs:
            return {
                "socioeconomic_return": None,
                "socioeconomic_return_status": "not_calculable",
                "socioeconomic_return_label": "Información insuficiente",
                "socioeconomic_return_reason": (
                    "Faltan entradas trazables para el método aprobado "
                    f"{calculator.method_name} ({calculator.method_version}): "
                    + ", ".join(missing_inputs)
                ),
                "socioeconomic_return_integration_status": "awaiting_complete_inputs",
                "socioeconomic_return_missing_requirements": missing_inputs,
                "socioeconomic_return_method": {
                    "method_name": calculator.method_name,
                    "method_version": calculator.method_version,
                    "formula": calculator.formula,
                },
                "socioeconomic_return_variables": {},
                "socioeconomic_return_sources": sources,
            }

        computed = calculator.compute(
            budget_pen=Decimal(str(budget_pen)),
            beneficiaries_count=int(beneficiaries_count),
            inputs=data,
        )
        return {
            "socioeconomic_return": computed["value"],
            "socioeconomic_return_status": "calculated",
            "socioeconomic_return_label": computed.get("label") or f"{computed['value']}",
            "socioeconomic_return_reason": computed.get("reason")
            or f"Calculado con método aprobado {calculator.method_name} ({calculator.method_version}).",
            "socioeconomic_return_integration_status": "enabled_approved_method",
            "socioeconomic_return_missing_requirements": [],
            "socioeconomic_return_method": {
                "method_name": calculator.method_name,
                "method_version": calculator.method_version,
                "formula": calculator.formula,
            },
            "socioeconomic_return_variables": dict(computed.get("variables") or {}),
            "socioeconomic_return_sources": sources,
        }


class EconomicProcessor:
    """Consumidor propietario exclusivo de economic_db e idempotente por event_id."""

    def __init__(self, db_url: str) -> None:
        self.db_url = _dsn(db_url)

    @staticmethod
    def calculate(
        budget: Decimal,
        beneficiaries: int,
        excellent: Decimal,
        unacceptable: Decimal,
    ) -> tuple[Decimal, Decimal]:
        if budget <= 0 or beneficiaries <= 0 or not 0 < excellent < unacceptable:
            raise ValueError("Datos economicos o umbrales invalidos")
        exact_cost = budget / Decimal(beneficiaries)
        if exact_cost <= excellent:
            score = Decimal(100)
        elif exact_cost >= unacceptable:
            score = Decimal(0)
        else:
            score = (
                Decimal(100) * (unacceptable - exact_cost) / (unacceptable - excellent)
            )
        return (
            exact_cost.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            score.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )

    def process(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = UUID(payload["event_id"])
        evaluation_id = UUID(payload["evaluation_id"])
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn, conn.transaction():
                existing = conn.execute(
                    """SELECT ea.* FROM processed_requests pr
                    JOIN economic_assessments ea ON ea.id=pr.assessment_id
                    WHERE pr.event_id=%s OR pr.evaluation_id=%s""",
                    (event_id, evaluation_id),
                ).fetchone()
                if existing:
                    return self._completion(event_id, existing)

                budget = Decimal(payload["budget_pen"])
                beneficiaries = int(payload["beneficiaries_count"])
                excellent = Decimal(payload["excellent_cost_pen"])
                unacceptable = Decimal(payload["unacceptable_cost_pen"])
                cost, score = self.calculate(
                    budget, beneficiaries, excellent, unacceptable
                )
                assessment_id = uuid4()
                explanation = (
                    f"Costo por beneficiario = S/ {budget} / {beneficiaries} = S/ {cost}. "
                    f"Umbrales: excelente S/ {excellent}, inaceptable S/ {unacceptable}; score {score}/100. "
                    "No representa retorno socioeconomico ni viabilidad oficial."
                )
                row = {
                    "id": assessment_id,
                    "evaluation_id": evaluation_id,
                    "cost_per_beneficiary_pen": cost,
                    "score_0_100": score,
                    "explanation": explanation,
                    "algorithm_version": "economic-cost-per-beneficiary-v1",
                }
                conn.execute(
                    """INSERT INTO economic_assessments
                    (id,evaluation_id,project_version_id,criteria_version_id,budget_snapshot_pen,
                     beneficiaries_snapshot,excellent_cost_snapshot_pen,unacceptable_cost_snapshot_pen,
                     cost_per_beneficiary_pen,score_0_100,explanation,algorithm_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        assessment_id,
                        evaluation_id,
                        UUID(payload["project_version_id"]),
                        UUID(payload["criteria_version_id"]),
                        budget,
                        beneficiaries,
                        excellent,
                        unacceptable,
                        cost,
                        score,
                        explanation,
                        row["algorithm_version"],
                    ),
                )
                conn.execute(
                    """INSERT INTO assessment_metrics (id,assessment_id,metric_code,numeric_value,unit,formula)
                    VALUES (%s,%s,'COST_PER_BENEFICIARY',%s,'PEN/person','budget / beneficiaries'),
                           (%s,%s,'ECONOMIC_SCORE',%s,'points','linear normalization between thresholds')""",
                    (uuid4(), assessment_id, cost, uuid4(), assessment_id, score),
                )
                conn.execute(
                    "INSERT INTO processed_requests (event_id,evaluation_id,assessment_id) VALUES (%s,%s,%s)",
                    (event_id, evaluation_id, assessment_id),
                )
        return self._completion(event_id, row)

    @staticmethod
    def _completion(request_event_id: UUID, row: dict[str, Any]) -> dict[str, Any]:
        return {
            # Estable para que un reintento no genere un segundo evento lógico.
            "event_id": str(
                uuid5(
                    NAMESPACE_URL, f"siprim:economic-completed:{row['evaluation_id']}"
                )
            ),
            "request_event_id": str(request_event_id),
            "event_type": "EconomicEvaluationCompleted",
            "evaluation_id": str(row["evaluation_id"]),
            "economic_assessment_id": str(row["id"]),
            "cost_per_beneficiary_pen": str(row["cost_per_beneficiary_pen"]),
            "score_0_100": str(row["score_0_100"]),
            "explanation": row["explanation"],
            "algorithm_version": row["algorithm_version"],
            "schema_version": 1,
        }


class CompletionProcessor:
    """Proyecta el evento en platform_db, con inbox para tolerar reintentos."""

    def __init__(self, db_url: str) -> None:
        self.db_url = _dsn(db_url)

    def process(self, payload: dict[str, Any]) -> None:
        event_id, evaluation_id = (
            UUID(payload["event_id"]),
            UUID(payload["evaluation_id"]),
        )
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            with conn.transaction():
                inserted = conn.execute(
                    "INSERT INTO inbox_events (event_id) VALUES (%s) ON CONFLICT DO NOTHING RETURNING event_id",
                    (event_id,),
                ).fetchone()
                if not inserted:
                    return

                ev_row = conn.execute(
                    """SELECT e.id, e.status, e.project_version_id, pv.project_id
                    FROM evaluations e
                    JOIN project_versions pv ON pv.id = e.project_version_id
                    WHERE e.id = %s""",
                    (evaluation_id,),
                ).fetchone()

                if not ev_row:
                    return

                project_id = ev_row["project_id"]
                project_version_id = ev_row["project_version_id"]

                # Comprobar si la evaluación ya está en pausa o si existe o surge una condición de riesgo alto
                is_already_paused = (ev_row["status"] == "pending_review")
                existing_checkpoint = RiskCheckpointEngine.get_active_checkpoint(conn, project_version_id)
                risk_info = RiskCheckpointEngine.detect_high_risk_condition(conn, project_version_id)

                if is_already_paused or existing_checkpoint or risk_info:
                    if not existing_checkpoint and risk_info:
                        RiskCheckpointEngine.create_or_get_checkpoint(
                            conn,
                            project_id=project_id,
                            project_version_id=project_version_id,
                            evaluation_id=evaluation_id,
                            risk_info=risk_info,
                        )
                    conn.execute(
                        "UPDATE evaluations SET status='pending_review' WHERE id=%s",
                        (evaluation_id,),
                    )
                    conn.execute(
                        "UPDATE projects SET status='pending_review', updated_at=now() WHERE id=%s",
                        (project_id,),
                    )
                    conn.execute(
                        """
                        UPDATE evaluation_dimension_dispatches
                        SET status = 'pending_review',
                            status_label = 'Pendiente de revisión (HITL)',
                            error_message = NULL,
                            updated_at = now()
                        WHERE project_version_id = %s AND dimension_code = 'economic'
                        """,
                        (project_version_id,),
                    )
                    # Detención estricta: NO insertar en economic_result_projections para evaluaciones pausadas
                    return

                # CA-HU3.1-02: Continuación cuando no hay riesgo alto
                conn.execute(
                    """INSERT INTO economic_result_projections
                    (evaluation_id,economic_assessment_id,cost_per_beneficiary_pen,score_0_100,
                     explanation,algorithm_version) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (evaluation_id) DO UPDATE SET
                        economic_assessment_id = EXCLUDED.economic_assessment_id,
                        cost_per_beneficiary_pen = EXCLUDED.cost_per_beneficiary_pen,
                        score_0_100 = EXCLUDED.score_0_100,
                        explanation = EXCLUDED.explanation,
                        algorithm_version = EXCLUDED.algorithm_version""",
                    (
                        evaluation_id,
                        UUID(payload["economic_assessment_id"]),
                        Decimal(payload["cost_per_beneficiary_pen"]),
                        Decimal(payload["score_0_100"]),
                        payload["explanation"],
                        payload["algorithm_version"],
                    ),
                )
                conn.execute(
                    "UPDATE evaluations SET status='completed',finished_at=now() WHERE id=%s",
                    (evaluation_id,),
                )
                conn.execute(
                    """
                    UPDATE evaluation_dimension_dispatches
                    SET status = 'completed',
                        status_label = 'Completado',
                        result_reference_id = %s,
                        summary = %s,
                        error_message = NULL,
                        updated_at = now()
                    WHERE project_version_id = %s AND dimension_code = 'economic'
                    """,
                    (
                        UUID(payload["economic_assessment_id"]),
                        f"Costo por beneficiario S/ {payload['cost_per_beneficiary_pen']} ({payload['score_0_100']}/100).",
                        project_version_id,
                    ),
                )
                has_sibling_failure = bool(
                    conn.execute(
                        """
                        SELECT 1 FROM evaluation_dimension_dispatches
                        WHERE project_version_id = %s AND status = 'failed'
                        LIMIT 1
                        """,
                        (project_version_id,),
                    ).fetchone()
                )
                if not has_sibling_failure:
                    conn.execute(
                        "UPDATE projects SET status='evaluated',updated_at=now() WHERE id=%s",
                        (project_id,),
                    )
                conn.execute(
                    """INSERT INTO audit_events (action,entity_type,entity_id,details)
                    VALUES ('EVALUATION_COMPLETED','evaluation',%s,%s)""",
                    (
                        evaluation_id,
                        Jsonb(
                            {
                                "event_id": str(event_id),
                                "assessment_id": payload["economic_assessment_id"],
                            }
                        ),
                    ),
                )


class EvaluationStartedProcessor:
    def __init__(self, db_url: str) -> None:
        self.db_url = _dsn(db_url)

    def process(self, evaluation_id: str) -> None:
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            conn.execute(
                """UPDATE evaluations SET status='processing', started_at=COALESCE(started_at,now())
                WHERE id=%s AND status='queued'""",
                (UUID(evaluation_id),),
            )


class OutboxPublisher:
    """Recupera publicaciones que fallaron tras confirmar la transaccion local."""

    def __init__(self, db_url: str, dispatcher: TaskDispatcher) -> None:
        self.db_url = _dsn(db_url)
        self.dispatcher = dispatcher

    def publish_pending(self, limit: int = 50) -> int:
        published = 0
        with psycopg.connect(self.db_url, row_factory=dict_row) as conn:
            rows = conn.execute(
                """SELECT id,payload FROM outbox_events WHERE published_at IS NULL
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s""",
                (limit,),
            ).fetchall()
            for row in rows:
                try:
                    self.dispatcher.dispatch_economic(row["payload"])
                    conn.execute(
                        "UPDATE outbox_events SET published_at=now(),publish_attempts=publish_attempts+1 WHERE id=%s",
                        (row["id"],),
                    )
                    published += 1
                except Exception:
                    conn.execute(
                        "UPDATE outbox_events SET publish_attempts=publish_attempts+1 WHERE id=%s",
                        (row["id"],),
                    )
        return published
