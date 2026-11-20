import { useEffect, useMemo, useState } from 'react'
import {
  getProjectHistory,
  listAuditableProjects,
  verifyDecisionIntegrity,
  type AuditProjectSummaryItem,
  type DecisionIntegrityVerificationResponse,
  type HistoryTimelineEventItem,
  type ProjectHistoryResponse,
} from '../../api/audit.api'
import { Alert } from '../../components/ui/Alert'

const CATEGORY_LABELS: Record<string, string> = {
  version: 'Versión',
  territorial: 'Territorial',
  evaluation: 'Evaluación',
  review: 'Revisión',
  checkpoint: 'Punto de control',
  decision: 'Decisión de aprobación',
  resume: 'Reanudación',
  approval: 'Dictamen final',
  audit: 'Bitácora',
}

const CATEGORY_COLORS: Record<string, { bg: string; fg: string }> = {
  version: { bg: '#e0f2fe', fg: '#075985' },
  territorial: { bg: '#ecfdf5', fg: '#065f46' },
  evaluation: { bg: '#f3e8ff', fg: '#6b21a8' },
  review: { bg: '#fef3c7', fg: '#92400e' },
  checkpoint: { bg: '#ffedd5', fg: '#9a3412' },
  decision: { bg: '#dbeafe', fg: '#1e40af' },
  resume: { bg: '#dcfce7', fg: '#166534' },
  approval: { bg: '#d1fae5', fg: '#065f46' },
  audit: { bg: '#f1f5f9', fg: '#334155' },
}

const STATUS_LABELS: Record<string, string> = {
  draft: 'Borrador',
  in_evaluation: 'En evaluación',
  paused_high_risk: 'Pausado por riesgo alto',
  evaluated: 'Evaluado',
  approved: 'Aprobado',
  rejected: 'Rechazado',
  observed: 'Observado',
}

const CHECKPOINT_STATUS_LABELS: Record<string, string> = {
  pending_review: 'Pendiente de revisión',
  resolved: 'Resuelto / Reanudado',
  dismissed: 'Desestimado',
}

