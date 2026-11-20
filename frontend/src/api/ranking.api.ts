import { clearAccessToken, loadAccessToken, notifySessionExpired } from './authStorage'
import { API_BASE, apiFetch, parseApiError } from './client'

export interface RankingProjectItem {
  rank_position: number
  project_id: string
  project_version_id: string
  code: string
  version_number: number
  title: string
  status: string
  location: string | null
  estimated_budget_pen: string
  beneficiaries_count: number
  global_score: number
  formula: string
  explanation: string
  criteria_version_number: number
  weights: Record<string, number>
  input_scores: Record<string, number>
  tie_break_reason: string | null
  has_coordinates: boolean
  latitude: number | null
  longitude: number | null
  coordinates_origin: string | null
  coordinates_source: string | null
  allows_final_approval: boolean
  geospatial_layers_count?: number
}

export interface NonComparableProjectItem {
  project_id: string
  project_version_id: string
  code: string
  version_number: number
  title: string
  status: string
  exclusion_code: 'incomplete' | 'blocked' | 'criteria_version_mismatch' | 'no_active_criteria'
  exclusion_reason: string
  missing_dimensions: string[]
  blocking_reasons: string[]
  has_coordinates: boolean
  latitude: number | null
  longitude: number | null
}

export interface ProjectMapRankingResponse {
  is_recommendation: boolean
  recommendation_notice: string
  criteria_version_id: string | null
  criteria_version_number: number | null
  total_projects: number
  comparable_count: number
  mapped_count: number
  unmapped_count: number
  excluded_count: number
  ranked_projects: RankingProjectItem[]
  mapped_projects: RankingProjectItem[]
  unmapped_projects: RankingProjectItem[]
  excluded_projects: NonComparableProjectItem[]
  generated_at: string
}

export interface GeospatialLayerItem {
  layer_id: string
  project_version_id: string
  layer_type: 'influence_area' | 'beneficiary_coverage' | 'territorial_zone' | string
  layer_type_label: string
  layer_name: string
  meaning: string
  source_name: string
  data_origin: string
  availability_status: 'available' | 'unavailable' | string
  geometry_type: string | null
  geojson_data: Record<string, unknown> | null
  aggregate_beneficiaries_count: number | null
  created_at: string
}

export interface ProjectGeospatialLayersResponse {
  project_id: string
  project_code: string
  project_version_id: string
  version_number: number
  has_available_geometry: boolean
  layers: GeospatialLayerItem[]
  empty_message: string | null
  privacy_notice: string
}

export async function getProjectMapRanking(): Promise<ProjectMapRankingResponse> {
  return apiFetch<ProjectMapRankingResponse>('/api/v1/projects/ranking', {
    auth: true,
  })
}

export async function getProjectGeospatialLayers(
  projectId: string,
): Promise<ProjectGeospatialLayersResponse> {
  return apiFetch<ProjectGeospatialLayersResponse>(
    `/api/v1/projects/${projectId}/geospatial-layers`,
    { auth: true },
  )
}

export async function downloadPrioritizationPdf(): Promise<string> {
  const token = loadAccessToken()
  if (!token) {
    notifySessionExpired('Sesión expirada. Vuelva a iniciar sesión.')
    throw new Error('Sesión expirada. Vuelva a iniciar sesión.')
  }

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), 30_000)
  let res: Response
  try {
    res = await fetch(`${API_BASE}/api/v1/projects/ranking/pdf`, {
      method: 'GET',
      headers: { Authorization: `Bearer ${token}` },
      signal: controller.signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error('La exportación del PDF excedió el tiempo de espera.')
    }
    throw new Error('No se pudo conectar con el servidor para exportar el PDF.')
  } finally {
    window.clearTimeout(timer)
  }

  if (res.status === 401) {
    clearAccessToken()
    notifySessionExpired('Sesión expirada. Vuelva a iniciar sesión.')
    throw new Error('Sesión expirada. Vuelva a iniciar sesión.')
  }

  if (!res.ok) {
    throw new Error(await parseApiError(res))
  }

  const blob = await res.blob()
  const disposition = res.headers.get('Content-Disposition') ?? ''
  const match = disposition.match(/filename="?([^";]+)"?/i)
  const filename = match?.[1] ?? 'siprim-ranking-priorizacion.pdf'

  const url = URL.createObjectURL(blob)
  try {
    const link = document.createElement('a')
    link.href = url
    link.download = filename
    document.body.appendChild(link)
    link.click()
    link.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
  return filename
}
