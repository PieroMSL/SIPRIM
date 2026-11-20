import { useEffect, useMemo, useRef, useState } from 'react'
import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import {
  downloadPrioritizationPdf,
  getProjectGeospatialLayers,
  getProjectMapRanking,
  type GeospatialLayerItem,
  type ProjectGeospatialLayersResponse,
  type ProjectMapRankingResponse,
  type RankingProjectItem,
} from '../../api/ranking.api'
import '../../styles/ranking-map.css'

const DEFAULT_CENTER: [number, number] = [-12.0544, -75.2198] // El Tambo, Huancayo

const LAYER_STYLES: Record<string, L.PathOptions> = {
  influence_area: {
    color: '#0d9488',
    weight: 2.5,
    dashArray: '6 4',
    fillColor: '#14b8a6',
    fillOpacity: 0.22,
  },
  beneficiary_coverage: {
    color: '#2563eb',
    weight: 2.5,
    fillColor: '#3b82f6',
    fillOpacity: 0.2,
  },
  territorial_zone: {
    color: '#d97706',
    weight: 2.5,
    dashArray: '2 4',
    fillColor: '#f59e0b',
    fillOpacity: 0.18,
  },
}

function formatCurrencyPen(value: string): string {
  const num = Number(value)
  if (Number.isNaN(num)) return `S/ ${value}`
  return new Intl.NumberFormat('es-PE', {
    style: 'currency',
    currency: 'PEN',
    maximumFractionDigits: 2,
  }).format(num)
}

