import { useEffect, useState, type FormEvent } from 'react'
import { listNormativeDocuments, searchNormative } from '../../api/pmv1.api'
import { Alert } from '../../components/ui/Alert'
import { FormField } from '../../components/ui/FormField'
import { PageHeader } from '../../components/ui/PageHeader'
import type {
  NormativeChunkItem,
  NormativeDocumentItem,
  NormativeSearchResponse,
} from '../../types/pmv1'
import { newIdempotencyKey } from '../../utils/idempotency'

function resolveFallbackLocation(item: NormativeChunkItem): string {
  if (item.location && item.location.trim().length > 0) {
    return item.location.trim()
  }
  const mTopic = item.topic.match(
    /\(([^)]*(?:Art|Título|Titulo|Reglamento|RNE|SINAGERD|Sección|Seccion|Ley DUS|Índice|Indice)[^)]*)\)/i,
  )
  if (mTopic) {
    return mTopic[1].trim()
  }
  const mContent = item.content.match(/^(Art(?:[ií]culo)?\.\s*\d+[^.\-:]*)/i)
  if (mContent) {
    return mContent[1].trim()
  }
  return `Fragmento ID ${item.id}`
}

function originBadge(origin?: string): { label: string; bg: string; color: string } {
  const norm = (origin ?? 'public').toLowerCase()
  if (norm === 'simulated') {
    return { label: 'Simulado / Demostración', bg: '#fef3c7', color: '#92400e' }
  }
  if (norm === 'declared') {
    return { label: 'Declarado', bg: '#e0f2fe', color: '#075985' }
  }
  if (norm === 'official' || norm === 'oficial') {
    return { label: 'Fuente oficial verificada', bg: '#dcfce7', color: '#166534' }
  }
  return { label: 'Fuente pública verificable', bg: '#dcfce7', color: '#166534' }
}