function formatDateTime(iso: string): string {
  try {
    return new Intl.DateTimeFormat('es-PE', {
      dateStyle: 'medium',
      timeStyle: 'medium',
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

function formatDateTimeWithTz(iso: string): string {
  try {
    const dt = new Date(iso)
    const formatted = new Intl.DateTimeFormat('es-PE', {
      year: 'numeric',
      month: 'short',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      timeZoneName: 'short',
    }).format(dt)
    return `${formatted} (${iso})`
  } catch {
    return iso
  }
}

function formatCurrency(value: string | number): string {
  const num = Number(value)
  if (!Number.isFinite(num)) return String(value)
  return new Intl.NumberFormat('es-PE', {
    style: 'currency',
    currency: 'PEN',
    maximumFractionDigits: 2,
  }).format(num)
}

function renderActorLabel(ev: HistoryTimelineEventItem): string {
  if (ev.actor_name && ev.actor_name.trim()) {
    return ev.actor_name
  }
  if (ev.actor_user_id) {
    return 'Cuenta municipal no localizada en el directorio activo'
  }
  return 'Registro automático del sistema (sin usuario asociado)'
}

export function AuditHistoryPage() {
  const [projects, setProjects] = useState<AuditProjectSummaryItem[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState<string>('')
  const [searchTerm, setSearchTerm] = useState<string>('')
  const [versionFilter, setVersionFilter] = useState<string>('all')
  const [categoryFilter, setCategoryFilter] = useState<string>('all')

  const [history, setHistory] = useState<ProjectHistoryResponse | null>(null)
  const [loadingProjects, setLoadingProjects] = useState<boolean>(true)
  const [loadingHistory, setLoadingHistory] = useState<boolean>(false)
  const [error, setError] = useState<string | null>(null)
  const [selectedDecisionId, setSelectedDecisionId] = useState<string | null>(null)
  const [liveVerification, setLiveVerification] =
    useState<DecisionIntegrityVerificationResponse | null>(null)
  const [verifiedByEventId, setVerifiedByEventId] = useState<
    Record<string, DecisionIntegrityVerificationResponse>
  >({})
  const [verificationErrorByEventId, setVerificationErrorByEventId] = useState<
    Record<string, string>
  >({})
  const [verifyingIntegrity, setVerifyingIntegrity] = useState<boolean>(false)
  const [verifyError, setVerifyError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listAuditableProjects()
      .then((res) => {
        if (cancelled) return
        setProjects(res.projects)
        if (res.projects.length > 0) {
          setSelectedProjectId((prev) => prev || res.projects[0].project_id)
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(
          err instanceof Error
            ? err.message
            : 'No se pudo cargar la lista de expedientes para auditoría.',
        )
      })
      .finally(() => {
        if (!cancelled) setLoadingProjects(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!selectedProjectId) return
    let cancelled = false
    Promise.resolve()
      .then(() => {
        if (cancelled) return null
        setLoadingHistory(true)
        setError(null)
        return getProjectHistory(selectedProjectId)
      })
      .then((data) => {
        if (cancelled || !data) return
        setHistory(data)
        setVersionFilter('all')
        setCategoryFilter('all')
        setSelectedDecisionId(null)
        setLiveVerification(null)
        setVerifiedByEventId({})
        setVerificationErrorByEventId({})
        setVerifyError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setHistory(null)
        setError(
          err instanceof Error
            ? err.message
            : 'No se pudo reconstruir el historial del expediente seleccionado.',
        )
      })
      .finally(() => {
        if (!cancelled) setLoadingHistory(false)
      })
    return () => {
      cancelled = true
    }
  }, [selectedProjectId])

  const filteredProjects = useMemo(() => {
    const q = searchTerm.trim().toLowerCase()
    if (!q) return projects
    return projects.filter(
      (p) =>
        p.code.toLowerCase().includes(q) ||
        p.title.toLowerCase().includes(q) ||
        p.status.toLowerCase().includes(q),
    )
  }, [projects, searchTerm])

  const visibleEvents = useMemo(() => {
    if (!history) return []
    return history.events.filter((ev) => {
      if (versionFilter !== 'all') {
        if (versionFilter === 'unassigned') {
          if (ev.version_number !== null) return false
        } else if (String(ev.version_number ?? '') !== versionFilter) {
          return false
        }
      }
      if (categoryFilter !== 'all' && ev.category !== categoryFilter) {
        return false
      }
      return true
    })
  }, [history, versionFilter, categoryFilter])

  const selectedDecisionEvent = useMemo(() => {
    if (!history || !selectedDecisionId) return null
    return history.events.find((ev) => ev.event_id === selectedDecisionId) ?? null
  }, [history, selectedDecisionId])

  const handleSelectDecision = (ev: HistoryTimelineEventItem) => {
    setSelectedDecisionId(ev.event_id)
    setLiveVerification(null)
    setVerifyError(null)
  }

  const handleVerifyIntegrity = async (ev: HistoryTimelineEventItem) => {
    if (!history || !ev.record_id || !ev.project_version_id) return
    setVerifyingIntegrity(true)
    setVerifyError(null)
    setLiveVerification(null)
    setVerificationErrorByEventId((prev) => {
      const next = { ...prev }
      delete next[ev.event_id]
      return next
    })
    try {
      const result = await verifyDecisionIntegrity(
        history.project_id,
        ev.project_version_id,
        ev.record_id,
      )
      let refreshed: ProjectHistoryResponse | null = null
      try {
        refreshed = await getProjectHistory(history.project_id)
      } catch {
        // Si la recarga complementaria del historial falla, conservamos el resultado en vivo en ambas vistas.
      }
      if (refreshed) {
        setHistory(refreshed)
      }
      setLiveVerification(result)
      setVerifiedByEventId((prev) => ({ ...prev, [ev.event_id]: result }))
    } catch (err: unknown) {
      const msg =
        err instanceof Error
          ? err.message
          : 'No se pudo comprobar la integridad del registro en el servidor.'
      setVerifyError(msg)
      setVerifiedByEventId((prev) => {
        const next = { ...prev }
        delete next[ev.event_id]
        return next
      })
      setVerificationErrorByEventId((prev) => ({ ...prev, [ev.event_id]: msg }))
    } finally {
      setVerifyingIntegrity(false)
    }
  }

  return (
    <div className="page-container" style={{ maxWidth: '1120px', margin: '0 auto', padding: '1.5rem' }}>
      <header style={{ marginBottom: '1.25rem' }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            flexWrap: 'wrap',
            gap: '0.75rem',
          }}
        >
          <div>
            <h1 style={{ marginBottom: '0.35rem' }}>Auditoría · Historial y trazabilidad de decisiones</h1>
            <p style={{ color: '#475569', margin: 0 }}>
              Reconstrucción cronológica en solo lectura a partir de las fuentes registradas en el
              expediente y la bitácora del sistema, sujeta a las limitaciones históricas documentadas.
            </p>
          </div>
          <span
            style={{
              padding: '0.3rem 0.7rem',
              borderRadius: '999px',
              background: '#e0f2fe',
              color: '#075985',
              fontSize: '0.82rem',
              fontWeight: 600,
            }}
          >
            Consulta de solo lectura
          </span>
        </div>
      </header>

      {error && (
        <div style={{ marginBottom: '1rem' }}>
          <Alert variant="error">{error}</Alert>
        </div>
      )}

      <section
        style={{
          background: '#ffffff',
          border: '1px solid #cbd5e1',
          borderRadius: '10px',
          padding: '1rem 1.15rem',
          marginBottom: '1.25rem',
          display: 'grid',
          gap: '0.85rem',
        }}
      >
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
            gap: '0.85rem',
            alignItems: 'end',
          }}
        >
          <div>
            <label
              htmlFor="audit-search-input"
              style={{ display: 'block', fontSize: '0.85rem', fontWeight: 600, marginBottom: '0.3rem' }}
            >
              Buscar expediente por código o nombre
            </label>
            <input
              id="audit-search-input"
              type="text"
              value={searchTerm}
              onChange={(e) => setSearchTerm(e.target.value)}
              placeholder="Ej. PROY-2026 o Mejoramiento..."
              style={{
                width: '100%',
                padding: '0.5rem 0.65rem',
                borderRadius: '6px',
                border: '1px solid #94a3b8',
              }}
            />
          </div>

          <div>
            <label
              htmlFor="audit-project-select"
              style={{ display: 'block', fontSize: '0.85rem', fontWeight: 600, marginBottom: '0.3rem' }}
            >
              Seleccionar expediente ({filteredProjects.length})
            </label>
            <select
              id="audit-project-select"
              value={selectedProjectId}
              onChange={(e) => setSelectedProjectId(e.target.value)}
              disabled={loadingProjects || filteredProjects.length === 0}
              style={{
                width: '100%',
                padding: '0.5rem 0.65rem',
                borderRadius: '6px',
                border: '1px solid #94a3b8',
                background: '#ffffff',
              }}
            >
              {filteredProjects.length === 0 ? (
                <option value="">Sin expedientes que coincidan</option>
              ) : (
                filteredProjects.map((p) => (
                  <option key={p.project_id} value={p.project_id}>
                    {p.code} — {p.title} (v{p.current_version_number} ·{' '}
                    {STATUS_LABELS[p.status] ?? p.status})
                  </option>
                ))
              )}
            </select>
          </div>
        </div>
      </section>

      {loadingProjects || loadingHistory ? (
        <p style={{ color: '#475569' }}>Reconstruyendo historial cronológico del expediente...</p>
      ) : !history ? (
        <section
          style={{
            background: '#f8fafc',
            border: '1px dashed #cbd5e1',
            borderRadius: '10px',
            padding: '1.25rem',
            color: '#475569',
          }}
        >
          No hay un expediente seleccionado o no existen registros disponibles para mostrar.
        </section>
      ) : (
        <>
          <section
            style={{
              background: '#ffffff',
              border: '1px solid #cbd5e1',
              borderRadius: '10px',
              padding: '1rem 1.15rem',
              marginBottom: '1.25rem',
            }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'flex-start',
                flexWrap: 'wrap',
                gap: '0.75rem',
                marginBottom: '0.85rem',
              }}
            >
              <div>
                <span style={{ fontSize: '0.82rem', fontWeight: 700, color: '#075985' }}>
                  {history.code}
                </span>
                <h2 style={{ margin: '0.15rem 0 0 0', fontSize: '1.15rem' }}>
                  {history.current_title}
                </h2>
              </div>
              <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                <span
                  style={{
                    padding: '0.25rem 0.65rem',
                    borderRadius: '999px',
                    fontSize: '0.82rem',
                    fontWeight: 600,
                    background: '#f1f5f9',
                    color: '#0f172a',
                  }}
                >
                  Estado actual: {STATUS_LABELS[history.current_status] ?? history.current_status}
                </span>
                <span
                  style={{
                    padding: '0.25rem 0.65rem',
                    borderRadius: '999px',
                    fontSize: '0.82rem',
                    fontWeight: 600,
                    background: '#e0f2fe',
                    color: '#075985',
                  }}
                >
                  {history.versions.length} versión(es) · {history.events.length} hitos cronológicos
                </span>
              </div>
            </div>

            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(230px, 1fr))',
                gap: '0.75rem',
              }}
            >
              {history.versions.map((v) => (
                <div
                  key={v.project_version_id}
                  style={{
                    background: '#f8fafc',
                    border:
                      v.version_number === history.current_version_number
                        ? '1px solid #0284c7'
                        : '1px solid #e2e8f0',
                    borderRadius: '8px',
                    padding: '0.75rem 0.9rem',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      marginBottom: '0.25rem',
                    }}
                  >
                    <strong style={{ fontSize: '0.92rem' }}>Versión v{v.version_number}</strong>
                    <span style={{ fontSize: '0.75rem', color: '#475569' }}>
                      {formatDateTime(v.created_at)}
                    </span>
                  </div>
                  <div style={{ fontSize: '0.86rem', color: '#1e293b', marginBottom: '0.25rem' }}>
                    {v.title}
                  </div>
                  <div style={{ fontSize: '0.8rem', color: '#475569' }}>
                    Presupuesto: {formatCurrency(v.estimated_budget_pen)} · Beneficiarios est.:{' '}
                    {v.beneficiaries_count.toLocaleString('es-PE')}
                  </div>
                  <div style={{ fontSize: '0.78rem', color: '#64748b', marginTop: '0.2rem' }}>
                    Autor:{' '}
                    {v.created_by_name ?? 'Cuenta municipal no localizada en el directorio activo'}
                  </div>
                </div>
              ))}
            </div>
          </section>

          {selectedDecisionEvent && (
            <section
              id="decision-audit-detail-panel"
              aria-label="Detalle de decisión auditada"
              style={{
                background: '#ffffff',
                border: '2px solid #0284c7',
                borderRadius: '10px',
                padding: '1.1rem 1.25rem',
                marginBottom: '1.25rem',
                display: 'grid',
                gap: '0.85rem',
              }}
            >
              <div
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'flex-start',
                  flexWrap: 'wrap',
                  gap: '0.5rem',
                }}
              >
                <div>
                  <span style={{ fontSize: '0.78rem', fontWeight: 700, color: '#0369a1' }}>
                    DETALLE DE DECISIÓN Y AUDITORÍA DE FIRMA Y FECHA (HU5.4)
                  </span>
                  <h3 style={{ margin: '0.2rem 0 0 0', fontSize: '1.08rem', color: '#0f172a' }}>
                    {selectedDecisionEvent.title}
                  </h3>
                </div>
                <button
                  type="button"
                  onClick={() => {
                    setSelectedDecisionId(null)
                    setLiveVerification(null)
                    setVerifyError(null)
                  }}
                  style={{
                    padding: '0.32rem 0.7rem',
                    borderRadius: '6px',
                    border: '1px solid #cbd5e1',
                    background: '#f8fafc',
                    color: '#334155',
                    cursor: 'pointer',
                    fontSize: '0.82rem',
                    fontWeight: 600,
                  }}
                >
                  Cerrar detalle
                </button>
              </div>

              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fit, minmax(250px, 1fr))',
                  gap: '0.65rem 1rem',
                  fontSize: '0.86rem',
                  color: '#1e293b',
                  background: '#f8fafc',
                  border: '1px solid #e2e8f0',
                  borderRadius: '8px',
                  padding: '0.85rem 1rem',
                }}
              >
                <div>
                  <strong>Expediente y código:</strong>
                  <div>
                    {history.code} — {history.current_title}
                  </div>
                </div>
                <div>
                  <strong>Versión asociada:</strong>
                  <div>
                    Versión v{selectedDecisionEvent.version_number ?? '—'} ·{' '}
                    <code>{selectedDecisionEvent.project_version_id ?? 'Sin ID'}</code>
                  </div>
                </div>
                <div>
                  <strong>ID de decisión:</strong>
                  <div>
                    <code>{selectedDecisionEvent.record_id ?? selectedDecisionEvent.event_id}</code>
                  </div>
                </div>
                <div>
                  <strong>Tipo y resultado:</strong>
                  <div>
                    {selectedDecisionEvent.decision_kind === 'checkpoint_decision'
                      ? 'Decisión de revisión en punto de control'
                      : 'Dictamen final del expediente'}{' '}
                    · {selectedDecisionEvent.title}
                    {selectedDecisionEvent.decision_code
                      ? ` (${selectedDecisionEvent.decision_code})`
                      : ''}
                  </div>
                </div>
                <div>
                  <strong>Responsable registrado:</strong>
                  <div>
                    {selectedDecisionEvent.actor_name ? (
                      <>
                        {selectedDecisionEvent.actor_name} · UUID:{' '}
                        <code>{selectedDecisionEvent.actor_user_id}</code>
                      </>
                    ) : (
                      <>
                        Nombre no disponible en el directorio actual (cuenta ausente o retirada) ·
                        UUID conservado: <code>{selectedDecisionEvent.actor_user_id ?? 'N/D'}</code>
                      </>
                    )}
                  </div>
                </div>
                <div>
                  <strong>Fecha y hora (con zona horaria):</strong>
                  <div>{formatDateTimeWithTz(selectedDecisionEvent.occurred_at)}</div>
                </div>
                <div>
                  <strong>Estado del punto de control:</strong>
                  <div>
                    {selectedDecisionEvent.decision_kind === 'checkpoint_decision' ? (
                      <>
                        {CHECKPOINT_STATUS_LABELS[selectedDecisionEvent.checkpoint_status ?? ''] ??
                          selectedDecisionEvent.checkpoint_status ??
                          'No registrado'}
                        {selectedDecisionEvent.checkpoint_id && (
                          <>
                            {' '}
                            · ID: <code>{selectedDecisionEvent.checkpoint_id}</code>
                          </>
                        )}
                      </>
                    ) : (
                      'No aplica (dictamen final emitido fuera de punto de control)'
                    )}
                  </div>
                </div>
                <div>
                  <strong>Condiciones registradas:</strong>
                  <div>
                    {selectedDecisionEvent.conditions && selectedDecisionEvent.conditions.trim()
                      ? selectedDecisionEvent.conditions
                      : 'Sin condiciones adicionales registradas'}
                  </div>
                </div>
                <div style={{ gridColumn: '1 / -1' }}>
                  <strong>Fundamento / justificación:</strong>
                  <div style={{ marginTop: '0.15rem' }}>
                    {liveVerification?.justification ??
                      selectedDecisionEvent.justification ??
                      selectedDecisionEvent.detail}
                  </div>
                </div>
              </div>

              {selectedDecisionEvent.has_integrity_hash ? (
                <div
                  style={{
                    borderRadius: '8px',
                    padding: '0.85rem 1rem',
                    border:
                      liveVerification?.integrity_status === 'tampered'
                        ? '1px solid #dc2626'
                        : liveVerification?.integrity_status === 'intact'
                          ? '1px solid #16a34a'
                          : '1px solid #cbd5e1',
                    background:
                      liveVerification?.integrity_status === 'tampered'
                        ? '#fef2f2'
                        : liveVerification?.integrity_status === 'intact'
                          ? '#f0fdf4'
                          : '#f8fafc',
                    display: 'grid',
                    gap: '0.55rem',
                  }}
                >
                  <div
                    style={{
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      flexWrap: 'wrap',
                      gap: '0.75rem',
                    }}
                  >
                    <div style={{ fontSize: '0.86rem', color: '#1e293b' }}>
                      <strong>Comprobación criptográfica SHA-256 en servidor:</strong>{' '}
                      {liveVerification ? (
                        <span
                          style={{
                            padding: '0.2rem 0.6rem',
                            borderRadius: '999px',
                            fontWeight: 700,
                            background:
                              liveVerification.integrity_status === 'intact'
                                ? '#dcfce7'
                                : '#fee2e2',
                            color:
                              liveVerification.integrity_status === 'intact'
                                ? '#166534'
                                : '#991b1b',
                          }}
                        >
                          {liveVerification.integrity_label}
                        </span>
                      ) : (
                        <span style={{ color: '#475569' }}>
                          Huella guardada disponible; solicite verificación en vivo en el servidor.
                        </span>
                      )}
                    </div>

                    <button
                      type="button"
                      id="btn-verify-integrity"
                      disabled={
                        verifyingIntegrity ||
                        !selectedDecisionEvent.record_id ||
                        !selectedDecisionEvent.project_version_id
                      }
                      onClick={() => void handleVerifyIntegrity(selectedDecisionEvent)}
                      style={{
                        padding: '0.45rem 0.9rem',
                        borderRadius: '6px',
                        border: 'none',
                        background: '#0f766e',
                        color: '#ffffff',
                        fontWeight: 600,
                        fontSize: '0.85rem',
                        cursor: verifyingIntegrity ? 'wait' : 'pointer',
                      }}
                    >
                      {verifyingIntegrity ? 'Verificando en servidor…' : 'Verificar integridad'}
                    </button>
                  </div>

                  {verifyError && <Alert variant="error">{verifyError}</Alert>}

                  {liveVerification && (
                    <div
                      style={{
                        fontSize: '0.82rem',
                        color:
                          liveVerification.integrity_status === 'tampered' ? '#991b1b' : '#1e293b',
                        display: 'grid',
                        gap: '0.25rem',
                      }}
                    >
                      <div>
                        <strong>Resultado de la comprobación:</strong>{' '}
                        {liveVerification.integrity_label} ·{' '}
                        <strong>Comprobado el:</strong>{' '}
                        {formatDateTimeWithTz(liveVerification.checked_at)}
                      </div>
                      <div>
                        <strong>Huella SHA-256 almacenada:</strong>{' '}
                        <code>{liveVerification.stored_hash_sha256}</code>
                      </div>
                      <div>
                        <strong>Huella SHA-256 recomputada:</strong>{' '}
                        <code>{liveVerification.recomputed_hash_sha256}</code>
                      </div>
                    </div>
                  )}

                  {!liveVerification && selectedDecisionEvent.integrity_hash_sha256 && (
                    <div style={{ fontSize: '0.8rem', color: '#475569' }}>
                      Huella almacenada: <code>{selectedDecisionEvent.integrity_hash_sha256}</code>
                    </div>
                  )}

                  <div style={{ fontSize: '0.78rem', color: '#475569' }}>
                    {liveVerification?.prototype_notice ??
                      selectedDecisionEvent.prototype_notice ??
                      'La constancia SHA-256 del prototipo detecta alteraciones internas del contenido, pero no equivale a firma digital certificada con validez jurídica.'}
                  </div>
                </div>
              ) : (
                <div
                  style={{
                    borderRadius: '8px',
                    padding: '0.85rem 1rem',
                    border: '1px solid #cbd5e1',
                    background: '#f8fafc',
                    fontSize: '0.84rem',
                    color: '#334155',
                    display: 'grid',
                    gap: '0.25rem',
                  }}
                >
                  <div>
                    <strong>Estado de integridad:</strong> Verificación de integridad no disponible
                    para este registro.
                  </div>
                  <div style={{ fontSize: '0.78rem', color: '#475569' }}>
                    {selectedDecisionEvent.prototype_notice ??
                      'Este registro no cuenta con huella SHA-256 almacenada y no equivale a firma digital certificada con validez jurídica.'}
                  </div>
                </div>
              )}
            </section>
          )}

          <section
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              flexWrap: 'wrap',
              gap: '0.75rem',
              marginBottom: '1rem',
            }}
          >
            <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', alignItems: 'center' }}>
              <div>
                <label
                  htmlFor="audit-version-filter"
                  style={{ fontSize: '0.83rem', fontWeight: 600, marginRight: '0.4rem' }}
                >
                  Versión:
                </label>
                <select
                  id="audit-version-filter"
                  value={versionFilter}
                  onChange={(e) => setVersionFilter(e.target.value)}
                  style={{
                    padding: '0.38rem 0.55rem',
                    borderRadius: '6px',
                    border: '1px solid #94a3b8',
                    background: '#ffffff',
                  }}
                >
                  <option value="all">Todas las versiones ({history.events.length})</option>
                  {history.versions.map((v) => (
                    <option key={v.project_version_id} value={String(v.version_number)}>
                      Versión v{v.version_number}
                    </option>
                  ))}
                  {history.events.some((e) => e.version_number === null) && (
                    <option value="unassigned">Sin versión específica</option>
                  )}
                </select>
              </div>

              <div>
                <label
                  htmlFor="audit-category-filter"
                  style={{ fontSize: '0.83rem', fontWeight: 600, marginRight: '0.4rem' }}
                >
                  Tipo de hito:
                </label>
                <select
                  id="audit-category-filter"
                  value={categoryFilter}
                  onChange={(e) => setCategoryFilter(e.target.value)}
                  style={{
                    padding: '0.38rem 0.55rem',
                    borderRadius: '6px',
                    border: '1px solid #94a3b8',
                    background: '#ffffff',
                  }}
                >
                  <option value="all">Todos los hitos</option>
                  {Object.entries(CATEGORY_LABELS).map(([k, label]) => (
                    <option key={k} value={k}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <span style={{ fontSize: '0.84rem', color: '#475569' }}>
              Mostrando {visibleEvents.length} de {history.events.length} registros cronológicos
            </span>
          </section>

          {visibleEvents.length === 0 ? (
            <section
              style={{
                background: '#f8fafc',
                border: '1px dashed #cbd5e1',
                borderRadius: '10px',
                padding: '1.25rem',
                color: '#475569',
                marginBottom: '1.25rem',
              }}
            >
              No existen eventos para la combinación de filtros seleccionada.
            </section>
          ) : (
            <ol
              style={{
                listStyle: 'none',
                margin: '0 0 1.25rem 0',
                padding: 0,
                display: 'grid',
                gap: '0.75rem',
              }}
            >
              {visibleEvents.map((ev, idx) => {
                const badgeStyle = CATEGORY_COLORS[ev.category] ?? {
                  bg: '#f1f5f9',
                  fg: '#334155',
                }
                const liveCheck = verifiedByEventId[ev.event_id] ?? null
                const liveCheckError = verificationErrorByEventId[ev.event_id] ?? null
                const integrityFailed =
                  liveCheckError !== null ||
                  (liveCheck !== null
                    ? liveCheck.integrity_status !== 'intact'
                    : ev.verification_status !== null && ev.verification_status !== 'valid')
                const effectiveTitle = liveCheck ? liveCheck.decision_label : ev.title
                const effectiveResult = liveCheckError
                  ? 'Nueva comprobación no completada'
                  : liveCheck
                    ? liveCheck.integrity_label
                    : ev.result
                const effectiveDetail = liveCheck
                  ? `${liveCheck.justification}${liveCheck.conditions ? ` · Condiciones/corrección: ${liveCheck.conditions}` : ''}`
                  : ev.detail
                const effectiveHash = liveCheck?.stored_hash_sha256 ?? ev.integrity_hash_sha256
                const effectiveVerificationText = liveCheckError
                  ? `La nueva comprobación no pudo completarse (${liveCheckError})`
                  : liveCheck
                    ? `${liveCheck.integrity_label} · Comprobado el: ${formatDateTimeWithTz(liveCheck.checked_at)}`
                    : (ev.verification_label ?? ev.verification_status)

                return (
                  <li
                    key={ev.event_id}
                    style={{
                      background: '#ffffff',
                      border: integrityFailed ? '1px solid #dc2626' : '1px solid #cbd5e1',
                      borderRadius: '10px',
                      padding: '0.9rem 1.1rem',
                      display: 'grid',
                      gap: '0.45rem',
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
                      <div style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', flexWrap: 'wrap' }}>
                        <span style={{ fontSize: '0.78rem', fontWeight: 700, color: '#64748b' }}>
                          #{idx + 1}
                        </span>
                        <span
                          style={{
                            padding: '0.18rem 0.5rem',
                            borderRadius: '999px',
                            fontSize: '0.76rem',
                            fontWeight: 600,
                            background: badgeStyle.bg,
                            color: badgeStyle.fg,
                          }}
                        >
                          {CATEGORY_LABELS[ev.category] ?? ev.category}
                        </span>
                        {ev.version_number !== null && (
                          <span
                            style={{
                              padding: '0.18rem 0.5rem',
                              borderRadius: '999px',
                              fontSize: '0.76rem',
                              fontWeight: 600,
                              background: '#e2e8f0',
                              color: '#1e293b',
                            }}
                          >
                            v{ev.version_number}
                          </span>
                        )}
                        <strong style={{ fontSize: '0.98rem', color: '#0f172a' }}>
                          {effectiveTitle}
                        </strong>
                      </div>
                      <span style={{ fontSize: '0.82rem', color: '#475569', fontWeight: 500 }}>
                        {formatDateTime(ev.occurred_at)}
                      </span>
                    </div>

                    <div
                      style={{
                        display: 'flex',
                        flexWrap: 'wrap',
                        gap: '0.75rem',
                        fontSize: '0.86rem',
                        color: '#334155',
                      }}
                    >
                      <span>
                        <strong>Actor:</strong> {renderActorLabel(ev)}
                      </span>
                      <span>
                        <strong>Resultado:</strong> {effectiveResult}
                      </span>
                      <span>
                        <strong>Registro:</strong> {ev.source}
                      </span>
                    </div>

                    <p style={{ margin: 0, fontSize: '0.88rem', color: '#1e293b' }}>
                      {effectiveDetail}
                    </p>

                    {(ev.decision_kind === 'checkpoint_decision' ||
                      ev.decision_kind === 'final_approval') && (
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          flexWrap: 'wrap',
                          gap: '0.5rem',
                          marginTop: '0.2rem',
                        }}
                      >
                        <button
                          type="button"
                          onClick={() => handleSelectDecision(ev)}
                          style={{
                            padding: '0.35rem 0.75rem',
                            borderRadius: '6px',
                            border: '1px solid #0284c7',
                            background:
                              selectedDecisionId === ev.event_id ? '#0284c7' : '#f0f9ff',
                            color: selectedDecisionId === ev.event_id ? '#ffffff' : '#0369a1',
                            fontWeight: 600,
                            fontSize: '0.82rem',
                            cursor: 'pointer',
                          }}
                        >
                          {selectedDecisionId === ev.event_id
                            ? 'Detalle seleccionado'
                            : 'Ver detalle de decisión'}
                        </button>
                      </div>
                    )}

                    {effectiveHash && (
                      <div
                        style={{
                          marginTop: '0.25rem',
                          padding: '0.55rem 0.75rem',
                          borderRadius: '6px',
                          background: integrityFailed ? '#fef2f2' : '#f8fafc',
                          border: integrityFailed ? '1px solid #fecaca' : '1px solid #e2e8f0',
                          fontSize: '0.8rem',
                          color: integrityFailed ? '#991b1b' : '#334155',
                        }}
                      >
                        <div>
                          <strong>Verificación de integridad SHA-256:</strong>{' '}
                          {effectiveVerificationText} · Huella:{' '}
                          <code>{effectiveHash.slice(0, 16)}...</code>
                        </div>
                        {liveCheck?.integrity_status === 'tampered' &&
                          liveCheck.recomputed_hash_sha256 && (
                            <div style={{ marginTop: '0.15rem' }}>
                              <strong>Huella recomputada en servidor:</strong>{' '}
                              <code>{liveCheck.recomputed_hash_sha256.slice(0, 16)}...</code>
                            </div>
                          )}
                        {(liveCheck?.prototype_notice ?? ev.prototype_notice) && (
                          <div style={{ marginTop: '0.2rem', color: '#475569' }}>
                            {liveCheck?.prototype_notice ?? ev.prototype_notice}
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                )
              })}
            </ol>
          )}

          <section
            style={{
              background: '#f8fafc',
              border: '1px solid #cbd5e1',
              borderRadius: '10px',
              padding: '0.95rem 1.1rem',
              fontSize: '0.84rem',
              color: '#475569',
            }}
          >
            <strong style={{ display: 'block', marginBottom: '0.35rem', color: '#1e293b' }}>
              Alcance y limitaciones del registro histórico
            </strong>
            <ul style={{ margin: 0, paddingLeft: '1.2rem', display: 'grid', gap: '0.25rem' }}>
              {history.historical_limitations.map((lim, i) => (
                <li key={i}>{lim}</li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  )
}
