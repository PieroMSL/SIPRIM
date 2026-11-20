import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useAuth } from '../../features/auth/AuthContext'
import { useApiHealth } from '../../hooks/useApiHealth'
import { ApiStatusPill } from './ApiStatusPill'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  isActive ? 'nav-link active' : 'nav-link'

function formatRoles(roles: string[]): string {
  const labels: Record<string, string> = {
    PLANNER: 'Planificador',
    PLANNING_MANAGER: 'Gerente de Planeamiento',
    LEGAL_ADVISOR: 'Asesor jurídico',
    ADMIN: 'Administrador',
    MAYOR: 'Alcalde',
    AUDITOR: 'Auditor / Contraloría',
    SUPERADMIN: 'Superadministrador',
  }
  return roles.map((r) => labels[r] ?? r).join(' · ')
}

export function Layout() {
  const apiOnline = useApiHealth()
  const { user, logout, hasRole, roleUpdateNotice, clearRoleUpdateNotice } = useAuth()
  const navigate = useNavigate()

  const handleLogout = () => {
    logout()
    navigate('/login', { replace: true })
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="header-inner">
          <div className="brand">
            <span className="brand-mark" aria-hidden>
              S
            </span>
            <div>
              <p className="brand-title">SIPRIM</p>
              <p className="brand-subtitle">
                Priorización de proyectos de inversión municipal
              </p>
            </div>
          </div>
          <nav className="main-nav" aria-label="Principal">
            <NavLink to="/" end className={navLinkClass}>
              Inicio
            </NavLink>
            {(hasRole('PLANNER') || hasRole('ADMIN')) && (
              <NavLink to="/registrar" className={navLinkClass}>
                Cargar expediente
              </NavLink>
            )}
            <NavLink to="/consultar" className={navLinkClass}>
              Consultar
            </NavLink>
            <NavLink to="/integracion-ia" className={navLinkClass}>
              Integración IA
            </NavLink>
            {(hasRole('PLANNER') || hasRole('ADMIN')) && <NavLink to="/prediccion-ml" className={navLinkClass}>Predicciones ML</NavLink>}
            {(hasRole('MAYOR') || hasRole('ADMIN') || hasRole('PLANNER')) && (
              <NavLink to="/ranking-mapa" className={navLinkClass}>
                Ranking y Mapa
              </NavLink>
            )}
            <NavLink to="/pruebas-metricas" className={navLinkClass}>
              Pruebas y Métricas
            </NavLink>
            {hasRole('LEGAL_ADVISOR') && (
              <NavLink to="/normativa" className={navLinkClass}>
                Normativa
              </NavLink>
            )}
            {(hasRole('AUDITOR') || hasRole('ADMIN')) && (
              <NavLink to="/auditoria/historial" className={navLinkClass}>
                Auditoría e historial
              </NavLink>
            )}
            {hasRole('ADMIN') && (
              <NavLink to="/admin/roles" className={navLinkClass}>
                Roles y usuarios
              </NavLink>
            )}
          </nav>
          <div className="user-menu">
            <ApiStatusPill apiOnline={apiOnline} />
            {user && (
              <>
                <div className="user-info">
                  <p className="user-name">{user.nombre}</p>
                  <p className="user-role">{formatRoles(user.roles ?? [])}</p>
                </div>
                <button type="button" className="btn-logout" onClick={handleLogout}>
                  Cerrar sesión
                </button>
              </>
            )}
          </div>
        </div>
      </header>

      <main className="app-main">
        {roleUpdateNotice && (
          <div
            role="status"
            style={{
              maxWidth: '1200px',
              margin: '0 auto 1rem',
              padding: '0.75rem 1rem',
              borderRadius: '8px',
              background: '#fef3c7',
              border: '1px solid #f59e0b',
              color: '#92400e',
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'center',
              gap: '0.75rem',
            }}
          >
            <span>{roleUpdateNotice}</span>
            <button
              type="button"
              onClick={clearRoleUpdateNotice}
              style={{
                background: 'transparent',
                border: '1px solid #d97706',
                borderRadius: '6px',
                padding: '0.25rem 0.6rem',
                color: '#92400e',
                cursor: 'pointer',
              }}
            >
              Entendido
            </button>
          </div>
        )}
        <Outlet />
      </main>

      <footer className="app-footer">
        <p>
          Municipalidad Distrital de El Tambo · Soporte a la preevaluación de inversiones
          (Invierte.pe)
        </p>
        <p className="footer-muted">
          Las decisiones finales permanecen en manos de las autoridades y especialistas
          municipales.
        </p>
      </footer>
    </div>
  )
}