function escapeHtml(text: string): string {
  return text
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

export function RankingMapPage() {
  const [data, setData] = useState<ProjectMapRankingResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [tileWarning, setTileWarning] = useState(false)
  const [layersData, setLayersData] = useState<ProjectGeospatialLayersResponse | null>(null)
  const [layersLoading, setLayersLoading] = useState(false)
  const [layersError, setLayersError] = useState<string | null>(null)
  const [activeLayerIds, setActiveLayerIds] = useState<Record<string, boolean>>({})
  const [pdfLoading, setPdfLoading] = useState(false)
  const [pdfError, setPdfError] = useState<string | null>(null)
  const [pdfSuccess, setPdfSuccess] = useState<string | null>(null)

  const mapContainerRef = useRef<HTMLDivElement | null>(null)
  const mapInstanceRef = useRef<L.Map | null>(null)
  const markersRef = useRef<Map<string, L.Marker>>(new Map())
  const geoLayerGroupRef = useRef<L.LayerGroup | null>(null)

  useEffect(() => {
    let cancelled = false
    getProjectMapRanking()
      .then((res) => {
        if (cancelled) return
        setData(res)
        setError(null)
        const firstMapped = res.mapped_projects[0]
        const firstRanked = res.ranked_projects[0]
        setSelectedId(firstMapped?.project_id ?? firstRanked?.project_id ?? null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'No se pudo cargar el ranking en mapa.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  const selectedProject: RankingProjectItem | null = useMemo(() => {
    if (!data || !selectedId) return null
    return data.ranked_projects.find((item) => item.project_id === selectedId) ?? null
  }, [data, selectedId])

  useEffect(() => {
    const container = mapContainerRef.current
    if (!container || !data || data.mapped_projects.length === 0) {
      return
    }

    if (mapInstanceRef.current) {
      mapInstanceRef.current.remove()
      mapInstanceRef.current = null
      markersRef.current.clear()
    }

    const map = L.map(container, {
      center: DEFAULT_CENTER,
      zoom: 13,
      zoomSnap: 0.25,
    })
    mapInstanceRef.current = map
    const geoGroup = L.layerGroup().addTo(map)
    geoLayerGroupRef.current = geoGroup

    const tileLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      attribution: '&copy; OpenStreetMap contributors',
      maxZoom: 19,
    })
    tileLayer.on('tileerror', () => {
      setTileWarning(true)
    })
    tileLayer.addTo(map)

    const localMarkers = markersRef.current
    const bounds: [number, number][] = []
    for (const item of data.mapped_projects) {
      if (item.latitude === null || item.longitude === null) continue
      const latlng: [number, number] = [item.latitude, item.longitude]
      bounds.push(latlng)

      const icon = L.divIcon({
        className: '',
        html: `<div class="ranking-map-marker-pin">#${item.rank_position}</div>`,
        iconSize: [34, 34],
        iconAnchor: [17, 17],
      })

      const marker = L.marker(latlng, {
        icon,
        title: `#${item.rank_position} ${item.title}`,
        alt: `Puesto ${item.rank_position}: ${item.title}`,
      })

      marker.bindPopup(
        `<strong>#${item.rank_position} · ${escapeHtml(item.code)}</strong><br/>` +
          `${escapeHtml(item.title)}<br/>` +
          `Puntuación: <strong>${item.global_score.toFixed(2)}/100</strong>`,
        { autoPan: false },
      )

      marker.on('click', () => {
        setSelectedId(item.project_id)
      })

      marker.addTo(map)
      localMarkers.set(item.project_id, marker)
    }

    if (bounds.length === 1) {
      map.setView(bounds[0], 14, { animate: false })
    } else if (bounds.length > 1) {
      map.fitBounds(bounds, { padding: [36, 36], animate: false })
    }
    window.setTimeout(() => {
      map.invalidateSize()
    }, 100)

    return () => {
      geoGroup.clearLayers()
      geoLayerGroupRef.current = null
      map.remove()
      mapInstanceRef.current = null
      localMarkers.clear()
    }
  }, [data])

  useEffect(() => {
    const map = mapInstanceRef.current
    if (!map || !data) return

    for (const item of data.mapped_projects) {
      const marker = markersRef.current.get(item.project_id)
      if (!marker) continue
      const isSelected = item.project_id === selectedId
      marker.setIcon(
        L.divIcon({
          className: '',
          html: `<div class="ranking-map-marker-pin${isSelected ? ' selected' : ''}">#${item.rank_position}</div>`,
          iconSize: [34, 34],
          iconAnchor: [17, 17],
        }),
      )
      if (isSelected && item.latitude !== null && item.longitude !== null) {
        marker.openPopup()
        const hasActiveGeo =
          layersData?.project_id === item.project_id &&
          layersData.layers.some(
            (l) => activeLayerIds[l.layer_id] && l.availability_status === 'available',
          )
        if (!hasActiveGeo) {
          map.panTo([item.latitude, item.longitude], { animate: false })
        }
      } else {
        marker.closePopup()
      }
    }
  }, [selectedId, data, layersData, activeLayerIds])

  useEffect(() => {
    if (!selectedId) return
    let cancelled = false
    Promise.resolve()
      .then(() => {
        if (cancelled) return null
        setLayersLoading(true)
        setLayersError(null)
        return getProjectGeospatialLayers(selectedId)
      })
      .then((res) => {
        if (cancelled || !res) return
        setLayersData(res)
        const defaults: Record<string, boolean> = {}
        for (const layer of res.layers) {
          defaults[layer.layer_id] = layer.availability_status === 'available'
        }
        setActiveLayerIds(defaults)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLayersData(null)
        setLayersError(
          err instanceof Error ? err.message : 'No se pudieron cargar las capas geoespaciales.',
        )
      })
      .finally(() => {
        if (!cancelled) setLayersLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [selectedId])

  useEffect(() => {
    const map = mapInstanceRef.current
    const group = geoLayerGroupRef.current
    if (!group) return
    group.clearLayers()
    if (!layersData) return

    const layerBounds = L.latLngBounds([])
    for (const layer of layersData.layers) {
      if (
        !activeLayerIds[layer.layer_id] ||
        layer.availability_status !== 'available' ||
        !layer.geojson_data
      ) {
        continue
      }
      const style = LAYER_STYLES[layer.layer_type] ?? LAYER_STYLES.influence_area
      const geoLayer = L.geoJSON(layer.geojson_data as unknown as GeoJSON.GeoJsonObject, {
        style: () => style,
      })
      geoLayer.bindPopup(
        `<strong>${escapeHtml(layer.layer_name)}</strong><br/>` +
          `${escapeHtml(layer.layer_type_label)}<br/>` +
          `Fuente: ${escapeHtml(layer.source_name)}`,
      )
      geoLayer.addTo(group)
      const b = geoLayer.getBounds()
      if (b.isValid()) {
        layerBounds.extend(b)
      }
    }
    if (map && layerBounds.isValid()) {
      map.stop()
      map.invalidateSize()
      map.fitBounds(layerBounds, { padding: [36, 36], animate: false })
      ;(window as unknown as Record<string, unknown>).__siprimLastBounds = {
        map: map.getBounds().toBBoxString(),
        layer: layerBounds.toBBoxString(),
        contains: map.getBounds().contains(layerBounds),
      }
    }
  }, [layersData, activeLayerIds])

  const handleToggleLayer = (layer: GeospatialLayerItem) => {
    setActiveLayerIds((prev) => ({
      ...prev,
      [layer.layer_id]: !prev[layer.layer_id],
    }))
  }

  const handleExportPdf = async () => {
    setPdfLoading(true)
    setPdfError(null)
    setPdfSuccess(null)
    try {
      const filename = await downloadPrioritizationPdf()
      setPdfSuccess(`Informe descargado: ${filename}`)
    } catch (err: unknown) {
      setPdfError(err instanceof Error ? err.message : 'No se pudo exportar el informe PDF.')
    } finally {
      setPdfLoading(false)
    }
  }

  const activeWeightsSummary = useMemo(() => {
    const first = data?.ranked_projects[0]
    if (!first) return null
    const entries = Object.entries(first.weights)
    if (entries.length === 0) return null
    const weightsText = entries.map(([k, v]) => `${k}: ${v.toFixed(1)}%`).join(' · ')
    const isEconomicOnly = first.weights.ECONOMIC === 100 && entries.length === 1
    return { weightsText, isEconomicOnly }
  }, [data])

  if (loading) {
    return (
      <section className="ranking-page">
        <div className="ranking-panel">
          <p>Cargando ranking de priorización y mapa territorial...</p>
        </div>
      </section>
    )
  }

  if (error) {
    return (
      <section className="ranking-page">
        <div className="alert-error" role="alert">
          {error}
        </div>
      </section>
    )
  }

  if (!data) {
    return null
  }

  return (
    <section className="ranking-page">
      <div className="ranking-header">
        <div>
          <h1>Ranking de proyectos en mapa (HU4.1)</h1>
          <p className="subtitle">
            Priorización territorial basada en el cálculo unificado de cinco dimensiones (HU1.12)
            {data.criteria_version_number ? ` · Criterios vigentes v${data.criteria_version_number}` : ''}.
          </p>
        </div>
        <div className="ranking-export-actions">
          <button
            type="button"
            className="btn-export-pdf"
            disabled={pdfLoading}
            onClick={() => void handleExportPdf()}
          >
            {pdfLoading ? 'Generando informe PDF...' : 'Exportar informe PDF'}
          </button>
        </div>
      </div>

      {pdfError && (
        <div className="alert-error" role="alert">
          {pdfError}
        </div>
      )}
      {pdfSuccess && (
        <div className="ranking-pdf-status" role="status">
          {pdfSuccess}
        </div>
      )}

      <div className="ranking-recommendation-banner" role="note" aria-label="Aviso de recomendación">
        <strong>Recomendación técnica preliminar (no es aprobación municipal)</strong>
        <span>{data.recommendation_notice}</span>
        {activeWeightsSummary && (
          <p className="ranking-item-meta" style={{ marginTop: '0.4rem', marginBottom: 0 }}>
            <span>
              <strong>Ponderación aplicada (v{data.criteria_version_number ?? 1}):</strong>{' '}
              {activeWeightsSummary.weightsText}.
              {activeWeightsSummary.isEconomicOnly
                ? ' La versión semilla vigente pondera el factor económico al 100.0%, aunque el sistema exige que las 5 dimensiones (económica, social, ambiental, técnica y jurídica) estén completas para ingresar al ranking.'
                : ' El sistema exige que las 5 dimensiones estén completas para ingresar al ranking.'}
            </span>
          </p>
        )}
      </div>

      <div className="ranking-summary-grid">
        <div className="ranking-stat-card">
          <p className="ranking-stat-label">Expedientes comparables</p>
          <p className="ranking-stat-value">{data.comparable_count}</p>
        </div>
        <div className="ranking-stat-card">
          <p className="ranking-stat-label">Ubicados en el mapa</p>
          <p className="ranking-stat-value">{data.mapped_count}</p>
        </div>
        <div className="ranking-stat-card">
          <p className="ranking-stat-label">Sin coordenadas</p>
          <p className="ranking-stat-value">{data.unmapped_count}</p>
        </div>
        <div className="ranking-stat-card">
          <p className="ranking-stat-label">Excluidos (no comparables)</p>
          <p className="ranking-stat-value">{data.excluded_count}</p>
        </div>
      </div>

      <div className="ranking-layout">
        <div className="ranking-panel">
          <h2>Orden propuesto (recomendación)</h2>
          {data.ranked_projects.length === 0 ? (
            <div className="ranking-empty-box">
              No existen expedientes comparables con evaluación multidimensional completa bajo la versión activa de criterios.
            </div>
          ) : (
            <ul className="ranking-list" aria-label="Ranking de proyectos priorizados">
              {data.ranked_projects.map((item) => {
                const isSelected = item.project_id === selectedId
                return (
                  <li key={item.project_id}>
                    <button
                      type="button"
                      className={`ranking-item-btn${isSelected ? ' selected' : ''}`}
                      aria-pressed={isSelected}
                      onClick={() => setSelectedId(item.project_id)}
                    >
                      <div className="ranking-item-top">
                        <span className="ranking-pos-badge">
                          Puesto #{item.rank_position} · {item.code}
                        </span>
                        <span className="ranking-score-pill">
                          {item.global_score.toFixed(2)} / 100
                        </span>
                      </div>
                      <div className="ranking-item-title">{item.title}</div>
                      <div className="ranking-item-meta">
                        <span>Presupuesto: {formatCurrencyPen(item.estimated_budget_pen)}</span>
                        <span>
                          Beneficiarios estimados (ficha):{' '}
                          {item.beneficiaries_count.toLocaleString('es-PE')}
                        </span>
                        <span
                          className={`ranking-geo-badge ${item.has_coordinates ? 'mapped' : 'unmapped'}`}
                        >
                          {item.has_coordinates ? 'Con ubicación en mapa' : 'Sin coordenadas'}
                        </span>
                      </div>
                      <div className="ranking-item-formula">{item.formula}</div>
                      {item.tie_break_reason && (
                        <div className="ranking-item-meta">
                          <span>{item.tie_break_reason}</span>
                        </div>
                      )}
                    </button>
                  </li>
                )
              })}
            </ul>
          )}

          <h2>Proyectos sin coordenadas verificables (lista complementaria)</h2>
          {data.unmapped_projects.length === 0 ? (
            <p className="footer-muted">
              Todos los proyectos comparables cuentan con coordenadas verificadas.
            </p>
          ) : (
            <ul className="ranking-list" aria-label="Proyectos sin coordenadas verificables">
              {data.unmapped_projects.map((item) => {
                const isSelected = item.project_id === selectedId
                return (
                  <li key={`unmapped-${item.project_id}`}>
                    <button
                      type="button"
                      className={`ranking-item-btn${isSelected ? ' selected' : ''}`}
                      aria-pressed={isSelected}
                      onClick={() => setSelectedId(item.project_id)}
                    >
                      <div className="ranking-item-top">
                        <span className="ranking-pos-badge">
                          Puesto #{item.rank_position} · {item.code}
                        </span>
                        <span className="ranking-geo-badge unmapped">
                          Sin coordenadas verificables o sin fuente (no graficado)
                        </span>
                      </div>
                      <div className="ranking-item-title">{item.title}</div>
                      <div className="ranking-item-meta">
                        <span>Puntuación: {item.global_score.toFixed(2)}/100</span>
                        <span>Ubicación declarada: {item.location ?? 'No especificada'}</span>
                      </div>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}

          {data.excluded_projects.length > 0 && (
            <>
              <h2>Expedientes excluidos del ranking ({data.excluded_projects.length})</h2>
              <ul className="ranking-list" aria-label="Expedientes no comparables">
                {data.excluded_projects.map((exc) => (
                  <li key={exc.project_id} className="ranking-excluded-item">
                    <strong>
                      {exc.code} · {exc.title}
                    </strong>{' '}
                    (v{exc.version_number}) — {exc.exclusion_reason}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>

        <div className="ranking-panel">
          <h2>Vista cartográfica de proyectos</h2>
          {data.mapped_projects.length === 0 ? (
            <div className="ranking-empty-box" role="status">
              Ningún proyecto comparable dispone de coordenadas geográficas verificadas. Los proyectos se muestran únicamente en la lista complementaria sin asignar puntos ficticios.
            </div>
          ) : (
            <>
              <div className="ranking-map-wrapper">
                <div
                  ref={mapContainerRef}
                  className="ranking-map-canvas"
                  aria-label="Mapa de proyectos priorizados"
                />
              </div>
              {tileWarning && (
                <div className="ranking-excluded-item" role="alert">
                  <strong>Aviso de cartografía:</strong> No se pudieron cargar algunas teselas de OpenStreetMap desde el navegador (el servicio de mapas externo puede estar bloqueado o con límite de tasa). La lista de ranking y las coordenadas verificadas siguen operativas.
                </div>
              )}
              <p className="ranking-attribution">
                Cartografía base: © OpenStreetMap contributors. Las coordenadas se muestran únicamente cuando tienen fuente registrada; nunca se infieren puntos ficticios.
              </p>
            </>
          )}

          {selectedProject && (
            <div className="ranking-selected-card" aria-live="polite">
              <h3>
                Seleccionado: #{selectedProject.rank_position} · {selectedProject.title} (
                {selectedProject.code})
              </h3>
              <p>
                <strong>Posición en el ranking:</strong> #{selectedProject.rank_position} de{' '}
                {data.comparable_count} (Recomendación · {selectedProject.global_score.toFixed(2)}/100 pts)
              </p>
              <p>
                <strong>Justificación:</strong> {selectedProject.explanation}
              </p>
              <p>
                <strong>Fórmula:</strong> {selectedProject.formula}
              </p>
              {selectedProject.has_coordinates ? (
                <p>
                  <strong>Coordenadas:</strong> {selectedProject.latitude?.toFixed(6)},{' '}
                  {selectedProject.longitude?.toFixed(6)} · Origen:{' '}
                  {selectedProject.coordinates_origin ?? 'declared'}
                  {selectedProject.coordinates_source
                    ? ` (${selectedProject.coordinates_source})`
                    : ''}
                </p>
              ) : (
                <p>
                  <strong>Ubicación cartográfica:</strong> Sin coordenadas verificables. Se mantiene en la lista complementaria sin punto ficticio.
                </p>
              )}
            </div>
          )}

          {selectedProject && (
            <div className="geo-layers-card" aria-label="Capas geoespaciales del proyecto">
              <h3>Capas geoespaciales (HU4.2) · {selectedProject.code}</h3>
              {layersLoading && <p className="geo-layer-meta">Cargando capas territoriales...</p>}
              {layersError && (
                <div className="alert-error" role="alert">
                  {layersError}
                </div>
              )}
              {!layersLoading && !layersError && layersData && (
                <>
                  {layersData.layers.length === 0 ? (
                    <div className="ranking-empty-box" role="status">
                      {layersData.empty_message ??
                        'Sin geometría geoespacial verificada para esta versión; no se dibuja ningún área de influencia ficticia.'}
                    </div>
                  ) : (
                    <>
                      <ul className="geo-layer-list">
                        {layersData.layers.map((layer) => {
                          const isAvailable = layer.availability_status === 'available'
                          const checked = Boolean(activeLayerIds[layer.layer_id])
                          return (
                            <li key={layer.layer_id} className="geo-layer-item">
                              <label className="geo-layer-toggle">
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  disabled={!isAvailable}
                                  onChange={() => handleToggleLayer(layer)}
                                />
                                <span
                                  className={`geo-legend-swatch ${layer.layer_type}`}
                                  aria-hidden="true"
                                />
                                <span>
                                  {layer.layer_name} ({layer.layer_type_label})
                                </span>
                              </label>
                              <p className="geo-layer-meta">
                                <strong>Significado:</strong> {layer.meaning}
                              </p>
                              <p className="geo-layer-meta">
                                <strong>Fuente:</strong> {layer.source_name} ·{' '}
                                <strong>Origen:</strong> {layer.data_origin}
                                {layer.aggregate_beneficiaries_count !== null
                                  ? ` · Población agregada: ${layer.aggregate_beneficiaries_count.toLocaleString('es-PE')} hab.`
                                  : ' · Beneficiarios efectivos en polígono: no medidos (la cifra del expediente es estimación de su ficha)'}
                              </p>
                              {!isAvailable && (
                                <p className="geo-layer-meta" role="status">
                                  Geometría no disponible; no se grafica ningún polígono ficticio.
                                </p>
                              )}
                            </li>
                          )
                        })}
                      </ul>
                      {!layersData.has_available_geometry && layersData.empty_message && (
                        <div className="ranking-empty-box" role="status">
                          {layersData.empty_message}
                        </div>
                      )}
                    </>
                  )}
                  <p className="ranking-attribution">{layersData.privacy_notice}</p>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