export function NormativaPage() {
  const [query, setQuery] = useState('contrataciones obras municipales')
  const [onlyInForce, setOnlyInForce] = useState(true)
  const [documentFilter, setDocumentFilter] = useState('')
  const [catalog, setCatalog] = useState<NormativeDocumentItem[]>([])
  const [result, setResult] = useState<NormativeSearchResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let active = true
    listNormativeDocuments()
      .then((res) => {
        if (active) setCatalog(res.documents)
      })
      .catch(() => {
        // Catálogo referencial opcional; la búsqueda muestra errores explícitos al consultar
      })
    return () => {
      active = false
    }
  }, [])

  const onSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError(null)
    try {
      const data = await searchNormative(query.trim(), newIdempotencyKey('norm'), {
        onlyInForce,
        documentFilter: documentFilter.trim() || null,
        limit: 8,
      })
      setResult(data)
    } catch (err) {
      setResult(null)
      setError(err instanceof Error ? err.message : 'Búsqueda fallida.')
    } finally {
      setLoading(false)
    }
  }

  const activeCatalogDocs = catalog.filter((d) => d.in_force)
  const historicalCatalogDocs = catalog.filter((d) => !d.in_force)

  return (
    <div className="form-page pmv1-flow">
      <PageHeader
        title="Consulta normativa"
        description="Consulta automática de normativa vigente y trazabilidad histórica para Asesoría Jurídica (PMV 1 · HU2.1)."
      />

      <form className="lookup-form" onSubmit={(e) => void onSubmit(e)}>
        {error && <Alert variant="error">{error}</Alert>}
        <FormField
          label="Consulta jurídica / semántica"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          required
        />
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.85rem', alignItems: 'center', margin: '0.5rem 0' }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.45rem', fontSize: '0.86rem', fontWeight: 600 }}>
            <input
              type="checkbox"
              data-testid="normative-only-in-force"
              checked={onlyInForce}
              onChange={(e) => setOnlyInForce(e.target.checked)}
            />
            Solo normativa vigente (excluir fuentes históricas o sustituidas)
          </label>
          <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.84rem' }}>
            <span>Filtro por norma:</span>
            <select
              data-testid="normative-doc-filter"
              className="form-field-input"
              style={{ width: 'auto', minWidth: '210px', padding: '0.3rem 0.5rem' }}
              value={documentFilter}
              onChange={(e) => setDocumentFilter(e.target.value)}
            >
              <option value="">Todas las normas del corpus</option>
              <option value="LEY_32069">Ley N.° 32069 (OECE vigente)</option>
              <option value="REG_32069">Reglamento D.S. N.° 009-2025-EF</option>
              <option value="LEY_27972">Ley N.° 27972 (LOM)</option>
              <option value="DL_1252">D.L. N.° 1252 (Invierte.pe)</option>
              <option value="PDU_EL_TAMBO">PDM Huancayo / El Tambo</option>
            </select>
          </label>
        </div>
        <button type="submit" className="btn btn-primary" data-testid="normative-search-btn" disabled={loading}>
          {loading ? 'Buscando…' : 'Buscar normativa'}
        </button>
      </form>

      {result && (
        <section className="flow-panel" aria-live="polite" data-testid="normative-results-panel">
          {result.is_demo_fallback && (
            <Alert variant="error">
              Modo demostración sin base de datos: los resultados mostrados NO provienen de una consulta vigente real en PostgreSQL.
            </Alert>
          )}
          <p className="flow-hint">{result.disclaimer}</p>
          {result.alerts_found.length > 0 && (
            <Alert variant="error">Alertas: {result.alerts_found.join(' · ')}</Alert>
          )}

          {result.results.length === 0 ? (
            <div className="norm-empty-notice" role="status" data-testid="normative-empty-state">
              <h3>No se encontró sustento normativo suficiente para esta consulta</h3>
              <p>
                El repositorio normativo vigente no contiene fragmentos aplicables para el criterio consultado.
                El sistema no genera normas, artículos, ubicaciones ni conclusiones jurídicas ficticias ante la ausencia de evidencia documental.
              </p>
            </div>
          ) : (
            <ul className="norm-results" data-testid="normative-results-list">
              {result.results.map((item) => {
                const locationDisplay = resolveFallbackLocation(item)
                const origin = originBadge(item.data_origin)
                const isActive = item.in_force !== false
                return (
                  <li key={item.id} className={item.has_alert ? 'norm-hit alert' : 'norm-hit'} data-testid={`norm-hit-${item.id}`}>
                    <header className="norm-hit-header">
                      <div className="norm-hit-title-group">
                        <strong className="norm-doc-name">{item.document_name}</strong>
                        <span className="norm-version-tag">Versión: {item.version}</span>
                        <span
                          style={{
                            padding: '0.12rem 0.5rem',
                            borderRadius: '999px',
                            fontSize: '0.73rem',
                            fontWeight: 700,
                            background: isActive ? '#dcfce7' : '#fee2e2',
                            color: isActive ? '#166534' : '#991b1b',
                          }}
                        >
                          {isActive ? 'Vigente' : 'Histórica / Sustituida'}
                        </span>
                        <span
                          data-testid={`norm-origin-${item.id}`}
                          style={{
                            padding: '0.12rem 0.5rem',
                            borderRadius: '999px',
                            fontSize: '0.73rem',
                            fontWeight: 700,
                            background: origin.bg,
                            color: origin.color,
                          }}
                        >
                          {origin.label}
                        </span>
                      </div>
                      <div className="norm-hit-meta">
                        <span className="norm-code-badge">{item.short_code}</span>
                        <span className="norm-score-badge">Relevancia: {item.relevance_score.toFixed(2)}</span>
                      </div>
                    </header>
                    <div className="norm-hit-location-bar">
                      <span className="norm-location-badge">
                        <span className="location-label">Ubicación verificable:</span>{' '}
                        <strong className="location-value">{locationDisplay}</strong>
                      </span>
                      <span className="norm-chunk-id">ID Fragmento: {item.id}</span>
                    </div>
                    <p className="norm-topic">
                      <strong>Materia:</strong> {item.topic}
                    </p>
                    <blockquote className="norm-content">{item.content}</blockquote>
                    {(item.official_url || item.amendments_summary || item.consulted_at) && (
                      <div style={{ marginTop: '0.4rem', fontSize: '0.78rem', color: '#475569', display: 'grid', gap: '0.15rem' }}>
                        {item.amendments_summary && (
                          <div>
                            <strong>Vigencia y modificatorias:</strong> {item.amendments_summary}
                          </div>
                        )}
                        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.65rem' }}>
                          {item.consulted_at && (
                            <span>
                              <strong>Fecha de consulta:</strong> {item.consulted_at}
                            </span>
                          )}
                          {item.official_url && (
                            <a href={item.official_url} target="_blank" rel="noreferrer">
                              Fuente oficial: {item.official_url}
                            </a>
                          )}
                        </div>
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}

          {result.historical_references_excluded && result.historical_references_excluded.length > 0 && (
            <div
              data-testid="normative-excluded-historical"
              style={{
                marginTop: '0.9rem',
                padding: '0.75rem 0.9rem',
                background: '#f8fafc',
                border: '1px solid #cbd5e1',
                borderRadius: '8px',
                fontSize: '0.82rem',
                display: 'grid',
                gap: '0.45rem',
              }}
            >
              <strong style={{ color: '#0f172a' }}>
                Fuentes históricas / sustituidas excluidas de vigencia actual ({result.historical_references_excluded.length}):
              </strong>
              <p style={{ margin: 0, color: '#475569' }}>
                Las siguientes referencias coinciden con los términos consultados, pero permanecen marcadas como inactivas o sustituidas y no se utilizan como sustento vigente (CA-HU2.1-02):
              </p>
              <ul style={{ margin: 0, paddingLeft: '1.1rem', display: 'grid', gap: '0.25rem' }}>
                {result.historical_references_excluded.map((hist) => (
                  <li key={hist.id}>
                    <strong>{hist.id}</strong> · {hist.document_name} (<code>{hist.version}</code>) —{' '}
                    <span>{resolveFallbackLocation(hist)}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      )}

      {catalog.length > 0 && (
        <section className="flow-panel" data-testid="normative-catalog-panel">
          <h3 style={{ margin: '0 0 0.5rem', fontSize: '0.96rem' }}>
            Catálogo normativo indexado (Vigentes: {activeCatalogDocs.length} · Históricas conservadas: {historicalCatalogDocs.length})
          </h3>
          <div style={{ display: 'grid', gap: '0.45rem', fontSize: '0.8rem' }}>
            {catalog.map((doc) => {
              const origin = originBadge(doc.data_origin)
              return (
                <div
                  key={`${doc.short_code}-${doc.version}-${doc.in_force}`}
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    flexWrap: 'wrap',
                    gap: '0.4rem',
                    padding: '0.45rem 0.65rem',
                    border: '1px solid #e2e8f0',
                    borderRadius: '6px',
                    background: doc.in_force ? '#ffffff' : '#f8fafc',
                  }}
                >
                  <div>
                    <strong>{doc.document_name}</strong> · <code>{doc.version}</code> ({doc.chunks_count} fragmentos)
                    {doc.official_url && (
                      <>
                        {' '}·{' '}
                        <a href={doc.official_url} target="_blank" rel="noreferrer">
                          Enlace fuente
                        </a>
                      </>
                    )}
                  </div>
                  <div style={{ display: 'flex', gap: '0.35rem', alignItems: 'center' }}>
                    <span
                      style={{
                        padding: '0.1rem 0.45rem',
                        borderRadius: '999px',
                        fontWeight: 700,
                        background: doc.in_force ? '#dcfce7' : '#fee2e2',
                        color: doc.in_force ? '#166534' : '#991b1b',
                      }}
                    >
                      {doc.in_force ? 'Vigente' : 'Histórica / Excluida'}
                    </span>
                    <span
                      style={{
                        padding: '0.1rem 0.45rem',
                        borderRadius: '999px',
                        fontWeight: 600,
                        background: origin.bg,
                        color: origin.color,
                      }}
                    >
                      {origin.label}
                    </span>
                  </div>
                </div>
              )
            })}
          </div>
        </section>
      )}
    </div>
  )
}
