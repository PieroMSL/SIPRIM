from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response, status

from backend.application.dto.evaluacion_ia_dto import DictamenIAResponse
from backend.application.dto.pmv1_dto import (
    AdminUsersListResponse,
    AuditProjectSummaryListResponse,
    CheckpointDecisionRequest,
    CheckpointDecisionResponse,
    CheckpointDecisionsListResponse,
    CheckpointResponse,
    CheckpointResumeRequest,
    CheckpointResumeResponse,
    CurrentUserMeResponse,
    DecisionIntegrityVerificationResponse,
    EvaluationAccepted,
    EvaluationCreate,
    EvaluationResponse,
    HumanApprovalCreate,
    HumanApprovalResponse,
    HumanApprovalStatusResponse,
    LegalAlertResolveRequest,
    LegalAlertResolutionResponse,
    LegalAssessmentResponse,
    LegalPrecheckResponse,
    LoginRequest,
    LoginResponse,
    NormativeDocumentListResponse,
    NormativeSearchRequest,
    NormativeSearchResponse,
    PertEstimateCreate,
    PertEstimateResponse,
    ProcurementAdvisorReviewRequest,
    ProcurementAntecedentResponse,
    ProcurementAntecedentUpsert,
    ProcurementVerificationResponse,
    PMV1ProjectCreate,
    PMV1ProjectResponse,
    PMV1ProjectUpdate,
    PMV1ValidationResponse,
    GeospatialLayerCreate,
    GeospatialLayerItem,
    ProjectGeospatialLayersResponse,
    ProjectHistoryResponse,
    ProjectMapRankingResponse,
    EnvironmentalAssessmentResponse,
    SocialAccessibilityResponse,
    TechnicalFeasibilityResponse,
    UnifiedAssessmentSummaryResponse,
    UserRolesUpdateRequest,
    UserRolesUpdateResponse,
    ZoningPrecheckResponse,
)
from backend.application.services.pmv1_services import AuthService, PlatformService
from backend.domain.exceptions.ia_exceptions import (
    IAAuthenticationError,
    IAConfigurationError,
    IAError,
    IAGraphError,
    IARateLimitError,
    IATimeoutError,
)
from backend.domain.exceptions.pmv1_exceptions import ExpedientePMV1IncompletoError
from backend.domain.ports.ia_service_port import IAServicePort
from backend.infrastructure.config.dependencies import (
    get_auth_service,
    get_current_user,
    get_ia_service,
    get_platform_service,
    require_admin,
    require_auditor,
    require_legal_advisor,
    require_legal_or_planner,
    require_planner,
    require_planner_or_manager,
    require_planning_manager,
    require_ranking_authority,
)

router = APIRouter(prefix="/api/v1", tags=["PMV1 oficial"])


@router.post("/auth/login", response_model=LoginResponse)
def login(
    data: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, Any]:
    return service.login(data.email, data.password)


