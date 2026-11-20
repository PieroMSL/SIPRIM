import type {
  CheckpointDecisionRequest,
  CheckpointDecisionResponse,
  CheckpointDecisionsListResponse,
  CheckpointResponse,
  CheckpointResumeRequest,
  CheckpointResumeResponse,
  EvaluationAccepted,
  EvaluationResponse,
  HumanApprovalStatusResponse,
  LegalAlertResolveRequest,
  LegalAlertResolutionResponse,
  LegalAssessmentResponse,
  LegalPrecheckResponse,
  NormativeDocumentListResponse,
  NormativeSearchResponse,
  PertEstimateCreate,
  PertEstimateResponse,
  ProcurementAdvisorReviewRequest,
  ProcurementAntecedentResponse,
  ProcurementAntecedentUpsert,
  ProcurementVerificationResponse,
  TechnicalFeasibilityResponse,
  UnifiedAssessmentSummaryResponse,
  PMV1ProjectCreate,
  PMV1ProjectResponse,
  PMV1ProjectUpdate,
  PMV1ValidationResponse,
  EnvironmentalAssessmentResponse,
  SocialAccessibilityResponse,
  ZoningPrecheckResponse,
} from '../types/pmv1'
import { isExpedienteUuid } from '../utils/expedienteRef'
import { apiFetch } from './client'

export function createProject(
  payload: PMV1ProjectCreate,
  idempotencyKey?: string,
): Promise<PMV1ProjectResponse> {
  return apiFetch<PMV1ProjectResponse>('/api/v1/projects', {
    method: 'POST',
    auth: true,
    idempotencyKey,
    body: JSON.stringify(payload),
  })
}

export function resumeProjectCheckpoint(
  projectId: string,
  payload: CheckpointResumeRequest,
  idempotencyKey?: string,
): Promise<CheckpointResumeResponse> {
  return apiFetch<CheckpointResumeResponse>(`/api/v1/projects/${projectId}/checkpoint/resume`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
    body: JSON.stringify(payload),
  })
}

export function getProjectCheckpoint(projectId: string): Promise<CheckpointResponse | null> {
  return apiFetch<CheckpointResponse | null>(`/api/v1/projects/${projectId}/checkpoint`, {
    auth: true,
  })
}

export function registerCheckpointDecision(
  projectId: string,
  payload: CheckpointDecisionRequest,
  idempotencyKey: string,
): Promise<CheckpointDecisionResponse> {
  return apiFetch<CheckpointDecisionResponse>(`/api/v1/projects/${projectId}/checkpoint/decision`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
    body: JSON.stringify(payload),
  })
}

export function getCheckpointDecisions(
  projectId: string,
): Promise<CheckpointDecisionsListResponse> {
  return apiFetch<CheckpointDecisionsListResponse>(
    `/api/v1/projects/${projectId}/checkpoint/decisions`,
    { auth: true },
  )
}

export async function getProject(projectId: string): Promise<PMV1ProjectResponse> {
  const project = await apiFetch<PMV1ProjectResponse>(`/api/v1/projects/${projectId}`, { auth: true })
  return project.economic_evaluation
    ? { ...project, economic_evaluation: normalizeEvaluationResponse(project.economic_evaluation) }
    : project
}

export async function getProjectByCode(code: string): Promise<PMV1ProjectResponse> {
  const encoded = encodeURIComponent(code.trim())
  const project = await apiFetch<PMV1ProjectResponse>(`/api/v1/projects/by-code/${encoded}`, {
    auth: true,
  })
  return project.economic_evaluation
    ? { ...project, economic_evaluation: normalizeEvaluationResponse(project.economic_evaluation) }
    : project
}

export async function resolveProject(ref: string): Promise<PMV1ProjectResponse> {
  const query = ref.trim()
  if (isExpedienteUuid(query)) {
    return getProject(query)
  }
  return getProjectByCode(query)
}

export function evaluateProjectWithIA(projectId: string): Promise<{
  puntaje: number
  viabilidad: 'ALTA' | 'MEDIA' | 'BAJA'
  justificacion: string
  observaciones: string[]
  recomendaciones: string[]
}> {
  return apiFetch(`/api/v1/projects/${projectId}/evaluacion-ia`, {
    method: 'POST',
    auth: true,
    timeoutMs: 60_000,
  })
}

export function updateProject(
  projectId: string,
  payload: PMV1ProjectUpdate,
): Promise<PMV1ProjectResponse> {
  return apiFetch<PMV1ProjectResponse>(`/api/v1/projects/${projectId}`, {
    method: 'PUT',
    auth: true,
    body: JSON.stringify(payload),
  })
}

