import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { createProject } from '../../api/pmv1.api'
import type { ProjectInput } from '../../types/proyecto'
import { newIdempotencyKey } from '../../utils/idempotency'
import {
  PHYSICAL_EXPEDIENTE_MAX_BYTES,
  savePhysicalExpediente,
} from '../../utils/physicalExpedienteStorage'
import { formatFileSize } from '../../utils/format'
import { EMPTY_PROYECTO_FORM } from './constants'
import {
  normalizeProyectoForm,
  validateProyecto,
  type ProyectoFieldErrors,
} from './validateProyecto'

export function useRegistrarProyecto() {
  const navigate = useNavigate()
  const [form, setForm] = useState<ProjectInput>(EMPTY_PROYECTO_FORM)
  const [fieldErrors, setFieldErrors] = useState<ProyectoFieldErrors>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [physicalFile, setPhysicalFileState] = useState<File | null>(null)
  const [physicalFileError, setPhysicalFileError] = useState<string | null>(null)

  const setPhysicalFile = useCallback((file: File | null) => {
    if (file && file.size > PHYSICAL_EXPEDIENTE_MAX_BYTES) {
      setPhysicalFileState(null)
      setPhysicalFileError(
        `El archivo supera ${formatFileSize(PHYSICAL_EXPEDIENTE_MAX_BYTES)}. Elija uno más liviano.`,
      )
      return
    }
    setPhysicalFileError(null)
    setPhysicalFileState(file)
  }, [])

  const updateField = useCallback(<K extends keyof ProjectInput>(key: K, value: ProjectInput[K]) => {
    setForm((prev) => ({ ...prev, [key]: value }))
    setFieldErrors((prev) => {
      const next = { ...prev }
      delete next[key]
      return next
    })
  }, [])

  const resetForm = useCallback(() => {
    setForm(EMPTY_PROYECTO_FORM)
    setFieldErrors({})
    setSubmitError(null)
    setPhysicalFileState(null)
    setPhysicalFileError(null)
  }, [])

  const submitWithMode = useCallback(
    async (asDraft: boolean) => {
      setSubmitError(null)
      const errors = validateProyecto(form, { asDraft })
      setFieldErrors(errors)
      if (Object.keys(errors).length > 0) return

      setLoading(true)
      try {
        const payload = normalizeProyectoForm(form)
        const project = await createProject(
          {
            title: payload.nombre,
            description: payload.descripcion || undefined,
            location: payload.ubicacion || undefined,
            proposed_land_use: payload.usoSueloPropuesto || undefined,
            estimated_budget_pen: payload.presupuesto > 0 ? payload.presupuesto : undefined,
            beneficiaries_count: payload.beneficiarios > 0 ? payload.beneficiarios : undefined,
            save_as_draft: asDraft,
          },
          newIdempotencyKey('proj'),
        )

        let mensaje =
          project.status === 'draft'
            ? `Borrador guardado en el servidor en estado pendiente de completar (${project.code}, ID: ${project.project_id}). Complete los datos obligatorios antes de evaluar.`
            : `Expediente registrado en el servidor. Código: ${project.code} · ID: ${project.project_id} · Versión: v${project.version_number}. Puede consultarlo posteriormente sin volver a cargarlo.`
        if (physicalFile) {
          try {
            await savePhysicalExpediente(project.project_id, physicalFile)
            mensaje = `Expediente ${project.code} registrado en el servidor (v${project.version_number}). Archivo adjunto "${physicalFile.name}" conservado solo localmente en este navegador.`
          } catch (fileErr) {
            mensaje =
              fileErr instanceof Error
                ? `Proyecto creado, pero no se guardó el archivo: ${fileErr.message}`
                : 'Proyecto creado, pero no se pudo guardar el archivo adjunto.'
          }
        }

        navigate(`/proyectos/${project.project_id}`, {
          state: { mensaje },
        })
      } catch (err) {
        setSubmitError(err instanceof Error ? err.message : 'No se pudo registrar el proyecto.')
      } finally {
        setLoading(false)
      }
    },
    [form, navigate, physicalFile],
  )

  const submit = useCallback(() => submitWithMode(false), [submitWithMode])
  const saveDraft = useCallback(() => submitWithMode(true), [submitWithMode])

  return {
    form,
    fieldErrors,
    submitError,
    loading,
    physicalFile,
    physicalFileError,
    updateField,
    setPhysicalFile,
    resetForm,
    submit,
    saveDraft,
  }
}