@router.get(
    "/audit/projects",
    response_model=AuditProjectSummaryListResponse,
    summary="Lista expedientes disponibles para consulta de auditoría en solo lectura (HU5.3)",
)
def list_auditable_projects(
    _: Annotated[dict[str, Any], Depends(require_auditor)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.list_auditable_projects()


@router.get(
    "/projects/{project_id}/history",
    response_model=ProjectHistoryResponse,
    summary="Consulta el historial cronológico completo de un expediente en solo lectura (HU5.3)",
)
def get_project_history(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_auditor)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_project_history(project_id)


@router.get(
    "/projects/{project_id}/versions/{version_id}/decisions/{decision_id}/verify-integrity",
    response_model=DecisionIntegrityVerificationResponse,
    summary="Verifica en vivo y en solo lectura la integridad SHA-256 de una decisión (HU5.4)",
)
def verify_decision_integrity(
    project_id: Annotated[UUID, Path()],
    version_id: Annotated[UUID, Path()],
    decision_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_auditor)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.verify_decision_integrity(
        project_id=project_id,
        project_version_id=version_id,
        decision_id=decision_id,
    )


@router.get("/auth/me", response_model=CurrentUserMeResponse)
def auth_me(
    user: Annotated[dict[str, Any], Depends(get_current_user)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, Any]:
    return service.get_current_user_profile(UUID(user["sub"]))


@router.get(
    "/admin/users",
    response_model=AdminUsersListResponse,
    summary="Lista usuarios municipales y catalogo de roles permitidos (HU5.1)",
)
def list_admin_users(
    _: Annotated[dict[str, Any], Depends(require_admin)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, Any]:
    return service.list_users_and_roles()


@router.put(
    "/admin/users/{user_id}/roles",
    response_model=UserRolesUpdateResponse,
    summary="Asigna, modifica o revoca roles de un usuario municipal (HU5.1)",
)
def update_admin_user_roles(
    user_id: Annotated[UUID, Path()],
    data: UserRolesUpdateRequest,
    actor: Annotated[dict[str, Any], Depends(require_admin)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> dict[str, Any]:
    return service.update_user_roles(
        target_user_id=user_id,
        new_roles=data.roles,
        actor_roles=list(actor.get("roles", [])),
    )


@router.post(
    "/projects", response_model=PMV1ProjectResponse, status_code=status.HTTP_201_CREATED
)
def create_project(
    data: PMV1ProjectCreate,
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=120)
    ] = None,
) -> dict[str, Any]:
    if idempotency_key and idempotency_key.strip():
        return service.create_project(data, UUID(user["sub"]), idempotency_key=idempotency_key.strip())
    return service.create_project(data, UUID(user["sub"]))


@router.get("/projects/by-code/{code}", response_model=PMV1ProjectResponse)
def get_project_by_code(
    code: Annotated[str, Path(min_length=3, max_length=40)],
    _: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_project_by_code(code)


@router.get(
    "/projects/ranking",
    response_model=ProjectMapRankingResponse,
    summary="Obtener ranking determinista de proyectos priorizados para mapa (HU4.1)",
    tags=["Priorizacion y Mapa"],
)
def get_project_map_ranking(
    _: Annotated[dict[str, Any], Depends(require_ranking_authority)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_project_map_ranking()


@router.get(
    "/projects/ranking/pdf",
    summary="Exportar informe preliminar de priorizacion en PDF (HU4.3)",
    tags=["Priorizacion y Mapa"],
    responses={
        200: {"content": {"application/pdf": {}}},
    },
)
def export_project_map_ranking_pdf(
    _: Annotated[dict[str, Any], Depends(require_ranking_authority)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> Response:
    pdf_bytes, filename = service.export_prioritization_ranking_pdf()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/projects/{project_id}", response_model=PMV1ProjectResponse)
def get_project(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_project(project_id)


@router.put("/projects/{project_id}", response_model=PMV1ProjectResponse)
def update_project(
    project_id: Annotated[UUID, Path()],
    data: PMV1ProjectUpdate,
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.update_project(project_id=project_id, data=data, actor_id=UUID(user["sub"]))


@router.get(
    "/projects/{project_id}/geospatial-layers",
    response_model=ProjectGeospatialLayersResponse,
    summary="Consultar capas geoespaciales versionadas de un expediente (HU4.2)",
    tags=["Priorizacion y Mapa"],
)
def get_project_geospatial_layers(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_ranking_authority)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.get_project_geospatial_layers(project_id=project_id, version_id=version_id)


@router.post(
    "/projects/{project_id}/geospatial-layers",
    response_model=GeospatialLayerItem,
    status_code=status.HTTP_201_CREATED,
    summary="Registrar capa geoespacial verificable para la version vigente (HU4.2)",
    tags=["Priorizacion y Mapa"],
)
def register_geospatial_layer(
    project_id: Annotated[UUID, Path()],
    data: GeospatialLayerCreate,
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.register_geospatial_layer(
        project_id=project_id,
        data=data,
        actor_id=UUID(user["sub"]),
    )


@router.get(
    "/projects/{project_id}/checkpoint",
    response_model=CheckpointResponse | None,
    summary="Consulta el punto de control persistente del proyecto ante riesgo alto (HU3.1 / HU3.4)",
)
def get_project_checkpoint(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any] | None:
    return service.get_project_checkpoint(project_id)


@router.post(
    "/projects/{project_id}/checkpoint/decision",
    response_model=CheckpointDecisionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Registra una decisión autenticada con huella SHA-256 sobre un checkpoint pendiente (HU3.3)",
)
def register_checkpoint_decision(
    project_id: Annotated[UUID, Path()],
    payload: CheckpointDecisionRequest,
    user: Annotated[dict[str, Any], Depends(require_planning_manager)],
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ],
) -> dict[str, Any]:
    actor_id = UUID(user["sub"])
    if isinstance(auth_service, AuthService):
        profile = auth_service.get_current_user_profile(actor_id)
        actor_name = profile.get("full_name")
        actor_email = profile.get("email")
    elif hasattr(auth_service, "get_current_user_profile"):
        profile = auth_service.get_current_user_profile(actor_id)
        actor_name = profile.get("full_name")
        actor_email = profile.get("email")
    else:
        actor_name = user.get("full_name")
        actor_email = user.get("email")
    return service.register_checkpoint_decision(
        project_id=project_id,
        actor_id=actor_id,
        actor_name=actor_name,
        actor_email=actor_email,
        decision=payload.decision,
        justification=payload.justification,
        conditions=payload.conditions,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/projects/{project_id}/checkpoint/decisions",
    response_model=CheckpointDecisionsListResponse,
    summary="Consulta y verifica la huella SHA-256 de decisiones de checkpoint de la versión vigente (HU3.3)",
)
def list_checkpoint_decisions(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(get_current_user)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.list_checkpoint_decisions(project_id)


@router.post(
    "/projects/{project_id}/checkpoint/resume",
    response_model=CheckpointResumeResponse,
    status_code=status.HTTP_200_OK,
    summary="Reanuda la evaluación desde un checkpoint HITL persistente autorizado por Gerencia (HU3.4)",
)
def resume_project_checkpoint(
    project_id: Annotated[UUID, Path()],
    payload: CheckpointResumeRequest,
    user: Annotated[dict[str, Any], Depends(require_planner_or_manager)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=128)
    ],
) -> dict[str, Any]:
    return service.resume_project_checkpoint(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        justification=payload.justification,
        conditions=payload.conditions,
        idempotency_key=idempotency_key,
    )


@router.post("/projects/{project_id}/validate", response_model=PMV1ValidationResponse)
def validate_project(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.validate_project(project_id, version_id=version_id)


@router.post(
    "/projects/{project_id}/evaluacion-ia",
    response_model=DictamenIAResponse,
    summary="Dictamen preliminar Gemini sobre un expediente PMV1",
)
async def evaluar_proyecto_pmv1_ia(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    platform: Annotated[PlatformService, Depends(get_platform_service)],
    ia_service: Annotated[IAServicePort, Depends(get_ia_service)],
) -> DictamenIAResponse:
    project = platform.get_project(project_id)
    missing = PlatformService._missing_from_values(project)
    if missing:
        raise ExpedientePMV1IncompletoError(missing)
    ficha = {
        "id": str(project_id),
        "codigo": project.get("code"),
        "nombre": project["title"],
        "descripcion": project["description"],
        "ubicacion": project["location"],
        "presupuesto": float(project["estimated_budget_pen"]),
        "beneficiarios": project["beneficiaries_count"],
        "tipo_proyecto": project["proposed_land_use"],
    }
    try:
        dictamen = await ia_service.generar_dictamen(ficha)
    except IAError as exc:
        status_code = 502
        if isinstance(exc, (IAConfigurationError, IAAuthenticationError)):
            status_code = 503
        elif isinstance(exc, IARateLimitError):
            status_code = 429
        elif isinstance(exc, IATimeoutError):
            status_code = 504
        elif isinstance(exc, IAGraphError):
            status_code = 500
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from None
    return DictamenIAResponse.model_validate(dictamen)


@router.post(
    "/evaluations",
    response_model=EvaluationAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_evaluation(
    data: EvaluationCreate,
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=120)
    ],
) -> dict[str, Any]:
    return service.request_evaluation(
        data.project_version_id, UUID(user["sub"]), idempotency_key
    )


@router.get("/evaluations/{evaluation_id}", response_model=EvaluationResponse)
def get_evaluation(
    evaluation_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_evaluation(evaluation_id)


@router.post(
    "/projects/{project_id}/evaluations/retry",
    response_model=EvaluationResponse,
    status_code=status.HTTP_200_OK,
    summary="Reintenta de forma segura únicamente las dimensiones fallidas o pendientes de la versión vigente (HU1.1)",
)
def retry_failed_evaluation_dimensions(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.retry_failed_dimensions(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        version_id=version_id,
    )


@router.post(
    "/projects/{project_id}/legal-precheck", response_model=LegalPrecheckResponse
)
def legal_precheck(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    res = service.record_legal_abstention(project_id, UUID(user["sub"]))
    if isinstance(res, dict):
        return res
    return {"project_id": project_id}


@router.get(
    "/projects/{project_id}/legal-assessment", response_model=LegalAssessmentResponse
)
def get_legal_assessment(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_legal_assessment(project_id)


@router.post(
    "/projects/{project_id}/legal-assessment", response_model=LegalAssessmentResponse
)
def evaluate_legal_assessment(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=120)
    ] = None,
) -> dict[str, Any]:
    return service.evaluate_legal_assessment(
        project_id, UUID(user["sub"]), idempotency_key=idempotency_key
    )


@router.post(
    "/projects/{project_id}/legal-alert/resolve",
    response_model=LegalAlertResolutionResponse,
    status_code=status.HTTP_200_OK,
    summary="Levanta formalmente una alerta legal por un asesor jurídico autorizado (HU2.3)",
)
def resolve_legal_alert(
    project_id: Annotated[UUID, Path()],
    payload: LegalAlertResolveRequest,
    user: Annotated[dict[str, Any], Depends(require_legal_advisor)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=120)
    ],
) -> dict[str, Any]:
    return service.resolve_legal_alert(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        resolution_action=payload.resolution_action,
        justification=payload.justification,
        idempotency_key=idempotency_key,
        evidence_reference=payload.evidence_reference,
    )


@router.get(
    "/projects/{project_id}/zoning-precheck",
    response_model=ZoningPrecheckResponse | None,
    summary="Consulta en solo lectura el último precheck de zonificación PDU/PDM de la versión vigente (HU1.11)",
)
def get_zoning_precheck(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any] | None:
    return service.get_zoning_precheck(project_id, version_id=version_id)


@router.post(
    "/projects/{project_id}/zoning-precheck",
    response_model=ZoningPrecheckResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def zoning_precheck(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=120)
    ],
) -> dict[str, Any]:
    return service.request_zoning_precheck(
        project_id, UUID(user["sub"]), idempotency_key
    )


@router.post("/normative/search", response_model=NormativeSearchResponse)
def search_normative(
    data: NormativeSearchRequest,
    user: Annotated[dict[str, Any], Depends(require_legal_advisor)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=120)
    ] = None,
) -> dict[str, Any]:
    return service.search_normatives(
        query=data.query,
        actor_id=UUID(user["sub"]),
        document_filter=data.document_filter,
        only_in_force=data.only_in_force,
        limit=data.limit,
        idempotency_key=idempotency_key,
    )


@router.get("/normative/documents", response_model=NormativeDocumentListResponse)
def list_normative_documents(
    _: Annotated[dict[str, Any], Depends(require_legal_advisor)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    docs = service.list_normative_documents()
    return {
        "total_documents": len(docs),
        "documents": docs,
        "disclaimer": (
            "Corpus normativo versionado para el PMV1 en El Tambo. "
            "Las fuentes activas son referenciales y requieren verificacion humana."
        ),
    }


@router.post(
    "/projects/{project_id}/approval",
    response_model=HumanApprovalResponse,
    status_code=status.HTTP_200_OK,
)
def submit_human_approval(
    project_id: Annotated[UUID, Path()],
    data: HumanApprovalCreate,
    user: Annotated[dict[str, Any], Depends(require_admin)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=1, max_length=120)
    ],
) -> dict[str, Any]:
    return service.submit_human_approval(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        decision=data.decision,
        justification=data.justification,
        conditions=data.conditions,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/projects/{project_id}/approval",
    response_model=HumanApprovalStatusResponse,
)
def get_human_approval(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_human_approval(project_id)

@router.post(
    "/projects/{project_id}/social-assessment",
    response_model=SocialAccessibilityResponse,
    status_code=status.HTTP_200_OK,
    summary="Evalúa la accesibilidad territorial en salud, educación y transporte (HU1.4)",
)
def evaluate_social_assessment(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=120)
    ] = None,
) -> dict[str, Any]:
    return service.evaluate_social_accessibility(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        idempotency_key=idempotency_key,
    )


@router.get(
    "/projects/{project_id}/social-assessment",
    response_model=SocialAccessibilityResponse,
    summary="Consulta la accesibilidad social territorial del proyecto (HU1.4)",
)
def get_social_assessment(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_social_accessibility(project_id=project_id)

@router.post(
    "/projects/{project_id}/environmental-assessment",
    response_model=EnvironmentalAssessmentResponse,
    status_code=status.HTTP_200_OK,
    summary="Evalúa el impacto ambiental territorial de la obra (HU1.6)",
)
def evaluate_environmental_assessment(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=120)
    ] = None,
) -> dict[str, Any]:
    return service.evaluate_environmental_assessment(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        idempotency_key=idempotency_key,
    )


@router.get(
    "/projects/{project_id}/environmental-assessment",
    response_model=EnvironmentalAssessmentResponse,
    summary="Consulta la evaluación de impacto ambiental territorial del proyecto (HU1.6)",
)
def get_environmental_assessment(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.get_environmental_assessment(project_id=project_id)

@router.post(
    "/projects/{project_id}/pert-estimate",
    response_model=PertEstimateResponse,
    status_code=status.HTTP_200_OK,
    summary="Registra o actualiza la estimación de duración de obra e incertidumbre PERT (HU1.9)",
)
def register_pert_estimate(
    project_id: Annotated[UUID, Path()],
    data: PertEstimateCreate,
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any]:
    return service.register_pert_estimate(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        data=data,
    )


@router.get(
    "/projects/{project_id}/pert-estimate",
    response_model=PertEstimateResponse | None,
    summary="Consulta la estimación de duración de obra e incertidumbre PERT del proyecto (HU1.9)",
)
def get_pert_estimate(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
) -> dict[str, Any] | None:
    return service.get_pert_estimate(project_id=project_id)

@router.post(
    "/projects/{project_id}/technical-assessment",
    response_model=TechnicalFeasibilityResponse,
    status_code=status.HTTP_200_OK,
    summary="Ejecuta y registra la preevaluación de viabilidad técnica del expediente (HU1.8)",
)
def evaluate_technical_feasibility(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    return service.evaluate_technical_feasibility(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        idempotency_key=idempotency_key,
    )


@router.get(
    "/projects/{project_id}/technical-assessment",
    response_model=TechnicalFeasibilityResponse | None,
    summary="Consulta la preevaluación de viabilidad técnica del expediente (HU1.8)",
)
def get_technical_feasibility(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any] | None:
    return service.get_technical_feasibility(project_id=project_id, version_id=version_id)

@router.get(
    "/projects/{project_id}/unified-summary",
    response_model=UnifiedAssessmentSummaryResponse,
    status_code=status.HTTP_200_OK,
    summary="Obtener resumen unificado de evaluacion multidimensional (HU1.12)",
    tags=["Evaluaciones"],
)
def get_unified_assessment_summary(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_planner_or_manager)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.get_unified_assessment_summary(project_id=project_id, version_id=version_id)


@router.get(
    "/projects/{project_id}/procurement-antecedents",
    response_model=ProcurementAntecedentResponse,
    status_code=status.HTTP_200_OK,
    summary="Consulta antecedentes contractuales estructurados y vigencia Ley N.° 32069 (HU1.10)",
    tags=["Normativa y Legal"],
)
def get_procurement_antecedents(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.get_procurement_antecedents(project_id=project_id, version_id=version_id)


@router.put(
    "/projects/{project_id}/procurement-antecedents",
    response_model=ProcurementAntecedentResponse,
    status_code=status.HTTP_200_OK,
    summary="Registra antecedentes contractuales de la versión vigente del expediente (HU1.10)",
    tags=["Normativa y Legal"],
)
def upsert_procurement_antecedents(
    project_id: Annotated[UUID, Path()],
    payload: ProcurementAntecedentUpsert,
    user: Annotated[dict[str, Any], Depends(require_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.upsert_procurement_antecedents(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        data=payload,
        version_id=version_id,
    )


@router.get(
    "/projects/{project_id}/procurement-verification",
    response_model=ProcurementVerificationResponse | None,
    status_code=status.HTTP_200_OK,
    summary="Consulta en solo lectura la verificación contra Ley de Contrataciones Públicas (HU1.10)",
    tags=["Normativa y Legal"],
)
def get_procurement_verification(
    project_id: Annotated[UUID, Path()],
    _: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any] | None:
    return service.get_procurement_verification(project_id=project_id, version_id=version_id)


@router.post(
    "/projects/{project_id}/procurement-verification",
    response_model=ProcurementVerificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Ejecuta verificación preliminar de Ley N.° 32069 y D.S. N.° 009-2025-EF (HU1.10)",
    tags=["Normativa y Legal"],
)
def request_procurement_verification(
    project_id: Annotated[UUID, Path()],
    user: Annotated[dict[str, Any], Depends(require_legal_or_planner)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)] = None,
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.request_procurement_verification(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        idempotency_key=idempotency_key or f"proc-verif-{uuid4()}",
        version_id=version_id,
    )


@router.post(
    "/projects/{project_id}/procurement-verification/review",
    response_model=ProcurementVerificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Registra validación jurídica humana del Asesor Jurídico sobre observación contractual (HU1.10)",
    tags=["Normativa y Legal"],
)
def review_procurement_observation(
    project_id: Annotated[UUID, Path()],
    payload: ProcurementAdvisorReviewRequest,
    user: Annotated[dict[str, Any], Depends(require_legal_advisor)],
    service: Annotated[PlatformService, Depends(get_platform_service)],
    version_id: Annotated[UUID | None, Query()] = None,
) -> dict[str, Any]:
    return service.review_procurement_observation(
        project_id=project_id,
        actor_id=UUID(user["sub"]),
        advisory_review_status=payload.advisory_review_status,
        advisory_review_notes=payload.advisory_review_notes,
        version_id=version_id,
    )

