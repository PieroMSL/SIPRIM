import type { ProjectInput } from '../../types/proyecto'

export type ProyectoFieldErrors = Partial<Record<keyof ProjectInput, string>>

export function validateProyecto(
  form: ProjectInput,
  options: { asDraft?: boolean } = {},
): ProyectoFieldErrors {
  const errors: ProyectoFieldErrors = {}
  const asDraft = Boolean(options.asDraft)

  if (!form.nombre.trim()) errors.nombre = 'El nombre es obligatorio.'
  else if (form.nombre.length > 200) errors.nombre = 'Máximo 200 caracteres.'

  if (!asDraft && !form.descripcion.trim()) {
    errors.descripcion = 'La descripción es obligatoria.'
  } else if (form.descripcion.length > 2000) {
    errors.descripcion = 'Máximo 2000 caracteres.'
  }

  if (!asDraft && !(form.ubicacion ?? '').trim()) {
    errors.ubicacion = 'La ubicación es obligatoria.'
  } else if ((form.ubicacion ?? '').length > 300) {
    errors.ubicacion = 'Máximo 300 caracteres.'
  }

  if (!asDraft && !(form.usoSueloPropuesto ?? '').trim()) {
    errors.usoSueloPropuesto = 'El uso de suelo propuesto es obligatorio.'
  } else if ((form.usoSueloPropuesto ?? '').length > 150) {
    errors.usoSueloPropuesto = 'Máximo 150 caracteres.'
  }

  if (!asDraft && (!Number.isFinite(form.presupuesto) || form.presupuesto <= 0)) {
    errors.presupuesto = 'Ingrese un presupuesto mayor a cero.'
  } else if (asDraft && form.presupuesto !== 0 && (!Number.isFinite(form.presupuesto) || form.presupuesto <= 0)) {
    errors.presupuesto = 'Si ingresa presupuesto en el borrador, debe ser mayor a cero.'
  }

  if (!asDraft && (!Number.isInteger(form.beneficiarios) || form.beneficiarios <= 0)) {
    errors.beneficiarios = 'Ingrese un número entero de beneficiarios mayor a cero.'
  } else if (asDraft && form.beneficiarios !== 0 && (!Number.isInteger(form.beneficiarios) || form.beneficiarios <= 0)) {
    errors.beneficiarios = 'Si ingresa beneficiarios en el borrador, debe ser un entero mayor a cero.'
  }

  return errors
}

export function normalizeProyectoForm(form: ProjectInput): ProjectInput {
  return {
    nombre: form.nombre.trim(),
    descripcion: form.descripcion.trim(),
    ubicacion: (form.ubicacion ?? '').trim(),
    usoSueloPropuesto: (form.usoSueloPropuesto ?? '').trim(),
    presupuesto: Number(form.presupuesto),
    beneficiarios: Number(form.beneficiarios),
  }
}
