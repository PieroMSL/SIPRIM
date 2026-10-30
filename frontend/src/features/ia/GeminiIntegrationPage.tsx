import { useState, type FormEvent } from 'react'
import { apiFetch } from '../../api/client'
import { Alert } from '../../components/ui/Alert'
import { FormField } from '../../components/ui/FormField'
import { PageHeader } from '../../components/ui/PageHeader'

interface ProyectoRegistrado {
  proyecto: { id: number; nombre: string; codigo: string }
}

interface DictamenIA {
  puntaje: number
  viabilidad: 'ALTA' | 'MEDIA' | 'BAJA'
  justificacion: string
  observaciones: string[]
  recomendaciones: string[]
}

const initialForm = {
  nombre: '',
  descripcion: '',
  ubicacion: '',
  presupuesto: '',
  beneficiarios: '',
  tipo_proyecto: '',
}

export function GeminiIntegrationPage() {
  const [form, setForm] = useState(initialForm)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dictamen, setDictamen] = useState<DictamenIA | null>(null)
  const [proyecto, setProyecto] = useState<ProyectoRegistrado['proyecto'] | null>(null)

  const update = (field: keyof typeof initialForm, value: string) => {
    setForm((previous) => ({ ...previous, [field]: value }))
    setDictamen(null)
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (loading) return
    setError(null)
    setDictamen(null)

    const presupuesto = Number(form.presupuesto)
    const beneficiarios = Number(form.beneficiarios)
    if (
      !form.nombre.trim() || !form.descripcion.trim() || !form.ubicacion.trim() ||
      !form.tipo_proyecto.trim() || !Number.isFinite(presupuesto) ||
      presupuesto <= 0 || !Number.isInteger(beneficiarios) || beneficiarios <= 0
    ) {
      setError('Complete todos los datos. Presupuesto y beneficiarios deben ser mayores que cero.')
      return
    }

    setLoading(true)
    try {
      // Se usa el backend real: el proyecto se registra y después Gemini genera el dictamen.
      // Si la evaluación falla, se conserva la referencia del expediente registrado.
      const registro = await apiFetch<ProyectoRegistrado>('/api/v1/proyectos', {
        method: 'POST',
        body: JSON.stringify({
          nombre: form.nombre.trim(),
          descripcion: form.descripcion.trim(),
          ubicacion: form.ubicacion.trim(),
          presupuesto,
          beneficiarios,
          tipo_proyecto: form.tipo_proyecto.trim(),
        }),
        timeoutMs: 45000,
      })
      setProyecto(registro.proyecto)
      const resultado = await apiFetch<DictamenIA>(
        `/api/v1/proyectos/${registro.proyecto.id}/evaluacion-ia`,
        { method: 'POST', timeoutMs: 120000 },
      )
      setDictamen(resultado)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'No se pudo completar la evaluación con IA.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="form-page">
      <PageHeader
        title="Evaluación preliminar con IA"
        description="Registre un proyecto, valide sus datos y solicite un dictamen orientativo generado por Gemini."
      />
      <form className="expediente-form" onSubmit={submit}>
        <h3>Registrar expediente para evaluación IA</h3>
        <p>Esta evaluación complementa la validación y evaluación económica de Cargar expediente. El dictamen de IA es orientativo y requiere revisión humana.</p>
        {error && <Alert variant="error">{error}</Alert>}
        {proyecto && (
          <Alert variant="success">
            Proyecto registrado: {proyecto.codigo} (ID {proyecto.id}).
            {error && ' El registro se conservó, aunque la evaluación IA no terminó.'}
          </Alert>
        )}
        <div className="form-grid">
          <FormField label="Nombre del proyecto *" value={form.nombre} maxLength={200}
            onChange={(event) => update('nombre', event.target.value)} required />
          <FormField label="Descripción y objetivo *" as="textarea" rows={3}
            value={form.descripcion} maxLength={2000}
            onChange={(event) => update('descripcion', event.target.value)} required />
          <FormField label="Ubicación *" value={form.ubicacion} maxLength={300}
            onChange={(event) => update('ubicacion', event.target.value)} required />
          <FormField label="Presupuesto (S/) *" type="number" min="0.01" step="0.01"
            value={form.presupuesto} onChange={(event) => update('presupuesto', event.target.value)} required />
          <FormField label="Beneficiarios *" type="number" min="1" step="1"
            value={form.beneficiarios} onChange={(event) => update('beneficiarios', event.target.value)} required />
          <FormField label="Tipo de proyecto *" value={form.tipo_proyecto} maxLength={150}
            onChange={(event) => update('tipo_proyecto', event.target.value)} required />
        </div>
        <div className="form-actions">
          <button type="submit" className="btn btn-primary" disabled={loading}>
            {loading ? 'Consultando a Gemini…' : 'Registrar, validar y evaluar con IA'}
          </button>
        </div>
        {dictamen && (
          <section aria-label="Dictamen de Gemini">
            <h3>Dictamen preliminar de Gemini</h3>
            <p><strong>Puntaje:</strong> {dictamen.puntaje}/100 · <strong>Viabilidad:</strong> {dictamen.viabilidad}</p>
            <p><strong>Justificación:</strong> {dictamen.justificacion}</p>
            <h4>Observaciones</h4>
            {dictamen.observaciones.length ? (
              <ul>{dictamen.observaciones.map((item, i) => <li key={i}>{item}</li>)}</ul>
            ) : <p>Sin observaciones adicionales.</p>}
            <h4>Recomendaciones</h4>
            {dictamen.recomendaciones.length ? (
              <ul>{dictamen.recomendaciones.map((item, i) => <li key={i}>{item}</li>)}</ul>
            ) : <p>Sin recomendaciones adicionales.</p>}
            <p>El resultado no reemplaza la decisión de los especialistas municipales.</p>
          </section>
        )}
      </form>
    </div>
  )
}
