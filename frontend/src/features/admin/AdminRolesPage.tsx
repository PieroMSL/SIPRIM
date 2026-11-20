import { useCallback, useEffect, useState } from 'react'
import {
  getAdminUsers,
  updateAdminUserRoles,
  type MunicipalUserItem,
  type RoleCatalogItem,
} from '../../api/adminAuth.api'
import { Alert } from '../../components/ui/Alert'
import { useAuth } from '../auth/AuthContext'

const ROLE_LABELS: Record<string, string> = {
  ADMIN: 'Administrador',
  PLANNER: 'Planificador',
  PLANNING_MANAGER: 'Gerente de Planeamiento',
  LEGAL_ADVISOR: 'Asesor jurídico',
  MAYOR: 'Alcalde',
  AUDITOR: 'Auditor / Contraloría',
  SUPERADMIN: 'Superadministrador',
}

export function AdminRolesPage() {
  const { user: currentUser, refreshSessionRoles } = useAuth()
  const actorIsSuperadmin = (currentUser?.roles ?? []).includes('SUPERADMIN')

  const [users, setUsers] = useState<MunicipalUserItem[]>([])
  const [availableRoles, setAvailableRoles] = useState<RoleCatalogItem[]>([])
  const [draftRoles, setDraftRoles] = useState<Record<string, string[]>>({})
  const [loading, setLoading] = useState(true)
  const [savingId, setSavingId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const loadData = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await getAdminUsers()
      setUsers(data.users)
      setAvailableRoles(data.available_roles)
      const nextDrafts: Record<string, string[]> = {}
      for (const u of data.users) {
        nextDrafts[u.id] = [...u.roles]
      }
      setDraftRoles(nextDrafts)
    } catch (err) {
      setError(
        err instanceof Error ? err.message : 'No se pudo cargar la lista de usuarios municipales.',
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    getAdminUsers()
      .then((data) => {
        if (cancelled) return
        setUsers(data.users)
        setAvailableRoles(data.available_roles)
        const nextDrafts: Record<string, string[]> = {}
        for (const u of data.users) {
          nextDrafts[u.id] = [...u.roles]
        }
        setDraftRoles(nextDrafts)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(
          err instanceof Error ? err.message : 'No se pudo cargar la lista de usuarios municipales.',
        )
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const toggleRole = (userId: string, roleCode: string) => {
    setSuccess(null)
    setError(null)
    setDraftRoles((prev) => {
      const current = prev[userId] ?? []
      const exists = current.includes(roleCode)
      const updated = exists ? current.filter((r) => r !== roleCode) : [...current, roleCode]
      return { ...prev, [userId]: updated.sort() }
    })
  }

  const handleSave = async (targetUser: MunicipalUserItem) => {
    setSavingId(targetUser.id)
    setError(null)
    setSuccess(null)
    try {
      const selected = draftRoles[targetUser.id] ?? []
      const res = await updateAdminUserRoles(targetUser.id, selected)
      setUsers((prev) => prev.map((u) => (u.id === targetUser.id ? res.user : u)))
      setDraftRoles((prev) => ({ ...prev, [targetUser.id]: [...res.user.roles] }))
      setSuccess(
        `Roles actualizados para ${res.user.full_name} (${res.user.email}): ${
          res.user.roles.length ? res.user.roles.join(', ') : 'Sin roles asignados'
        }.`,
      )
      if (currentUser && res.user.id === currentUser.id) {
        await refreshSessionRoles()
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'No se pudieron actualizar los roles.')
    } finally {
      setSavingId(null)
    }
  }

  return (
    <section className="page-container">
      <header className="page-header">
        <div>
          <h1>Administración de roles municipales</h1>
          <p className="page-subtitle">
            Asigne, modifique o revoque los roles vigentes de cada cuenta municipal. Los cambios
            surten efecto inmediato en los permisos del sistema.
          </p>
        </div>
        <button
          type="button"
          className="btn btn-secondary"
          onClick={() => void loadData()}
          disabled={loading}
        >
          {loading ? 'Actualizando…' : 'Actualizar lista'}
        </button>
      </header>

      <div
        className="card"
        style={{
          marginBottom: '1rem',
          padding: '0.85rem 1rem',
          background: '#f8fafc',
          border: '1px solid #cbd5e1',
          borderRadius: '8px',
          fontSize: '0.9rem',
        }}
      >
        <strong>Política de privilegios:</strong> Solo una cuenta <code>SUPERADMIN</code> puede
        asignar o modificar el rol <code>SUPERADMIN</code>. No se permite dejar al sistema sin un
        administrador activo ni asignar roles a cuentas inactivas.
      </div>

      {error && <Alert variant="error">{error}</Alert>}
      {success && <Alert variant="success">{success}</Alert>}

      {loading ? (
        <p>Cargando usuarios municipales…</p>
      ) : (
        <div style={{ display: 'grid', gap: '0.85rem', marginTop: '0.75rem' }}>
          {users.map((u) => {
            const currentDraft = draftRoles[u.id] ?? u.roles
            const targetIsSuperadmin = u.roles.includes('SUPERADMIN')
            const lockedByPrivilege = targetIsSuperadmin && !actorIsSuperadmin
            const unchanged =
              currentDraft.length === u.roles.length &&
              currentDraft.every((r) => u.roles.includes(r))

            return (
              <article
                key={u.id}
                style={{
                  background: '#ffffff',
                  border: '1px solid #cbd5e1',
                  borderRadius: '10px',
                  padding: '1rem 1.15rem',
                  display: 'grid',
                  gap: '0.65rem',
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
                  <div>
                    <strong style={{ fontSize: '1.02rem' }}>{u.full_name}</strong>{' '}
                    <span style={{ color: '#475569', fontSize: '0.9rem' }}>({u.email})</span>
                  </div>
                  <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'center' }}>
                    <span
                      style={{
                        padding: '0.2rem 0.55rem',
                        borderRadius: '999px',
                        fontSize: '0.78rem',
                        fontWeight: 600,
                        background: u.is_active ? '#dcfce7' : '#fee2e2',
                        color: u.is_active ? '#166534' : '#991b1b',
                      }}
                    >
                      {u.is_active ? 'Cuenta activa' : 'Cuenta inactiva'}
                    </span>
                    {u.roles.length === 0 ? (
                      <span
                        style={{
                          padding: '0.2rem 0.55rem',
                          borderRadius: '999px',
                          fontSize: '0.78rem',
                          background: '#f1f5f9',
                          color: '#475569',
                        }}
                      >
                        Sin roles
                      </span>
                    ) : (
                      u.roles.map((r) => (
                        <span
                          key={r}
                          style={{
                            padding: '0.2rem 0.55rem',
                            borderRadius: '999px',
                            fontSize: '0.78rem',
                            fontWeight: 600,
                            background: r === 'SUPERADMIN' ? '#fef3c7' : '#e0f2fe',
                            color: r === 'SUPERADMIN' ? '#92400e' : '#075985',
                          }}
                        >
                          {ROLE_LABELS[r] ?? r}
                        </span>
                      ))
                    )}
                  </div>
                </div>

                <div
                  style={{
                    display: 'flex',
                    flexWrap: 'wrap',
                    gap: '0.85rem',
                    alignItems: 'center',
                  }}
                >
                  {availableRoles.map((role) => {
                    const checked = currentDraft.includes(role.code)
                    const disabled =
                      !u.is_active ||
                      lockedByPrivilege ||
                      (role.privileged && !actorIsSuperadmin) ||
                      savingId === u.id

                    return (
                      <label
                        key={role.code}
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '0.35rem',
                          fontSize: '0.9rem',
                          opacity: disabled ? 0.55 : 1,
                          cursor: disabled ? 'not-allowed' : 'pointer',
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={disabled}
                          onChange={() => toggleRole(u.id, role.code)}
                        />
                        <span>
                          {role.name} (<code>{role.code}</code>)
                        </span>
                      </label>
                    )
                  })}
                </div>

                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    alignItems: 'center',
                    gap: '0.5rem',
                    flexWrap: 'wrap',
                  }}
                >
                  <span style={{ fontSize: '0.8rem', color: '#64748b' }}>
                    {lockedByPrivilege
                      ? 'Cuenta SUPERADMIN protegida: solo otro SUPERADMIN puede modificarla.'
                      : !u.is_active
                        ? 'Cuenta inactiva: no se pueden asignar roles.'
                        : 'Seleccione los roles vigentes y guarde los cambios.'}
                  </span>
                  <button
                    type="button"
                    className="btn btn-primary"
                    disabled={savingId === u.id || !u.is_active || lockedByPrivilege || unchanged}
                    onClick={() => void handleSave(u)}
                  >
                    {savingId === u.id ? 'Guardando…' : 'Guardar roles'}
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
