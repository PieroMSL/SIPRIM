from __future__ import annotations

import json
import math
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    email: str = Field(
        min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    password: str = Field(min_length=8, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: UUID
    full_name: str
    roles: list[str]


class RoleCatalogItemResponse(BaseModel):
    code: str
    name: str
    privileged: bool = False


class MunicipalUserResponse(BaseModel):
    id: UUID
    email: str
    full_name: str
    is_active: bool
    roles: list[str]
    created_at: datetime
    updated_at: datetime


class AdminUsersListResponse(BaseModel):
    users: list[MunicipalUserResponse]
    available_roles: list[RoleCatalogItemResponse]


class UserRolesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    roles: list[str] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def validate_roles(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in value:
            code = raw.strip().upper()
            if not code:
                raise ValueError("El codigo de rol no puede estar vacio")
            if code in seen:
                raise ValueError(f"Rol duplicado en la solicitud: {code}")
            seen.add(code)
            normalized.append(code)
        return normalized


class UserRolesUpdateResponse(BaseModel):
    user: MunicipalUserResponse
    message: str


class CurrentUserMeResponse(BaseModel):
    user_id: UUID
    email: str
    full_name: str
    is_active: bool
    roles: list[str]


class PMV1ProjectCreate(BaseModel):
    """Expediente mínimo persistido por ms-platform según el PMV1."""

    model_config = ConfigDict(extra="forbid")
    code: str | None = Field(
        default=None, min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$"
    )
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    location: str | None = Field(default=None, max_length=300)
    proposed_land_use: str | None = Field(default=None, max_length=150)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    coordinates_origin: Literal[
        "declared", "official_cadastre", "field_survey", "public", "simulated"
    ] | None = None
    coordinates_source: str | None = Field(default=None, max_length=240)
    estimated_budget_pen: Decimal | None = Field(
        default=None, max_digits=18, decimal_places=2
    )
    beneficiaries_count: int | None = None

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value

    @field_validator(
        "title",
        "description",
        "location",
        "proposed_land_use",
        "coordinates_source",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def validate_coordinates_pair(self) -> "PMV1ProjectCreate":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Debe proporcionar tanto latitud como longitud, o dejar ambas vacias")
        if self.latitude is not None:
            source = (self.coordinates_source or "").strip()
            if len(source) < 3:
                raise ValueError(
                    "Debe indicar una fuente verificable (coordinates_source, min. 3 caracteres) para registrar coordenadas"
                )
            self.coordinates_source = source
            if self.coordinates_origin is None:
                self.coordinates_origin = "declared"
        else:
            self.coordinates_origin = None
            self.coordinates_source = None
        return self



class CheckpointResponse(BaseModel):
    """Punto de control persistente del flujo de evaluación ante riesgos altos (HU3.1 / HU3.2)."""

    checkpoint_id: UUID | None = None
    project_id: UUID
    project_version_id: UUID
    evaluation_id: UUID | None = None
    checkpoint_type: str = "high_risk_pause"
    status: str = "pending_review"
    status_label: str = "Pendiente de revisión"
    reason: str
    cause: str | None = None
    observation: str | None = None
    risk_source: str
    risk_source_label: str
    risk_level: str = "high"
    risk_level_label: str = "Riesgo Alto"
    requires_human_review: bool = True
    allows_final_approval: bool = False
    details: dict[str, Any] = Field(default_factory=dict)
    resolved_by_user_id: UUID | None = None
    resolution_motive: str | None = None
    resolution_conditions: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CheckpointResumeRequest(BaseModel):
    """Solicitud de reanudación de evaluación desde el checkpoint de riesgo alto (HU3.4)."""

    justification: str = Field(
        ...,
        min_length=5,
        description="Justificación y sustento del responsable autorizado para reanudar el procesamiento.",
    )
    conditions: str | None = Field(
        None,
        description="Condiciones técnicas u operativas impuestas para la continuación.",
    )

    @field_validator("justification", "conditions", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class CheckpointResumeResponse(BaseModel):
    """Respuesta a la reanudación controlada de evaluación desde el checkpoint (HU3.4)."""

    checkpoint_id: UUID
    project_id: UUID
    project_version_id: UUID
    evaluation_id: UUID | None = None
    status: str = "resolved"
    status_label: str = "Reanudado / Resuelto"
    resolved_by_user_id: UUID
    resolution_motive: str
    resolution_conditions: str | None = None
    resolved_at: datetime
    project_status: str
    duplicated: bool = False
    message: str


class PertEstimateCreate(BaseModel):
    """Solicitud de registro o cálculo de estimación PERT (HU1.9)."""

    project_version_id: UUID | None = None
    optimistic_duration: Decimal = Field(description="Duración optimista (a)")
    most_likely_duration: Decimal = Field(description="Duración más probable (m)")
    pessimistic_duration: Decimal = Field(description="Duración pesimista (b)")
    time_unit: str = Field(default="meses", description="Unidad de tiempo (ej. meses, días)")


class PertEstimateResponse(BaseModel):
    """Respuesta con cálculo e incertidumbre PERT (HU1.9)."""

    id: UUID
    project_id: UUID
    project_version_id: UUID
    registered_by_user_id: UUID
    time_unit: str
    optimistic_duration: float
    most_likely_duration: float
    pessimistic_duration: float
    expected_duration: float
    standard_deviation: float
    variance: float
    confidence_interval_low: float
    confidence_interval_high: float
    formula_applied: str
    disclaimer: str
    explanation: str
    created_at: datetime
    updated_at: datetime

class PMV1ProjectResponse(BaseModel):
    project_id: UUID
    project_version_id: UUID
    code: str
    version_number: int
    status: str
    title: str
    description: str
    location: str | None
    proposed_land_use: str | None
    territorial_data_origin: str
    latitude: float | None = None
    longitude: float | None = None
    coordinates_origin: str | None = None
    coordinates_source: str | None = None
    estimated_budget_pen: Decimal
    beneficiaries_count: int
    checkpoint: CheckpointResponse | None = None
    pert_estimate: PertEstimateResponse | None = None
    technical_assessment: "TechnicalFeasibilityResponse | None" = None


class PMV1ValidationResponse(BaseModel):
    complete: bool
    status: str
    missing_fields: list[str]
    message: str


class EvaluationCreate(BaseModel):
    project_version_id: UUID


class EvaluationAccepted(BaseModel):
    evaluation_id: UUID
    event_id: UUID | None = None
    status: str
    status_label: str | None = None
    duplicated: bool
    publication_pending: bool
    checkpoint: CheckpointResponse | None = None


class EconomicResult(BaseModel):
    economic_assessment_id: UUID
    cost_per_beneficiary_pen: Decimal
    score_0_100: Decimal
    explanation: str
    algorithm_version: str
    socioeconomic_return: None = None
    warnings: list[str] = [
        "No se calcula retorno socioeconomico sin beneficios monetizados y trazables."
    ]


class EvaluationResponse(BaseModel):
    evaluation_id: UUID
    project_version_id: UUID
    criteria_version_id: UUID
    status: str
    status_label: str | None = None
    checkpoint: CheckpointResponse | None = None
    result: EconomicResult | None = None


class LegalPrecheckResponse(BaseModel):
    project_id: UUID
    status: str = "requires_review"
    complies: bool | None = None
    sources: list[str] = []
    requires_review: bool = True
    observations: list[str] = [
        "El PMV1 no contiene un corpus normativo versionado para emitir un dictamen de la Ley de Contrataciones.",
        "Se requiere revision de un asesor juridico; el sistema se abstiene de afirmar cumplimiento.",
    ]
    assessment: "LegalAssessmentResponse | None" = None
    findings: list["LegalFindingItem"] = []


class ZoningEvidence(BaseModel):
    document: str
    version: str
    locator: str
    excerpt: str


class ZoningPrecheckResponse(BaseModel):
    review_id: UUID
    project_id: UUID
    project_version_id: UUID
    status: str
    compatible: bool | None
    location: str
    proposed_land_use: str
    territorial_data_origin: str
    evidence: list[ZoningEvidence]
    alerts: list[str]
    limitations: str
    requires_human_review: bool
    duplicated: bool


class NormativeSearchRequest(BaseModel):
    """Solicitud de busqueda automatica en normativas vigentes (HU2.1 / RF12)."""

    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=500)
    document_filter: str | None = Field(default=None, max_length=150)
    only_in_force: bool = True
    limit: int = Field(default=5, ge=1, le=20)

    @field_validator("query", "document_filter", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or None
        return value


class NormativeChunkItem(BaseModel):
    """Fragmento normativo recuperado con trazabilidad de origen y metadatos."""

    id: str
    document_name: str
    short_code: str
    version: str
    topic: str
    content: str
    in_force: bool
    has_alert: bool
    data_origin: str
    relevance_score: float
    location: str | None = None


class NormativeSearchResponse(BaseModel):
    """Resultado de busqueda normativa para el Asesor Juridico (HU2.1)."""

    query: str
    document_filter: str | None
    total_results: int
    results: list[NormativeChunkItem]
    alerts_found: list[str]
    disclaimer: str
    requires_human_review: bool
    duplicated: bool = False


class NormativeDocumentItem(BaseModel):
    """Resumen de norma o plan territorial registrado en el catalogo."""

    short_code: str
    document_name: str
    version: str
    in_force: bool
    data_origin: str
    chunks_count: int


class NormativeDocumentListResponse(BaseModel):
    """Listado del catalogo normativo activo y versionado (RF03)."""

    total_documents: int
    documents: list[NormativeDocumentItem]
    disclaimer: str


class HumanApprovalCreate(BaseModel):
    """Solicitud de dictamen humano formal (aprobacion, rechazo u observacion)."""

    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected", "observed"]
    justification: str = Field(min_length=5, max_length=2000)
    conditions: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_conditions_when_observed(self) -> "HumanApprovalCreate":
        if self.decision == "observed" and (self.conditions is None or not self.conditions.strip()):
            raise ValueError("El dictamen 'observed' requiere especificar condiciones u observaciones")
        if self.conditions is not None:
            self.conditions = self.conditions.strip() if self.conditions.strip() else None
        return self


class HumanApprovalResponse(BaseModel):
    """Dictamen humano formal registrado para un proyecto."""

    approval_id: UUID
    project_id: UUID
    project_version_id: UUID
    decision: str
    justification: str
    conditions: str | None = None
    decided_by_user_id: UUID
    idempotency_key: str
    decided_at: datetime
    duplicated: bool = False


class HumanApprovalStatusResponse(BaseModel):
    """Estado de compuerta humana formal y dictamen actual del proyecto."""

    project_id: UUID
    project_version_id: UUID
    project_status: str
    is_evaluated: bool
    requires_human_approval: bool
    is_paused: bool = False
    pause_reason: str | None = None
    checkpoint: CheckpointResponse | None = None
    current_approval: HumanApprovalResponse | None = None
    has_legal_alert: bool = False
    legal_alert_motive: str | None = None
    legal_alert_norm: str | None = None
    allows_final_approval: bool = True
    blocking_reasons: list[str] = Field(default_factory=list)

class SocialAccessibilityCategoryItem(BaseModel):
    category: str
    category_label: str
    status: str
    has_sufficient_data: bool
    indicator: str | None = None
    value: float | None = None
    unit: str | None = None
    source: str
    data_origin: str
    is_simulated: bool = True
    message: str


class DemographicEquityDimensionItem(BaseModel):
    dimension: Literal["genero", "edad"]
    dimension_label: str
    target_group: str
    has_sufficient_data: bool
    aggregated_population: int | None = None
    aggregated_percentage: float | None = None
    equity_criterion: str
    declared_benefit: str | None = None
    verifiable_data: str | None = None
    has_favorable_evidence: bool = False
    score_effect_pts: float | None = None
    effect_type: Literal["favorable", "neutro", "desfavorable", "indeterminado"] = "indeterminado"
    effect_explanation: str
    source_name: str
    data_origin: str = "simulated"
    is_simulated: bool = True


class DemographicEquityItem(BaseModel):
    has_sufficient_data: bool
    gender: DemographicEquityDimensionItem
    age: DemographicEquityDimensionItem
    equity_score_pts: float | None = None
    effect_summary: str


class SocialAccessibilityResponse(BaseModel):
    project_id: UUID
    project_code: str
    project_version_id: UUID | None = None
    status: str
    summary: str
    categories: list[SocialAccessibilityCategoryItem]
    equity: DemographicEquityItem | None = None
    social_score: float | None = None
    score_explanation: str | None = None
    evaluated_at: datetime
    duplicated: bool = False

class EnvironmentalImpactItem(BaseModel):
    component: str
    component_label: str
    description: str
    severity: Literal["baja", "moderada", "alta", "indeterminada"]
    substantiation: str
    mitigations: list[str]


class EnvironmentalMitigationItem(BaseModel):
    component: str
    measure: str
    type: str
    origin: str


class EnvironmentalDeclaredVsEvidence(BaseModel):
    declared_by_applicant: str | None
    declared_measures: str | None
    verifiable_evidence: str | None
    evidence_source: str
    is_simulated: bool = True
    proximity_to_water_meters: float | None = None
    is_in_protected_or_risk_area: bool | None = None



class GreenAreaRecordItem(BaseModel):
    has_sufficient_data: bool = True
    total_project_area_m2: float | None = None
    green_area_m2: float | None = None
    green_area_percentage: float | None = None
    delimitation_description: str | None = None
    declared_affectation: str | None = None
    verifiable_affectation: str | None = None
    affectation_type: str | None = None
    affectation_type_label: str | None = None
    protection_measures: str | None = None
    has_sufficient_measures: bool = True
    score_effect_pts: float | None = None
    effect_type: Literal["bonus", "penalizacion", "neutro", "indeterminado"] = "indeterminado"
    calculation_explanation: str
    source_name: str
    data_origin: str = "simulated"
    is_simulated: bool = True

class EnvironmentalAssessmentResponse(BaseModel):
    project_id: UUID
    project_code: str
    project_version_id: UUID | None = None
    status: Literal["sufficient", "insufficient"]
    risk_level: Literal["low", "moderate", "high", "insufficient"]
    risk_level_label: str
    risk_observation: str | None = None
    requires_human_review: bool
    favorable: bool | None = None
    favorable_label: str
    summary: str
    declared_vs_evidence: EnvironmentalDeclaredVsEvidence
    impacts: list[EnvironmentalImpactItem]
    mitigations: list[EnvironmentalMitigationItem]
    green_area: GreenAreaRecordItem | None = None
    environmental_score: float | None = None
    score_explanation: str | None = None
    evaluated_at: datetime
    duplicated: bool = False

# =========================================================
# PMV 2 - HU2.2: Citas Jurídicas Verificables
# =========================================================
class NormativeCitationItem(BaseModel):
    fragment_id: str | None = None
    document_name: str | None = None
    short_code: str | None = None
    version: str | None = None
    article_or_section: str | None = None
    fragment_content: str | None = None
    citation_status: Literal["verificable", "no_verificable"]
    is_verifiable: bool
    status_label: str
    limitation_note: str | None = None
    in_force: bool = True
    is_simulated: bool = False
    data_origin: str = "public"
    citation_hash: str | None = None


class LegalFindingItem(BaseModel):
    finding_id: UUID
    finding_key: str
    title: str
    description: str
    complies: bool | None = None
    requires_human_review: bool = True
    citation: NormativeCitationItem


class LegalAlertResolveRequest(BaseModel):
    """Solicitud formal de levantamiento o subsanación de alerta legal (HU2.3)."""

    resolution_action: str = Field(
        ...,
        description="Acción formal ('falso_positivo', 'subsanado_en_expediente', 'excepcion_fundada', 'riesgo_asumido')",
    )
    justification: str = Field(
        ...,
        min_length=5,
        description="Justificación legal formal y motivada del levantamiento de la alerta",
    )
    evidence_reference: str | None = Field(
        None,
        description="Referencia o código de sustento documental probatorio (ej. informe legal, resolución)",
    )


class LegalAlertResolutionResponse(BaseModel):
    """Respuesta al levantamiento de una alerta legal."""

    resolution_id: UUID
    project_id: UUID
    project_version_id: UUID
    resolved_by_user_id: UUID
    resolution_action: str
    justification: str
    evidence_reference: str | None = None
    resolved_at: datetime
    duplicated: bool = False


class LegalAssessmentResponse(BaseModel):
    assessment_id: UUID
    project_id: UUID
    project_code: str
    project_version_id: UUID | None = None
    status: Literal["requires_review", "verified", "insufficient", "alerta_legal"]
    status_label: str
    summary: str
    findings: list[LegalFindingItem]
    total_findings: int
    verifiable_citations_count: int
    unverifiable_citations_count: int
    requires_human_review: bool
    has_legal_alert: bool = False
    legal_alert_motive: str | None = None
    legal_alert_norm: str | None = None
    is_resolved: bool = False
    resolution_justification: str | None = None
    evaluated_at: datetime
    is_simulated_corpus: bool = True
    duplicated: bool = False



class TechnicalCriterionItem(BaseModel):
    """Criterio técnico evaluado dentro de la preevaluación de viabilidad técnica (HU1.8)."""

    criterion_code: str
    criterion_name: str
    normative_reference: str
    normative_version: str
    has_sufficient_data: bool
    parameter_name: str | None = None
    parameter_value_numeric: float | None = None
    parameter_unit: str | None = None
    parameter_value_text: str | None = None
    applied_rule: str
    supporting_document: str | None = None
    source_origin: str
    is_simulated: bool
    status: str  # 'cumple', 'observado', 'informacion_insuficiente'
    status_label: str  # 'Cumple', 'Observado', 'Información insuficiente'
    explanation: str
    correction_guidance: str | None = None


class TechnicalCriteriaSummary(BaseModel):
    """Resumen cuantitativo de criterios evaluados en la preevaluación técnica (HU1.8)."""

    total: int
    cumple: int
    observado: int
    informacion_insuficiente: int
    simulado_no_verificado: int = 0


class TechnicalFeasibilityResponse(BaseModel):
    """Preevaluación multidimensional de viabilidad técnica del proyecto (HU1.8)."""

    assessment_id: UUID
    project_id: UUID
    project_code: str
    project_version_id: UUID
    version_number: int
    status: str = "completed"
    overall_result: str  # 'cumple', 'observado', 'informacion_insuficiente'
    overall_result_label: str
    is_viable: bool
    is_preliminary_opinion: bool = True
    disclaimer: str
    summary: str
    criteria_summary: TechnicalCriteriaSummary
    criteria: list[TechnicalCriterionItem]
    technical_score: float | None = None
    score_explanation: str | None = None
    dimension_conclusion: str
    dimension_justification: str
    evaluated_at: datetime
    duplicated: bool = False


# =========================================================
# PMV 2 - Sprint 2 - HU1.12: Resumen unificado de evaluación multidimensional
# =========================================================

class DimensionSummaryItem(BaseModel):
    """Resumen de una de las cinco dimensiones de evaluación (HU1.12)."""

    dimension_code: str = Field(description="Código de la dimensión ('economic', 'social', 'environmental', 'technical', 'legal')")
    dimension_name: str = Field(description="Nombre en español de la dimensión")
    status: str = Field(description="Estado de la dimensión ('completed', 'pending', 'warning', 'failed', 'blocked')")
    status_label: str = Field(description="Etiqueta legible del estado")
    conclusion: str = Field(description="Conclusión técnica o ejecutiva de la dimensión")
    justification: str = Field(description="Justificación y sustento de la conclusión")
    score: float | None = Field(None, description="Puntuación cuantitativa en escala 0 a 100 si aplica")
    is_simulated: bool = Field(False, description="Indica si la dimensión involucra datos o corpus simulados")
    warning: str | None = Field(None, description="Advertencia normativa o de datos no comprobados")
    has_blocker: bool = Field(False, description="Indica si esta dimensión bloquea la aprobación o priorización")
    blocker_message: str | None = Field(None, description="Detalle del bloqueo si existe")
    evaluated_at: datetime | None = Field(None, description="Fecha de emisión de la evaluación")
    details: dict[str, Any] = Field(default_factory=dict, description="Metadatos complementarios")


class GlobalScoreExplanation(BaseModel):
    """Explicación y desglose de la puntuación global consolidada (HU1.12 - CA-HU1.12-02)."""

    score: float | None = Field(None, description="Puntaje consolidado de 0 a 100, o None si no es calculable")
    is_calculated: bool = Field(False, description="Indica si existen puntuaciones válidas y versión activa para calcular")
    formula: str = Field(description="Fórmula matemática explícita y reproducible")
    criteria_version_id: UUID | None = Field(None, description="ID de la versión de criterios y pesos activa")
    criteria_version_number: int | None = Field(None, description="Número de versión activa de criterios")
    weights: dict[str, float] = Field(default_factory=dict, description="Pesos asignados por criterio normativo")
    input_scores: dict[str, float] = Field(default_factory=dict, description="Puntuaciones de entrada de cada dimensión")
    explanation: str = Field(description="Explicación metodológica de la valoración consolidada")


class UnifiedAssessmentSummaryResponse(BaseModel):
    """Resumen final unificado de evaluación multidimensional (HU1.12)."""

    project_id: UUID
    project_code: str
    project_version_id: UUID
    version_number: int
    is_complete: bool = Field(description="True si las 5 dimensiones finalizaron válidamente")
    status: str = Field(description="Estado general ('completed', 'incomplete', 'blocked')")
    status_label: str
    summary_text: str = Field(description="Resumen ejecutivo integrado para el planificador")
    dimensions: list[DimensionSummaryItem] = Field(description="Consolidación de las 5 dimensiones")
    missing_dimensions: list[str] = Field(default_factory=list, description="Lista de dimensiones pendientes o fallidas")
    global_score: GlobalScoreExplanation
    allows_final_approval: bool = Field(description="True si está habilitado para avanzar a dictamen final")
    blocking_reasons: list[str] = Field(default_factory=list, description="Motivos de detención o bloqueo si los hay")
    generated_at: datetime


class RankingProjectItem(BaseModel):
    """Elemento comparable del ranking de priorizacion en mapa (HU4.1)."""

    rank_position: int = Field(ge=1)
    project_id: UUID
    project_version_id: UUID
    code: str
    version_number: int
    title: str
    status: str
    location: str | None = None
    estimated_budget_pen: Decimal
    beneficiaries_count: int
    global_score: float
    formula: str
    explanation: str
    criteria_version_number: int
    weights: dict[str, float] = Field(default_factory=dict)
    input_scores: dict[str, float] = Field(default_factory=dict)
    tie_break_reason: str | None = None
    has_coordinates: bool
    latitude: float | None = None
    longitude: float | None = None
    coordinates_origin: str | None = None
    coordinates_source: str | None = None
    allows_final_approval: bool
    geospatial_layers_count: int = 0


class NonComparableProjectItem(BaseModel):
    """Expediente excluido del orden numerico por evaluacion incompleta, bloqueada o no comparable."""

    project_id: UUID
    project_version_id: UUID
    code: str
    version_number: int
    title: str
    status: str
    exclusion_code: Literal[
        "incomplete", "blocked", "criteria_version_mismatch", "no_active_criteria"
    ]
    exclusion_reason: str
    missing_dimensions: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    has_coordinates: bool
    latitude: float | None = None
    longitude: float | None = None


class ProjectMapRankingResponse(BaseModel):
    """Respuesta consolidada de ranking y mapa de proyectos (HU4.1)."""

    is_recommendation: bool = True
    recommendation_notice: str
    criteria_version_id: UUID | None = None
    criteria_version_number: int | None = None
    total_projects: int
    comparable_count: int
    mapped_count: int
    unmapped_count: int
    excluded_count: int
    ranked_projects: list[RankingProjectItem] = Field(default_factory=list)
    mapped_projects: list[RankingProjectItem] = Field(default_factory=list)
    unmapped_projects: list[RankingProjectItem] = Field(default_factory=list)
    excluded_projects: list[NonComparableProjectItem] = Field(default_factory=list)
    generated_at: datetime


def _validate_lon_lat_pair(pair: Any) -> list[float]:
    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
        raise ValueError("Cada coordenada GeoJSON debe ser [longitud, latitud]")
    lon, lat = pair[0], pair[1]
    if isinstance(lon, bool) or isinstance(lat, bool) or not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        raise ValueError("Longitud y latitud deben ser numericas")
    lon_f, lat_f = float(lon), float(lat)
    if not (math.isfinite(lon_f) and math.isfinite(lat_f)):
        raise ValueError("Coordenadas GeoJSON no pueden ser NaN o infinito")
    if not (-180.0 <= lon_f <= 180.0 and -90.0 <= lat_f <= 90.0):
        raise ValueError("Coordenadas GeoJSON fuera de rango valido (lon [-180,180], lat [-90,90])")
    return [lon_f, lat_f]


def _validate_ring(ring: Any) -> tuple[list[list[float]], int]:
    if not isinstance(ring, list) or len(ring) < 4:
        raise ValueError("Cada anillo de poligono GeoJSON requiere al menos 4 vertices")
    pts = [_validate_lon_lat_pair(pt) for pt in ring]
    if pts[0] != pts[-1]:
        raise ValueError("Cada anillo de poligono GeoJSON debe estar cerrado (primer y ultimo punto iguales)")
    return pts, len(pts)


def validate_and_sanitize_geojson(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("geojson_data debe ser un objeto GeoJSON")
    serialized = json.dumps(raw)
    if len(serialized.encode("utf-8")) > 65536:
        raise ValueError("El objeto GeoJSON excede el tamano maximo permitido (64 KB)")
    gtype = raw.get("type")
    if gtype not in ("Point", "LineString", "Polygon", "MultiPolygon"):
        raise ValueError("Tipo GeoJSON no soportado; use Point, LineString, Polygon o MultiPolygon")
    coords = raw.get("coordinates")
    if coords is None:
        raise ValueError("El objeto GeoJSON debe incluir 'coordinates'")

    total_vertices = 0
    if gtype == "Point":
        clean_coords: Any = _validate_lon_lat_pair(coords)
        total_vertices = 1
    elif gtype == "LineString":
        if not isinstance(coords, list) or len(coords) < 2:
            raise ValueError("LineString requiere al menos 2 puntos")
        clean_coords = [_validate_lon_lat_pair(pt) for pt in coords]
        total_vertices = len(clean_coords)
    elif gtype == "Polygon":
        if not isinstance(coords, list) or len(coords) < 1:
            raise ValueError("Polygon requiere al menos un anillo")
        clean_rings = []
        for ring in coords:
            pts, cnt = _validate_ring(ring)
            clean_rings.append(pts)
            total_vertices += cnt
        clean_coords = clean_rings
    else:  # MultiPolygon
        if not isinstance(coords, list) or len(coords) < 1:
            raise ValueError("MultiPolygon requiere al menos un poligono")
        clean_polys = []
        for poly in coords:
            if not isinstance(poly, list) or len(poly) < 1:
                raise ValueError("Cada poligono de MultiPolygon requiere al menos un anillo")
            clean_rings = []
            for ring in poly:
                pts, cnt = _validate_ring(ring)
                clean_rings.append(pts)
                total_vertices += cnt
            clean_polys.append(clean_rings)
        clean_coords = clean_polys

    if total_vertices > 500:
        raise ValueError("La geometria GeoJSON excede el limite de 500 vertices")
    return gtype, {"type": gtype, "coordinates": clean_coords}


class GeospatialLayerCreate(BaseModel):
    """Registro de capa geoespacial versionada y con procedencia verificable (HU4.2)."""

    model_config = ConfigDict(extra="forbid")
    layer_type: Literal["influence_area", "beneficiary_coverage", "territorial_zone"]
    layer_name: str = Field(min_length=3, max_length=160)
    meaning: str = Field(min_length=5, max_length=600)
    source_name: str = Field(min_length=3, max_length=240)
    data_origin: Literal[
        "declared", "official_cadastre", "field_survey", "public", "simulated"
    ] = "declared"
    availability_status: Literal["available", "unavailable"] = "available"
    geojson_data: dict[str, Any] | None = None
    aggregate_beneficiaries_count: int | None = Field(default=None, ge=0)

    @field_validator("layer_name", "meaning", "source_name", mode="before")
    @classmethod
    def strip_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @model_validator(mode="after")
    def validate_geometry_by_availability(self) -> "GeospatialLayerCreate":
        if self.availability_status == "unavailable":
            self.geojson_data = None
            return self
        if self.geojson_data is None:
            raise ValueError("Una capa disponible requiere geojson_data valido")
        _, sanitized = validate_and_sanitize_geojson(self.geojson_data)
        self.geojson_data = sanitized
        return self


class GeospatialLayerItem(BaseModel):
    layer_id: UUID
    project_version_id: UUID
    layer_type: str
    layer_type_label: str
    layer_name: str
    meaning: str
    source_name: str
    data_origin: str
    availability_status: str
    geometry_type: str | None = None
    geojson_data: dict[str, Any] | None = None
    aggregate_beneficiaries_count: int | None = None
    created_at: datetime


class ProjectGeospatialLayersResponse(BaseModel):
    project_id: UUID
    project_code: str
    project_version_id: UUID
    version_number: int
    has_available_geometry: bool
    layers: list[GeospatialLayerItem] = Field(default_factory=list)
    empty_message: str | None = None
    privacy_notice: str


class CheckpointDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve_continue", "request_correction"]
    justification: str = Field(min_length=5, max_length=2000)
    conditions: str | None = Field(default=None, max_length=2000)


class CheckpointDecisionResponse(BaseModel):
    decision_id: UUID
    checkpoint_id: UUID
    project_id: UUID
    project_version_id: UUID
    version_number: int
    decision: str
    decision_label: str
    justification: str
    conditions: str | None = None
    decided_by_user_id: UUID
    decided_by_name: str | None = None
    decided_by_email: str | None = None
    decided_at: datetime
    idempotency_key: str
    integrity_hash_sha256: str
    recomputed_hash_sha256: str
    verification_status: Literal["valid", "failed"]
    verification_label: str
    prototype_notice: str
    checkpoint_status: str
    duplicated: bool = False


class CheckpointDecisionsListResponse(BaseModel):
    project_id: UUID
    project_version_id: UUID
    version_number: int
    decisions: list[CheckpointDecisionResponse] = Field(default_factory=list)
    prototype_notice: str


class AuditProjectSummaryItem(BaseModel):
    project_id: UUID
    code: str
    title: str
    status: str
    current_version_number: int
    versions_count: int
    created_at: datetime
    updated_at: datetime


class AuditProjectSummaryListResponse(BaseModel):
    projects: list[AuditProjectSummaryItem] = Field(default_factory=list)


class HistoryVersionSummaryItem(BaseModel):
    project_version_id: UUID
    version_number: int
    title: str
    estimated_budget_pen: Decimal
    beneficiaries_count: int
    created_by_user_id: UUID
    created_by_name: str | None = None
    created_at: datetime


class HistoryTimelineEventItem(BaseModel):
    event_id: str
    event_type: str
    category: str
    title: str
    source: str
    occurred_at: datetime
    project_version_id: UUID | None = None
    version_number: int | None = None
    actor_user_id: UUID | None = None
    actor_name: str | None = None
    actor_in_directory: bool | None = None
    record_id: UUID | None = None
    decision_kind: Literal["checkpoint_decision", "final_approval"] | None = None
    decision_code: str | None = None
    justification: str | None = None
    conditions: str | None = None
    checkpoint_id: UUID | None = None
    checkpoint_status: str | None = None
    has_integrity_hash: bool = False
    result: str
    detail: str
    integrity_hash_sha256: str | None = None
    verification_status: Literal["valid", "failed"] | None = None
    verification_label: str | None = None
    prototype_notice: str | None = None


class DecisionIntegrityVerificationResponse(BaseModel):
    project_id: UUID
    project_code: str
    project_title: str
    project_version_id: UUID
    version_number: int
    decision_id: UUID
    decision_kind: Literal["checkpoint_decision", "final_approval"]
    checkpoint_id: UUID | None = None
    checkpoint_status: str | None = None
    decision: str
    decision_label: str
    justification: str
    conditions: str | None = None
    decided_by_user_id: UUID
    decided_by_name: str | None = None
    actor_in_directory: bool
    decided_at: datetime
    has_integrity_hash: bool
    integrity_status: Literal["intact", "tampered", "unavailable"]
    integrity_label: str
    stored_hash_sha256: str | None = None
    recomputed_hash_sha256: str | None = None
    canonical_payload_matches: bool | None = None
    checked_at: datetime
    prototype_notice: str
    read_only: bool = True


class ProjectHistoryResponse(BaseModel):
    project_id: UUID
    code: str
    current_title: str
    current_status: str
    current_version_id: UUID
    current_version_number: int
    versions: list[HistoryVersionSummaryItem] = Field(default_factory=list)
    events: list[HistoryTimelineEventItem] = Field(default_factory=list)
    historical_limitations: list[str] = Field(default_factory=list)
    read_only: bool = True
    generated_at: datetime


# Resolve the technical assessment declared after the project response.
PMV1ProjectResponse.model_rebuild()
