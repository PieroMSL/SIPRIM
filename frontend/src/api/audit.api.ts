import { apiFetch } from './client'

export interface AuditProjectSummaryItem {
  project_id: string
  code: string
  title: string
  status: string
  current_version_number: number
  versions_count: number
  created_at: string
  updated_at: string
}

export interface AuditProjectSummaryListResponse {
  projects: AuditProjectSummaryItem[]
}

export interface HistoryVersionSummaryItem {
  project_version_id: string
  version_number: number
  title: string
  estimated_budget_pen: string | number
  beneficiaries_count: number
  created_by_user_id: string
  created_by_name: string | null
  created_at: string
}

export interface HistoryTimelineEventItem {
  event_id: string
  event_type: string
  category: string
  title: string
  source: string
  occurred_at: string
  project_version_id: string | null
  version_number: number | null
  actor_user_id: string | null
  actor_name: string | null
  actor_in_directory?: boolean | null
  record_id?: string | null
  decision_kind?: 'checkpoint_decision' | 'final_approval' | null
  decision_code?: string | null
  justification?: string | null
  conditions?: string | null
  checkpoint_id?: string | null
  checkpoint_status?: string | null
  has_integrity_hash?: boolean
  result: string
  detail: string
  integrity_hash_sha256: string | null
  verification_status: string | null
  verification_label: string | null
  prototype_notice: string | null
}

export interface DecisionIntegrityVerificationResponse {
  project_id: string
  project_code: string
  project_title: string
  project_version_id: string
  version_number: number
  decision_id: string
  decision_kind: 'checkpoint_decision' | 'final_approval'
  checkpoint_id: string | null
  checkpoint_status: string | null
  decision: string
  decision_label: string
  justification: string
  conditions: string | null
  decided_by_user_id: string
  decided_by_name: string | null
  actor_in_directory: boolean
  decided_at: string
  has_integrity_hash: boolean
  integrity_status: 'intact' | 'tampered' | 'unavailable'
  integrity_label: string
  stored_hash_sha256: string | null
  recomputed_hash_sha256: string | null
  canonical_payload_matches: boolean | null
  checked_at: string
  prototype_notice: string
  read_only: boolean
}

export interface ProjectHistoryResponse {
  project_id: string
  code: string
  current_title: string
  current_status: string
  current_version_id: string
  current_version_number: number
  versions: HistoryVersionSummaryItem[]
  events: HistoryTimelineEventItem[]
  historical_limitations: string[]
  read_only: boolean
  generated_at: string
}

export async function listAuditableProjects(): Promise<AuditProjectSummaryListResponse> {
  return apiFetch<AuditProjectSummaryListResponse>('/api/v1/audit/projects', { auth: true })
}

export async function getProjectHistory(projectId: string): Promise<ProjectHistoryResponse> {
  return apiFetch<ProjectHistoryResponse>(`/api/v1/projects/${projectId}/history`, {
    auth: true,
  })
}

export async function verifyDecisionIntegrity(
  projectId: string,
  versionId: string,
  decisionId: string,
): Promise<DecisionIntegrityVerificationResponse> {
  return apiFetch<DecisionIntegrityVerificationResponse>(
    `/api/v1/projects/${projectId}/versions/${versionId}/decisions/${decisionId}/verify-integrity`,
    { auth: true },
  )
}
