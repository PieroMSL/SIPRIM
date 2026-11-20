/* oxlint-disable react/only-export-components -- el proveedor y su hook forman una API unica */
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { getCurrentSessionMe } from '../../api/adminAuth.api'
import { loginApi } from '../../api/auth.api'
import {
  clearAccessToken,
  hasUsableAccessToken,
  saveAccessToken,
  subscribeSessionExpired,
} from '../../api/authStorage'
import type { AuthSession, AuthUser, LoginCredentials } from '../../types/auth'
import { roleGrantsAccess } from '../../utils/roles'
import { clearAuthSession, loadAuthSession, saveAuthSession } from './authStorage'

interface AuthContextValue {
  user: AuthUser | null
  isAuthenticated: boolean
  authNotice: string | null
  roleUpdateNotice: string | null
  clearAuthNotice: () => void
  clearRoleUpdateNotice: () => void
  refreshSessionRoles: () => Promise<void>
  hasRole: (role: string) => boolean
  login: (credentials: LoginCredentials) => Promise<string | null>
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

function readInitialAuthState(): { session: AuthSession | null; notice: string | null } {
  const storedSession = loadAuthSession()
  if (!storedSession) {
    if (!hasUsableAccessToken()) {
      clearAccessToken()
    }
    return { session: null, notice: null }
  }
  if (!hasUsableAccessToken()) {
    clearAuthSession()
    return {
      session: null,
      notice: 'Su sesión ha caducado o ya no es válida. Vuelva a iniciar sesión.',
    }
  }
  return { session: storedSession, notice: null }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [initialState] = useState(() => readInitialAuthState())
  const [session, setSession] = useState<AuthSession | null>(initialState.session)
  const [authNotice, setAuthNotice] = useState<string | null>(initialState.notice)
  const [roleUpdateNotice, setRoleUpdateNotice] = useState<string | null>(null)

  useEffect(() => {
    return subscribeSessionExpired((message) => {
      clearAuthSession()
      setSession(null)
      setRoleUpdateNotice(null)
      setAuthNotice(message)
    })
  }, [])

  const clearAuthNotice = useCallback(() => {
    setAuthNotice(null)
  }, [])

  const clearRoleUpdateNotice = useCallback(() => {
    setRoleUpdateNotice(null)
  }, [])

  const refreshSessionRoles = useCallback(async () => {
    if (!hasUsableAccessToken()) return
    try {
      const me = await getCurrentSessionMe()
      setSession((prev) => {
        if (!prev) return prev
        const prevSorted = [...prev.user.roles].sort().join(',')
        const nextSorted = [...me.roles].sort().join(',')
        if (prevSorted === nextSorted) return prev
        const updated: AuthSession = {
          ...prev,
          user: { ...prev.user, roles: me.roles },
        }
        saveAuthSession(updated)
        setRoleUpdateNotice(
          me.roles.length
            ? `Sus permisos vigentes fueron actualizados por la administración: ${me.roles.join(', ')}.`
            : 'Sus roles fueron revocados por la administración. Algunas funciones ya no están disponibles.',
        )
        return updated
      })
    } catch {
      /* 401 es manejado por apiFetch/subscribeSessionExpired */
    }
  }, [])

  const currentUserId = session?.user.id ?? null

  useEffect(() => {
    if (!currentUserId) return
    const timer = window.setTimeout(() => {
      void refreshSessionRoles()
    }, 0)
    const onFocus = () => {
      void refreshSessionRoles()
    }
    window.addEventListener('focus', onFocus)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [currentUserId, refreshSessionRoles])

  const login = useCallback(async (credentials: LoginCredentials) => {
    setAuthNotice(null)
    try {
      const response = await loginApi(credentials)
      saveAccessToken(response.access_token)

      const user: AuthUser = {
        id: response.user_id,
        nombre: response.full_name,
        email: credentials.email.trim().toLowerCase(),
        roles: response.roles,
      }

      const next: AuthSession = {
        user,
        loggedInAt: new Date().toISOString(),
      }
      saveAuthSession(next)
      setSession(next)
      return null
    } catch (err) {
      clearAuthSession()
      setSession(null)
      const message = err instanceof Error ? err.message : 'No se pudo iniciar sesión.'
      if (message.toLowerCase().includes('credenciales') || message.includes('401')) {
        return 'Correo o contraseña incorrectos. Use las credenciales asignadas por la municipalidad.'
      }
      if (message.includes('no respondió a tiempo')) {
        return (
          'El servicio de autenticación no respondió. Compruebe que la API esté en ' +
          'http://127.0.0.1:8000/health y que PostgreSQL (auth_db) esté disponible.'
        )
      }
      if (message.includes('No se pudo conectar')) {
        return 'No hay conexión con la API en el puerto 8000. Inicie el backend antes de iniciar sesión.'
      }
      return message
    }
  }, [])

  const logout = useCallback(() => {
    clearAccessToken()
    clearAuthSession()
    setSession(null)
    setAuthNotice(null)
  }, [])

  const hasRole = useCallback(
    (role: string) => {
      const roles = session?.user.roles ?? []
      return roleGrantsAccess(roles, role)
    },
    [session],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      user: session?.user ?? null,
      isAuthenticated: session !== null && hasUsableAccessToken(),
      authNotice,
      roleUpdateNotice,
      clearAuthNotice,
      clearRoleUpdateNotice,
      refreshSessionRoles,
      hasRole,
      login,
      logout,
    }),
    [
      session,
      authNotice,
      roleUpdateNotice,
      clearAuthNotice,
      clearRoleUpdateNotice,
      refreshSessionRoles,
      hasRole,
      login,
      logout,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth debe usarse dentro de AuthProvider')
  }
  return ctx
}
