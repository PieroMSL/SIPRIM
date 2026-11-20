import { useCallback, useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router-dom'
import { ExpedienteCodeBadge } from '../../components/proyecto/ExpedienteCodeBadge'
import { ProyectoCard } from '../../components/proyecto/ProyectoCard'
import { Alert } from '../../components/ui/Alert'
import { PageHeader } from '../../components/ui/PageHeader'
import { TechnicalDetails } from '../../components/ui/TechnicalDetails'
import {
  evaluateEnvironmentalAssessment,
  evaluateSocialAccessibility,
  getEnvironmentalAssessment,
  getEvaluation,
  getHumanApproval,
  evaluateTechnicalFeasibility,
  getCheckpointDecisions,
  getTechnicalFeasibility,
  getUnifiedAssessmentSummary,
  getPertEstimate,
  getProcurementAntecedents,
  getProcurementVerification,
  getProject,
  getProjectCheckpoint,
  registerCheckpointDecision,
  resumeProjectCheckpoint,
  getSocialAccessibility,
  registerPertEstimate,
  requestEvaluation,
  requestProcurementVerification,
  retryFailedDimensions,
  updateProject,
  upsertProcurementAntecedents,
  validateProject,
  zoningPrecheck,
} from '../../api/pmv1.api'
import { useAuth } from '../auth/AuthContext'
import type {
  CheckpointDecisionResponse,
  CheckpointResponse,
  DimensionDispatchStatusItem,
  EvaluationResponse,
  HumanApprovalStatusResponse,
  PertEstimateResponse,
  PMV1ProjectResponse,
  ProcurementAntecedentResponse,
  ProcurementVerificationResponse,
  TechnicalFeasibilityResponse,
  UnifiedAssessmentSummaryResponse,
  EnvironmentalAssessmentResponse,
  PMV1ValidationResponse,
  SocialAccessibilityResponse,
  ZoningPrecheckResponse,
} from '../../types/pmv1'
import { newIdempotencyKey } from '../../utils/idempotency'
import { mapPmV1ToProyecto } from '../../utils/pmv1Mapper'
import { formatCurrencyPrecise, formatDate, formatFileSize, formatNumber } from '../../utils/format'
import { getPhysicalExpediente } from '../../utils/physicalExpedienteStorage'
import { rememberProject } from '../../utils/recentProjects'

const POLL_MS = 2000
const POLL_MAX = 45

const EXPEDIENTE_FIELD_LABELS: Record<string, string> = {
  title: 'Nombre del proyecto',
  description: 'Descripción',
  location: 'Ubicación territorial',
  proposed_land_use: 'Uso de suelo propuesto',
  estimated_budget_pen: 'Presupuesto estimado (S/)',
  beneficiaries_count: 'Beneficiarios estimados',
  'estimated_budget_pen (> 0)': 'Presupuesto estimado (mayor a cero)',
  'beneficiaries_count (> 0)': 'Beneficiarios estimados (mayor a cero)',
}

function formatMissingLabels(codes: string[]): string[] {
  return codes.map((code) => EXPEDIENTE_FIELD_LABELS[code] || code)
}

function getMissingFieldsFromProject(p: PMV1ProjectResponse | null): { codes: string[]; labels: string[] } {
  if (!p) return { codes: [], labels: [] }
  const codes: string[] = []
  if (!p.title || !p.title.trim()) codes.push('title')
  if (!p.description || !p.description.trim()) codes.push('description')
  if (!p.location || !p.location.trim()) codes.push('location')
  if (!p.proposed_land_use || !p.proposed_land_use.trim()) codes.push('proposed_land_use')
  if (p.estimated_budget_pen === null || p.estimated_budget_pen === undefined) {
    codes.push('estimated_budget_pen')
  } else if (Number(p.estimated_budget_pen) <= 0 || !Number.isFinite(Number(p.estimated_budget_pen))) {
    codes.push('estimated_budget_pen (> 0)')
  }
  if (p.beneficiaries_count === null || p.beneficiaries_count === undefined) {
    codes.push('beneficiaries_count')
  } else if (Number(p.beneficiaries_count) <= 0 || !Number.isFinite(Number(p.beneficiaries_count))) {
    codes.push('beneficiaries_count (> 0)')
  }
  return { codes, labels: formatMissingLabels(codes) }
}

export function ProyectoDetallePage() {
  const { projectId = '' } = useParams()
  const location = useLocation()
  const flash = (location.state as { mensaje?: string } | null)?.mensaje
  const { hasRole } = useAuth()

  const [project, setProject] = useState<PMV1ProjectResponse | null>(null)
  const [validation, setValidation] = useState<PMV1ValidationResponse | null>(null)
  const [evaluation, setEvaluation] = useState<EvaluationResponse | null>(null)
  const [zoning, setZoning] = useState<ZoningPrecheckResponse | null>(null)
  const [approval, setApproval] = useState<HumanApprovalStatusResponse | null>(null)
  const [checkpoint, setCheckpoint] = useState<CheckpointResponse | null>(null)
  const [checkpointDecisions, setCheckpointDecisions] = useState<CheckpointDecisionResponse[]>([])
  const [decisionChoice, setDecisionChoice] = useState<'approve_continue' | 'request_correction'>('approve_continue')
  const [decisionJustification, setDecisionJustification] = useState('')
  const [decisionConditions, setDecisionConditions] = useState('')
  const [submittingDecision, setSubmittingDecision] = useState(false)
  const [decisionSuccess, setDecisionSuccess] = useState<string | null>(null)
  const [verifyingDecisions, setVerifyingDecisions] = useState(false)
  const [resumeJustification, setResumeJustification] = useState('')
  const [resumeConditions, setResumeConditions] = useState('')
  const [resumingCheckpoint, setResumingCheckpoint] = useState(false)
  const [resumeSuccess, setResumeSuccess] = useState<string | null>(null)
  const [socialAssessment, setSocialAssessment] = useState<SocialAccessibilityResponse | null>(null)
  const [socialError, setSocialError] = useState<string | null>(null)
  const [environmentalAssessment, setEnvironmentalAssessment] = useState<EnvironmentalAssessmentResponse | null>(null)
  const [environmentalError, setEnvironmentalError] = useState<string | null>(null)
  const [pertEstimate, setPertEstimate] = useState<PertEstimateResponse | null>(null)
  const [pertInputA, setPertInputA] = useState<string>('')
  const [pertInputM, setPertInputM] = useState<string>('')
  const [pertInputB, setPertInputB] = useState<string>('')
  const [pertTimeUnit, setPertTimeUnit] = useState<string>('meses')
  const [pertError, setPertError] = useState<{ field?: string; reason?: string; message: string } | null>(null)
  const [pertSuccess, setPertSuccess] = useState<string | null>(null)
  const [technicalAssessment, setTechnicalAssessment] = useState<TechnicalFeasibilityResponse | null>(null)
  const [technicalLoading, setTechnicalLoading] = useState<boolean>(false)
  const [technicalError, setTechnicalError] = useState<string | null>(null)
  const [technicalSuccess, setTechnicalSuccess] = useState<string | null>(null)
  const [unifiedSummary, setUnifiedSummary] = useState<UnifiedAssessmentSummaryResponse | null>(null)
  const [unifiedSummaryLoading, setUnifiedSummaryLoading] = useState<boolean>(false)
  const [unifiedSummaryError, setUnifiedSummaryError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(flash ?? null)
  const [busy, setBusy] = useState<string | null>(null)
  const [draftTitle, setDraftTitle] = useState('')
  const [draftDescription, setDraftDescription] = useState('')
  const [draftLocation, setDraftLocation] = useState('')
  const [draftProposedLandUse, setDraftProposedLandUse] = useState('')
  const [draftBudget, setDraftBudget] = useState('')
  const [draftBeneficiaries, setDraftBeneficiaries] = useState('')
  const [savingDraft, setSavingDraft] = useState(false)
  const [draftError, setDraftError] = useState<string | null>(null)
  const [draftSuccess, setDraftSuccess] = useState<string | null>(null)
  const [showEditForm, setShowEditForm] = useState(false)
  const [procAntecedents, setProcAntecedents] = useState<ProcurementAntecedentResponse | null>(null)
  const [procVerification, setProcVerification] = useState<ProcurementVerificationResponse | null>(null)
  const [procRegime, setProcRegime] = useState('')
  const [procObject, setProcObject] = useState('')
  const [procPhase, setProcPhase] = useState('')
  const [procProcedure, setProcProcedure] = useState('')
  const [procReqDefined, setProcReqDefined] = useState<'' | 'true' | 'false'>('')
  const [procInCmn, setProcInCmn] = useState<'' | 'true' | 'false'>('')
  const [procBudgetCert, setProcBudgetCert] = useState<'' | 'true' | 'false'>('')
  const [procPladicop, setProcPladicop] = useState<'' | 'true' | 'false'>('')
  const [procNoFrac, setProcNoFrac] = useState<'' | 'true' | 'false'>('')
  const [procDocRef, setProcDocRef] = useState('')
  const [procDirty, setProcDirty] = useState(false)
  const [procError, setProcError] = useState<string | null>(null)
  const [procSuccess, setProcSuccess] = useState<string | null>(null)

  const canEditExpediente = hasRole('PLANNER') || hasRole('ADMIN')

  const syncDraftFields = useCallback((data: PMV1ProjectResponse) => {
    setDraftTitle(data.title ?? '')
    setDraftDescription(data.description ?? '')
    setDraftLocation(data.location ?? '')
    setDraftProposedLandUse(data.proposed_land_use ?? '')
    setDraftBudget(data.estimated_budget_pen != null ? String(data.estimated_budget_pen) : '')
    setDraftBeneficiaries(data.beneficiaries_count != null ? String(data.beneficiaries_count) : '')
  }, [])

  const loadProject = useCallback(async () => {
    if (!projectId) return
    setError(null)
    try {
      const data = await getProject(projectId)
      setProject(data)
      syncDraftFields(data)
      if (data.checkpoint) {
        setCheckpoint(data.checkpoint)
      }
      if (data.pert_estimate) {
        setPertEstimate(data.pert_estimate)
        setPertInputA(data.pert_estimate.optimistic_duration.toString())
        setPertInputM(data.pert_estimate.most_likely_duration.toString())
        setPertInputB(data.pert_estimate.pessimistic_duration.toString())
        setPertTimeUnit(data.pert_estimate.time_unit)
      }
      if (data.technical_assessment) {
        setTechnicalAssessment(data.technical_assessment)
      }
      setEvaluation(
        data.economic_evaluation &&
          data.economic_evaluation.project_version_id === data.project_version_id
          ? data.economic_evaluation
          : null,
      )
      setZoning(
        data.zoning_precheck &&
          (!data.zoning_precheck.project_version_id ||
            data.zoning_precheck.project_version_id === data.project_version_id)
          ? data.zoning_precheck
          : null,
      )
      rememberProject(mapPmV1ToProyecto(data))
      if (canEditExpediente) {
        try {
          const valRes = await validateProject(projectId, data.project_version_id)
          setValidation(valRes)
        } catch {
          /* la validación oficial no bloquea la lectura del proyecto */
        }
      } else {
        setValidation(null)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo cargar el proyecto.')
    }
  }, [projectId, canEditExpediente, syncDraftFields])

  const loadApproval = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await getHumanApproval(projectId)
      setApproval(data)
    } catch {
      /* opcional hasta evaluación completada */
    }
  }, [projectId])

  const loadCheckpointDecisions = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await getCheckpointDecisions(projectId)
      setCheckpointDecisions(data.decisions)
    } catch {
      /* sin decisiones o sin acceso */
    }
  }, [projectId])

  const loadCheckpoint = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await getProjectCheckpoint(projectId)
      setCheckpoint(data)
      void loadCheckpointDecisions()
      if (data?.evaluation_id) {
        try {
          const persistedEvaluation = await getEvaluation(data.evaluation_id)
          if (persistedEvaluation.project_version_id === data.project_version_id) {
            setEvaluation(persistedEvaluation)
          }
        } catch {
          /* mantener estado vigente si falla esta consulta opcional */
        }
      }
    } catch {
      /* opcional */
    }
  }, [projectId, loadCheckpointDecisions])

  const loadSocialAssessment = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await getSocialAccessibility(projectId)
      setSocialAssessment(data)
    } catch {
      /* opcional hasta evaluación o consulta */
    }
  }, [projectId])

  const loadEnvironmentalAssessment = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await getEnvironmentalAssessment(projectId)
      setEnvironmentalAssessment(data)
    } catch {
      /* opcional hasta evaluación o consulta */
    }
  }, [projectId])


  const loadTechnicalAssessment = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await getTechnicalFeasibility(projectId)
      setTechnicalAssessment(data)
    } catch {
      /* opcional */
    }
  }, [projectId])

  const loadUnifiedSummary = useCallback(async () => {
    if (!projectId) return
    try {
      setUnifiedSummaryLoading(true)
      const data = await getUnifiedAssessmentSummary(projectId)
      setUnifiedSummary(data)
      setUnifiedSummaryError(null)
    } catch (err) {
      setUnifiedSummaryError(err instanceof Error ? err.message : 'No se pudo cargar el resumen unificado.')
    } finally {
      setUnifiedSummaryLoading(false)
    }
  }, [projectId])

  const loadProcurement = useCallback(async (targetVersionId?: string) => {
    if (!projectId) return
    try {
      const [ant, ver] = await Promise.all([
        getProcurementAntecedents(projectId, targetVersionId),
        getProcurementVerification(projectId, targetVersionId),
      ])
      setProcAntecedents(ant)
      setProcVerification(ver)
      setProcRegime(ant.contracting_regime ?? '')
      setProcObject(ant.contract_object ?? '')
      setProcPhase(ant.contracting_phase ?? '')
      setProcProcedure(ant.selection_procedure ?? '')
      setProcReqDefined(ant.requirement_defined === true ? 'true' : ant.requirement_defined === false ? 'false' : '')
      setProcInCmn(ant.in_cmn_and_viability === true ? 'true' : ant.in_cmn_and_viability === false ? 'false' : '')
      setProcBudgetCert(ant.budget_certification_declared === true ? 'true' : ant.budget_certification_declared === false ? 'false' : '')
      setProcPladicop(ant.pladicop_registration_planned === true ? 'true' : ant.pladicop_registration_planned === false ? 'false' : '')
      setProcNoFrac(ant.no_fractionation_declared === true ? 'true' : ant.no_fractionation_declared === false ? 'false' : '')
      setProcDocRef(ant.supporting_document_ref ?? '')
      setProcDirty(false)
    } catch {
      /* opcional si el rol no tiene acceso o no existe registro */
    }
  }, [projectId])

  const handleEvaluateTechnical = async () => {
    if (!projectId) return
    setTechnicalLoading(true)
    setTechnicalError(null)
    setTechnicalSuccess(null)
    try {
      const res = await evaluateTechnicalFeasibility(projectId)
      setTechnicalAssessment(res)
      setTechnicalSuccess('Preevaluación de viabilidad técnica ejecutada y registrada correctamente.')
      await loadProject()
      await loadUnifiedSummary()
    } catch (err) {
      setTechnicalError(err instanceof Error ? err.message : 'Error al ejecutar la preevaluación técnica.')
    } finally {
      setTechnicalLoading(false)
    }
  }

  const loadPertEstimate = useCallback(async () => {
    if (!projectId) return
    try {
      const data = await getPertEstimate(projectId)
      setPertEstimate(data)
      if (data) {
        setPertInputA(data.optimistic_duration.toString())
        setPertInputM(data.most_likely_duration.toString())
        setPertInputB(data.pessimistic_duration.toString())
        setPertTimeUnit(data.time_unit)
      }
    } catch {
      /* opcional */
    }
  }, [projectId])

  const handleCalculatePert = async () => {
    if (!projectId) return
    setPertSuccess(null)

    const rawA = pertInputA.trim()
    const rawM = pertInputM.trim()
    const rawB = pertInputB.trim()

    if (!rawA) {
      setPertEstimate(null)
      setPertError({
        field: 'optimistic_duration',
        reason: 'Falta ingresar la duración optimista (a).',
        message: 'Falta ingresar la duración optimista (a). Ingrese un valor numérico mayor o igual a 0.',
      })
      return
    }
    if (!rawM) {
      setPertEstimate(null)
      setPertError({
        field: 'most_likely_duration',
        reason: 'Falta ingresar la duración más probable (m).',
        message: 'Falta ingresar la duración más probable (m). Ingrese un valor que cumpla a ≤ m ≤ b.',
      })
      return
    }
    if (!rawB) {
      setPertEstimate(null)
      setPertError({
        field: 'pessimistic_duration',
        reason: 'Falta ingresar la duración pesimista (b).',
        message: 'Falta ingresar la duración pesimista (b). Ingrese un valor que cumpla m ≤ b.',
      })
      return
    }

    const a = Number(rawA)
    const m = Number(rawM)
    const b = Number(rawB)

    if (isNaN(a) || !isFinite(a)) {
      setPertEstimate(null)
      setPertError({
        field: 'optimistic_duration',
        reason: 'La duración optimista (a) debe ser un número finito válido.',
        message: 'El valor ingresado en duración optimista (a) no es un número finito.',
      })
      return
    }
    if (isNaN(m) || !isFinite(m)) {
      setPertEstimate(null)
      setPertError({
        field: 'most_likely_duration',
        reason: 'La duración más probable (m) debe ser un número finito válido.',
        message: 'El valor ingresado en duración más probable (m) no es un número finito.',
      })
      return
    }
    if (isNaN(b) || !isFinite(b)) {
      setPertEstimate(null)
      setPertError({
        field: 'pessimistic_duration',
        reason: 'La duración pesimista (b) debe ser un número finito válido.',
        message: 'El valor ingresado en duración pesimista (b) no es un número finito.',
      })
      return
    }

    if (a < 0) {
      setPertEstimate(null)
      setPertError({
        field: 'optimistic_duration',
        reason: `La duración optimista (a = ${a}) no puede ser negativa; debe ser mayor o igual a 0.`,
        message: 'La duración optimista no puede ser un número negativo.',
      })
      return
    }
    if (m < 0) {
      setPertEstimate(null)
      setPertError({
        field: 'most_likely_duration',
        reason: `La duración más probable (m = ${m}) no puede ser negativa; debe ser mayor o igual a 0.`,
        message: 'La duración más probable no puede ser un número negativo.',
      })
      return
    }
    if (b < 0) {
      setPertEstimate(null)
      setPertError({
        field: 'pessimistic_duration',
        reason: `La duración pesimista (b = ${b}) no puede ser negativa; debe ser mayor o igual a 0.`,
        message: 'La duración pesimista no puede ser un número negativo.',
      })
      return
    }

    if (a > m) {
      setPertEstimate(null)
      setPertError({
        field: 'optimistic_duration',
        reason: `Orden inválido: la duración optimista (a = ${a}) no puede ser mayor que la duración más probable (m = ${m}). Corrija el orden 0 ≤ a ≤ m ≤ b.`,
        message: `El valor optimista (${a}) supera al más probable (${m}). Corrija el orden para poder calcular la estimación PERT.`,
      })
      return
    }
    if (m > b) {
      setPertEstimate(null)
      setPertError({
        field: 'most_likely_duration',
        reason: `Orden inválido: la duración más probable (m = ${m}) no puede ser mayor que la duración pesimista (b = ${b}). Corrija el orden 0 ≤ a ≤ m ≤ b.`,
        message: `El valor más probable (${m}) supera al pesimista (${b}). Corrija el orden para poder calcular la estimación PERT.`,
      })
      return
    }
    if (a > b) {
      setPertEstimate(null)
      setPertError({
        field: 'pessimistic_duration',
        reason: `Orden inválido: la duración optimista (a = ${a}) no puede ser mayor que la duración pesimista (b = ${b}). Corrija el orden 0 ≤ a ≤ m ≤ b.`,
        message: `El valor optimista (${a}) supera al pesimista (${b}). Corrija el orden para poder calcular la estimación PERT.`,
      })
      return
    }

    setBusy('pert')
    setPertError(null)
    try {
      const res = await registerPertEstimate(projectId, {
        project_version_id: project?.project_version_id,
        optimistic_duration: a,
        most_likely_duration: m,
        pessimistic_duration: b,
        time_unit: pertTimeUnit,
      })
      setPertEstimate(res)
      setPertSuccess('Estimación PERT calculada y registrada correctamente para la versión del expediente.')
    } catch (err: any) {
      setPertEstimate(null)
      setPertError({
        field: err.field,
        reason: err.reason || err.detail,
        message: err.detail || err.message || 'No se pudo calcular la estimación PERT.',
      })
    } finally {
      setBusy(null)
    }
  }

  useEffect(() => {
    void loadProject()
    void loadApproval()
    void loadCheckpoint()
    void loadSocialAssessment()
    void loadEnvironmentalAssessment()
    void loadTechnicalAssessment()
    void loadPertEstimate()
    void loadUnifiedSummary()
    void loadProcurement()
  }, [loadProject, loadApproval, loadCheckpoint, loadSocialAssessment, loadEnvironmentalAssessment, loadTechnicalAssessment, loadPertEstimate, loadUnifiedSummary, loadProcurement])

  const runValidate = async () => {
    if (!projectId) return
    setBusy('validate')
    setError(null)
    try {
      const result = await validateProject(projectId, project?.project_version_id)
      setValidation(result)
      await loadProject()
      setInfo(result.complete ? 'Expediente completo — puede solicitar evaluación económica.' : result.message)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Validación fallida.')
    } finally {
      setBusy(null)
    }
  }

  const handleSaveExpedienteCompletion = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!projectId || !project) return
    setSavingDraft(true)
    setDraftError(null)
    setDraftSuccess(null)
    setError(null)
    try {
      const hasBudget = draftBudget.trim() !== ''
      const hasBeneficiaries = draftBeneficiaries.trim() !== ''
      const parsedBudget = Number(draftBudget)
      const parsedBeneficiaries = Number(draftBeneficiaries)
      const updated = await updateProject(projectId, {
        title: draftTitle,
        description: draftDescription,
        location: draftLocation,
        proposed_land_use: draftProposedLandUse,
        estimated_budget_pen: hasBudget && Number.isFinite(parsedBudget) ? parsedBudget : null,
        beneficiaries_count: hasBeneficiaries && Number.isFinite(parsedBeneficiaries) ? Math.trunc(parsedBeneficiaries) : null,
        save_as_draft: false,
      })
      setProject(updated)
      syncDraftFields(updated)
      const valRes = await validateProject(projectId, updated.project_version_id)
      setValidation(valRes)
      setShowEditForm(false)
      setDraftSuccess('Datos del expediente actualizados y validados correctamente. Ya puede solicitar la evaluación.')
      setInfo('Expediente completado y habilitado para evaluación económica.')
      await loadUnifiedSummary()
    } catch (err) {
      setDraftError(err instanceof Error ? err.message : 'No se pudo actualizar el expediente.')
    } finally {
      setSavingDraft(false)
    }
  }

  const refreshAllDimensions = async () => {
    await Promise.all([
      loadCheckpoint(),
      loadProject(),
      loadApproval(),
      loadSocialAssessment(),
      loadEnvironmentalAssessment(),
      loadTechnicalAssessment(),
      loadUnifiedSummary(),
    ])
  }

  const pollEvaluation = async (evaluationId: string) => {
    for (let attempt = 0; attempt < POLL_MAX; attempt += 1) {
      const ev = await getEvaluation(evaluationId)
      setEvaluation(ev)
      if (ev.status === 'completed' || ev.status === 'failed' || ev.status === 'pending_review') {
        await refreshAllDimensions()
        return ev
      }
      await new Promise((resolve) => setTimeout(resolve, POLL_MS))
    }
    throw new Error('La evaluación sigue en cola. Actualice la página en unos segundos.')
  }

  const runEvaluation = async () => {
    if (!project?.project_version_id) return
    setBusy('evaluation')
    setError(null)
    try {
      const accepted = await requestEvaluation(
        project.project_version_id,
        newIdempotencyKey('eval'),
      )
      if (accepted.status === 'pending_review' || accepted.checkpoint) {
        setInfo(
          'Procesamiento detenido en punto de control: se detectó riesgo alto configurado para revisión humana (HU3.1).',
        )
        if (accepted.checkpoint) setCheckpoint(accepted.checkpoint)
        await refreshAllDimensions()
        return
      }
      if (accepted.has_failed_dimensions) {
        setInfo(
          'Distribución ejecutada con falla controlada en uno o más especialistas. Revise el estado individual y reintente de forma segura sin repetir las dimensiones completadas.',
        )
      } else {
        setInfo(
          accepted.duplicated
            ? 'Distribución ya registrada para esta versión del expediente (sin duplicados).'
            : 'Expediente entregado a los cinco especialistas sobre la misma versión.',
        )
      }
      await pollEvaluation(accepted.evaluation_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo evaluar.')
    } finally {
      setBusy(null)
    }
  }

  const runRetryFailedDimensions = async () => {
    if (!projectId || !project?.project_version_id) return
    setBusy('retry-dimensions')
    setError(null)
    try {
      const ev = await retryFailedDimensions(projectId, project.project_version_id)
      setEvaluation(ev)
      await refreshAllDimensions()
      setInfo(
        ev.has_failed_dimensions
          ? 'Se reintentaron las dimensiones pendientes, pero persiste una falla.'
          : 'Dimensiones fallidas recuperadas de forma segura manteniendo intactos los resultados previos.',
      )
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo reintentar la evaluación.')
    } finally {
      setBusy(null)
    }
  }

  const runEnvironmentalAssessment = async () => {
    if (!projectId) return
    setBusy('environmental')
    setEnvironmentalError(null)
    try {
      const data = await evaluateEnvironmentalAssessment(projectId, newIdempotencyKey('env'))
      setEnvironmentalAssessment(data)
      await loadProject()
      await loadApproval()
      await loadCheckpoint()
      setInfo('Evaluación de impacto ambiental territorial registrada exitosamente.')
    } catch (err) {
      setEnvironmentalError(err instanceof Error ? err.message : 'Error al evaluar impacto ambiental.')
    } finally {
      setBusy(null)
    }
  }

  const runSocialAssessment = async () => {
    if (!projectId) return
    setBusy('social')
    setSocialError(null)
    try {
      const data = await evaluateSocialAccessibility(projectId, newIdempotencyKey('social'))
      setSocialAssessment(data)
      setInfo('Evaluación de accesibilidad social territorial registrada exitosamente.')
    } catch (err) {
      setSocialError(err instanceof Error ? err.message : 'Error al evaluar accesibilidad social.')
    } finally {
      setBusy(null)
    }
  }

  const runZoning = async () => {
    if (!projectId) return
    setBusy('zoning')
    setError(null)
    try {
      setZoning(await zoningPrecheck(projectId, newIdempotencyKey('zone')))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Precheck de zonificación fallido.')
    } finally {
      setBusy(null)
    }
  }

  const buildProcurementPayload = () => ({
    contracting_regime: procRegime || null,
    contract_object: procObject || null,
    contracting_phase: procPhase || null,
    selection_procedure: procProcedure || null,
    requirement_defined: procReqDefined === 'true' ? true : procReqDefined === 'false' ? false : null,
    in_cmn_and_viability: procInCmn === 'true' ? true : procInCmn === 'false' ? false : null,
    budget_certification_declared: procBudgetCert === 'true' ? true : procBudgetCert === 'false' ? false : null,
    pladicop_registration_planned: procPladicop === 'true' ? true : procPladicop === 'false' ? false : null,
    no_fractionation_declared: procNoFrac === 'true' ? true : procNoFrac === 'false' ? false : null,
    supporting_document_ref: procDocRef.trim() || null,
  })

  const saveProcurementAntecedents = async () => {
    if (!projectId || !project?.project_version_id) return
    setBusy('proc-save')
    setProcError(null)
    setProcSuccess(null)
    try {
      const updated = await upsertProcurementAntecedents(
        projectId,
        buildProcurementPayload(),
        project.project_version_id,
      )
      setProcAntecedents(updated)
      setProcDirty(false)
      setProcSuccess('Antecedentes contractuales guardados para la versión vigente.')
    } catch (err) {
      setProcError(err instanceof Error ? err.message : 'No se pudieron guardar los antecedentes contractuales.')
    } finally {
      setBusy(null)
    }
  }

  const runProcurementVerification = async () => {
    if (!projectId || !project?.project_version_id) return
    setBusy('proc-verify')
    setProcError(null)
    setProcSuccess(null)
    try {
      if (canEditExpediente && procDirty) {
        const updated = await upsertProcurementAntecedents(
          projectId,
          buildProcurementPayload(),
          project.project_version_id,
        )
        setProcAntecedents(updated)
        setProcDirty(false)
      }
      const res = await requestProcurementVerification(
        projectId,
        newIdempotencyKey('proc'),
        project.project_version_id,
      )
      setProcVerification(res)
      await loadUnifiedSummary()
    } catch (err) {
      setProcError(err instanceof Error ? err.message : 'No se pudo verificar la Ley de Contrataciones.')
    } finally {
      setBusy(null)
    }
  }

  const proyectoView = project ? mapPmV1ToProyecto(project) : null
  const physicalExpediente = projectId ? getPhysicalExpediente(projectId) : null

  // HU3.2: Garantizar aislamiento estricto por versión del expediente.
  // Un punto de control de una versión anterior u obsoleta no debe presentarse como activo en la versión vigente.
  const rawCheckpoint = checkpoint ?? project?.checkpoint ?? approval?.checkpoint ?? null
  const handleRegisterCheckpointDecision = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!projectId || decisionJustification.trim().length < 5) return
    setSubmittingDecision(true)
    setError(null)
    setDecisionSuccess(null)
    try {
      const res = await registerCheckpointDecision(
        projectId,
        {
          decision: decisionChoice,
          justification: decisionJustification.trim(),
          conditions: decisionConditions.trim() || undefined,
        },
        newIdempotencyKey('cp-decision'),
      )
      setDecisionSuccess(
        res.decision === 'approve_continue'
          ? 'Constancia registrada: aprobación de continuación con huella SHA-256 verificada.'
          : 'Constancia registrada: corrección solicitada. La pausa del expediente se mantiene activa.',
      )
      setDecisionJustification('')
      setDecisionConditions('')
      await loadProject()
      await loadCheckpoint()
      await loadCheckpointDecisions()
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'No se pudo registrar la constancia de decisión.',
      )
    } finally {
      setSubmittingDecision(false)
    }
  }

  const handleVerifyDecisions = async () => {
    setVerifyingDecisions(true)
    try {
      await loadCheckpointDecisions()
    } finally {
      setVerifyingDecisions(false)
    }
  }

  const handleResumeCheckpoint = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!projectId || resumeJustification.trim().length < 5) return
    setResumingCheckpoint(true)
    setError(null)
    setResumeSuccess(null)
    try {
      const res = await resumeProjectCheckpoint(
        projectId,
        {
          justification: resumeJustification.trim(),
          conditions: resumeConditions.trim() || undefined,
        },
        newIdempotencyKey('resume-cp'),
      )
      setResumeSuccess(res.message || 'Evaluación reanudada exitosamente desde el punto de control.')
      setResumeJustification('')
      setResumeConditions('')
      await loadProject()
      await loadCheckpoint()
      await loadApproval()
      await loadUnifiedSummary()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Error al reanudar desde el punto de control.')
    } finally {
      setResumingCheckpoint(false)
    }
  }

  const activeCheckpoint =
    rawCheckpoint && project && rawCheckpoint.project_version_id === project.project_version_id
      ? rawCheckpoint
      : null

  // HU3.3 / HU3.4: La API devuelve las decisiones en orden determinista descendente
  // (decided_at DESC, created_at DESC, id DESC); solo habilita reanudar la última decisión vigente e íntegra.
  const latestCheckpointDecision =
    activeCheckpoint && project
      ? checkpointDecisions.find(
          (d) =>
            d.checkpoint_id === activeCheckpoint.checkpoint_id &&
            d.project_version_id === project.project_version_id,
        ) ?? null
      : null
  const hasValidApproveContinueLatest = Boolean(
    latestCheckpointDecision &&
      latestCheckpointDecision.decision === 'approve_continue' &&
      latestCheckpointDecision.verification_status === 'valid',
  )

  // HU3.1 / HU3.2: El aviso de pausa aplica ÚNICAMENTE si la versión vigente posee un punto de control
  // activo en estado 'pending_review' correspondiente a esa versión.
  const isPaused = Boolean(
    activeCheckpoint &&
      activeCheckpoint.status === 'pending_review' &&
      project &&
      activeCheckpoint.project_version_id === project.project_version_id
  )

  // HU3.2 CA01: Extraer la causa técnica específica y la observación para revisión humana.
  // Si los datos están incompletos, indicarlo con claridad sin inventar explicaciones.
  const pauseCause =
    (typeof activeCheckpoint?.cause === 'string' && activeCheckpoint.cause.trim()) ||
    (typeof activeCheckpoint?.reason === 'string' && activeCheckpoint.reason.trim()) ||
    (typeof activeCheckpoint?.details?.cause === 'string' && activeCheckpoint.details.cause.trim()) ||
    (typeof approval?.pause_reason === 'string' && approval.pause_reason.trim()) ||
    null

  const rawObservation =
    (typeof activeCheckpoint?.observation === 'string' && activeCheckpoint.observation.trim()) ||
    (typeof activeCheckpoint?.details?.observation === 'string' &&
      activeCheckpoint.details.observation.trim()) ||
    (typeof activeCheckpoint?.details?.risk_observation === 'string' &&
      activeCheckpoint.details.risk_observation.trim()) ||
    (typeof activeCheckpoint?.details?.declared_vs_evidence === 'object' &&
    activeCheckpoint.details.declared_vs_evidence !== null &&
    'verifiable_evidence' in activeCheckpoint.details.declared_vs_evidence &&
    typeof (activeCheckpoint.details.declared_vs_evidence as Record<string, unknown>).verifiable_evidence === 'string'
      ? ((activeCheckpoint.details.declared_vs_evidence as Record<string, unknown>).verifiable_evidence as string).trim()
      : null) ||
    null

  const pauseObservation =
    rawObservation && rawObservation !== pauseCause && rawObservation !== 'Observación pendiente de aclaración'
      ? rawObservation
      : null

  const localCompleteness = getMissingFieldsFromProject(project)
  const effectiveValidation =
    validation &&
    project &&
    (!validation.project_version_id || validation.project_version_id === project.project_version_id)
      ? validation
      : null
  const missingFieldCodes = effectiveValidation
    ? effectiveValidation.missing_fields
    : localCompleteness.codes
  const missingFieldLabels =
    effectiveValidation && !effectiveValidation.complete
      ? effectiveValidation.missing_field_labels && effectiveValidation.missing_field_labels.length > 0
        ? effectiveValidation.missing_field_labels
        : formatMissingLabels(effectiveValidation.missing_fields)
      : localCompleteness.labels
  const isProjectComplete = Boolean(
    project &&
      localCompleteness.codes.length === 0 &&
      (effectiveValidation ? effectiveValidation.complete : true),
  )
  const activeEvaluation =
    evaluation &&
    project &&
    evaluation.project_version_id === project.project_version_id
      ? evaluation
      : null
  const activeDispatches: DimensionDispatchStatusItem[] =
    project && activeEvaluation?.dimension_dispatches && activeEvaluation.dimension_dispatches.length > 0
      ? activeEvaluation.dimension_dispatches
      : project?.dimension_dispatches ?? []
  const activeHasFailedDimensions = Boolean(
    activeDispatches.some((d) => d.status === 'failed') ||
      activeEvaluation?.has_failed_dimensions ||
      project?.has_failed_dimensions,
  )
  const activeZoning =
    zoning &&
    project &&
    (!zoning.project_version_id || zoning.project_version_id === project.project_version_id)
      ? zoning
      : null
  const hasEconomicInputs = Boolean(
    project &&
      project.estimated_budget_pen !== null &&
      project.estimated_budget_pen !== undefined &&
      Number(project.estimated_budget_pen) > 0 &&
      project.beneficiaries_count !== null &&
      project.beneficiaries_count !== undefined &&
      Number(project.beneficiaries_count) > 0,
  )

  return (
    <div className="form-page pmv1-flow">
      <PageHeader
        title="Evaluación del expediente"
        description="Revise datos, solicite la evaluación preliminar y complete las revisiones según su rol."
      />

      {info && <Alert variant="success">{info}</Alert>}
      {error && <Alert variant="error">{error}</Alert>}

      {!project && !error && <p className="muted">Cargando expediente…</p>}

      {proyectoView && (
        <>
          <ProyectoCard proyecto={proyectoView} />

          {project?.code && (
            <ExpedienteCodeBadge code={project.code} projectId={project.project_id} />
          )}

          {isPaused && (
            <section
              className="checkpoint-pause-banner"
              aria-label="Aviso gerencial de punto de control de riesgo"
              data-testid="checkpoint-pause-banner"
            >
              <div className="checkpoint-pause-header">
                <h3 className="checkpoint-pause-title">
                  <span aria-hidden="true">⚠️</span> Punto de control activo · Evaluación pausada por riesgo alto
                </h3>
                <span className="checkpoint-badge">
                  Pendiente de revisión
                </span>
              </div>

              <div className="checkpoint-target-bar">
                <strong>Expediente afectado:</strong>{' '}
                <span>
                  {project?.code} (Versión {project?.version_number}) · {project?.title}
                </span>
              </div>

              <div className="checkpoint-boxes-grid">
                <div className="checkpoint-cause-box">
                  <div className="checkpoint-reason-title">
                    Causa técnica específica de la pausa (HU3.2 · CA01)
                  </div>
                  <p className="checkpoint-reason-text">
                    {pauseCause ? (
                      pauseCause
                    ) : (
                      <span className="checkpoint-incomplete-warning">
                        Información incompleta: La causa técnica específica de la pausa no se encuentra registrada en el punto de control. Requiere aclaración por el equipo técnico. Se conserva el estado pendiente de revisión.
                      </span>
                    )}
                  </p>
                </div>

                <div className="checkpoint-observation-box">
                  <div className="checkpoint-observation-title">
                    Observación concreta para revisión humana (HU3.2 · CA01)
                  </div>
                  <p className="checkpoint-observation-text">
                    {pauseObservation ? (
                      pauseObservation
                    ) : (
                      <span className="checkpoint-incomplete-warning">
                        Observación pendiente de aclaración: Se requiere comprobación técnica en campo y sustento formal de mitigaciones aprobadas.
                      </span>
                    )}
                  </p>
                </div>
              </div>

              <div className="checkpoint-meta-grid">
                <div className="checkpoint-meta-item">
                  <div className="checkpoint-meta-label">Expediente y versión</div>
                  <div className="checkpoint-meta-value">
                    {project?.code} (v{project?.version_number})
                  </div>
                </div>
                <div className="checkpoint-meta-item">
                  <div className="checkpoint-meta-label">Estado de procesamiento</div>
                  <div className="checkpoint-meta-value" style={{ color: '#b91c1c', fontWeight: 700 }}>
                    Pendiente de revisión
                  </div>
                </div>
                <div className="checkpoint-meta-item">
                  <div className="checkpoint-meta-label">Origen de detección</div>
                  <div className="checkpoint-meta-value">
                    {activeCheckpoint?.risk_source_label ?? 'Impacto ambiental territorial (Recurso hídrico / Ecosistema)'}
                  </div>
                </div>
                <div className="checkpoint-meta-item">
                  <div className="checkpoint-meta-label">Nivel de riesgo</div>
                  <div className="checkpoint-meta-value" style={{ color: '#dc2626', fontWeight: 700 }}>
                    {activeCheckpoint?.risk_level_label ?? 'Riesgo Alto'}
                  </div>
                </div>
                <div className="checkpoint-meta-item">
                  <div className="checkpoint-meta-label">Fecha del punto de control</div>
                  <div className="checkpoint-meta-value">
                    {activeCheckpoint?.created_at ? formatDate(activeCheckpoint.created_at) : 'Fecha no registrada'}
                  </div>
                </div>
                <div className="checkpoint-meta-item">
                  <div className="checkpoint-meta-label">Acción requerida</div>
                  <div className="checkpoint-meta-value" style={{ color: '#9a3412', fontWeight: 600 }}>
                    Revisión humana obligatoria (Gerente de Planeamiento)
                  </div>
                </div>
              </div>

              <div className="checkpoint-footer-container">
                <p className="checkpoint-footer-notice">
                  <strong>Control gerencial (CA-HU3.2-01 / CA-HU3.2-02 / CA-HU3.4-01):</strong> El procesamiento automático ha sido detenido en este punto de control persistente. La evaluación no concluye ni se da por aprobada automáticamente. Un responsable facultado (Gerente de Planeamiento) puede autorizar la continuación del procesamiento desde este estado conservado sin repetir etapas completadas.
                </p>

                {/* Formulario de Decisión y Constancia SHA-256 (HU3.3) */}
                {(hasRole('PLANNING_MANAGER') || hasRole('ADMIN')) ? (
                  <div style={{ display: 'grid', gap: '0.85rem', marginTop: '1rem' }}>
                    <form
                      onSubmit={handleRegisterCheckpointDecision}
                      style={{
                        padding: '1rem',
                        background: '#ffffff',
                        borderRadius: '8px',
                        border: '1px solid #cbd5e1',
                        display: 'flex',
                        flexDirection: 'column',
                        gap: '0.75rem',
                      }}
                      data-testid="checkpoint-decision-form"
                    >
                      <h4 style={{ margin: 0, color: '#0f172a', fontSize: '0.95rem', fontWeight: 700 }}>
                        Registrar decisión autenticada del punto de control (Versión v{project?.version_number ?? 1})
                      </h4>
                      <p style={{ margin: 0, fontSize: '0.82rem', color: '#475569' }}>
                        La decisión genera una constancia de integridad SHA-256 en el servidor. Solicitar una corrección mantiene la pausa activa y el dictamen final bloqueado.
                      </p>
                      {decisionSuccess && <Alert variant="success">{decisionSuccess}</Alert>}
                      <div>
                        <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, color: '#374151', marginBottom: '0.25rem' }}>
                          Decisión sobre la pausa *
                        </label>
                        <select
                          className="form-field-input"
                          style={{ width: '100%', fontSize: '0.85rem' }}
                          value={decisionChoice}
                          onChange={(e) =>
                            setDecisionChoice(e.target.value as 'approve_continue' | 'request_correction')
                          }
                        >
                          <option value="approve_continue">
                            Aprobar continuación de evaluación
                          </option>
                          <option value="request_correction">
                            Solicitar corrección (mantener pausa)
                          </option>
                        </select>
                      </div>
                      <div>
                        <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, color: '#374151', marginBottom: '0.25rem' }}>
                          Fundamento de la decisión (mínimo 5 caracteres) *
                        </label>
                        <textarea
                          rows={2}
                          className="form-field-input"
                          style={{ width: '100%', fontSize: '0.85rem' }}
                          placeholder="Sustento técnico o normativo de la decisión sobre este punto de control..."
                          value={decisionJustification}
                          onChange={(e) => setDecisionJustification(e.target.value)}
                          required
                        />
                      </div>
                      <div>
                        <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, color: '#374151', marginBottom: '0.25rem' }}>
                          {decisionChoice === 'request_correction'
                            ? 'Corrección u observaciones requeridas (obligatorio) *'
                            : 'Condiciones u observaciones operativas (opcional)'}
                        </label>
                        <input
                          type="text"
                          className="form-field-input"
                          style={{ width: '100%', fontSize: '0.85rem' }}
                          placeholder={
                            decisionChoice === 'request_correction'
                              ? 'Detalle las correcciones o documentos de subsanación requeridos...'
                              : 'Condiciones técnicas impuestas para continuar...'
                          }
                          value={decisionConditions}
                          onChange={(e) => setDecisionConditions(e.target.value)}
                          required={decisionChoice === 'request_correction'}
                        />
                      </div>
                      <button
                        type="submit"
                        className="btn btn-primary"
                        disabled={
                          submittingDecision ||
                          decisionJustification.trim().length < 5 ||
                          (decisionChoice === 'request_correction' && !decisionConditions.trim())
                        }
                        style={{ alignSelf: 'flex-start' }}
                      >
                        {submittingDecision ? 'Registrando constancia...' : 'Registrar constancia de decisión'}
                      </button>
                    </form>

                    {hasValidApproveContinueLatest ? (
                      <form
                        onSubmit={handleResumeCheckpoint}
                        style={{
                          padding: '1rem',
                          background: '#ffffff',
                          borderRadius: '8px',
                          border: '1px solid #f87171',
                          display: 'flex',
                          flexDirection: 'column',
                          gap: '0.75rem',
                        }}
                        data-testid="checkpoint-resume-form"
                      >
                        <h4 style={{ margin: 0, color: '#991b1b', fontSize: '0.95rem', fontWeight: 700 }}>
                          Reanudar evaluación desde el punto de control (HU3.4)
                        </h4>
                        <p style={{ margin: 0, fontSize: '0.8rem', color: '#4b5563' }}>
                          La última constancia vigente es una aprobación de continuación con integridad SHA-256 verificada para esta versión. Puede reanudar el procesamiento conservando los resultados previos.
                        </p>
                        <div>
                          <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, color: '#374151', marginBottom: '0.25rem' }}>
                            Justificación de reanudación (mínimo 5 caracteres) *
                          </label>
                          <textarea
                            rows={2}
                            className="form-field-input"
                            style={{ width: '100%', fontSize: '0.85rem' }}
                            placeholder="Confirme el motivo operativo de reanudación..."
                            value={resumeJustification}
                            onChange={(e) => setResumeJustification(e.target.value)}
                            required
                          />
                        </div>
                        <div>
                          <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, color: '#374151', marginBottom: '0.25rem' }}>
                            Condiciones operativas (opcional)
                          </label>
                          <input
                            type="text"
                            className="form-field-input"
                            style={{ width: '100%', fontSize: '0.85rem' }}
                            placeholder="Condiciones técnicas..."
                            value={resumeConditions}
                            onChange={(e) => setResumeConditions(e.target.value)}
                          />
                        </div>
                        <button
                          type="submit"
                          className="btn btn-primary"
                          disabled={resumingCheckpoint || resumeJustification.trim().length < 5}
                          style={{ alignSelf: 'flex-start', background: '#b91c1c', borderColor: '#991b1b' }}
                        >
                          {resumingCheckpoint ? 'Reanudando evaluación...' : 'Autorizar y reanudar evaluación'}
                        </button>
                      </form>
                    ) : latestCheckpointDecision && latestCheckpointDecision.verification_status !== 'valid' ? (
                      <div
                        className="checkpoint-blocked-alert"
                        style={{ margin: 0 }}
                        data-testid="checkpoint-resume-blocked-notice"
                      >
                        <strong>Reanudación bloqueada por integridad:</strong> la última constancia de decisión
                        registrada para este punto de control falló la verificación SHA-256. Registre una nueva
                        aprobación de continuación íntegra para habilitar la reanudación.
                      </div>
                    ) : latestCheckpointDecision?.decision === 'request_correction' ? (
                      <div
                        className="checkpoint-blocked-alert"
                        style={{ margin: 0 }}
                        data-testid="checkpoint-resume-blocked-notice"
                      >
                        <strong>Reanudación bloqueada por decisión vigente:</strong> la última decisión registrada
                        para este punto de control solicita corrección. La pausa permanece activa y las aprobaciones
                        anteriores no habilitan reanudar hasta registrar una nueva aprobación de continuación.
                      </div>
                    ) : (
                      <p className="checkpoint-footer-notice" style={{ margin: 0 }}>
                        Para reanudar la evaluación (HU3.4), primero debe registrarse una constancia de <strong>Aprobación para continuar</strong> sobre este punto de control y versión.
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="checkpoint-footer-notice">
                    Su perfil puede consultar la pausa, pero no está autorizado para registrar decisiones o reanudar.
                    Se requiere el rol Gerente de Planeamiento o Administrador.
                  </p>
                )}
              </div>
            </section>
          )}

          {/* Banner de Punto de Control Resuelto / Reanudado (HU3.4) */}
          {activeCheckpoint && activeCheckpoint.status === 'resolved' && (
            <section
              className="flow-panel"
              style={{
                background: '#f0fdf4',
                borderColor: '#86efac',
                padding: '1.25rem',
                marginBottom: '1.5rem',
              }}
              data-testid="checkpoint-resolved-banner"
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                <span style={{ fontSize: '1.25rem' }}>✅</span>
                <h3 style={{ margin: 0, color: '#166534', fontSize: '1.1rem' }}>
                  Punto de Control Resuelto — Evaluación Reanudada (HU3.4)
                </h3>
              </div>
              <p style={{ margin: '0 0 0.75rem', fontSize: '0.875rem', color: '#15803d' }}>
                La pausa de riesgo alto fue autorizada y reanudada formalmente por el responsable facultado. El procesamiento continuó conservando los resultados previos.
              </p>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0.75rem', background: '#ffffff', padding: '0.75rem', borderRadius: '6px', border: '1px solid #bbf7d0', fontSize: '0.85rem' }}>
                <div>
                  <div style={{ color: '#6b7280', fontSize: '0.75rem', fontWeight: 600 }}>MOTIVO DE REANUDACIÓN</div>
                  <div style={{ color: '#111827', fontWeight: 500 }}>{activeCheckpoint.resolution_motive || 'Autorización concedida'}</div>
                </div>
                {activeCheckpoint.resolution_conditions && (
                  <div>
                    <div style={{ color: '#6b7280', fontSize: '0.75rem', fontWeight: 600 }}>CONDICIONES IMPUESTAS</div>
                    <div style={{ color: '#111827', fontWeight: 500 }}>{activeCheckpoint.resolution_conditions}</div>
                  </div>
                )}
                <div>
                  <div style={{ color: '#6b7280', fontSize: '0.75rem', fontWeight: 600 }}>FECHA DE REANUDACIÓN</div>
                  <div style={{ color: '#111827' }}>{activeCheckpoint.resolved_at ? formatDate(activeCheckpoint.resolved_at) : 'Reciente'}</div>
                </div>
                <div>
                  <div style={{ color: '#6b7280', fontSize: '0.75rem', fontWeight: 600 }}>ESTADO ACTUAL</div>
                  <div style={{ color: '#166534', fontWeight: 700 }}>Evaluación completada / Lista para dictamen</div>
                </div>
              </div>
            </section>
          )}

          {resumeSuccess && (
            <Alert variant="success">{resumeSuccess}</Alert>
          )}

          {checkpointDecisions.length > 0 && (
            <section
              className="flow-panel"
              data-testid="checkpoint-decisions-panel"
              style={{
                background: '#f8fafc',
                borderColor: '#cbd5e1',
                padding: '1.15rem',
                marginBottom: '1.5rem',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  flexWrap: 'wrap',
                  gap: '0.5rem',
                  marginBottom: '0.75rem',
                }}
              >
                <div>
                  <h3 style={{ margin: 0, color: '#0f172a', fontSize: '1.05rem' }}>
                    Constancia de decisión e integridad SHA-256 (Versión v{project?.version_number ?? 1})
                  </h3>
                  <p style={{ margin: '0.2rem 0 0', fontSize: '0.82rem', color: '#475569' }}>
                    {checkpointDecisions[0].prototype_notice}
                  </p>
                </div>
                <button
                  type="button"
                  className="btn btn-secondary"
                  disabled={verifyingDecisions}
                  onClick={() => void handleVerifyDecisions()}
                >
                  {verifyingDecisions ? 'Verificando…' : 'Verificar integridad'}
                </button>
              </div>

              <div style={{ display: 'grid', gap: '0.75rem' }}>
                {checkpointDecisions.map((d) => {
                  const isValid = d.verification_status === 'valid'
                  return (
                    <article
                      key={d.decision_id}
                      style={{
                        background: '#ffffff',
                        border: `1px solid ${isValid ? '#86efac' : '#fca5a5'}`,
                        borderRadius: '8px',
                        padding: '0.9rem',
                        display: 'grid',
                        gap: '0.45rem',
                        fontSize: '0.85rem',
                      }}
                    >
                      <div
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          alignItems: 'center',
                          flexWrap: 'wrap',
                          gap: '0.5rem',
                        }}
                      >
                        <strong style={{ color: '#0f172a' }}>{d.decision_label}</strong>
                        <span
                          style={{
                            padding: '0.2rem 0.6rem',
                            borderRadius: '999px',
                            fontSize: '0.78rem',
                            fontWeight: 700,
                            background: isValid ? '#dcfce7' : '#fee2e2',
                            color: isValid ? '#166534' : '#991b1b',
                          }}
                        >
                          {d.verification_label}
                        </span>
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: '0.5rem' }}>
                        <div>
                          <span style={{ color: '#64748b', fontSize: '0.75rem', display: 'block' }}>RESPONSABLE</span>
                          <span>{d.decided_by_name || d.decided_by_email || d.decided_by_user_id}</span>
                        </div>
                        <div>
                          <span style={{ color: '#64748b', fontSize: '0.75rem', display: 'block' }}>FECHA Y HORA</span>
                          <span>{formatDate(d.decided_at)}</span>
                        </div>
                        <div>
                          <span style={{ color: '#64748b', fontSize: '0.75rem', display: 'block' }}>VERSIÓN REVISADA</span>
                          <span>v{d.version_number}</span>
                        </div>
                      </div>
                      <div>
                        <span style={{ color: '#64748b', fontSize: '0.75rem', display: 'block' }}>FUNDAMENTO</span>
                        <span>{d.justification}</span>
                      </div>
                      {d.conditions && (
                        <div>
                          <span style={{ color: '#64748b', fontSize: '0.75rem', display: 'block' }}>CONDICIONES / CORRECCIÓN</span>
                          <span>{d.conditions}</span>
                        </div>
                      )}
                      <div>
                        <span style={{ color: '#64748b', fontSize: '0.75rem', display: 'block' }}>HUELLA SHA-256 ALMACENADA</span>
                        <code style={{ fontSize: '0.76rem', wordBreak: 'break-all' }}>{d.integrity_hash_sha256}</code>
                      </div>
                    </article>
                  )
                })}
              </div>
            </section>
          )}

          {physicalExpediente && (
            <section className="flow-panel expediente-physical-panel">
              <h2>Expediente físico adjunto</h2>
              <p className="flow-hint">
                {physicalExpediente.fileName} · {formatFileSize(physicalExpediente.size)} · subido{' '}
                {formatDate(physicalExpediente.uploadedAt)}
              </p>
              <a
                className="btn btn-secondary"
                href={physicalExpediente.dataUrl}
                download={physicalExpediente.fileName}
              >
                Descargar documento
              </a>
            </section>
          )}

          <section className="flow-panel" data-testid="validation-panel">
            <h2>1. Validación del expediente</h2>
            <p className="flow-hint">Verifica presupuesto, beneficiarios, descripción y datos territoriales antes de evaluar.</p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
              <button
                type="button"
                className="btn btn-secondary"
                disabled={busy !== null}
                onClick={() => void runValidate()}
              >
                {busy === 'validate' ? 'Validando…' : 'Validar expediente'}
              </button>
              {canEditExpediente && (project?.status === 'draft' || project?.status === 'ready') && (
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={() => setShowEditForm((prev) => !prev)}
                  data-testid="toggle-edit-draft-btn"
                >
                  {showEditForm || !isProjectComplete ? 'Completar / corregir datos del expediente' : 'Editar datos del expediente'}
                </button>
              )}
            </div>

            {draftSuccess && <Alert variant="success">{draftSuccess}</Alert>}

            {!isProjectComplete && (
              <div data-testid="validation-missing-alert" style={{ marginTop: '0.75rem' }}>
                <Alert variant="error">
                  <strong>Expediente incompleto:</strong> complete los datos obligatorios antes de solicitar la evaluación:
                  <ul style={{ margin: '0.35rem 0 0', paddingLeft: '1.25rem' }}>
                    {missingFieldLabels.map((label, idx) => (
                      <li key={`${missingFieldCodes[idx] ?? label}`}>{label}</li>
                    ))}
                  </ul>
                </Alert>
              </div>
            )}

            {isProjectComplete && effectiveValidation?.complete && (
              <div data-testid="validation-complete-alert" style={{ marginTop: '0.75rem' }}>
                <Alert variant="success">
                  {effectiveValidation.message}
                </Alert>
              </div>
            )}

            {canEditExpediente &&
              (project?.status === 'draft' || project?.status === 'ready') &&
              (!isProjectComplete || showEditForm) && (
                <form
                  onSubmit={handleSaveExpedienteCompletion}
                  data-testid="complete-draft-form"
                  style={{
                    marginTop: '1rem',
                    padding: '1rem',
                    background: '#f8fafc',
                    border: '1px solid #cbd5e1',
                    borderRadius: '8px',
                    display: 'grid',
                    gap: '0.75rem',
                  }}
                >
                  <h3 style={{ margin: 0, fontSize: '0.98rem', color: '#0f172a' }}>
                    Completar o corregir datos obligatorios (Versión v{project.version_number})
                  </h3>
                  {draftError && <Alert variant="error">{draftError}</Alert>}
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0.75rem' }}>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.2rem' }}>
                        Nombre del proyecto *
                      </label>
                      <input
                        type="text"
                        className="form-field-input"
                        style={{ width: '100%' }}
                        value={draftTitle}
                        onChange={(e) => setDraftTitle(e.target.value)}
                        required
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.2rem' }}>
                        Ubicación territorial *
                      </label>
                      <input
                        type="text"
                        className="form-field-input"
                        style={{ width: '100%' }}
                        placeholder="Ej. Jr. Huancavelica cdra. 4, El Tambo"
                        value={draftLocation}
                        onChange={(e) => setDraftLocation(e.target.value)}
                        required
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.2rem' }}>
                        Uso de suelo propuesto *
                      </label>
                      <input
                        type="text"
                        className="form-field-input"
                        style={{ width: '100%' }}
                        placeholder="Ej. Infraestructura urbana / Recreación"
                        value={draftProposedLandUse}
                        onChange={(e) => setDraftProposedLandUse(e.target.value)}
                        required
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.2rem' }}>
                        Presupuesto estimado (S/) *
                      </label>
                      <input
                        type="number"
                        min={0.01}
                        step={0.01}
                        className="form-field-input"
                        style={{ width: '100%' }}
                        value={draftBudget}
                        onChange={(e) => setDraftBudget(e.target.value)}
                        required
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.2rem' }}>
                        Beneficiarios estimados *
                      </label>
                      <input
                        type="number"
                        min={1}
                        step={1}
                        className="form-field-input"
                        style={{ width: '100%' }}
                        value={draftBeneficiaries}
                        onChange={(e) => setDraftBeneficiaries(e.target.value)}
                        required
                      />
                    </div>
                  </div>
                  <div>
                    <label style={{ display: 'block', fontSize: '0.8rem', fontWeight: 600, marginBottom: '0.2rem' }}>
                      Descripción *
                    </label>
                    <textarea
                      rows={3}
                      className="form-field-input"
                      style={{ width: '100%' }}
                      placeholder="Objetivo, alcance y beneficios esperados para la población."
                      value={draftDescription}
                      onChange={(e) => setDraftDescription(e.target.value)}
                      required
                    />
                  </div>
                  <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                    <button
                      type="submit"
                      className="btn btn-primary"
                      disabled={savingDraft}
                      data-testid="save-draft-completion-btn"
                    >
                      {savingDraft ? 'Guardando y validando…' : 'Guardar y validar expediente'}
                    </button>
                  </div>
                </form>
              )}

            {effectiveValidation && <TechnicalDetails data={effectiveValidation} />}
          </section>

          <section className="flow-panel" data-testid="economic-evaluation-section">
            <h2>2. Distribución a cinco especialistas y evaluación multidimensional (HU1.1)</h2>
            {isPaused && (
              <div className="checkpoint-blocked-alert" style={{ marginBottom: '1rem' }}>
                <strong>Punto de control activo:</strong> La evaluación de este expediente se encuentra detenida en estado <strong>Pendiente de revisión</strong> por detección de riesgo alto (HU3.1). Distribuir o iniciar evaluación no equivale a aprobar el proyecto automáticamente.
              </div>
            )}
            <p className="flow-hint">
              {isProjectComplete
                ? 'Expediente completo: al solicitar evaluación, el coordinador entrega esta misma versión (sin volver a cargarla) a los cinco especialistas: económico, social, ambiental, técnico y jurídico.'
                : 'Evaluación bloqueada hasta completar los datos obligatorios del expediente (HU1.2).'}
            </p>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.6rem', alignItems: 'center' }}>
              <button
                type="button"
                className="btn btn-primary"
                data-testid="request-evaluation-btn"
                disabled={busy !== null || !project?.project_version_id || isPaused || !isProjectComplete}
                onClick={() => void runEvaluation()}
              >
                {busy === 'evaluation'
                  ? 'Distribuyendo y evaluando…'
                  : isPaused
                    ? 'Evaluación en pausa por riesgo'
                    : !isProjectComplete
                      ? 'Solicitar evaluación (datos incompletos)'
                      : 'Solicitar evaluación'}
              </button>
              {activeHasFailedDimensions && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  data-testid="retry-failed-dimensions-btn"
                  disabled={busy !== null || isPaused || !isProjectComplete}
                  onClick={() => void runRetryFailedDimensions()}
                >
                  {busy === 'retry-dimensions'
                    ? 'Reintentando…'
                    : 'Reintentar solo dimensiones fallidas'}
                </button>
              )}
            </div>

            {activeDispatches.length > 0 && project && (
              <div
                data-testid="multidimensional-dispatch-panel"
                style={{
                  marginTop: '1rem',
                  background: '#f8fafc',
                  border: '1px solid #cbd5e1',
                  borderRadius: '8px',
                  padding: '0.9rem 1rem',
                  display: 'grid',
                  gap: '0.75rem',
                }}
              >
                <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center' }}>
                  <strong style={{ fontSize: '0.92rem', color: '#0f172a' }}>
                    Estado de distribución por especialista · Versión v{project.version_number}
                  </strong>
                  <code style={{ fontSize: '0.76rem', color: '#475569' }}>
                    project_version_id: {project.project_version_id}
                  </code>
                </div>

                {activeHasFailedDimensions && (
                  <Alert variant="error">
                    Evaluación multidimensional incompleta: uno o más especialistas fallaron. El expediente no se presenta como totalmente evaluado ni aprobado; los resultados exitosos se conservan intactos.
                  </Alert>
                )}

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: '0.65rem' }}>
                  {activeDispatches.map((d) => {
                    const isOk = d.status === 'completed'
                    const isFail = d.status === 'failed'
                    const isInsuff = d.status === 'insufficient_data'
                    const isReview = d.status === 'pending_review' || d.status === 'requires_review'
                    const bg = isOk ? '#f0fdf4' : isFail ? '#fef2f2' : isInsuff || isReview ? '#fffbeb' : '#f1f5f9'
                    const bd = isOk ? '#86efac' : isFail ? '#fca5a5' : isInsuff || isReview ? '#fcd34d' : '#cbd5e1'
                    const fg = isOk ? '#166534' : isFail ? '#991b1b' : isInsuff || isReview ? '#92400e' : '#334155'
                    return (
                      <article
                        key={d.dimension_code}
                        data-testid={`dispatch-dim-${d.dimension_code}`}
                        style={{
                          background: bg,
                          border: `1px solid ${bd}`,
                          borderRadius: '6px',
                          padding: '0.65rem 0.75rem',
                          fontSize: '0.82rem',
                          display: 'grid',
                          gap: '0.25rem',
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '0.4rem' }}>
                          <strong style={{ color: '#0f172a' }}>{d.dimension_name}</strong>
                          <span style={{ fontWeight: 700, fontSize: '0.74rem', color: fg }}>
                            {d.status_label}
                          </span>
                        </div>
                        <div style={{ fontSize: '0.73rem', color: '#64748b' }}>
                          Versión asignada: <code>{d.project_version_id.slice(0, 8)}</code> · Intento {d.attempt_count}
                        </div>
                        {(d.error_message || d.summary) && (
                          <div style={{ color: isFail ? '#991b1b' : '#334155', lineHeight: 1.35 }}>
                            {d.error_message || d.summary}
                          </div>
                        )}
                      </article>
                    )
                  })}
                </div>
              </div>
            )}

            {!activeEvaluation?.result && !hasEconomicInputs && (
              <div data-testid="economic-insufficient-card" style={{ marginTop: '0.85rem' }}>
                <Alert variant="error">
                  <strong>Información insuficiente (HU1.3):</strong> el costo por beneficiario y el retorno socioeconómico figuran como <strong>No calculable</strong> porque faltan el presupuesto estimado (&gt; 0) y/o la cantidad de beneficiarios (&gt; 0), y no existe método aprobado de retorno monetizado. No se realizan divisiones ni se inventan valores.
                </Alert>
              </div>
            )}

            {activeEvaluation?.result && (
              <div
                data-testid="economic-result-panel"
                style={{
                  marginTop: '1rem',
                  display: 'grid',
                  gap: '0.85rem',
                }}
              >
                <div
                  data-testid="cost-per-beneficiary-card"
                  style={{
                    background: '#f0fdf4',
                    border: '1px solid #86efac',
                    borderRadius: '8px',
                    padding: '1rem',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', flexWrap: 'wrap', gap: '0.5rem' }}>
                    <strong style={{ color: '#14532d', fontSize: '0.98rem' }}>
                      {activeEvaluation.result.indicator_name ?? 'Costo por beneficiario'}
                    </strong>
                    <span style={{ fontSize: '1.35rem', fontWeight: 800, color: '#166534' }}>
                      {formatCurrencyPrecise(activeEvaluation.result.cost_per_beneficiary_pen)} por beneficiario
                    </span>
                  </div>
                  <div style={{ marginTop: '0.45rem', fontSize: '0.88rem', color: '#166534', display: 'grid', gap: '0.25rem' }}>
                    <div>
                      <strong>Fórmula aplicada:</strong>{' '}
                      <code>{activeEvaluation.result.formula ?? 'presupuesto / beneficiarios'}</code>
                      {' · '}
                      {formatCurrencyPrecise(activeEvaluation.result.budget_pen ?? project?.estimated_budget_pen)}
                      {' / '}
                      {formatNumber(activeEvaluation.result.beneficiaries_count ?? project?.beneficiaries_count)} beneficiarios
                    </div>
                    <div>
                      <strong>Moneda y redondeo:</strong>{' '}
                      {activeEvaluation.result.currency ?? 'PEN (S/)'} · {activeEvaluation.result.rounding_rule ?? '2 decimales (ROUND_HALF_UP)'}
                    </div>
                    <div>
                      <strong>Puntuación económica normalizada:</strong>{' '}
                      {activeEvaluation.result.score_0_100.toFixed(2)}/100 · {activeEvaluation.result.explanation}
                    </div>
                  </div>
                </div>

                <div
                  data-testid="socioeconomic-return-card"
                  style={{
                    background:
                      activeEvaluation.result.socioeconomic_return !== null &&
                      activeEvaluation.result.socioeconomic_return_status === 'calculated'
                        ? '#f0fdf4'
                        : '#fffbeb',
                    border: `1px solid ${
                      activeEvaluation.result.socioeconomic_return !== null &&
                      activeEvaluation.result.socioeconomic_return_status === 'calculated'
                        ? '#86efac'
                        : '#fcd34d'
                    }`,
                    borderRadius: '8px',
                    padding: '1rem',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                    <strong style={{ color: '#78350f', fontSize: '0.98rem' }}>
                      Retorno socioeconómico
                    </strong>
                    <span
                      style={{
                        padding: '0.2rem 0.65rem',
                        borderRadius: '999px',
                        fontSize: '0.78rem',
                        fontWeight: 700,
                        background:
                          activeEvaluation.result.socioeconomic_return !== null ? '#dcfce7' : '#fef3c7',
                        color:
                          activeEvaluation.result.socioeconomic_return !== null ? '#166534' : '#92400e',
                        border: `1px solid ${
                          activeEvaluation.result.socioeconomic_return !== null ? '#22c55e' : '#f59e0b'
                        }`,
                      }}
                    >
                      {activeEvaluation.result.socioeconomic_return !== null
                        ? `${activeEvaluation.result.socioeconomic_return}`
                        : (activeEvaluation.result.socioeconomic_return_label ?? 'No calculable')}
                    </span>
                  </div>
                  {activeEvaluation.result.socioeconomic_return !== null &&
                  activeEvaluation.result.socioeconomic_return_method ? (
                    <div style={{ marginTop: '0.5rem', fontSize: '0.86rem', color: '#14532d', display: 'grid', gap: '0.25rem' }}>
                      <div>
                        <strong>Método aprobado:</strong>{' '}
                        {activeEvaluation.result.socioeconomic_return_method.method_name} (
                        {activeEvaluation.result.socioeconomic_return_method.method_version}) ·{' '}
                        <code>{activeEvaluation.result.socioeconomic_return_method.formula}</code>
                      </div>
                      {activeEvaluation.result.socioeconomic_return_variables && (
                        <div>
                          <strong>Variables empleadas:</strong>{' '}
                          {Object.entries(activeEvaluation.result.socioeconomic_return_variables)
                            .map(([k, v]) => `${k}: ${String(v)}`)
                            .join(' · ')}
                        </div>
                      )}
                      {activeEvaluation.result.socioeconomic_return_sources &&
                        activeEvaluation.result.socioeconomic_return_sources.length > 0 && (
                          <div>
                            <strong>Fuentes trazables:</strong>{' '}
                            {activeEvaluation.result.socioeconomic_return_sources.join('; ')}
                          </div>
                        )}
                    </div>
                  ) : (
                    <>
                      <p style={{ margin: '0.45rem 0 0', fontSize: '0.88rem', color: '#78350f', lineHeight: 1.45 }}>
                        <strong>Motivo (Información insuficiente / cálculo deshabilitado sin método aprobado):</strong>{' '}
                        {activeEvaluation.result.socioeconomic_return_reason ??
                          'No existe un método aprobado de retorno socioeconómico (p. ej. VAN/TIR/B-C) ni flujos trazables de beneficios monetizados, costos de operación y tasa social de descuento; no se inventa ningún valor.'}
                      </p>
                      <div style={{ marginTop: '0.55rem', fontSize: '0.82rem', color: '#78350f' }}>
                        <strong>Punto de integración:</strong>{' '}
                        {activeEvaluation.result.socioeconomic_return_integration_status ===
                        'awaiting_complete_inputs'
                          ? 'Método configurado, esperando entradas verificables del expediente.'
                          : 'Deshabilitado hasta que se defina y apruebe el método oficial y sus variables obligatorias:'}
                        {activeEvaluation.result.socioeconomic_return_missing_requirements &&
                          activeEvaluation.result.socioeconomic_return_missing_requirements.length > 0 && (
                            <ul
                              data-testid="socioeconomic-missing-requirements"
                              style={{ margin: '0.35rem 0 0', paddingLeft: '1.2rem', display: 'grid', gap: '0.15rem' }}
                            >
                              {activeEvaluation.result.socioeconomic_return_missing_requirements.map((req) => (
                                <li key={req}>{req}</li>
                              ))}
                            </ul>
                          )}
                      </div>
                    </>
                  )}
                </div>
              </div>
            )}
            {activeEvaluation && <TechnicalDetails data={activeEvaluation} />}
          </section>

          <section className="flow-panel social-flow-panel">
            <div className="flow-panel-heading">
              <div>
                <span className="story-label">PMV 2 · HU1.4 / HU1.5</span>
                <h2>Accesibilidad territorial y equidad demográfica</h2>
              </div>
              <span className="review-badge">Análisis territorial social</span>
            </div>
            <p className="flow-hint">
              Evaluación social integrada de accesibilidad a servicios esenciales (salud, educación, transporte) y equidad demográfica por género y grupos de edad con datos agregados y delimitación territorial (El Tambo, Huancayo).
            </p>

            {hasRole('PLANNER') && (
              <div style={{ marginBottom: '1rem' }}>
                {!isProjectComplete && (
                  <div style={{ marginBottom: '0.65rem' }}>
                    <Alert variant="error">
                      Evaluación social bloqueada hasta completar los datos obligatorios: {missingFieldLabels.join(', ')}. La vista territorial previa sigue disponible en solo lectura.
                    </Alert>
                  </div>
                )}
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="evaluate-social-btn"
                  disabled={busy !== null || isPaused || !isProjectComplete}
                  onClick={() => void runSocialAssessment()}
                >
                  {busy === 'social'
                    ? 'Evaluando accesibilidad y equidad…'
                    : !isProjectComplete
                      ? 'Evaluación social bloqueada (datos incompletos)'
                      : socialAssessment
                        ? 'Actualizar evaluación social'
                        : 'Evaluar accesibilidad y equidad social'}
                </button>
              </div>
            )}

            {socialError && <Alert variant="error">{socialError}</Alert>}

            {socialAssessment && (
              <>
                <Alert variant={socialAssessment.status === 'complete' ? 'success' : 'error'}>
                  <strong>{socialAssessment.summary}</strong>
                  <div style={{ marginTop: '0.25rem', fontSize: '0.82rem' }}>
                    Evaluado el: {formatDate(socialAssessment.evaluated_at)}
                  </div>
                </Alert>

                <div className="social-cards-grid">
                  {socialAssessment.categories.map((cat) => (
                    <div
                      key={cat.category}
                      className={`social-card ${cat.has_sufficient_data ? 'is-sufficient' : 'is-insufficient'}`}
                    >
                      <div className="social-card-header">
                        <h3 className="social-card-title">{cat.category_label}</h3>
                        <span className={`social-badge-status ${cat.has_sufficient_data ? 'sufficient' : 'insufficient'}`}>
                          {cat.has_sufficient_data ? 'Dato disponible' : 'Insuficiente'}
                        </span>
                      </div>

                      <div className="social-indicator-label">{cat.indicator}</div>

                      {cat.has_sufficient_data && cat.value !== null ? (
                        <div className="social-metric-box">
                          <span className="social-metric-value">{cat.value}</span>
                          <span className="social-metric-unit">{cat.unit}</span>
                        </div>
                      ) : (
                        <div className="social-insufficient-callout">
                          Información insuficiente
                        </div>
                      )}

                      <p style={{ margin: '0.35rem 0', fontSize: '0.82rem', color: '#334155', lineHeight: 1.45 }}>
                        {cat.message}
                      </p>

                      <div className="social-card-meta">
                        <div>
                          <strong>Fuente:</strong>
                          <div className="social-source-tag">
                            {cat.source}
                            {cat.is_simulated && <span className="social-simulated-pill">Simulado</span>}
                          </div>
                        </div>
                        <div style={{ marginTop: '0.35rem' }}>
                          <strong>Origen del dato:</strong> {cat.data_origin}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>

                {/* Panel de Puntuación Social Ponderada HU1.4 + HU1.5 */}
                <div className="social-score-card">
                  <div className="social-score-header">
                    <div>
                      <span className="social-score-tag">HU1.4 + HU1.5 · Puntuación Social Integrada</span>
                      <h3 style={{ margin: '0.2rem 0', fontSize: '1.15rem', color: '#0f172a' }}>
                        Puntaje Social:{' '}
                        {socialAssessment.social_score !== null && socialAssessment.social_score !== undefined
                          ? `${socialAssessment.social_score.toFixed(1)} / 100`
                          : 'No determinado'}
                      </h3>
                    </div>
                    {socialAssessment.social_score !== null && socialAssessment.social_score !== undefined ? (
                      <span
                        className={`social-score-badge ${
                          socialAssessment.social_score >= 70 ? 'score-good' : 'score-low'
                        }`}
                      >
                        {socialAssessment.social_score >= 70 ? 'Favorable' : 'Bajo'}
                      </span>
                    ) : (
                      <span className="social-score-badge score-neutral">
                        Sin ventaja automática
                      </span>
                    )}
                  </div>

                  <div className="social-score-breakdown">
                    <div className="social-score-item">
                      <span className="social-score-label">Base Accesibilidad (HU1.4):</span>
                      <span className="social-score-val">
                        {socialAssessment.status === 'complete'
                          ? '70.0 pts (Integral)'
                          : socialAssessment.status === 'partial'
                          ? '50.0 pts (Parcial)'
                          : 'No determinado (Insuficiente)'}
                      </span>
                    </div>
                    <div className="social-score-item">
                      <span className="social-score-label">Efecto Género (HU1.5):</span>
                      <span className="social-score-val">
                        {socialAssessment.equity?.gender?.score_effect_pts !== null &&
                        socialAssessment.equity?.gender?.score_effect_pts !== undefined
                          ? socialAssessment.equity.gender.score_effect_pts > 0
                            ? `+${socialAssessment.equity.gender.score_effect_pts.toFixed(1)} pts (${socialAssessment.equity.gender.effect_type})`
                            : socialAssessment.equity.gender.score_effect_pts < 0
                            ? `${socialAssessment.equity.gender.score_effect_pts.toFixed(1)} pts (${socialAssessment.equity.gender.effect_type})`
                            : '0.0 pts (Neutro)'
                          : 'No determinado · Sin ventaja automática'}
                      </span>
                    </div>
                    <div className="social-score-item">
                      <span className="social-score-label">Efecto Grupos de Edad (HU1.5):</span>
                      <span className="social-score-val">
                        {socialAssessment.equity?.age?.score_effect_pts !== null &&
                        socialAssessment.equity?.age?.score_effect_pts !== undefined
                          ? socialAssessment.equity.age.score_effect_pts > 0
                            ? `+${socialAssessment.equity.age.score_effect_pts.toFixed(1)} pts (${socialAssessment.equity.age.effect_type})`
                            : socialAssessment.equity.age.score_effect_pts < 0
                            ? `${socialAssessment.equity.age.score_effect_pts.toFixed(1)} pts (${socialAssessment.equity.age.effect_type})`
                            : '0.0 pts (Neutro)'
                          : 'No determinado · Sin ventaja automática'}
                      </span>
                    </div>
                  </div>

                  {socialAssessment.score_explanation && (
                    <p className="social-score-explanation">
                      <strong>Explicación del resultado:</strong> {socialAssessment.score_explanation}
                    </p>
                  )}
                </div>

                {/* Panel de Equidad Demográfica HU1.5 */}
                {socialAssessment.equity && (
                  <div className="equity-panel">
                    <div className="equity-header">
                      <div>
                        <span className="story-label" style={{ fontSize: '0.75rem' }}>PMV 2 · HU1.5</span>
                        <h3 style={{ margin: '0.15rem 0', fontSize: '1rem', color: '#0f172a' }}>
                          Equidad Demográfica: Género y Grupos de Edad
                        </h3>
                      </div>
                      <span
                        className={`green-status-badge ${
                          socialAssessment.equity.has_sufficient_data ? 'status-ok' : 'status-warn'
                        }`}
                      >
                        {socialAssessment.equity.has_sufficient_data
                          ? socialAssessment.equity.effect_summary
                          : 'Información insuficiente'}
                      </span>
                    </div>

                    <p className="equity-subtitle">
                      Datos demográficos agregados a nivel territorial sin información personal identificable. Fuente oficial censal INEI CPV de demostración para el distrito de El Tambo.
                    </p>

                    <div className="equity-grid">
                      {/* Dimensión Género */}
                      <div
                        className={`equity-card ${
                          socialAssessment.equity.gender.has_sufficient_data ? 'is-sufficient' : 'is-insufficient'
                        }`}
                      >
                        <div className="equity-card-header">
                          <h4 className="equity-card-title">{socialAssessment.equity.gender.dimension_label}</h4>
                          <span
                            className={`social-badge-status ${
                              socialAssessment.equity.gender.has_sufficient_data ? 'sufficient' : 'insufficient'
                            }`}
                          >
                            {socialAssessment.equity.gender.has_sufficient_data
                              ? 'Dato disponible'
                              : 'Información insuficiente'}
                          </span>
                        </div>

                        <div className="equity-target-group">
                          <strong>Grupo objetivo:</strong> {socialAssessment.equity.gender.target_group}
                        </div>

                        {socialAssessment.equity.gender.has_sufficient_data ? (
                          <div className="equity-metrics-box">
                            <div className="equity-metric-row">
                              <span className="equity-metric-label">Población agregada:</span>
                              <span className="equity-metric-value">
                                {socialAssessment.equity.gender.aggregated_population?.toLocaleString()} hab.
                              </span>
                            </div>
                            <div className="equity-metric-row">
                              <span className="equity-metric-label">Proporción poblacional:</span>
                              <span className="equity-metric-value">
                                {socialAssessment.equity.gender.aggregated_percentage}%
                              </span>
                            </div>
                          </div>
                        ) : (
                          <div className="equity-insufficient-callout">
                            <strong>⚠️ Limitación metodológica (CA-HU1.5-02):</strong>
                            <p style={{ margin: '0.25rem 0 0' }}>
                              No existen datos demográficos fiables ni medidas específicas en el expediente. No se infieren proporciones sin evidencia ni se asigna conclusión favorable por presunción.
                            </p>
                          </div>
                        )}

                        <div className="equity-detail-item">
                          <span className="equity-detail-label">Criterio de Equidad:</span>
                          <div>{socialAssessment.equity.gender.equity_criterion}</div>
                        </div>

                        <div className="equity-detail-item">
                          <span className="equity-detail-label">Medidas Declaradas en Expediente:</span>
                          <div>
                            {socialAssessment.equity.gender.declared_benefit ||
                              'No se declaran medidas específicas de género en el expediente técnico'}
                          </div>
                        </div>

                        <div className="equity-detail-item">
                          <span className="equity-detail-label">Evidencia Territorial / Sustento:</span>
                          <div>
                            {socialAssessment.equity.gender.verifiable_data ||
                              'Sin evidencia territorial verificable'}
                          </div>
                        </div>

                        <div className="equity-effect-box">
                          <div style={{ marginBottom: '0.35rem', fontSize: '0.8rem', color: '#64748b' }}>
                            <strong>Efecto en la evaluación social:</strong>
                          </div>
                          <div>
                            <span
                              className={`equity-effect-pill ${socialAssessment.equity.gender.effect_type}`}
                            >
                              {socialAssessment.equity.gender.score_effect_pts !== null &&
                              socialAssessment.equity.gender.score_effect_pts !== undefined
                                ? `${socialAssessment.equity.gender.effect_type} (${socialAssessment.equity.gender.score_effect_pts >= 0 ? '+' : ''}${socialAssessment.equity.gender.score_effect_pts.toFixed(1)} pts)`
                                : 'No determinado · Sin ventaja automática'}
                            </span>
                          </div>
                        </div>

                        <div className="social-card-meta">
                          <div>
                            <strong>Fuente:</strong>
                            <div className="social-source-tag">
                              {socialAssessment.equity.gender.source_name}
                              {socialAssessment.equity.gender.is_simulated && (
                                <span className="social-simulated-pill">Simulado</span>
                              )}
                            </div>
                          </div>
                          <div style={{ marginTop: '0.35rem' }}>
                            <strong>Origen del dato:</strong> {socialAssessment.equity.gender.data_origin}
                          </div>
                        </div>
                      </div>

                      {/* Dimensión Grupos de Edad */}
                      <div
                        className={`equity-card ${
                          socialAssessment.equity.age.has_sufficient_data ? 'is-sufficient' : 'is-insufficient'
                        }`}
                      >
                        <div className="equity-card-header">
                          <h4 className="equity-card-title">{socialAssessment.equity.age.dimension_label}</h4>
                          <span
                            className={`social-badge-status ${
                              socialAssessment.equity.age.has_sufficient_data ? 'sufficient' : 'insufficient'
                            }`}
                          >
                            {socialAssessment.equity.age.has_sufficient_data
                              ? 'Dato disponible'
                              : 'Información insuficiente'}
                          </span>
                        </div>

                        <div className="equity-target-group">
                          <strong>Grupo objetivo:</strong> {socialAssessment.equity.age.target_group}
                        </div>

                        {socialAssessment.equity.age.has_sufficient_data ? (
                          <div className="equity-metrics-box">
                            <div className="equity-metric-row">
                              <span className="equity-metric-label">Población agregada:</span>
                              <span className="equity-metric-value">
                                {socialAssessment.equity.age.aggregated_population?.toLocaleString()} hab.
                              </span>
                            </div>
                            <div className="equity-metric-row">
                              <span className="equity-metric-label">Proporción poblacional:</span>
                              <span className="equity-metric-value">
                                {socialAssessment.equity.age.aggregated_percentage}%
                              </span>
                            </div>
                          </div>
                        ) : (
                          <div className="equity-insufficient-callout">
                            <strong>⚠️ Limitación metodológica (CA-HU1.5-02):</strong>
                            <p style={{ margin: '0.25rem 0 0' }}>
                              No existen datos de grupos de edad fiables ni adecuaciones de accesibilidad universal verificables. No se infieren proporciones sin evidencia ni se asigna conclusión favorable por presunción.
                            </p>
                          </div>
                        )}

                        <div className="equity-detail-item">
                          <span className="equity-detail-label">Criterio de Equidad:</span>
                          <div>{socialAssessment.equity.age.equity_criterion}</div>
                        </div>

                        <div className="equity-detail-item">
                          <span className="equity-detail-label">Medidas Declaradas en Expediente:</span>
                          <div>
                            {socialAssessment.equity.age.declared_benefit ||
                              'No se declaran medidas de accesibilidad por edad en el expediente técnico'}
                          </div>
                        </div>

                        <div className="equity-detail-item">
                          <span className="equity-detail-label">Evidencia Territorial / Sustento:</span>
                          <div>
                            {socialAssessment.equity.age.verifiable_data ||
                              'Sin evidencia territorial verificable'}
                          </div>
                        </div>

                        <div className="equity-effect-box">
                          <div style={{ marginBottom: '0.35rem', fontSize: '0.8rem', color: '#64748b' }}>
                            <strong>Efecto en la evaluación social:</strong>
                          </div>
                          <div>
                            <span
                              className={`equity-effect-pill ${socialAssessment.equity.age.effect_type}`}
                            >
                              {socialAssessment.equity.age.score_effect_pts !== null &&
                              socialAssessment.equity.age.score_effect_pts !== undefined
                                ? `${socialAssessment.equity.age.effect_type} (${socialAssessment.equity.age.score_effect_pts >= 0 ? '+' : ''}${socialAssessment.equity.age.score_effect_pts.toFixed(1)} pts)`
                                : 'No determinado · Sin ventaja automática'}
                            </span>
                          </div>
                        </div>

                        <div className="social-card-meta">
                          <div>
                            <strong>Fuente:</strong>
                            <div className="social-source-tag">
                              {socialAssessment.equity.age.source_name}
                              {socialAssessment.equity.age.is_simulated && (
                                <span className="social-simulated-pill">Simulado</span>
                              )}
                            </div>
                          </div>
                          <div style={{ marginTop: '0.35rem' }}>
                            <strong>Origen del dato:</strong> {socialAssessment.equity.age.data_origin}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                )}

                <TechnicalDetails data={socialAssessment} />
              </>
            )}
          </section>

          <section className="flow-panel environmental-flow-panel">
            <div className="flow-panel-heading">
              <div>
                <span className="story-label">PMV 2 · HU1.6 / HU1.7</span>
                <h2>Evaluación ambiental y conservación de áreas verdes</h2>
              </div>
              <span className="review-badge">Análisis ambiental territorial</span>
            </div>
            <p className="flow-hint">
              Evaluación del impacto ambiental y conservación de áreas verdes de la obra, con ponderación y desglose de puntaje ambiental, distinguiendo declaraciones en el expediente de la evidencia territorial de demostración (El Tambo, Huancayo).
            </p>

            {hasRole('PLANNER') && (
              <div style={{ marginBottom: '1rem' }}>
                {!isProjectComplete && (
                  <div style={{ marginBottom: '0.65rem' }}>
                    <Alert variant="error">
                      Evaluación ambiental bloqueada hasta completar los datos obligatorios: {missingFieldLabels.join(', ')}. La consulta preliminar sigue disponible en solo lectura.
                    </Alert>
                  </div>
                )}
                <button
                  type="button"
                  className="btn btn-primary"
                  data-testid="evaluate-environmental-btn"
                  disabled={busy !== null || isPaused || !isProjectComplete}
                  onClick={() => void runEnvironmentalAssessment()}
                >
                  {busy === 'environmental'
                    ? 'Evaluando impacto ambiental…'
                    : !isProjectComplete
                      ? 'Evaluación ambiental bloqueada (datos incompletos)'
                      : environmentalAssessment
                        ? 'Actualizar evaluación ambiental'
                        : 'Evaluar impacto ambiental'}
                </button>
              </div>
            )}

            {environmentalError && <Alert variant="error">{environmentalError}</Alert>}

            {environmentalAssessment && (
              <>
                <Alert
                  variant={
                    environmentalAssessment.status === 'sufficient' && environmentalAssessment.risk_level !== 'high'
                      ? 'success'
                      : 'error'
                  }
                >
                  <div>
                    <strong>
                      Nivel de riesgo: {environmentalAssessment.risk_level_label} ·{' '}
                      {environmentalAssessment.favorable_label}
                    </strong>
                  </div>
                  <div style={{ marginTop: '0.35rem', lineHeight: 1.45 }}>
                    {environmentalAssessment.summary}
                  </div>
                  {environmentalAssessment.risk_observation && (
                    <div className="env-risk-observation-box">
                      <strong>⚠️ Observación de riesgo ambiental relevante:</strong>
                      <p style={{ margin: '0.25rem 0 0' }}>{environmentalAssessment.risk_observation}</p>
                    </div>
                  )}
                  <div style={{ marginTop: '0.35rem', fontSize: '0.8rem', opacity: 0.9 }}>
                    Evaluado el: {formatDate(environmentalAssessment.evaluated_at)}
                  </div>
                </Alert>

                {/* Panel de Puntaje Ambiental y Áreas Verdes HU1.7 */}
                <div className="env-score-card">
                  <div className="env-score-header">
                    <div>
                      <span className="env-score-tag">HU1.7 · Puntuación Ambiental Ponderada</span>
                      <h3 style={{ margin: '0.2rem 0', fontSize: '1.15rem', color: '#0f172a' }}>
                        Puntaje Ambiental: {environmentalAssessment.environmental_score !== null && environmentalAssessment.environmental_score !== undefined
                          ? `${environmentalAssessment.environmental_score.toFixed(1)} / 100`
                          : 'No determinado'}
                      </h3>
                    </div>
                    {environmentalAssessment.environmental_score !== null && environmentalAssessment.environmental_score !== undefined && (
                      <span className={`env-score-badge ${environmentalAssessment.environmental_score >= 70 ? 'score-good' : 'score-low'}`}>
                        {environmentalAssessment.environmental_score >= 70 ? 'Aceptable' : 'Bajo'}
                      </span>
                    )}
                  </div>

                  <div className="env-score-breakdown">
                    <div className="env-score-item">
                      <span className="env-score-label">Puntaje Base (HU1.6):</span>
                      <span className="env-score-val">
                        {environmentalAssessment.risk_level === 'low'
                          ? '85.0 pts (Riesgo Bajo)'
                          : environmentalAssessment.risk_level === 'moderate'
                          ? '70.0 pts (Riesgo Moderado)'
                          : environmentalAssessment.risk_level === 'high'
                          ? '25.0 pts (Riesgo Alto)'
                          : 'No determinado (Datos insuficientes)'}
                      </span>
                    </div>
                    <div className="env-score-item">
                      <span className="env-score-label">Efecto Áreas Verdes (HU1.7):</span>
                      <span className="env-score-val">
                        {environmentalAssessment.green_area?.score_effect_pts !== null && environmentalAssessment.green_area?.score_effect_pts !== undefined
                          ? environmentalAssessment.green_area.score_effect_pts > 0
                            ? `+${environmentalAssessment.green_area.score_effect_pts.toFixed(1)} pts (${environmentalAssessment.green_area.effect_type})`
                            : environmentalAssessment.green_area.score_effect_pts < 0
                            ? `${environmentalAssessment.green_area.score_effect_pts.toFixed(1)} pts (${environmentalAssessment.green_area.effect_type})`
                            : '0.0 pts (Neutro)'
                          : 'No determinado · Sin ventaja ambiental automática'}
                      </span>
                    </div>
                  </div>

                  {environmentalAssessment.score_explanation && (
                    <p className="env-score-explanation">
                      <strong>Explicación del cálculo:</strong> {environmentalAssessment.score_explanation}
                    </p>
                  )}
                </div>

                {/* Detalle de Áreas Verdes HU1.7 */}
                {environmentalAssessment.green_area && (
                  <div className="green-area-panel">
                    <div className="green-area-header">
                      <div>
                        <span className="story-label" style={{ fontSize: '0.75rem' }}>PMV 2 · HU1.7</span>
                        <h3 style={{ margin: '0.15rem 0', fontSize: '1rem', color: '#0f172a' }}>
                          Conservación y Protección de Áreas Verdes
                        </h3>
                      </div>
                      <span className={`green-status-badge ${environmentalAssessment.green_area.has_sufficient_data ? 'status-ok' : 'status-warn'}`}>
                        {environmentalAssessment.green_area.has_sufficient_data
                          ? environmentalAssessment.green_area.affectation_type_label || 'Evaluado'
                          : 'Información insuficiente'}
                      </span>
                    </div>

                    {!environmentalAssessment.green_area.has_sufficient_data ? (
                      <div className="green-insufficient-banner">
                        <div style={{ fontWeight: 700, color: '#b45309', marginBottom: '0.25rem' }}>
                          ⚠️ Información insuficiente sobre áreas verdes (Sin ventaja ambiental automática)
                        </div>
                        <p style={{ margin: '0.35rem 0', fontSize: '0.85rem', color: '#92400e', lineHeight: 1.45 }}>
                          <strong>Efecto en el puntaje:</strong>{' '}
                          {environmentalAssessment.green_area.score_effect_pts !== null && environmentalAssessment.green_area.score_effect_pts !== undefined
                            ? `${environmentalAssessment.green_area.score_effect_pts.toFixed(1)} pts`
                            : 'No determinado · Sin ventaja ambiental automática'}
                        </p>
                        <p style={{ margin: 0, fontSize: '0.85rem', color: '#92400e', lineHeight: 1.45 }}>
                          El expediente no cuenta con superficie delimitada ni medidas de protección verificables. De conformidad con el criterio de aceptación <strong>CA-HU1.7-02</strong>, no se asigna bonificación ni ventaja ambiental automática.
                        </p>
                        <div className="social-source-tag" style={{ marginTop: '0.5rem' }}>
                          {environmentalAssessment.green_area.source_name}
                          {environmentalAssessment.green_area.is_simulated && (
                            <span className="social-simulated-pill">Simulado</span>
                          )}
                        </div>
                      </div>
                    ) : (
                      <div className="green-details-grid">
                        <div className="green-detail-card">
                          <span className="green-detail-label">Superficie de Áreas Verdes</span>
                          <div className="green-metric-value">
                            {environmentalAssessment.green_area.green_area_m2 !== null && environmentalAssessment.green_area.green_area_m2 !== undefined
                              ? `${environmentalAssessment.green_area.green_area_m2.toLocaleString()} m²`
                              : 'No determinada'}
                            {environmentalAssessment.green_area.green_area_percentage !== null && environmentalAssessment.green_area.green_area_percentage !== undefined && (
                              <span className="green-metric-percent">
                                ({environmentalAssessment.green_area.green_area_percentage}% del predio)
                              </span>
                            )}
                          </div>
                          <span className="green-detail-sub">
                            Total predio: {environmentalAssessment.green_area.total_project_area_m2?.toLocaleString() || 'N/A'} m²
                          </span>
                        </div>

                        <div className="green-detail-card">
                          <span className="green-detail-label">Delimitación Cartográfica</span>
                          <p className="green-detail-text">
                            {environmentalAssessment.green_area.delimitation_description || 'No delimitado'}
                          </p>
                        </div>

                        <div className="green-detail-card">
                          <span className="green-detail-label">Afectación y Medidas de Protección</span>
                          <p className="green-detail-text">
                            <strong>Declarado:</strong> {environmentalAssessment.green_area.declared_affectation || 'No especificado'}
                          </p>
                          <p className="green-detail-text">
                            <strong>Verificable:</strong> {environmentalAssessment.green_area.verifiable_affectation || 'No verificado'}
                          </p>
                          <p className="green-detail-text">
                            <strong>Medidas:</strong> {environmentalAssessment.green_area.protection_measures || 'Ninguna'}
                          </p>
                          <div style={{ marginTop: '0.4rem' }}>
                            <span className={`green-measures-pill ${environmentalAssessment.green_area.has_sufficient_measures ? 'pill-ok' : 'pill-warn'}`}>
                              {environmentalAssessment.green_area.has_sufficient_measures
                                ? '✓ Medidas suficientes y acreditadas'
                                : '⚠️ Medidas insuficientes o no acreditadas'}
                            </span>
                          </div>
                        </div>

                        <div className="green-detail-card">
                          <span className="green-detail-label">Efecto en el Puntaje y Sustento</span>
                          <div className="green-metric-value" style={{ fontSize: '1.1rem' }}>
                            {environmentalAssessment.green_area.score_effect_pts !== null && environmentalAssessment.green_area.score_effect_pts !== undefined
                              ? environmentalAssessment.green_area.score_effect_pts > 0
                                ? `+${environmentalAssessment.green_area.score_effect_pts.toFixed(1)} pts (Bonificación)`
                                : environmentalAssessment.green_area.score_effect_pts < 0
                                ? `${environmentalAssessment.green_area.score_effect_pts.toFixed(1)} pts (Penalización)`
                                : '0.0 pts (Neutro)'
                              : 'No determinado · Sin ventaja ambiental automática'}
                          </div>
                          <p className="green-detail-text" style={{ marginTop: '0.25rem' }}>
                            {environmentalAssessment.green_area.calculation_explanation}
                          </p>
                          <div className="social-source-tag" style={{ marginTop: '0.5rem' }}>
                            {environmentalAssessment.green_area.source_name}
                            {environmentalAssessment.green_area.is_simulated && (
                              <span className="social-simulated-pill">Simulado</span>
                            )}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                <div className="env-contrast-panel">
                  <h3>Contraste: Declaración en expediente vs. Evidencia territorial de demostración</h3>
                  <div className="env-contrast-grid">
                    <div className="env-contrast-col env-col-declared">
                      <h4>Declarado en el expediente</h4>
                      <p>
                        <strong>Impactos declarados:</strong>{' '}
                        {environmentalAssessment.declared_vs_evidence.declared_by_applicant || 'No declarado'}
                      </p>
                      <p>
                        <strong>Medidas previstas:</strong>{' '}
                        {environmentalAssessment.declared_vs_evidence.declared_measures || 'No especificadas'}
                      </p>
                    </div>

                    <div className="env-contrast-col env-col-evidence">
                      <h4>Evidencia territorial de demostración (simulada)</h4>
                      <p>
                        <strong>Hallazgos cartográficos / normativos:</strong>{' '}
                        {environmentalAssessment.declared_vs_evidence.verifiable_evidence || (
                          <span className="env-insufficient-badge">Sin evidencia territorial (Información insuficiente)</span>
                        )}
                      </p>
                      <p>
                        <strong>Proximidad a fuentes hídricas:</strong>{' '}
                        {environmentalAssessment.declared_vs_evidence.proximity_to_water_meters !== null &&
                        environmentalAssessment.declared_vs_evidence.proximity_to_water_meters !== undefined
                          ? `${environmentalAssessment.declared_vs_evidence.proximity_to_water_meters} metros`
                          : 'No determinada'}
                      </p>
                      <p>
                        <strong>Zona de riesgo / faja marginal:</strong>{' '}
                        {environmentalAssessment.declared_vs_evidence.is_in_protected_or_risk_area === true ? (
                          'SÍ (Faja marginal intangible / Riesgo alto PDM)'
                        ) : environmentalAssessment.declared_vs_evidence.is_in_protected_or_risk_area === false ? (
                          'NO (Fuera de faja marginal)'
                        ) : (
                          <span className="env-insufficient-badge">No determinado: información insuficiente</span>
                        )}
                      </p>
                      <div className="social-source-tag">
                        {environmentalAssessment.declared_vs_evidence.evidence_source}
                        {environmentalAssessment.declared_vs_evidence.is_simulated && (
                          <span className="social-simulated-pill">Simulado</span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                {environmentalAssessment.status === 'insufficient' ? (
                  <div className="env-insufficient-banner">
                    <h4>Información insuficiente</h4>
                    <p>
                      Faltan datos esenciales para sustentar una valoración ambiental completa. No se emite pronunciamiento favorable por defecto.
                    </p>
                  </div>
                ) : (
                  <>
                    <h3 style={{ marginTop: '1.25rem', marginBottom: '0.5rem', fontSize: '1rem', color: '#0f172a' }}>
                      Impactos identificados y posibles mitigaciones
                    </h3>
                    <div className="env-impacts-grid">
                      {environmentalAssessment.impacts.map((imp, idx) => (
                        <div key={idx} className={`env-impact-card severity-${imp.severity}`}>
                          <div className="env-impact-header">
                            <span className="env-impact-comp">{imp.component_label}</span>
                            <span className={`env-severity-badge severity-${imp.severity}`}>
                              Severidad {imp.severity.toUpperCase()}
                            </span>
                          </div>
                          <p className="env-impact-desc">
                            <strong>Impacto:</strong> {imp.description}
                          </p>
                          <p className="env-impact-substantiation">
                            <strong>Sustento:</strong> {imp.substantiation}
                          </p>
                          {imp.mitigations && imp.mitigations.length > 0 && (
                            <div className="env-impact-mitigations">
                              <strong>Posibles mitigaciones:</strong>
                              <ul>
                                {imp.mitigations.map((m, mIdx) => (
                                  <li key={mIdx}>{m}</li>
                                ))}
                              </ul>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </>
                )}

                <TechnicalDetails data={environmentalAssessment} />
              </>
            )}
          </section>


          
          {/* PMV 2 · HU1.8: Viabilidad técnica del proyecto (Preevaluación multidimensional) */}
          <section
            className="flow-panel technical-flow-panel"
            aria-label="Preevaluación de viabilidad técnica (HU1.8)"
            data-testid="technical-assessment-section"
            id="seccion-viabilidad-tecnica"
          >
            <div className="flow-panel-heading">
              <div>
                <span className="story-label">PMV 2 · HU1.8</span>
                <h2>Viabilidad técnica del proyecto (Preevaluación multidimensional)</h2>
              </div>
              <span className="review-badge">Planificación Técnica · RNE</span>
            </div>

            <p className="flow-hint">
              Preevaluación automatizada de parámetros técnicos (mecánica de suelos, servicios básicos, topografía/taludes y dimensionamiento) conforme al RNE, Directiva Invierte.pe y normativa local.
            </p>

            <div className="technical-disclaimer-box" role="alert" data-testid="technical-disclaimer">
              <div className="disclaimer-title">
                <span>⚖️</span>
                <strong>Opinión preliminar automatizada · No constituye certificación profesional</strong>
              </div>
              <p>
                {technicalAssessment?.disclaimer ||
                  'ADVERTENCIA DE CONTROL Y ALCANCE LEGAL: Esta evaluación técnica constituye una opinión preliminar automatizada basada en los parámetros y documentos registrados en la plataforma. No tiene carácter de certificación profesional, no constituye liquidación ni conformidad de obra, y no sustituye la responsabilidad funcional ni la firma colegiada de los ingenieros especialistas responsables (Ley N.° 28858 y Reglamento Nacional de Edificaciones - RNE).'}
              </p>
            </div>

            {hasRole('PLANNER') && (
              <div className="technical-actions">
                {!isProjectComplete && (
                  <div style={{ marginBottom: '0.65rem' }}>
                    <Alert variant="error">
                      Evaluación técnica bloqueada hasta completar los datos obligatorios: {missingFieldLabels.join(', ')}. Los parámetros previos siguen consultables en solo lectura.
                    </Alert>
                  </div>
                )}
                <button
                  type="button"
                  className="btn btn-primary"
                  onClick={handleEvaluateTechnical}
                  disabled={technicalLoading || isPaused || !isProjectComplete}
                  data-testid="evaluate-technical-feasibility-btn"
                >
                  {technicalLoading
                    ? 'Evaluando parámetros técnicos...'
                    : !isProjectComplete
                      ? 'Evaluación técnica bloqueada (datos incompletos)'
                      : technicalAssessment
                        ? 'Reevaluar viabilidad técnica'
                        : 'Ejecutar preevaluación técnica'}
                </button>
              </div>
            )}

            {technicalError && (
              <div className="pert-error-banner" role="alert">
                <div className="pert-error-header">
                  <span>⚠️</span>
                  <span>Error en preevaluación técnica</span>
                </div>
                <div className="pert-error-detail">{technicalError}</div>
              </div>
            )}

            {technicalSuccess && (
              <div className="pert-success-banner" role="status">
                <div className="pert-success-header">
                  <span>✅</span>
                  <span>{technicalSuccess}</span>
                </div>
              </div>
            )}

            {technicalAssessment && (
              <div className="technical-results-card" data-testid="technical-results-card">
                <div className="technical-result-header">
                  <div>
                    <span className="technical-kicker">Resultado de la preevaluación técnica</span>
                    <h3 className="technical-headline">{technicalAssessment.summary}</h3>
                  </div>
                  <div className="technical-status-pill-group">
                    <span
                      className={`badge badge-${technicalAssessment.overall_result}`}
                      data-testid="technical-overall-badge"
                    >
                      {technicalAssessment.overall_result_label}
                    </span>
                    <span
                      className={`badge ${technicalAssessment.is_viable ? 'badge-viable' : 'badge-no-viable'}`}
                      data-testid="technical-viability-badge"
                    >
                      {technicalAssessment.is_viable ? 'Viable (Opinión preliminar)' : 'No viable preliminarmente'}
                    </span>
                  </div>
                </div>

                <div className="technical-metrics-grid">
                  <div className="technical-metric-card">
                    <span className="metric-label">Puntaje técnico</span>
                    <span className="metric-value" data-testid="technical-score-value">
                      {technicalAssessment.technical_score !== null && technicalAssessment.technical_score !== undefined
                        ? `${technicalAssessment.technical_score.toFixed(1)} / 100`
                        : 'No determinado'}
                    </span>
                    <span className="metric-hint">
                      {technicalAssessment.score_explanation || 'Puntaje sujeto a disponibilidad de datos suficientes'}
                    </span>
                  </div>

                  <div className="technical-metric-card">
                    <span className="metric-label">Criterios cumplidos</span>
                    <span className="metric-value text-success">
                      {technicalAssessment.criteria_summary.cumple} / {technicalAssessment.criteria_summary.total}
                    </span>
                    <span className="metric-hint">Conforme a norma aplicable</span>
                  </div>

                  <div className="technical-metric-card">
                    <span className="metric-label">Criterios observados</span>
                    <span className="metric-value text-warning">
                      {technicalAssessment.criteria_summary.observado}
                    </span>
                    <span className="metric-hint">Requieren subsanación técnica</span>
                  </div>

                  <div className="technical-metric-card">
                    <span className="metric-label">Información insuficiente</span>
                    <span className="metric-value text-danger">
                      {technicalAssessment.criteria_summary.informacion_insuficiente}
                    </span>
                    <span className="metric-hint">Datos faltantes indispensables</span>
                  </div>

                  {(technicalAssessment.criteria_summary.simulado_no_verificado ?? 0) > 0 && (
                    <div className="technical-metric-card">
                      <span className="metric-label">Sustento simulado</span>
                      <span className="metric-value text-warning">
                        {technicalAssessment.criteria_summary.simulado_no_verificado}
                      </span>
                      <span className="metric-hint">No comprobado en archivo físico</span>
                    </div>
                  )}
                </div>

                <div className="technical-criteria-list" data-testid="technical-criteria-list">
                  <h4>Desglose por criterio técnico reglamentario</h4>
                  <div className="criteria-cards-grid">
                    {technicalAssessment.criteria.map((c) => (
                      <div
                        key={c.criterion_code}
                        className={`criterion-card criterion-status-${c.status}`}
                        data-testid={`criterion-card-${c.criterion_code}`}
                      >
                        <div className="criterion-card-header">
                          <div>
                            <span className="criterion-code">{c.criterion_code}</span>
                            <h5>{c.criterion_name}</h5>
                          </div>
                          <span className={`badge badge-${c.status}`}>
                            {c.status_label}
                          </span>
                        </div>

                        <div className="criterion-body">
                          <div className="criterion-row">
                            <span className="row-label">Base normativa:</span>
                            <span className="row-value">{c.normative_reference} ({c.normative_version})</span>
                          </div>

                          <div className="criterion-row">
                            <span className="row-label">Regla aplicada:</span>
                            <span className="row-value">{c.applied_rule}</span>
                          </div>

                          <div className="criterion-row">
                            <span className="row-label">Parámetro medido:</span>
                            <span className="row-value font-mono">
                              {c.parameter_name
                                ? `${c.parameter_name}: ${
                                    c.parameter_value_numeric !== null && c.parameter_value_numeric !== undefined
                                      ? `${c.parameter_value_numeric} ${c.parameter_unit || ''}`
                                      : c.parameter_value_text || 'Registrado'
                                  }`
                                : 'No registrado en expediente'}
                            </span>
                          </div>

                          <div className="criterion-row">
                            <span className="row-label">Sustento documental:</span>
                            <span className="row-value">
                              {c.supporting_document || 'Sin documento sustentatorio aportado'}
                            </span>
                          </div>

                          <div className="criterion-row">
                            <span className="row-label">Origen de datos:</span>
                            <span className="row-value">
                              {c.is_simulated
                                ? 'Datos simulados de demostración (demo)'
                                : `Verificable (${c.source_origin})`}
                            </span>
                          </div>

                          <div className="criterion-explanation">
                            <strong>Diagnóstico técnico: </strong>
                            <span>{c.explanation}</span>
                          </div>

                          {c.correction_guidance && (
                            <div
                              className="correction-guidance-box"
                              data-testid={`guidance-${c.criterion_code}`}
                            >
                              <strong>Acción requerida para el planificador: </strong>
                              <span>{c.correction_guidance}</span>
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="dimension-integration-box">
                  <div className="dimension-header">
                    <strong>Integración multidimensional (HU1.12):</strong>
                    <span>{technicalAssessment.dimension_conclusion}</span>
                  </div>
                  <p className="dimension-justification">
                    {technicalAssessment.dimension_justification}
                  </p>
                </div>

                <TechnicalDetails data={technicalAssessment} />
              </div>
            )}
          </section>

          {/* PMV 2 · HU1.9: Estimación de duración de obra e incertidumbre (PERT) */}
          <section className="flow-panel pert-flow-panel" aria-label="Estimación PERT e incertidumbre de plazos">
            <div className="flow-panel-heading">
              <div>
                <span className="story-label">PMV 2 · HU1.9</span>
                <h2>Estimación de duración de obra e incertidumbre (PERT)</h2>
              </div>
              <span className="review-badge">Planificación y Plazos</span>
            </div>
            <p className="flow-hint">
              Registre las tres duraciones de la obra en la misma unidad de tiempo para estimar la duración esperada bajo incertidumbre con la distribución PERT (0 ≤ a ≤ m ≤ b).
            </p>

            {hasRole('PLANNER') && (
              <div className="pert-inputs-container">
                <div className="pert-inputs-grid">
                  <div className="pert-input-field">
                    <label htmlFor="pert-unit">
                      <span>Unidad de tiempo</span>
                      <span className="pert-unit-tag">unidad</span>
                    </label>
                    <select
                      id="pert-unit"
                      className="form-field-input"
                      value={pertTimeUnit}
                      onChange={(e) => {
                      setPertTimeUnit(e.target.value)
                      setPertEstimate(null)
                      setPertSuccess(null)
                      setPertError(null)
                    }}
                    >
                      <option value="meses">Meses</option>
                      <option value="días">Días</option>
                      <option value="semanas">Semanas</option>
                    </select>
                  </div>

                  <div className="pert-input-field">
                    <label htmlFor="pert-opt">
                      <span>Duración optimista (a)</span>
                      <span className="pert-unit-tag">{pertTimeUnit}</span>
                    </label>
                    <input
                      id="pert-opt"
                      type="number"
                      step="any"
                      min="0"
                      className="form-field-input"
                      placeholder={`Ej. 4.0 (${pertTimeUnit})`}
                      value={pertInputA}
                      onChange={(e) => {
                        setPertInputA(e.target.value)
                        setPertEstimate(null)
                        setPertSuccess(null)
                        setPertError(null)
                      }}
                    />
                  </div>

                  <div className="pert-input-field">
                    <label htmlFor="pert-likely">
                      <span>Duración más probable (m)</span>
                      <span className="pert-unit-tag">{pertTimeUnit}</span>
                    </label>
                    <input
                      id="pert-likely"
                      type="number"
                      step="any"
                      min="0"
                      className="form-field-input"
                      placeholder={`Ej. 6.0 (${pertTimeUnit})`}
                      value={pertInputM}
                      onChange={(e) => {
                        setPertInputM(e.target.value)
                        setPertEstimate(null)
                        setPertSuccess(null)
                        setPertError(null)
                      }}
                    />
                  </div>

                  <div className="pert-input-field">
                    <label htmlFor="pert-pess">
                      <span>Duración pesimista (b)</span>
                      <span className="pert-unit-tag">{pertTimeUnit}</span>
                    </label>
                    <input
                      id="pert-pess"
                      type="number"
                      step="any"
                      min="0"
                      className="form-field-input"
                      placeholder={`Ej. 10.0 (${pertTimeUnit})`}
                      value={pertInputB}
                      onChange={(e) => {
                        setPertInputB(e.target.value)
                        setPertEstimate(null)
                        setPertSuccess(null)
                        setPertError(null)
                      }}
                    />
                  </div>
                </div>

                <div style={{ marginBottom: '1.25rem' }}>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={busy !== null || isPaused}
                    onClick={() => void handleCalculatePert()}
                  >
                    {busy === 'pert' ? 'Calculando y guardando…' : 'Calcular y registrar estimación PERT'}
                  </button>
                </div>
              </div>
            )}

            {pertError && (
              <div className="pert-error-banner" role="alert">
                <div className="pert-error-header">
                  <span>⚠️</span>
                  <span>Error de validación en estimaciones de plazo (HU1.9)</span>
                </div>
                <div className="pert-error-detail">
                  {pertError.field && (
                    <strong>
                      Campo a corregir:{' '}
                      {pertError.field === 'optimistic_duration'
                        ? 'Duración optimista (a)'
                        : pertError.field === 'most_likely_duration'
                          ? 'Duración más probable (m)'
                          : pertError.field === 'pessimistic_duration'
                            ? 'Duración pesimista (b)'
                            : pertError.field}
                      .
                    </strong>
                  )}{' '}
                  {pertError.reason || pertError.message}
                </div>
                <div className="pert-error-advice">
                  Requisito: Los tres valores deben estar completos, ser finitos y cumplir estrictamente 0 ≤ a ≤ m ≤ b.
                </div>
              </div>
            )}

            {pertSuccess && (
              <div className="pert-success-alert">
                <Alert variant="success">{pertSuccess}</Alert>
              </div>
            )}

            {pertEstimate && !pertError && (
              <div className="pert-result-container">
                {/* Aviso obligatorio de estimación no garantizada */}
                <div className="pert-disclaimer-callout" role="note">
                  <span style={{ fontSize: '1.25rem', lineHeight: 1 }}>ℹ️</span>
                  <div>
                    <strong>AVISO OBLIGATORIO · ESTIMACIÓN REFERENCIAL, NO PLAZO GARANTIZADO</strong>
                    <p>{pertEstimate.disclaimer}</p>
                  </div>
                </div>

                {/* Resumen de magnitudes clave */}
                <div className="pert-metrics-grid">
                  <div className="pert-metric-card highlight">
                    <div className="pert-metric-label">Duración esperada PERT (μ)</div>
                    <div className="pert-metric-value-row">
                      <span className="pert-metric-num">{Number(pertEstimate.expected_duration).toFixed(2)}</span>
                      <span className="pert-metric-unit">{pertEstimate.time_unit}</span>
                    </div>
                    <div className="pert-metric-sub">Fórmula: (a + 4m + b) / 6</div>
                  </div>

                  <div className="pert-metric-card">
                    <div className="pert-metric-label">Incertidumbre / Desv. estándar (σ)</div>
                    <div className="pert-metric-value-row">
                      <span className="pert-metric-num">± {Number(pertEstimate.standard_deviation).toFixed(2)}</span>
                      <span className="pert-metric-unit">{pertEstimate.time_unit}</span>
                    </div>
                    <div className="pert-metric-sub">Incertidumbre PERT: (b - a) / 6 (No es margen garantizado)</div>
                  </div>

                  <div className="pert-metric-card">
                    <div className="pert-metric-label">Varianza del plazo (σ²)</div>
                    <div className="pert-metric-value-row">
                      <span className="pert-metric-num">{Number(pertEstimate.variance).toFixed(4)}</span>
                      <span className="pert-metric-unit">{pertEstimate.time_unit}²</span>
                    </div>
                    <div className="pert-metric-sub">Variabilidad cuadrática: ((b - a) / 6)²</div>
                  </div>
                </div>

                {/* Desglose de parámetros y fórmula aplicada */}
                <div className="pert-details-panel">
                  <h4>Desglose de estimaciones ingresadas y fórmula aplicada</h4>
                  <div className="pert-params-row">
                    <div className="pert-param-item">
                      <span className="pert-param-k">Optimista (a)</span>
                      <span className="pert-param-v">
                        {Number(pertEstimate.optimistic_duration).toFixed(2)} {pertEstimate.time_unit}
                      </span>
                    </div>
                    <div className="pert-param-item">
                      <span className="pert-param-k">Más probable (m)</span>
                      <span className="pert-param-v">
                        {Number(pertEstimate.most_likely_duration).toFixed(2)} {pertEstimate.time_unit}
                      </span>
                    </div>
                    <div className="pert-param-item">
                      <span className="pert-param-k">Pesimista (b)</span>
                      <span className="pert-param-v">
                        {Number(pertEstimate.pessimistic_duration).toFixed(2)} {pertEstimate.time_unit}
                      </span>
                    </div>
                    <div className="pert-param-item">
                      <span className="pert-param-k">Dispersión (b - a)</span>
                      <span className="pert-param-v">
                        {(Number(pertEstimate.pessimistic_duration) - Number(pertEstimate.optimistic_duration)).toFixed(2)}{' '}
                        {pertEstimate.time_unit}
                      </span>
                    </div>
                    <div className="pert-param-item">
                      <span className="pert-param-k">Rango de estimaciones [a, b]</span>
                      <span className="pert-param-v">
                        [{Number(pertEstimate.optimistic_duration).toFixed(2)}, {Number(pertEstimate.pessimistic_duration).toFixed(2)}]{' '}
                        {pertEstimate.time_unit}
                      </span>
                    </div>
                    <div className="pert-param-item">
                      <span className="pert-param-k">Rango esperado (μ ± σ)</span>
                      <span className="pert-param-v">
                        [{Number(pertEstimate.confidence_interval_low).toFixed(2)},{' '}
                        {Number(pertEstimate.confidence_interval_high).toFixed(2)}] {pertEstimate.time_unit}
                      </span>
                    </div>
                  </div>

                  <div className="pert-formula-box">
                    <strong>Fórmula aplicada: </strong>
                    {pertEstimate.formula_applied}
                  </div>
                  <p style={{ margin: '0.5rem 0 0 0', fontSize: '0.85rem', color: '#475569', lineHeight: 1.4 }}>
                    <strong>Explicación: </strong>
                    {pertEstimate.explanation}
                  </p>
                </div>

                <TechnicalDetails data={pertEstimate} />
              </div>
            )}
          </section>

          <section className="flow-panel legal-flow-panel">
            <div className="flow-panel-heading">
              <div>
                <span className="story-label">PMV 2 · HU2.2 / HU1.10</span>
                <h2>3. Conformidad jurídica y citas normativas exactas</h2>
              </div>
              <span className="review-badge">Asesoría Jurídica</span>
            </div>
            <p className="flow-hint">
              Contrasta los antecedentes del expediente con el corpus normativo versionado, citando el artículo o sección exacta de la norma aplicable para su cotejo directo, o advirtiendo cita no verificable sin fabricar artículos (Asesoría Jurídica).
            </p>
            {isPaused ? (
              <div className="checkpoint-blocked-alert">
                Revisión jurídica temporalmente detenida por el punto de control activo.
              </div>
            ) : (
              <>
                {!isProjectComplete && (
                  <div style={{ marginBottom: '0.65rem' }}>
                    <Alert variant="error">
                      Evaluación jurídica formal bloqueada hasta completar: {missingFieldLabels.join(', ')}. Puede consultar el detalle normativo en solo lectura.
                    </Alert>
                  </div>
                )}
                <Link
                  to={`/proyectos/${projectId}/revision-juridica`}
                  className="btn btn-primary"
                >
                  Abrir detalle de revisión jurídica y citas exactas
                </Link>

                <div
                  data-testid="procurement-section"
                  style={{
                    marginTop: '1.15rem',
                    paddingTop: '1rem',
                    borderTop: '1px solid #e2e8f0',
                    display: 'grid',
                    gap: '0.85rem',
                  }}
                >
                  <div>
                    <span className="story-label">PMV 1 · HU1.10 · Ley N.° 32069 y D.S. N.° 009-2025-EF</span>
                    <h3 style={{ margin: '0.2rem 0 0', fontSize: '1.02rem' }}>
                      Verificación preliminar de Ley de Contrataciones Públicas (v. OECE 2026)
                    </h3>
                    <p style={{ margin: '0.3rem 0 0', fontSize: '0.84rem', color: '#475569' }}>
                      Contrasta únicamente antecedentes contractuales estructurados (sin inferir cumplimiento desde el título o descripción) contra fragmentos vigentes y actualizados al Compendio OECE (excluyendo Ley 26850, Ley 30225 y texto 2024-v1 pre-vigencia).
                    </p>
                  </div>

                  {procError && <Alert variant="error">{procError}</Alert>}
                  {procSuccess && <Alert variant="success">{procSuccess}</Alert>}

                  {canEditExpediente && (
                    <div
                      data-testid="procurement-antecedents-form"
                      style={{
                        background: '#f8fafc',
                        border: '1px solid #cbd5e1',
                        borderRadius: '8px',
                        padding: '0.9rem',
                        display: 'grid',
                        gap: '0.7rem',
                      }}
                    >
                      <strong style={{ fontSize: '0.88rem', color: '#0f172a' }}>
                        Antecedentes contractuales de la versión vigente {project ? `(v${project.version_number})` : ''}
                      </strong>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '0.6rem' }}>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.8rem', fontWeight: 600 }}>
                          Régimen contractual
                          <select
                            data-testid="proc-regime-select"
                            className="form-field-input"
                            value={procRegime}
                            onChange={(e) => { setProcRegime(e.target.value); setProcDirty(true) }}
                          >
                            <option value="">-- Sin especificar --</option>
                            <option value="ley_32069">Ley N.° 32069 y D.S. N.° 009-2025-EF (Vigente)</option>
                            <option value="ley_30225_historica">Ley N.° 30225 (Sustituida el 22/04/2025)</option>
                            <option value="ley_26850_historica">Ley N.° 26850 (Histórica derogada)</option>
                            <option value="regimen_especial">Régimen especial</option>
                            <option value="administracion_directa">Administración directa</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.8rem', fontWeight: 600 }}>
                          Objeto contractual
                          <select
                            data-testid="proc-object-select"
                            className="form-field-input"
                            value={procObject}
                            onChange={(e) => { setProcObject(e.target.value); setProcDirty(true) }}
                          >
                            <option value="">-- Sin especificar --</option>
                            <option value="obra">Obra pública</option>
                            <option value="bien">Bienes</option>
                            <option value="servicio">Servicios</option>
                            <option value="consultoria_obra">Consultoría de obra</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.8rem', fontWeight: 600 }}>
                          Fase contractual
                          <select
                            data-testid="proc-phase-select"
                            className="form-field-input"
                            value={procPhase}
                            onChange={(e) => { setProcPhase(e.target.value); setProcDirty(true) }}
                          >
                            <option value="">-- Sin especificar --</option>
                            <option value="programacion_cmn">Programación Multianual (PMBSO / CMN)</option>
                            <option value="actuaciones_preparatorias">Actuaciones preparatorias</option>
                            <option value="seleccion">Selección</option>
                            <option value="ejecucion_contractual">Ejecución contractual</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.8rem', fontWeight: 600 }}>
                          Procedimiento / modalidad
                          <select
                            data-testid="proc-procedure-select"
                            className="form-field-input"
                            value={procProcedure}
                            onChange={(e) => { setProcProcedure(e.target.value); setProcDirty(true) }}
                          >
                            <option value="">-- Sin especificar --</option>
                            <option value="licitacion_publica">Licitación pública</option>
                            <option value="concurso_publico">Concurso público</option>
                            <option value="modalidad_abreviada">Modalidad abreviada</option>
                            <option value="subasta_inversa_electronica">Subasta inversa electrónica</option>
                            <option value="comparacion_precios">Comparación de precios</option>
                            <option value="contrato_menor">Contrato menor (&lt;= 8 UIT = S/ 44,000)</option>
                            <option value="contratacion_no_competitiva">Contratación no competitiva</option>
                          </select>
                        </label>
                      </div>

                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))', gap: '0.6rem' }}>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.78rem', fontWeight: 600 }}>
                          Requerimiento / Exp. Técnico (Art. 46)
                          <select
                            data-testid="proc-req-defined"
                            className="form-field-input"
                            value={procReqDefined}
                            onChange={(e) => { setProcReqDefined(e.target.value as '' | 'true' | 'false'); setProcDirty(true) }}
                          >
                            <option value="">Sin indicar</option>
                            <option value="true">Sí, definido y sustentado</option>
                            <option value="false">No definido / incompleto</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.78rem', fontWeight: 600 }}>
                          En CMN y viabilidad Invierte.pe (Reg. 46/54)
                          <select
                            data-testid="proc-cmn"
                            className="form-field-input"
                            value={procInCmn}
                            onChange={(e) => { setProcInCmn(e.target.value as '' | 'true' | 'false'); setProcDirty(true) }}
                          >
                            <option value="">Sin indicar</option>
                            <option value="true">Sí, incluido y viable</option>
                            <option value="false">No consta en CMN o sin viabilidad</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.78rem', fontWeight: 600 }}>
                          Certificación / previsión presupuestal (Reg. 54.2.f)
                          <select
                            data-testid="proc-budget-cert"
                            className="form-field-input"
                            value={procBudgetCert}
                            onChange={(e) => { setProcBudgetCert(e.target.value as '' | 'true' | 'false'); setProcDirty(true) }}
                          >
                            <option value="">Sin indicar</option>
                            <option value="true">Sí, declarada</option>
                            <option value="false">No cuenta con certificación</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.78rem', fontWeight: 600 }}>
                          Registro en PLADICOP (Art. 41 mod. Ley 32187)
                          <select
                            data-testid="proc-pladicop"
                            className="form-field-input"
                            value={procPladicop}
                            onChange={(e) => { setProcPladicop(e.target.value as '' | 'true' | 'false'); setProcDirty(true) }}
                          >
                            <option value="">Sin indicar</option>
                            <option value="true">Sí, previsto / registrado</option>
                            <option value="false">No previsto en PLADICOP</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.78rem', fontWeight: 600 }}>
                          Sin fraccionamiento indebido (Art. 50 / Reg. 45)
                          <select
                            data-testid="proc-no-frac"
                            className="form-field-input"
                            value={procNoFrac}
                            onChange={(e) => { setProcNoFrac(e.target.value as '' | 'true' | 'false'); setProcDirty(true) }}
                          >
                            <option value="">Sin indicar</option>
                            <option value="true">Sí, sin fraccionamiento</option>
                            <option value="false">Posible fraccionamiento advertido</option>
                          </select>
                        </label>
                        <label style={{ display: 'grid', gap: '0.25rem', fontSize: '0.78rem', fontWeight: 600 }}>
                          Documento / referencia contractual
                          <input
                            data-testid="proc-doc-ref"
                            type="text"
                            className="form-field-input"
                            placeholder="Ej. Memo-DEC-045-2026 / TdR-012"
                            value={procDocRef}
                            onChange={(e) => { setProcDocRef(e.target.value); setProcDirty(true) }}
                          />
                        </label>
                      </div>

                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem' }}>
                        <button
                          type="button"
                          className="btn btn-secondary"
                          data-testid="save-procurement-antecedents-btn"
                          disabled={busy !== null}
                          onClick={() => void saveProcurementAntecedents()}
                        >
                          {busy === 'proc-save' ? 'Guardando antecedentes…' : 'Guardar antecedentes contractuales'}
                        </button>
                        <button
                          type="button"
                          className="btn btn-primary"
                          data-testid="verify-procurement-btn"
                          disabled={busy !== null}
                          onClick={() => void runProcurementVerification()}
                        >
                          {busy === 'proc-verify' ? 'Verificando Ley 32069…' : 'Verificar Ley de Contrataciones (HU1.10)'}
                        </button>
                      </div>
                    </div>
                  )}

                  {!canEditExpediente && (
                    <div>
                      <button
                        type="button"
                        className="btn btn-secondary"
                        data-testid="verify-procurement-btn"
                        disabled={busy !== null}
                        onClick={() => void runProcurementVerification()}
                      >
                        {busy === 'proc-verify' ? 'Verificando Ley 32069…' : 'Ejecutar verificación Ley de Contrataciones (HU1.10)'}
                      </button>
                    </div>
                  )}

                  {procVerification && (
                    <div
                      data-testid="procurement-result-card"
                      style={{
                        background:
                          procVerification.status === 'verified_preliminary'
                            ? '#f0fdf4'
                            : procVerification.status === 'possible_incompatibility'
                              ? '#fef2f2'
                              : '#fffbeb',
                        border: `1px solid ${
                          procVerification.status === 'verified_preliminary'
                            ? '#86efac'
                            : procVerification.status === 'possible_incompatibility'
                              ? '#fca5a5'
                              : '#fcd34d'
                        }`,
                        borderRadius: '8px',
                        padding: '0.95rem',
                        display: 'grid',
                        gap: '0.65rem',
                      }}
                    >
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                        <strong
                          style={{
                            color:
                              procVerification.status === 'verified_preliminary'
                                ? '#166534'
                                : procVerification.status === 'possible_incompatibility'
                                  ? '#991b1b'
                                  : '#92400e',
                          }}
                        >
                          {procVerification.status_label} · Versión v{procVerification.version_number}
                        </strong>
                        <span
                          className={`status-pill ${
                            procVerification.status === 'verified_preliminary'
                              ? 'status-approved'
                              : procVerification.status === 'possible_incompatibility'
                                ? 'status-error'
                                : 'status-review'
                          }`}
                        >
                          {procVerification.status === 'verified_preliminary'
                            ? 'Preliminar compatible (sujeto a DEC/Asesoría)'
                            : procVerification.status === 'possible_incompatibility'
                              ? 'Posible incompatibilidad (No certifica infracción)'
                              : 'Requiere revisión'}
                        </span>
                      </div>

                      <p style={{ margin: 0, fontSize: '0.88rem', lineHeight: 1.45 }}>
                        {procVerification.summary}
                      </p>

                      {procVerification.sent_to_legal_advisor && procVerification.status === 'possible_incompatibility' && (
                        <div
                          data-testid="procurement-legal-handoff"
                          style={{
                            background: '#fee2e2',
                            padding: '0.55rem 0.75rem',
                            borderRadius: '6px',
                            fontSize: '0.83rem',
                            color: '#991b1b',
                            fontWeight: 600,
                          }}
                        >
                          Observación remitida al Asesor Jurídico para validación humana (sin certificar infracción automática).{' '}
                          <Link to={`/proyectos/${projectId}/revision-juridica`} style={{ color: '#7f1d1d', textDecoration: 'underline' }}>
                            Revisar en módulo del Asesor Jurídico
                          </Link>
                        </div>
                      )}

                      {procVerification.missing_fields.length > 0 && (
                        <div
                          data-testid="procurement-missing-fields"
                          style={{
                            background: '#fef3c7',
                            padding: '0.55rem 0.75rem',
                            borderRadius: '6px',
                            fontSize: '0.82rem',
                            color: '#78350f',
                          }}
                        >
                          <strong>Antecedentes o sustentos faltantes ({procVerification.missing_fields.length}):</strong>{' '}
                          {procVerification.missing_fields.join('; ')}.
                        </div>
                      )}

                      <div data-testid="procurement-requirements-list" style={{ display: 'grid', gap: '0.45rem' }}>
                        <strong style={{ fontSize: '0.84rem', color: '#0f172a' }}>
                          Requisitos examinados (Fecha de consulta oficial OECE/MEF: {procVerification.consulted_at})
                        </strong>
                        {procVerification.examined_requirements.map((req) => (
                          <article
                            key={req.requirement_code}
                            data-testid={`proc-req-${req.requirement_code}`}
                            style={{
                              background: '#ffffff',
                              border: '1px solid #cbd5e1',
                              borderRadius: '6px',
                              padding: '0.6rem 0.75rem',
                              fontSize: '0.81rem',
                              display: 'grid',
                              gap: '0.25rem',
                            }}
                          >
                            <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.35rem' }}>
                              <strong>{req.requirement_name}</strong>
                              <span
                                style={{
                                  fontWeight: 700,
                                  color:
                                    req.status === 'compliant_preliminary'
                                      ? '#166534'
                                      : req.status === 'possible_incompatibility'
                                        ? '#991b1b'
                                        : '#92400e',
                                }}
                              >
                                {req.status_label}
                              </span>
                            </div>
                            <div style={{ color: '#334155' }}>
                              <strong>Dato del expediente:</strong> {req.expedient_evidence}
                            </div>
                            <div style={{ color: '#0f172a' }}>
                              <strong>Cotejo:</strong> {req.comparison_detail}
                            </div>
                            {req.norm_id && (
                              <div style={{ color: '#475569', fontSize: '0.78rem' }}>
                                <strong>Norma y fragmento:</strong> {req.norm_name} · Versión <code>{req.norm_version}</code> ·{' '}
                                <strong>{req.article_or_section}</strong>
                                {req.fragment_excerpt && (
                                  <div style={{ marginTop: '0.2rem', fontStyle: 'italic' }}>
                                    “{req.fragment_excerpt}”
                                  </div>
                                )}
                                {req.amendments_note && (
                                  <div style={{ marginTop: '0.15rem' }}>
                                    <strong>Modificatorias y vigencia:</strong> {req.amendments_note}
                                  </div>
                                )}
                                {req.official_url && (
                                  <div style={{ marginTop: '0.15rem' }}>
                                    <a href={req.official_url} target="_blank" rel="noreferrer">
                                      {req.official_url}
                                    </a>
                                  </div>
                                )}
                              </div>
                            )}
                          </article>
                        ))}
                      </div>

                      {procVerification.superseded_sources_excluded.length > 0 && (
                        <div style={{ fontSize: '0.78rem', color: '#475569' }}>
                          <strong>Fuentes históricas excluidas ({procVerification.superseded_sources_excluded.length}):</strong>{' '}
                          {procVerification.superseded_sources_excluded.map((s) => `${s.fragment_id} (${s.version})`).join(', ')}.
                        </div>
                      )}
                    </div>
                  )}

                  {!procVerification && procAntecedents && (
                    <div style={{ fontSize: '0.78rem', color: '#475569' }}>
                      Fuentes activas cargadas: {procAntecedents.active_normative_sources.length} · Fuentes históricas excluidas:{' '}
                      {procAntecedents.historical_normative_sources.length}.
                    </div>
                  )}
                </div>
              </>
            )}
          </section>

          <section className="flow-panel" data-testid="zoning-section">
            <h2>4. Compatibilidad urbanística (HU1.11)</h2>
            <p className="flow-hint">
              Coteja el uso de suelo propuesto frente al PDM Huancayo 2017-2037 (O.M. N.° 636-2020-MPH/CM) y capas geoespaciales versionadas, absteniéndose si no existe capa GIS predial verificable.
            </p>
            <button
              type="button"
              className="btn btn-secondary"
              data-testid="run-zoning-btn"
              disabled={busy !== null || isPaused}
              onClick={() => void runZoning()}
            >
              {busy === 'zoning' ? 'Analizando…' : 'Analizar compatibilidad urbanística'}
            </button>

            {activeZoning && (
              <div data-testid="zoning-result-card" style={{ marginTop: '0.9rem', display: 'grid', gap: '0.75rem' }}>
                {activeZoning.is_synthetic_data && (
                  <div
                    data-testid="zoning-synthetic-banner"
                    style={{
                      background: '#ffedd5',
                      border: '2px solid #ea580c',
                      color: '#9a3412',
                      borderRadius: '8px',
                      padding: '0.75rem 1rem',
                      fontSize: '0.86rem',
                      fontWeight: 700,
                    }}
                  >
                    DATOS SINTÉTICOS DE PRUEBA TÉCNICA (NO OFICIAL): esta ejecución usa un polígono ficticio de ensayo para validar el mecanismo espacial. No acredita compatibilidad ni zonificación real del PDM Huancayo.
                  </div>
                )}
                <div
                  style={{
                    background:
                      activeZoning.compatible === true
                        ? '#f0fdf4'
                        : activeZoning.compatible === false
                          ? '#fef2f2'
                          : '#fffbeb',
                    border: `1px solid ${
                      activeZoning.compatible === true
                        ? '#86efac'
                        : activeZoning.compatible === false
                          ? '#fca5a5'
                          : '#fcd34d'
                    }`,
                    borderRadius: '8px',
                    padding: '1rem',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
                    <strong
                      style={{
                        color:
                          activeZoning.compatible === true
                            ? '#14532d'
                            : activeZoning.compatible === false
                              ? '#991b1b'
                              : '#78350f',
                      }}
                    >
                      {activeZoning.status_label ??
                        (activeZoning.compatible === true
                          ? 'Compatibilidad preliminar sustentada'
                          : activeZoning.compatible === false
                            ? 'Posible incompatibilidad · Derivado a revisión jurídica'
                            : 'Requiere revisión / Información insuficiente')}
                    </strong>
                    <span
                      style={{
                        padding: '0.2rem 0.65rem',
                        borderRadius: '999px',
                        fontSize: '0.78rem',
                        fontWeight: 700,
                        background:
                          activeZoning.compatible === true
                            ? '#dcfce7'
                            : activeZoning.compatible === false
                              ? '#fee2e2'
                              : '#fef3c7',
                        color:
                          activeZoning.compatible === true
                            ? '#166534'
                            : activeZoning.compatible === false
                              ? '#991b1b'
                              : '#92400e',
                      }}
                    >
                      {activeZoning.compatible === true
                        ? 'Compatible preliminar'
                        : activeZoning.compatible === false
                          ? 'Posible incompatibilidad'
                          : 'Información insuficiente / Requiere revisión'}
                    </span>
                    <span
                      data-testid="zoning-coverage-pill"
                      style={{
                        padding: '0.18rem 0.55rem',
                        borderRadius: '999px',
                        fontSize: '0.74rem',
                        fontWeight: 600,
                        background: '#e2e8f0',
                        color: '#334155',
                      }}
                    >
                      Cobertura espacial:{' '}
                      {activeZoning.coverage_status === 'inside_coverage'
                        ? 'Dentro de cobertura consultada'
                        : activeZoning.coverage_status === 'outside_coverage'
                          ? 'Fuera de cobertura territorial'
                          : 'Sin capa GIS oficial verificable'}
                    </span>
                  </div>

                  {activeZoning.preliminary_conclusion && (
                    <p style={{ margin: '0.5rem 0 0', fontSize: '0.9rem', lineHeight: 1.45 }}>
                      {activeZoning.preliminary_conclusion}
                    </p>
                  )}

                  <p style={{ margin: '0.45rem 0 0', fontSize: '0.85rem', color: '#475569', lineHeight: 1.45 }}>
                    <strong>Alcance y limitaciones:</strong> {activeZoning.limitations}
                  </p>

                  {activeZoning.environmental_risk_notice && (
                    <div
                      data-testid="zoning-environmental-risk-notice"
                      style={{
                        marginTop: '0.65rem',
                        padding: '0.6rem 0.75rem',
                        background: '#fff7ed',
                        border: '1px solid #fdba74',
                        borderRadius: '6px',
                        fontSize: '0.84rem',
                        color: '#9a3412',
                      }}
                    >
                      <strong>Aviso de riesgo territorial (sin dictamen zonal automático):</strong>{' '}
                      {activeZoning.environmental_risk_notice}
                    </div>
                  )}

                  {activeZoning.flagged_for_legal_review && (
                    <div
                      data-testid="zoning-legal-handoff-notice"
                      style={{
                        marginTop: '0.65rem',
                        padding: '0.5rem 0.75rem',
                        background: '#fee2e2',
                        borderRadius: '6px',
                        fontSize: '0.84rem',
                        color: '#991b1b',
                        fontWeight: 600,
                      }}
                    >
                      Observación territorial señalada para revisión jurídica humana (sin certificar infracción automática).{' '}
                      <Link to={`/proyectos/${projectId}/revision-juridica`} style={{ color: '#7f1d1d', textDecoration: 'underline' }}>
                        Ver en Revisión Jurídica
                      </Link>
                    </div>
                  )}
                </div>

                {activeZoning.normative_amendments && activeZoning.normative_amendments.length > 0 && (
                  <div style={{ display: 'grid', gap: '0.45rem' }} data-testid="zoning-amendments-list">
                    <strong style={{ fontSize: '0.88rem', color: '#0f172a' }}>
                      Vigencia por ámbito y modificaciones parciales del PDM 2017–2037 ({activeZoning.normative_amendments.length})
                    </strong>
                    <div style={{ display: 'grid', gap: '0.4rem', maxHeight: '280px', overflowY: 'auto', paddingRight: '0.25rem' }}>
                      {activeZoning.normative_amendments.map((am) => {
                        const isMatch = am.applicability_status === 'posible_coincidencia_pendiente_catastro'
                        const isBase = am.applicability_status === 'base_vigente'
                        const isGeneral = am.applicability_status === 'alcance_general_reglamentario'
                        return (
                          <article
                            key={am.ordinance}
                            data-testid={`zoning-amendment-${am.ordinance}`}
                            style={{
                              background: isMatch ? '#fffbeb' : isBase ? '#f0f9ff' : '#f8fafc',
                              border: `1px solid ${isMatch ? '#f59e0b' : isBase ? '#7dd3fc' : '#cbd5e1'}`,
                              borderRadius: '6px',
                              padding: '0.6rem 0.75rem',
                              fontSize: '0.82rem',
                            }}
                          >
                            <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.35rem' }}>
                              <strong>{am.ordinance} ({am.publication_date})</strong>
                              <span
                                style={{
                                  fontSize: '0.74rem',
                                  fontWeight: 700,
                                  color: isMatch ? '#92400e' : isBase ? '#0369a1' : isGeneral ? '#4338ca' : '#475569',
                                }}
                              >
                                {isMatch
                                  ? 'Posible coincidencia de sector/vía — requiere cotejo catastral'
                                  : isBase
                                    ? 'Norma base vigente (no desactivada globalmente)'
                                    : isGeneral
                                      ? 'Modificación / rectificación reglamentaria general'
                                      : 'Modificación parcial específica — no generalizable a todo El Tambo'}
                              </span>
                            </div>
                            <div style={{ color: '#334155', marginTop: '0.18rem' }}>
                              <strong>Relación y alcance:</strong> {am.relation_to_base} — {am.scope_summary}
                            </div>
                            <div style={{ marginTop: '0.2rem', fontSize: '0.78rem' }}>
                              Fuente verificable:{' '}
                              <a href={am.official_url} target="_blank" rel="noreferrer">
                                {am.official_url}
                              </a>
                            </div>
                          </article>
                        )
                      })}
                    </div>
                  </div>
                )}

                {activeZoning.evidence && activeZoning.evidence.length > 0 && (
                  <div style={{ display: 'grid', gap: '0.5rem' }} data-testid="zoning-evidence-list">
                    <strong style={{ fontSize: '0.88rem', color: '#0f172a' }}>
                      Sustento documental y territorial consultado ({activeZoning.evidence.length})
                    </strong>
                    {activeZoning.evidence.map((ev, idx) => (
                      <article
                        key={`${ev.locator}-${idx}`}
                        style={{
                          background: '#f8fafc',
                          border: '1px solid #cbd5e1',
                          borderRadius: '6px',
                          padding: '0.75rem',
                          fontSize: '0.84rem',
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '0.35rem' }}>
                          <strong>{ev.document} (v. {ev.version})</strong>
                          <span style={{ fontSize: '0.76rem', color: ev.is_spatial_classification ? '#166534' : '#475569', fontWeight: 600 }}>
                            {ev.is_spatial_classification
                              ? 'Sustento espacial / zonal'
                              : 'Marco normativo documental (sin polígono predial)'}
                          </span>
                        </div>
                        <div style={{ color: '#334155', marginTop: '0.2rem' }}>
                          <strong>Sección / Localizador:</strong> {ev.locator}
                        </div>
                        <p style={{ margin: '0.25rem 0 0', color: '#475569', lineHeight: 1.4 }}>{ev.excerpt}</p>
                        {ev.official_url && (
                          <div style={{ marginTop: '0.25rem', fontSize: '0.78rem', color: '#64748b' }}>
                            <strong>Autoridad:</strong> {ev.authority ?? 'MPH'} · Fuente oficial:{' '}
                            <a href={ev.official_url} target="_blank" rel="noreferrer">
                              {ev.official_url}
                            </a>
                          </div>
                        )}
                      </article>
                    ))}
                  </div>
                )}

                <TechnicalDetails data={activeZoning} />
              </div>
            )}
          </section>

          {/* PMV 2 - HU1.12: Resumen Unificado de Evaluación Multidimensional */}
          <section
            className="flow-panel unified-summary-flow-panel"
            aria-label="Resumen unificado de evaluación multidimensional (HU1.12)"
            data-testid="unified-summary-section"
            style={{ borderLeft: '4px solid #6366f1' }}
          >
            <div className="flow-panel-heading" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '0.75rem' }}>
              <div>
                <span className="story-label" style={{ background: '#e0e7ff', color: '#3730a3', padding: '0.2rem 0.5rem', borderRadius: '4px', fontSize: '0.75rem', fontWeight: 700 }}>
                  PMV 2 · HU1.12
                </span>
                <h2 style={{ marginTop: '0.25rem' }}>Resumen unificado de evaluación multidimensional</h2>
              </div>
              <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                {unifiedSummary && (
                  <span
                    className={`badge ${
                      unifiedSummary.status === 'completed'
                        ? 'badge-success'
                        : unifiedSummary.status === 'blocked'
                        ? 'badge-danger'
                        : 'badge-warning'
                    }`}
                    data-testid="unified-summary-status-badge"
                    style={{
                      padding: '0.35rem 0.75rem',
                      borderRadius: '9999px',
                      fontSize: '0.8rem',
                      fontWeight: 700,
                      background:
                        unifiedSummary.status === 'completed'
                          ? '#dcfce7'
                          : unifiedSummary.status === 'blocked'
                          ? '#fee2e2'
                          : '#fef3c7',
                      color:
                        unifiedSummary.status === 'completed'
                          ? '#15803d'
                          : unifiedSummary.status === 'blocked'
                          ? '#b91c1c'
                          : '#b45309',
                    }}
                  >
                    {unifiedSummary.status_label}
                  </span>
                )}
                <button
                  type="button"
                  className="btn btn-secondary"
                  style={{ fontSize: '0.8rem', padding: '0.3rem 0.6rem' }}
                  onClick={() => void loadUnifiedSummary()}
                  disabled={unifiedSummaryLoading}
                  title="Actualizar resumen unificado"
                >
                  {unifiedSummaryLoading ? 'Actualizando…' : '↻ Actualizar'}
                </button>
              </div>
            </div>

            <p className="flow-hint" style={{ fontSize: '0.95rem', color: '#475569', marginBottom: '1rem' }}>
              Consolidación ejecutiva de las cinco dimensiones de evaluación (económica, social, ambiental, técnica y jurídica) para la versión vigente del expediente, con puntuación global reproducible según la política activa de criterios (HU1.12).
            </p>

            {unifiedSummaryError && (
              <div style={{ marginBottom: '1rem' }}>
                <Alert variant="error">
                  {unifiedSummaryError}
                </Alert>
              </div>
            )}

            {unifiedSummary && (
              <div className="unified-summary-content" style={{ display: 'flex', flexDirection: 'column', gap: '1.25rem' }}>
                {/* 1. Resumen Ejecutivo Integrado */}
                <div
                  style={{
                    background: '#f8fafc',
                    border: '1px solid #e2e8f0',
                    borderRadius: '8px',
                    padding: '1rem 1.25rem',
                    borderLeft: unifiedSummary.allows_final_approval ? '4px solid #10b981' : '4px solid #f59e0b',
                  }}
                  data-testid="unified-executive-summary"
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
                    <span style={{ fontSize: '0.8rem', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.05em', color: '#64748b' }}>
                      Dictamen Ejecutivo Preliminar (Versión {unifiedSummary.version_number})
                    </span>
                    <span style={{ fontSize: '0.8rem', color: '#64748b' }}>
                      Generado: {formatDate(unifiedSummary.generated_at)}
                    </span>
                  </div>
                  <p style={{ margin: 0, fontSize: '1rem', color: '#1e293b', fontWeight: 500, lineHeight: 1.5 }}>
                    {unifiedSummary.summary_text}
                  </p>
                </div>

                {/* 2. Puntuación Global Explicable (CA-HU1.12-02) */}
                <div
                  style={{
                    background: '#ffffff',
                    border: '1px solid #cbd5e1',
                    borderRadius: '8px',
                    padding: '1.25rem',
                    boxShadow: '0 1px 3px rgba(0,0,0,0.05)',
                  }}
                  data-testid="unified-global-score-card"
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '1rem', marginBottom: '0.75rem' }}>
                    <div>
                      <h3 style={{ margin: 0, fontSize: '1.1rem', color: '#0f172a' }}>Puntuación Global Consolidada</h3>
                      <span style={{ fontSize: '0.82rem', color: '#64748b' }}>
                        {unifiedSummary.global_score.criteria_version_number
                          ? `Calculada bajo política de criterios v${unifiedSummary.global_score.criteria_version_number} (activa)`
                          : 'Sin versión activa de criterios configurada'}
                      </span>
                    </div>
                    <div style={{ textAlign: 'right' }}>
                      {unifiedSummary.global_score.is_calculated && typeof unifiedSummary.global_score.score === 'number' ? (
                        <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.25rem' }}>
                          <span
                            data-testid="global-score-value"
                            style={{ fontSize: '2rem', fontWeight: 800, color: '#1e3a8a', lineHeight: 1 }}
                          >
                            {unifiedSummary.global_score.score.toFixed(2)}
                          </span>
                          <span style={{ fontSize: '1rem', color: '#64748b', fontWeight: 600 }}>/ 100 pts</span>
                        </div>
                      ) : (
                        <span
                          style={{
                            background: '#f1f5f9',
                            color: '#64748b',
                            padding: '0.35rem 0.75rem',
                            borderRadius: '6px',
                            fontWeight: 600,
                            fontSize: '0.85rem',
                          }}
                        >
                          Puntaje no disponible
                        </span>
                      )}
                    </div>
                  </div>

                  {/* Fórmula matemática explícita */}
                  <div
                    style={{
                      background: '#f8fafc',
                      border: '1px solid #e2e8f0',
                      borderRadius: '6px',
                      padding: '0.75rem 1rem',
                      fontFamily: 'monospace',
                      fontSize: '0.88rem',
                      color: '#0f172a',
                      marginBottom: '0.75rem',
                      overflowX: 'auto',
                    }}
                    data-testid="global-score-formula"
                  >
                    <strong>Fórmula: </strong>
                    {unifiedSummary.global_score.formula}
                  </div>

                  <p style={{ margin: 0, fontSize: '0.85rem', color: '#475569' }}>
                    {unifiedSummary.global_score.explanation}
                  </p>

                  {/* Detalle de pesos y puntuaciones de entrada si está calculada */}
                  {unifiedSummary.global_score.is_calculated && Object.keys(unifiedSummary.global_score.weights).length > 0 && (
                    <div style={{ marginTop: '0.75rem', display: 'flex', gap: '1rem', flexWrap: 'wrap', borderTop: '1px solid #f1f5f9', paddingTop: '0.75rem' }}>
                      {Object.entries(unifiedSummary.global_score.weights).map(([code, weight]) => {
                        const inputScore = unifiedSummary.global_score.input_scores[code]
                        return (
                          <div
                            key={code}
                            style={{
                              background: '#f8fafc',
                              border: '1px solid #e2e8f0',
                              borderRadius: '6px',
                              padding: '0.4rem 0.75rem',
                              fontSize: '0.8rem',
                            }}
                          >
                            <span style={{ fontWeight: 700, color: '#334155' }}>{code}: </span>
                            <span style={{ color: '#0369a1' }}>
                              {inputScore !== undefined ? `${inputScore.toFixed(1)} pts` : 'Sin nota'}
                            </span>{' '}
                            <span style={{ color: '#64748b' }}>({weight}% peso)</span>
                          </div>
                        )
                      })}
                    </div>
                  )}
                </div>

                {/* 3. Rejilla de las Cinco Dimensiones (CA-HU1.12-01) */}
                <div>
                  <h3 style={{ fontSize: '1rem', color: '#1e293b', marginBottom: '0.75rem', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                    Desglose por Dimensión de Evaluación
                  </h3>
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))',
                      gap: '1rem',
                    }}
                    data-testid="dimensions-grid"
                  >
                    {unifiedSummary.dimensions.map((dim) => {
                      const isComplete = dim.status === 'completed'
                      const isBlocked = dim.status === 'blocked'
                      const isWarning = dim.status === 'warning'
                      const isFailed = dim.status === 'failed'

                      let borderColor = '#cbd5e1'
                      let headerBg = '#f8fafc'
                      if (isComplete) {
                        borderColor = '#86efac'
                        headerBg = '#f0fdf4'
                      } else if (isBlocked || isFailed) {
                        borderColor = '#fca5a5'
                        headerBg = '#fef2f2'
                      } else if (isWarning) {
                        borderColor = '#fde68a'
                        headerBg = '#fffbeb'
                      }

                      return (
                        <div
                          key={dim.dimension_code}
                          data-testid={`dimension-card-${dim.dimension_code}`}
                          style={{
                            background: '#ffffff',
                            border: `1px solid ${borderColor}`,
                            borderRadius: '8px',
                            display: 'flex',
                            flexDirection: 'column',
                            overflow: 'hidden',
                          }}
                        >
                          {/* Cabecera de tarjeta */}
                          <div
                            style={{
                              background: headerBg,
                              padding: '0.6rem 0.85rem',
                              borderBottom: `1px solid ${borderColor}`,
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                            }}
                          >
                            <span style={{ fontWeight: 700, fontSize: '0.9rem', color: '#0f172a' }}>
                              {dim.dimension_name}
                            </span>
                            <span
                              style={{
                                fontSize: '0.72rem',
                                fontWeight: 700,
                                padding: '0.2rem 0.5rem',
                                borderRadius: '9999px',
                                background: isComplete
                                  ? '#dcfce7'
                                  : isBlocked || isFailed
                                  ? '#fee2e2'
                                  : isWarning
                                  ? '#fef3c7'
                                  : '#f1f5f9',
                                color: isComplete
                                  ? '#166534'
                                  : isBlocked || isFailed
                                  ? '#991b1b'
                                  : isWarning
                                  ? '#92400e'
                                  : '#475569',
                              }}
                            >
                              {dim.status_label}
                            </span>
                          </div>

                          {/* Cuerpo */}
                          <div style={{ padding: '0.85rem', flex: 1, display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                            {/* Puntuación si cuantitativa */}
                            {dim.score !== null && dim.score !== undefined ? (
                              <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.35rem' }}>
                                <span style={{ fontSize: '1.25rem', fontWeight: 800, color: '#1e3a8a' }}>
                                  {dim.score.toFixed(1)}
                                </span>
                                <span style={{ fontSize: '0.8rem', color: '#64748b' }}>/ 100</span>
                              </div>
                            ) : dim.dimension_code === 'legal' ? (
                              <div style={{ fontSize: '0.8rem', color: '#475569', fontStyle: 'italic' }}>
                                Dimensión de control y cotejo normativo
                              </div>
                            ) : null}

                            {/* Conclusión */}
                            <div style={{ fontSize: '0.85rem', color: '#1e293b' }}>
                              <strong>Conclusión: </strong>
                              {dim.conclusion}
                            </div>

                            {/* Justificación */}
                            <div style={{ fontSize: '0.82rem', color: '#475569', flex: 1 }}>
                              <strong>Sustento: </strong>
                              {dim.justification}
                            </div>

                            {/* Alerta de bloqueo */}
                            {dim.has_blocker && dim.blocker_message && (
                              <div
                                style={{
                                  background: '#fee2e2',
                                  border: '1px solid #f87171',
                                  borderRadius: '4px',
                                  padding: '0.4rem 0.6rem',
                                  fontSize: '0.78rem',
                                  color: '#991b1b',
                                  fontWeight: 600,
                                }}
                              >
                                🚫 {dim.blocker_message}
                              </div>
                            )}

                            {/* Advertencia / Cita no verificable */}
                            {dim.warning && (
                              <div
                                style={{
                                  background: '#fffbeb',
                                  border: '1px solid #fcd34d',
                                  borderRadius: '4px',
                                  padding: '0.4rem 0.6rem',
                                  fontSize: '0.78rem',
                                  color: '#92400e',
                                }}
                              >
                                ⚠️ {dim.warning}
                              </div>
                            )}

                            {/* Distinción clara de datos simulados (HU1.8) */}
                            {dim.is_simulated && (
                              <div
                                style={{
                                  background: '#f1f5f9',
                                  border: '1px dashed #94a3b8',
                                  borderRadius: '4px',
                                  padding: '0.3rem 0.5rem',
                                  fontSize: '0.72rem',
                                  color: '#475569',
                                  fontWeight: 600,
                                }}
                              >
                                ℹ️ Datos técnicos o territoriales simulados
                              </div>
                            )}

                            {/* Metadatos adicionales */}
                            {dim.evaluated_at && (
                              <div style={{ fontSize: '0.72rem', color: '#94a3b8', marginTop: 'auto', paddingTop: '0.25rem' }}>
                                Evaluado: {formatDate(dim.evaluated_at)}
                              </div>
                            )}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>

                {/* 4. Alertas de dimensiones incompletas o bloqueos (CA-HU1.12-03) */}
                {unifiedSummary.missing_dimensions.length > 0 && (
                  <div
                    style={{
                      background: '#fffbeb',
                      border: '1px solid #f59e0b',
                      borderRadius: '8px',
                      padding: '0.85rem 1rem',
                      color: '#92400e',
                      fontSize: '0.88rem',
                    }}
                    data-testid="missing-dimensions-banner"
                  >
                    <strong>Evaluación multidimensional incompleta (CA-HU1.12-03): </strong>
                    Faltan por completar o subsanar las siguientes dimensiones:{' '}
                    <strong>{unifiedSummary.missing_dimensions.join(', ')}</strong>. No se permite emitir un dictamen final de aprobación sobre un expediente incompleto.
                  </div>
                )}

                {unifiedSummary.blocking_reasons.length > 0 && (
                  <div
                    style={{
                      background: '#fef2f2',
                      border: '1px solid #ef4444',
                      borderRadius: '8px',
                      padding: '0.85rem 1rem',
                      color: '#991b1b',
                      fontSize: '0.88rem',
                    }}
                    data-testid="blocking-reasons-banner"
                  >
                    <strong>Bloqueos normativos y de riesgo activos:</strong>
                    <ul style={{ margin: '0.4rem 0 0 1.25rem', padding: 0 }}>
                      {unifiedSummary.blocking_reasons.map((r, i) => (
                        <li key={i}>{r}</li>
                      ))}
                    </ul>
                  </div>
                )}

                {/* 5. Estado de habilitación para dictamen */}
                <div
                  style={{
                    background: unifiedSummary.allows_final_approval ? '#f0fdf4' : '#f8fafc',
                    border: `1px solid ${unifiedSummary.allows_final_approval ? '#86efac' : '#cbd5e1'}`,
                    borderRadius: '8px',
                    padding: '0.85rem 1rem',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    flexWrap: 'wrap',
                    gap: '0.75rem',
                  }}
                  data-testid="approval-readiness-banner"
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <span style={{ fontSize: '1.25rem' }}>
                      {unifiedSummary.allows_final_approval ? '✅' : '🔒'}
                    </span>
                    <div>
                      <strong style={{ color: unifiedSummary.allows_final_approval ? '#15803d' : '#475569' }}>
                        {unifiedSummary.allows_final_approval
                          ? 'Expediente habilitado para dictamen formal de aprobación'
                          : 'Aprobación definitiva y priorización deshabilitadas'}
                      </strong>
                      <div style={{ fontSize: '0.8rem', color: '#64748b' }}>
                        {unifiedSummary.allows_final_approval
                          ? 'Las cinco dimensiones están completadas y validadas sin alertas ni checkpoints pendientes.'
                          : 'Se requiere completar todas las dimensiones y resolver cualquier alerta o punto de control.'}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )}
          </section>

          <section className="flow-panel">
            <h2>5. Normativa y dictamen final</h2>
            {isPaused && (
              <div className="checkpoint-blocked-alert" style={{ marginBottom: '1rem' }}>
                <strong>Aprobación final bloqueada:</strong> El expediente se encuentra en estado <strong>Pendiente de revisión</strong> por punto de control persistente ante riesgo alto (HU3.1). Conforme a las reglas de control, no se permite emitir un dictamen final de aprobación hasta que la revisión humana sea resuelta.
              </div>
            )}
            {approval?.has_legal_alert && (
              <div className="legal-alert-blocked-alert" style={{ background: '#fef2f2', border: '1px solid #ef4444', borderRadius: '8px', padding: '1rem', marginBottom: '1rem', color: '#991b1b' }}>
                <strong>Aprobación final y priorización bloqueadas por Alerta Legal activa (HU2.3):</strong> El expediente presenta una incompatibilidad territorial o afectación a zona intangible ({approval.legal_alert_motive} [{approval.legal_alert_norm}]). Conforme a las reglas de control, no se permite emitir un dictamen de aprobación definitiva ni priorizar la obra hasta su levantamiento por Asesoría Jurídica.
              </div>
            )}
            <ul className="flow-links">
              {hasRole('LEGAL_ADVISOR') && (
                <li>
                  <Link to="/normativa">Consultar normativa aplicable</Link>
                </li>
              )}
              {hasRole('ADMIN') &&
                !isPaused &&
                !approval?.has_legal_alert &&
                unifiedSummary?.allows_final_approval && (
                <li>
                  <Link to={`/proyectos/${projectId}/aprobacion`}>
                    Emitir dictamen de aprobación
                  </Link>
                </li>
              )}
              {hasRole('ADMIN') &&
                !isPaused &&
                !approval?.has_legal_alert &&
                unifiedSummary &&
                !unifiedSummary.allows_final_approval && (
                  <li className="muted">
                    Emisión de aprobación deshabilitada: complete o subsane todas las dimensiones del resumen unificado.
                  </li>
                )}
              {hasRole('ADMIN') && !isPaused && approval?.has_legal_alert && (
                <li className="muted" style={{ color: '#b91c1c', fontWeight: 600 }}>
                  Emisión de dictamen y priorización deshabilitadas: expediente bloqueado por Alerta Legal activa (HU2.3).
                </li>
              )}
              {hasRole('ADMIN') && isPaused && (
                <li className="muted">
                  Emisión de dictamen deshabilitada: expediente detenido en punto de control por riesgo alto.
                </li>
              )}
              {!hasRole('LEGAL_ADVISOR') && !hasRole('ADMIN') && (
                <li className="muted">
                  La consulta normativa y el dictamen final las realiza personal autorizado.
                </li>
              )}
            </ul>
            {approval && <TechnicalDetails data={approval} />}
          </section>
        </>
      )}
    </div>
  )
}
