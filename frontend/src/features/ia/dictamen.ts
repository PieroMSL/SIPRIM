export interface DictamenIA {
  puntaje: number
  viabilidad: 'ALTA' | 'MEDIA' | 'BAJA'
  justificacion: string
  observaciones: string[]
  recomendaciones: string[]
}

/** Valida datos HTTP desconocidos antes de representarlos como dictamen. */
export function parseDictamenIA(value: unknown): DictamenIA {
  const invalid = () => new Error('El servicio IA devolvió un dictamen inválido. Intente nuevamente o solicite revisión técnica.')
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw invalid()
  const d = value as Record<string, unknown>
  const keys = ['puntaje', 'viabilidad', 'justificacion', 'observaciones', 'recomendaciones']
  if (Object.keys(d).some((key) => !keys.includes(key))) throw invalid()
  if (typeof d.puntaje !== 'number' || !Number.isFinite(d.puntaje) || d.puntaje < 0 || d.puntaje > 100) throw invalid()
  const expected = d.puntaje >= 70 ? 'ALTA' : d.puntaje >= 40 ? 'MEDIA' : 'BAJA'
  if (d.viabilidad !== expected || typeof d.justificacion !== 'string' || d.justificacion.trim().length < 10) throw invalid()
  const isTextList = (v: unknown): v is string[] => Array.isArray(v) && v.every((s) => typeof s === 'string' && s.trim().length > 0)
  if (!isTextList(d.observaciones) || !isTextList(d.recomendaciones)) throw invalid()
  return {
    puntaje: d.puntaje, viabilidad: expected, justificacion: d.justificacion.trim(),
    observaciones: d.observaciones.map((s) => s.trim()),
    recomendaciones: d.recomendaciones.map((s) => s.trim()),
  }
}
