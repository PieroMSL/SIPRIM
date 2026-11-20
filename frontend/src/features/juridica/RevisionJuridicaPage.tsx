import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import {
  evaluateLegalAssessment,
  getLegalAssessment,
  getProcurementVerification,
  getProject,
  getZoningPrecheck,
  requestProcurementVerification,
  resolveLegalAlert,
  reviewProcurementObservation,
} from '../../api/pmv1.api'
import { useAuth } from '../auth/AuthContext'
import { newIdempotencyKey } from '../../utils/idempotency'
import { ProyectoCard } from '../../components/proyecto/ProyectoCard'
import { Alert } from '../../components/ui/Alert'
import { PageHeader } from '../../components/ui/PageHeader'
import { TechnicalDetails } from '../../components/ui/TechnicalDetails'
import type {
  LegalAssessmentResponse,
  PMV1ProjectResponse,
  ProcurementVerificationResponse,
  ZoningPrecheckResponse,
} from '../../types/pmv1'
import { getPhysicalExpediente } from '../../utils/physicalExpedienteStorage'
import { mapPmV1ToProyecto } from '../../utils/pmv1Mapper'

const LEGAL_MISSING_LABELS: Record<string, string> = {
  title: 'Nombre del proyecto',
  description: 'Descripción',
  location: 'Ubicación territorial',
  proposed_land_use: 'Uso de suelo propuesto',
  estimated_budget_pen: 'Presupuesto estimado (S/)',
  beneficiaries_count: 'Beneficiarios estimados',
}

function getMissingFieldsForLegal(p: PMV1ProjectResponse | null): string[] {
  if (!p) return []
  const missing: string[] = []
  if (!p.title || !p.title.trim()) missing.push(LEGAL_MISSING_LABELS.title)
  if (!p.description || !p.description.trim()) missing.push(LEGAL_MISSING_LABELS.description)
  if (!p.location || !p.location.trim()) missing.push(LEGAL_MISSING_LABELS.location)
  if (!p.proposed_land_use || !p.proposed_land_use.trim()) missing.push(LEGAL_MISSING_LABELS.proposed_land_use)
  if (p.estimated_budget_pen === null || p.estimated_budget_pen === undefined || Number(p.estimated_budget_pen) <= 0) {
    missing.push(LEGAL_MISSING_LABELS.estimated_budget_pen)
  }
  if (p.beneficiaries_count === null || p.beneficiaries_count === undefined || Number(p.beneficiaries_count) <= 0) {
    missing.push(LEGAL_MISSING_LABELS.beneficiaries_count)
  }
  return missing
}

