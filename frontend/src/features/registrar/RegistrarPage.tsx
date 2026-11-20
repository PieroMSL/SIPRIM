import { PageHeader } from '../../components/ui/PageHeader'
import { ExpedienteForm } from './ExpedienteForm'
import { useRegistrarProyecto } from './useRegistrarProyecto'

export function RegistrarPage() {
  const {
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
  } = useRegistrarProyecto()

  return (
    <div className="form-page">
      <PageHeader
        title="Registrar proyecto"
        description="Complete los datos mínimos del PMV 1 o guarde un borrador incompleto para completarlo antes de evaluar (HU1.1, HU1.2)."
      />
      <ExpedienteForm
        form={form}
        fieldErrors={fieldErrors}
        submitError={submitError}
        loading={loading}
        onFieldChange={updateField}
        onSubmit={submit}
        onSaveDraft={saveDraft}
        onReset={resetForm}
        submitLabel="Registrar proyecto"
        physicalFile={physicalFile}
        onPhysicalFileChange={setPhysicalFile}
        physicalFileError={physicalFileError}
      />
    </div>
  )
}