export function validateProject(
  projectId: string,
  versionId?: string,
): Promise<PMV1ValidationResponse> {
  const qs = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<PMV1ValidationResponse>(`/api/v1/projects/${projectId}/validate${qs}`, {
    method: 'POST',
    auth: true,
  })
}

export function requestEvaluation(
  projectVersionId: string,
  idempotencyKey: string,
): Promise<EvaluationAccepted> {
  return apiFetch<EvaluationAccepted>('/api/v1/evaluations', {
    method: 'POST',
    auth: true,
    idempotencyKey,
    body: JSON.stringify({ project_version_id: projectVersionId }),
  })
}

function normalizeEvaluationResponse(response: EvaluationResponse): EvaluationResponse {
  if (!response.result) return response
  return {
    ...response,
    result: {
      ...response.result,
      cost_per_beneficiary_pen: Number(response.result.cost_per_beneficiary_pen),
      score_0_100: Number(response.result.score_0_100),
      budget_pen:
        response.result.budget_pen !== null && response.result.budget_pen !== undefined
          ? Number(response.result.budget_pen)
          : null,
      beneficiaries_count:
        response.result.beneficiaries_count !== null &&
        response.result.beneficiaries_count !== undefined
          ? Number(response.result.beneficiaries_count)
          : null,
    },
  }
}

export async function getEvaluation(evaluationId: string): Promise<EvaluationResponse> {
  const response = await apiFetch<EvaluationResponse>(`/api/v1/evaluations/${evaluationId}`, { auth: true })
  return normalizeEvaluationResponse(response)
}

export async function retryFailedDimensions(
  projectId: string,
  versionId?: string,
): Promise<EvaluationResponse> {
  const qs = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  const response = await apiFetch<EvaluationResponse>(`/api/v1/projects/${projectId}/evaluations/retry${qs}`, {
    method: 'POST',
    auth: true,
  })
  return normalizeEvaluationResponse(response)
}

export function legalPrecheck(projectId: string): Promise<LegalPrecheckResponse> {
  return apiFetch<LegalPrecheckResponse>(`/api/v1/projects/${projectId}/legal-precheck`, {
    method: 'POST',
    auth: true,
  })
}

export function getZoningPrecheck(
  projectId: string,
  versionId?: string,
): Promise<ZoningPrecheckResponse | null> {
  const qs = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<ZoningPrecheckResponse | null>(
    `/api/v1/projects/${projectId}/zoning-precheck${qs}`,
    { auth: true },
  )
}

export function zoningPrecheck(
  projectId: string,
  idempotencyKey: string,
): Promise<ZoningPrecheckResponse> {
  return apiFetch<ZoningPrecheckResponse>(`/api/v1/projects/${projectId}/zoning-precheck`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
  })
}

export function searchNormative(
  query: string,
  idempotencyKey?: string,
  options?: {
    onlyInForce?: boolean
    documentFilter?: string | null
    limit?: number
  },
): Promise<NormativeSearchResponse> {
  return apiFetch<NormativeSearchResponse>('/api/v1/normative/search', {
    method: 'POST',
    auth: true,
    idempotencyKey,
    body: JSON.stringify({
      query,
      only_in_force: options?.onlyInForce ?? true,
      document_filter: options?.documentFilter || null,
      limit: options?.limit ?? 8,
    }),
  })
}

export function listNormativeDocuments(): Promise<NormativeDocumentListResponse> {
  return apiFetch<NormativeDocumentListResponse>('/api/v1/normative/documents', {
    auth: true,
  })
}

export function getHumanApproval(projectId: string): Promise<HumanApprovalStatusResponse> {
  return apiFetch<HumanApprovalStatusResponse>(`/api/v1/projects/${projectId}/approval`, {
    auth: true,
  })
}

export function submitHumanApproval(
  projectId: string,
  payload: { decision: 'approved' | 'rejected' | 'observed'; justification: string; conditions?: string },
  idempotencyKey: string,
): Promise<unknown> {
  return apiFetch(`/api/v1/projects/${projectId}/approval`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
    body: JSON.stringify(payload),
  })
}

export function getSocialAccessibility(projectId: string): Promise<SocialAccessibilityResponse> {
  return apiFetch<SocialAccessibilityResponse>(`/api/v1/projects/${projectId}/social-assessment`, {
    auth: true,
  })
}

export function evaluateSocialAccessibility(
  projectId: string,
  idempotencyKey?: string,
): Promise<SocialAccessibilityResponse> {
  return apiFetch<SocialAccessibilityResponse>(`/api/v1/projects/${projectId}/social-assessment`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
  })
}

export function getEnvironmentalAssessment(projectId: string): Promise<EnvironmentalAssessmentResponse> {
  return apiFetch<EnvironmentalAssessmentResponse>(`/api/v1/projects/${projectId}/environmental-assessment`, {
    auth: true,
  })
}