export function RevisionJuridicaPage() {
  const { projectId = '' } = useParams()
  const { hasRole } = useAuth()
  const [project, setProject] = useState<PMV1ProjectResponse | null>(null)
  const [assessment, setAssessment] = useState<LegalAssessmentResponse | null>(null)
  const [zoningPrecheckState, setZoningPrecheckState] = useState<ZoningPrecheckResponse | null>(null)
  const [procVerification, setProcVerification] = useState<ProcurementVerificationResponse | null>(null)
  const [procReviewStatus, setProcReviewStatus] = useState<'validated_compatible' | 'confirmed_incompatibility' | 'subsanacion_requerida'>('subsanacion_requerida')
  const [procReviewNotes, setProcReviewNotes] = useState('')
  const [procReviewSubmitting, setProcReviewSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [resolving, setResolving] = useState(false)
  const [resolveAction, setResolveAction] = useState('subsanado_en_expediente')
  const [resolveJustification, setResolveJustification] = useState('')
  const [evidenceReference, setEvidenceReference] = useState('')

  useEffect(() => {
    if (!projectId) return
    getProject(projectId)
      .then(setProject)
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : 'No se pudo cargar el expediente.')
      })

    getLegalAssessment(projectId)
      .then(setAssessment)
      .catch(() => {
        // No evaluado previamente
      })

    getZoningPrecheck(projectId)
      .then(setZoningPrecheckState)
      .catch(() => {
        // Sin precheck de zonificación registrado
      })

    getProcurementVerification(projectId)
      .then((res) => {
        if (res) setProcVerification(res)
      })
      .catch(() => {
        // Sin verificación de contrataciones previa
      })
  }, [projectId])

  const runReview = async () => {
    if (!projectId) return
    setLoading(true)
    setError(null)
    try {
      const res = await evaluateLegalAssessment(projectId)
      setAssessment(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudo ejecutar la revisión jurídica.')
    } finally {
      setLoading(false)
    }
  }

  const physicalExpediente = projectId ? getPhysicalExpediente(projectId) : null
  const missingLegalFields = getMissingFieldsForLegal(project)
  const isProjectCompleteForLegal = Boolean(project && missingLegalFields.length === 0)

  return (
    <div className="form-page pmv1-flow">
      <PageHeader
        title="Detalle de revisión jurídica"
        description="Evaluación de conformidad normativa y citas exactas para el Asesor Jurídico (PMV 2 · HU2.2 / HU1.10)."
      />

      {error && <Alert variant="error">{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}
      {!project && !error && <p className="muted">Cargando expediente…</p>}
      {project && <ProyectoCard proyecto={mapPmV1ToProyecto(project)} compact />}

      {zoningPrecheckState && (
        <section
          className="flow-panel"
          data-testid="legal-zoning-observation"
          style={{
            borderLeft: `4px solid ${
              zoningPrecheckState.compatible === false
                ? '#dc2626'
                : zoningPrecheckState.compatible === true
                  ? '#16a34a'
                  : '#d97706'
            }`,
            background:
              zoningPrecheckState.compatible === false ? '#fef2f2' : '#f8fafc',
          }}
        >
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
            <div>
              <span className="story-label">PMV 1 · HU1.11 · Zonificación PDU/PDM</span>
              <h3 style={{ margin: '0.2rem 0 0' }}>
                {zoningPrecheckState.status_label ?? 'Observación territorial de zonificación'}
              </h3>
            </div>
            <span
              className={`status-pill ${
                zoningPrecheckState.compatible === false
                  ? 'status-error'
                  : zoningPrecheckState.compatible === true
                    ? 'status-approved'
                    : 'status-review'
              }`}
            >
              {zoningPrecheckState.compatible === false
                ? 'Posible incompatibilidad (Revisión jurídica)'
                : zoningPrecheckState.compatible === true
                  ? 'Compatible preliminar'
                  : 'Información insuficiente / Requiere revisión'}
            </span>
          </div>
          {zoningPrecheckState.is_synthetic_data && (
            <p
              data-testid="legal-zoning-synthetic-banner"
              style={{
                margin: '0.5rem 0 0',
                padding: '0.5rem 0.75rem',
                background: '#ffedd5',
                border: '1px solid #ea580c',
                borderRadius: '6px',
                color: '#9a3412',
                fontSize: '0.82rem',
                fontWeight: 700,
              }}
            >
              DATOS SINTÉTICOS DE PRUEBA TÉCNICA (NO OFICIAL): esta alerta espacial proviene de un ensayo con polígono ficticio. No certifica zonificación ni infracción territorial real.
            </p>
          )}
          {zoningPrecheckState.preliminary_conclusion && (
            <p style={{ margin: '0.5rem 0 0', fontSize: '0.9rem' }}>
              {zoningPrecheckState.preliminary_conclusion}
            </p>
          )}
          <p style={{ margin: '0.4rem 0 0', fontSize: '0.84rem', color: '#475569' }}>
            <strong>Limitaciones / Criterio humano:</strong> {zoningPrecheckState.limitations}
          </p>
          {zoningPrecheckState.environmental_risk_notice && (
            <p
              data-testid="legal-zoning-risk-notice"
              style={{ margin: '0.45rem 0 0', fontSize: '0.83rem', color: '#9a3412', background: '#fff7ed', padding: '0.45rem 0.65rem', borderRadius: '6px' }}
            >
              <strong>Aviso de riesgo territorial:</strong> {zoningPrecheckState.environmental_risk_notice}
            </p>
          )}
          {zoningPrecheckState.evidence && zoningPrecheckState.evidence.length > 0 && (
            <div style={{ marginTop: '0.55rem', fontSize: '0.82rem', color: '#334155' }}>
              <strong>Sustento principal:</strong> {zoningPrecheckState.evidence[0].document} (v.{' '}
              {zoningPrecheckState.evidence[0].version}, {zoningPrecheckState.evidence[0].locator}) —{' '}
              {zoningPrecheckState.evidence[0].excerpt}
              {zoningPrecheckState.evidence[0].official_url && (
                <>
                  {' '}
                  ·{' '}
                  <a href={zoningPrecheckState.evidence[0].official_url} target="_blank" rel="noreferrer">
                    {zoningPrecheckState.evidence[0].official_url}
                  </a>
                </>
              )}
            </div>
          )}
          {zoningPrecheckState.normative_amendments && zoningPrecheckState.normative_amendments.length > 0 && (
            <div data-testid="legal-zoning-amendments" style={{ marginTop: '0.55rem', fontSize: '0.8rem', color: '#334155' }}>
              <strong>Ordenanzas PDM verificadas por ámbito/predio:</strong>{' '}
              {zoningPrecheckState.normative_amendments
                .filter(
                  (a) =>
                    a.applicability_status === 'base_vigente' ||
                    a.applicability_status === 'posible_coincidencia_pendiente_catastro' ||
                    a.ordinance.includes('836') ||
                    a.ordinance.includes('838') ||
                    a.ordinance.includes('839'),
                )
                .map((a, idx) => (
                  <span key={a.ordinance}>
                    {idx > 0 ? ' · ' : ''}
                    <a href={a.official_url} target="_blank" rel="noreferrer">
                      {a.ordinance} ({a.publication_date})
                    </a>
                  </span>
                ))}
            </div>
          )}
        </section>
      )}

      {(procVerification || assessment?.procurement_verification) && (() => {
        const pv = procVerification ?? assessment?.procurement_verification ?? null
        if (!pv) return null
        return (
          <section
            className="flow-panel"
            data-testid="legal-procurement-observation"
            style={{
              borderLeft: `4px solid ${
                pv.status === 'possible_incompatibility'
                  ? '#dc2626'
                  : pv.status === 'verified_preliminary'
                    ? '#16a34a'
                    : '#d97706'
              }`,
              background: pv.status === 'possible_incompatibility' ? '#fef2f2' : '#f8fafc',
              display: 'grid',
              gap: '0.65rem',
            }}
          >
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
              <div>
                <span className="story-label">PMV 1 · HU1.10 · Ley N.° 32069 y D.S. N.° 009-2025-EF</span>
                <h3 style={{ margin: '0.2rem 0 0' }}>
                  {pv.status_label} (Versión v{pv.version_number})
                </h3>
              </div>
              <span
                className={`status-pill ${
                  pv.status === 'possible_incompatibility'
                    ? 'status-error'
                    : pv.status === 'verified_preliminary'
                      ? 'status-approved'
                      : 'status-review'
                }`}
              >
                {pv.status === 'possible_incompatibility'
                  ? 'Posible incompatibilidad (Revisión jurídica humana)'
                  : pv.status === 'verified_preliminary'
                    ? 'Preliminar compatible'
                    : 'Requiere revisión'}
              </span>
            </div>

            <p style={{ margin: 0, fontSize: '0.9rem', lineHeight: 1.45 }}>{pv.summary}</p>

            {pv.affected_requirement && (
              <p style={{ margin: 0, fontSize: '0.85rem', color: '#991b1b' }}>
                <strong>Requisito contractual observado:</strong> {pv.affected_requirement}
              </p>
            )}

            {pv.missing_fields.length > 0 && (
              <p style={{ margin: 0, fontSize: '0.84rem', color: '#78350f' }}>
                <strong>Antecedentes o sustentos faltantes:</strong> {pv.missing_fields.join('; ')}.
              </p>
            )}

            <div style={{ display: 'grid', gap: '0.4rem' }}>
              <strong style={{ fontSize: '0.83rem', color: '#0f172a' }}>
                Requisitos examinados y fuentes oficiales OECE/MEF (consulta {pv.consulted_at}):
              </strong>
              {pv.examined_requirements.map((req) => (
                <div
                  key={req.requirement_code}
                  style={{
                    background: '#ffffff',
                    border: '1px solid #cbd5e1',
                    borderRadius: '6px',
                    padding: '0.55rem 0.7rem',
                    fontSize: '0.81rem',
                  }}
                >
                  <div>
                    <strong>{req.requirement_name}</strong> — <em>{req.status_label}</em>
                  </div>
                  <div style={{ color: '#334155', marginTop: '0.15rem' }}>{req.comparison_detail}</div>
                  {req.norm_name && (
                    <div style={{ color: '#475569', fontSize: '0.77rem', marginTop: '0.15rem' }}>
                      {req.norm_name} ({req.norm_version}) · {req.article_or_section}
                      {req.official_url && (
                        <>
                          {' '}·{' '}
                          <a href={req.official_url} target="_blank" rel="noreferrer">
                            {req.official_url}
                          </a>
                        </>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>

            {pv.advisory_review_notes && (
              <div
                data-testid="procurement-advisor-decision-saved"
                style={{
                  background: '#ecfdf5',
                  border: '1px solid #86efac',
                  borderRadius: '6px',
                  padding: '0.55rem 0.75rem',
                  fontSize: '0.83rem',
                  color: '#166534',
                }}
              >
                <strong>Validación de Asesoría Jurídica ({pv.advisory_review_status}):</strong>{' '}
                {pv.advisory_review_notes}
              </div>
            )}

            {hasRole('LEGAL_ADVISOR') && (
              <form
                data-testid="procurement-advisor-review-form"
                onSubmit={async (e) => {
                  e.preventDefault()
                  if (!projectId) return
                  setProcReviewSubmitting(true)
                  setError(null)
                  try {
                    const updated = await reviewProcurementObservation(projectId, {
                      advisory_review_status: procReviewStatus,
                      advisory_review_notes: procReviewNotes,
                    })
                    setProcVerification(updated)
                    setSuccess('Validación jurídica contractual (HU1.10) registrada correctamente.')
                  } catch (err) {
                    setError(err instanceof Error ? err.message : 'No se pudo registrar la validación contractual.')
                  } finally {
                    setProcReviewSubmitting(false)
                  }
                }}
                style={{
                  background: '#ffffff',
                  border: '1px solid #cbd5e1',
                  borderRadius: '6px',
                  padding: '0.75rem',
                  display: 'grid',
                  gap: '0.5rem',
                }}
              >
                <strong style={{ fontSize: '0.84rem', color: '#0f172a' }}>
                  Validación humana del Asesor Jurídico (HU1.10)
                </strong>
                <select
                  data-testid="procurement-advisor-status-select"
                  className="form-field-input"
                  value={procReviewStatus}
                  onChange={(e) =>
                    setProcReviewStatus(
                      e.target.value as 'validated_compatible' | 'confirmed_incompatibility' | 'subsanacion_requerida',
                    )
                  }
                >
                  <option value="subsanacion_requerida">Requiere subsanación de expediente / ajuste DEC</option>
                  <option value="confirmed_incompatibility">Incompatibilidad confirmada por Asesoría Jurídica</option>
                  <option value="validated_compatible">Subsanado / Compatible tras revisión humana</option>
                </select>
                <textarea
                  data-testid="procurement-advisor-notes-input"
                  rows={2}
                  className="form-field-input"
                  placeholder="Fundamentación jurídica del Asesor Legal sobre el requisito contractual observado..."
                  value={procReviewNotes}
                  onChange={(e) => setProcReviewNotes(e.target.value)}
                  required
                />
                <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                  <button
                    type="submit"
                    className="btn btn-primary"
                    data-testid="procurement-advisor-submit-btn"
                    disabled={procReviewSubmitting || procReviewNotes.trim().length < 5}
                  >
                    {procReviewSubmitting ? 'Registrando…' : 'Registrar validación contractual'}
                  </button>
                  <button
                    type="button"
                    className="btn btn-secondary"
                    disabled={loading}
                    onClick={async () => {
                      if (!projectId) return
                      const refreshed = await requestProcurementVerification(projectId, newIdempotencyKey('proc-adv'))
                      setProcVerification(refreshed)
                    }}
                  >
                    Recalcular verificación Ley 32069
                  </button>
                </div>
              </form>
            )}
          </section>
        )
      })()}

      <section className="flow-panel legal-flow-panel">
        <div className="flow-panel-heading">
          <div>
            <span className="story-label">PMV 2 · HU2.2 / HU1.10</span>
            <h2>Conformidad legal y citas normativas exactas</h2>
          </div>
          <span className="review-badge">Revisión Asesoría Jurídica</span>
        </div>
        <p className="flow-hint">
          Contrasta los antecedentes del expediente con el corpus normativo versionado y cita el artículo o sección exacta aplicable para su cotejo por el Asesor Jurídico, advirtiendo citas no verificables sin fabricar artículos.
        </p>

        {project && !isProjectCompleteForLegal && (
          <div data-testid="legal-incomplete-alert" style={{ marginBottom: '0.85rem' }}>
            <Alert variant="error">
              <strong>Evaluación jurídica bloqueada (expediente incompleto):</strong> complete en la ficha del proyecto los campos faltantes: {missingLegalFields.join(', ')}. La consulta normativa en solo lectura sigue disponible.
            </Alert>
          </div>
        )}

        <button
          type="button"
          className="btn btn-primary"
          data-testid="evaluate-legal-btn"
          disabled={loading || !project || !isProjectCompleteForLegal}
          onClick={() => void runReview()}
        >
          {loading
            ? 'Analizando normativa…'
            : !isProjectCompleteForLegal && project
              ? 'Evaluación jurídica bloqueada (datos incompletos)'
              : assessment
                ? 'Actualizar revisión jurídica'
                : 'Ejecutar revisión jurídica'}
        </button>

        {assessment && (
          <article className="legal-result" aria-live="polite">
            <header className="legal-result-header">
              <div>
                <span className="result-eyebrow">Informe Jurídico Preliminar · HU2.2</span>
                <h3>{assessment.status_label}</h3>
              </div>
              <span
                className={`status-pill ${
                  assessment.has_legal_alert || assessment.status === 'alerta_legal'
                    ? 'status-error'
                    : assessment.status === 'verified'
                    ? 'status-approved'
                    : 'status-review'
                }`}
                style={assessment.has_legal_alert || assessment.status === 'alerta_legal' ? { background: '#fee2e2', color: '#991b1b', borderColor: '#ef4444' } : undefined}
              >
                {assessment.has_legal_alert || assessment.status === 'alerta_legal'
                  ? 'Alerta Legal'
                  : assessment.requires_human_review
                  ? 'Requiere revisión'
                  : 'Cotejo conforme'}
              </span>
            </header>

            <p className="legal-conclusion">{assessment.summary}</p>

            {assessment.has_legal_alert && (
              <div
                className="legal-alert-banner"
                role="alert"
                style={{
                  background: '#fef2f2',
                  border: '1px solid #ef4444',
                  borderRadius: '8px',
                  padding: '1rem',
                  margin: '1rem 0',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.5rem' }}>
                  <span style={{ fontSize: '1.25rem' }}>🚨</span>
                  <strong style={{ color: '#991b1b', fontSize: '1.05rem' }}>
                    Alerta Legal Activa (HU2.3): Incompatibilidad Territorial / Intangibilidad
                  </strong>
                </div>
                <p style={{ margin: '0.35rem 0', color: '#7f1d1d', fontSize: '0.92rem' }}>
                  <strong>Motivo de la alerta:</strong> {assessment.legal_alert_motive}
                </p>
                <p style={{ margin: '0.35rem 0', color: '#7f1d1d', fontSize: '0.92rem' }}>
                  <strong>Sustento normativo:</strong> {assessment.legal_alert_norm}
                </p>
                <div
                  style={{
                    marginTop: '0.75rem',
                    padding: '0.5rem 0.75rem',
                    background: '#fee2e2',
                    borderRadius: '6px',
                    fontSize: '0.85rem',
                    color: '#991b1b',
                    fontWeight: 600,
                  }}
                >
                  ⛔ Bloqueo de avance: El expediente no puede avanzar a priorización ni recibir aprobación final hasta que la alerta sea resuelta o levantada formalmente por personal autorizado de Asesoría Jurídica.
                </div>

                {hasRole('LEGAL_ADVISOR') && (
                  <form
                    onSubmit={async (e) => {
                      e.preventDefault()
                      if (!projectId) return
                      setResolving(true)
                      setError(null)
                      try {
                        await resolveLegalAlert(
                          projectId,
                          {
                            resolution_action: resolveAction.trim(),
                            justification: resolveJustification.trim(),
                            evidence_reference: evidenceReference.trim() || undefined,
                          },
                          newIdempotencyKey('resolve-alert'),
                        )
                        setSuccess('Alerta legal levantada formalmente por Asesoría Jurídica.')
                        const updated = await getLegalAssessment(projectId)
                        setAssessment(updated)
                      } catch (err) {
                        setError(err instanceof Error ? err.message : 'No se pudo levantar la alerta.')
                      } finally {
                        setResolving(false)
                      }
                    }}
                    style={{
                      marginTop: '1rem',
                      padding: '0.75rem',
                      background: '#ffffff',
                      borderRadius: '6px',
                      border: '1px solid #fca5a5',
                    }}
                  >
                    <h5 style={{ margin: '0 0 0.5rem', color: '#991b1b' }}>
                      Levantamiento de Alerta Legal (Asesoría Jurídica)
                    </h5>
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                      <label style={{ fontSize: '0.8rem', fontWeight: 600, color: '#374151' }}>
                        Tipo de decisión formal (HU2.3):
                      </label>
                      <select
                        value={resolveAction}
                        onChange={(e) => setResolveAction(e.target.value)}
                        required
                        className="form-field-input"
                        style={{ fontSize: '0.85rem', padding: '0.4rem' }}
                      >
                        <option value="subsanado_en_expediente">Subsanado en expediente / Replanteo técnico</option>
                        <option value="falso_positivo">Falso positivo / Determinación no aplicable tras análisis técnico</option>
                        <option value="excepcion_fundada">Excepción legal fundada / Régimen normativo especial</option>
                        <option value="riesgo_asumido">Riesgo normativo evaluado y mitigado con sustento</option>
                      </select>
                      <input
                        type="text"
                        placeholder="Referencia documental / Expediente de sustento (ej. RD-ANA-045-2026, Informe Legal 12-2026)"
                        value={evidenceReference}
                        onChange={(e) => setEvidenceReference(e.target.value)}
                        className="form-field-input"
                        style={{ fontSize: '0.85rem', padding: '0.4rem' }}
                      />
                      <textarea
                        rows={2}
                        placeholder="Justificación técnico-legal para el levantamiento de la alerta..."
                        value={resolveJustification}
                        onChange={(e) => setResolveJustification(e.target.value)}
                        required
                        className="form-field-input"
                        style={{ fontSize: '0.85rem', padding: '0.4rem' }}
                      />
                      <button
                        type="submit"
                        className="btn btn-primary"
                        disabled={resolving || !resolveAction.trim() || resolveJustification.trim().length < 5}
                        style={{ alignSelf: 'flex-start' }}
                      >
                        {resolving ? 'Registrando levantamiento…' : 'Levantar alerta legal'}
                      </button>
                    </div>
                  </form>
                )}
              </div>
            )}
            {assessment.is_resolved && (
              <div
                style={{
                  background: '#f0fdf4',
                  border: '1px solid #86efac',
                  borderRadius: '8px',
                  padding: '0.75rem',
                  margin: '1rem 0',
                  color: '#166534',
                }}
              >
                ✅ <strong>Alerta legal subsanada / levantada:</strong> {assessment.resolution_justification}
              </div>
            )}

            <dl className="legal-checks">
              <div>
                <dt>Antecedentes contractuales</dt>
                <dd>
                  {physicalExpediente
                    ? `${physicalExpediente.fileName} adjunto; contenido para cotejo.`
                    : 'No se adjuntaron antecedentes contractuales físicos o digitalizados.'}
                </dd>
              </div>
              <div>
                <dt>Citas comprobables (CA-HU2.2-01)</dt>
                <dd>
                  {assessment.verifiable_citations_count} cita(s) con artículo exacto y versión.
                </dd>
              </div>
              <div>
                <dt>Citas no verificables (CA-HU2.2-02)</dt>
                <dd>
                  {assessment.unverifiable_citations_count > 0
                    ? `${assessment.unverifiable_citations_count} hallazgo(s) sin cita verificable.`
                    : 'Ninguna (todas comprobables).'}
                </dd>
              </div>
            </dl>

            <section className="legal-findings-section" aria-labelledby="legal-findings-title">
              <h4 id="legal-findings-title" style={{ margin: '1rem 0 0.5rem', color: '#0f172a' }}>
                Hallazgos jurídicos y citas normativas vinculadas
              </h4>

              {assessment.findings.map((finding) => (
                <div
                  key={finding.finding_id}
                  className={`legal-finding-card ${
                    finding.citation.is_verifiable ? 'is-verifiable' : 'is-unverifiable'
                  }`}
                >
                  <div className="legal-finding-header">
                    <div>
                      <h4 className="legal-finding-title">{finding.title}</h4>
                      <p className="legal-finding-desc">{finding.description}</p>
                    </div>
                    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '0.35rem' }}>
                      <span
                        className={`social-badge-status ${
                          finding.citation.is_verifiable ? 'sufficient' : 'insufficient'
                        }`}
                      >
                        {finding.citation.status_label}
                      </span>
                      {finding.requires_human_review ? (
                        <span className="green-measures-pill pill-warn">
                          ⚠️ Revisión humana obligatoria
                        </span>
                      ) : (
                        <span className="green-measures-pill pill-ok">
                          Cotejo conforme
                        </span>
                      )}
                    </div>
                  </div>

                  {finding.citation.is_verifiable ? (
                    <div className="legal-citation-box">
                      <div className="social-score-tag" style={{ marginBottom: '0.35rem' }}>
                        CA-HU2.2-01 · Cita exacta y comprobable
                      </div>
                      <div style={{ fontSize: '0.86rem', color: '#0f172a', marginBottom: '0.35rem' }}>
                        <strong>Denominación de la norma:</strong> {finding.citation.document_name}
                      </div>
                      <div style={{ fontSize: '0.84rem', color: '#475569', marginBottom: '0.35rem' }}>
                        <strong>Versión de la norma:</strong> {finding.citation.version}
                      </div>
                      <div>
                        <strong>Ubicación precisa:</strong>{' '}
                        <span className="citation-article-badge">
                          {finding.citation.article_or_section}
                        </span>
                      </div>
                      <div style={{ marginTop: '0.5rem' }}>
                        <strong style={{ fontSize: '0.8rem', color: '#475569' }}>
                          Fragmento normativo para cotejo:
                        </strong>
                        <blockquote className="citation-fragment-quote">
                          “{finding.citation.fragment_content}”
                        </blockquote>
                      </div>

                      <div className="citation-meta">
                        <div>
                          <strong>Fuente:</strong> {finding.citation.data_origin}
                          {finding.citation.is_simulated && (
                            <span className="social-simulated-pill">Simulado</span>
                          )}
                        </div>
                        {finding.citation.citation_hash && (
                          <div>
                            <strong>Trazabilidad SHA-256:</strong>{' '}
                            <span className="citation-hash-pill">
                              {finding.citation.citation_hash.slice(0, 16)}…
                            </span>
                          </div>
                        )}
                      </div>
                    </div>
                  ) : (
                    <div className="citation-unverifiable-callout">
                      <div style={{ fontWeight: 700, marginBottom: '0.25rem', color: '#b45309' }}>
                        ⚠️ Cita no verificable (CA-HU2.2-02)
                      </div>
                      <p style={{ margin: '0.35rem 0', lineHeight: 1.45 }}>
                        {finding.citation.limitation_note}
                      </p>
                      <div style={{ marginTop: '0.35rem', fontSize: '0.82rem' }}>
                        <strong>Ubicación de artículo:</strong> No identificable (no se fabrica ningún número de artículo)
                      </div>
                      <div style={{ marginTop: '0.25rem', fontSize: '0.82rem' }}>
                        <strong>Referencia documental:</strong>{' '}
                        {finding.citation.document_name || 'No identificada'}{' '}
                        {finding.citation.version ? `(${finding.citation.version})` : ''}
                      </div>
                      {finding.citation.fragment_content && (
                        <div style={{ marginTop: '0.4rem', fontSize: '0.8rem' }}>
                          <strong>Fragmento histórico / referencial:</strong>
                          <blockquote className="citation-fragment-quote" style={{ margin: '0.25rem 0' }}>
                            “{finding.citation.fragment_content}”
                          </blockquote>
                        </div>
                      )}
                      <div style={{ marginTop: '0.5rem', fontWeight: 600, color: '#92400e', fontSize: '0.8rem' }}>
                        Estado del hallazgo: No confirmado jurídicamente · Requiere revisión humana por el Asesor Jurídico
                      </div>
                    </div>
                  )}
                </div>
              ))}
            </section>

            <TechnicalDetails data={assessment} />
          </article>
        )}
      </section>
    </div>
  )
}