export function evaluateEnvironmentalAssessment(
  projectId: string,
  idempotencyKey?: string,
): Promise<EnvironmentalAssessmentResponse> {
  return apiFetch<EnvironmentalAssessmentResponse>(`/api/v1/projects/${projectId}/environmental-assessment`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
  })
}
export function getLegalAssessment(projectId: string): Promise<LegalAssessmentResponse> {
  return apiFetch<LegalAssessmentResponse>(`/api/v1/projects/${projectId}/legal-assessment`, {
    auth: true,
  })
}

export function evaluateLegalAssessment(
  projectId: string,
  idempotencyKey?: string,
): Promise<LegalAssessmentResponse> {
  return apiFetch<LegalAssessmentResponse>(`/api/v1/projects/${projectId}/legal-assessment`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
  })
}

export function resolveLegalAlert(
  projectId: string,
  payload: LegalAlertResolveRequest,
  idempotencyKey?: string,
): Promise<LegalAlertResolutionResponse> {
  return apiFetch<LegalAlertResolutionResponse>(`/api/v1/projects/${projectId}/legal-alert/resolve`, {
    method: 'POST',
    auth: true,
    idempotencyKey,
    body: JSON.stringify(payload),
  })
}

export function getPertEstimate(projectId: string): Promise<PertEstimateResponse | null> {
  return apiFetch<PertEstimateResponse | null>(`/api/v1/projects/${projectId}/pert-estimate`, {
    auth: true,
  })
}

export function registerPertEstimate(
  projectId: string,
  payload: PertEstimateCreate,
): Promise<PertEstimateResponse> {
  return apiFetch<PertEstimateResponse>(`/api/v1/projects/${projectId}/pert-estimate`, {
    method: 'POST',
    auth: true,
    body: JSON.stringify(payload),
  })
}

export function getTechnicalFeasibility(
  projectId: string,
  versionId?: string,
): Promise<TechnicalFeasibilityResponse | null> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<TechnicalFeasibilityResponse | null>(
    `/api/v1/projects/${projectId}/technical-assessment${query}`,
    {
      auth: true,
    },
  )
}

export function evaluateTechnicalFeasibility(
  projectId: string,
  idempotencyKey?: string,
): Promise<TechnicalFeasibilityResponse> {
  return apiFetch<TechnicalFeasibilityResponse>(
    `/api/v1/projects/${projectId}/technical-assessment`,
    {
      method: 'POST',
      auth: true,
      idempotencyKey,
    },
  )
}

export function getUnifiedAssessmentSummary(
  projectId: string,
  versionId?: string,
): Promise<UnifiedAssessmentSummaryResponse> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<UnifiedAssessmentSummaryResponse>(
    `/api/v1/projects/${projectId}/unified-summary${query}`,
    {
      auth: true,
    },
  )
}

export function getProcurementAntecedents(
  projectId: string,
  versionId?: string,
): Promise<ProcurementAntecedentResponse> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<ProcurementAntecedentResponse>(
    `/api/v1/projects/${projectId}/procurement-antecedents${query}`,
    { auth: true },
  )
}

export function upsertProcurementAntecedents(
  projectId: string,
  payload: ProcurementAntecedentUpsert,
  versionId?: string,
): Promise<ProcurementAntecedentResponse> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<ProcurementAntecedentResponse>(
    `/api/v1/projects/${projectId}/procurement-antecedents${query}`,
    {
      method: 'PUT',
      auth: true,
      body: JSON.stringify(payload),
    },
  )
}

export function getProcurementVerification(
  projectId: string,
  versionId?: string,
): Promise<ProcurementVerificationResponse | null> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<ProcurementVerificationResponse | null>(
    `/api/v1/projects/${projectId}/procurement-verification${query}`,
    { auth: true },
  )
}

export function requestProcurementVerification(
  projectId: string,
  idempotencyKey?: string,
  versionId?: string,
): Promise<ProcurementVerificationResponse> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<ProcurementVerificationResponse>(
    `/api/v1/projects/${projectId}/procurement-verification${query}`,
    {
      method: 'POST',
      auth: true,
      idempotencyKey,
    },
  )
}

export function reviewProcurementObservation(
  projectId: string,
  payload: ProcurementAdvisorReviewRequest,
  versionId?: string,
): Promise<ProcurementVerificationResponse> {
  const query = versionId ? `?version_id=${encodeURIComponent(versionId)}` : ''
  return apiFetch<ProcurementVerificationResponse>(
    `/api/v1/projects/${projectId}/procurement-verification/review${query}`,
    {
      method: 'POST',
      auth: true,
      body: JSON.stringify(payload),
    },
  )
}

