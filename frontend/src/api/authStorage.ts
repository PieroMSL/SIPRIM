const TOKEN_KEY = 'siprim:access-token'

type SessionExpiredListener = (message: string) => void
const sessionExpiredListeners = new Set<SessionExpiredListener>()

export function loadAccessToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function saveAccessToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearAccessToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export function hasUsableAccessToken(): boolean {
  const token = loadAccessToken()
  if (!token) return false
  const parts = token.split('.')
  if (parts.length !== 3 || !parts[1]) return false
  try {
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64.padEnd(Math.ceil(base64.length / 4) * 4, '=')
    const payload = JSON.parse(atob(padded)) as { exp?: unknown }
    if (typeof payload.exp === 'number') {
      return payload.exp * 1000 > Date.now()
    }
    return false
  } catch {
    return false
  }
}

export function subscribeSessionExpired(listener: SessionExpiredListener): () => void {
  sessionExpiredListeners.add(listener)
  return () => {
    sessionExpiredListeners.delete(listener)
  }
}

export function notifySessionExpired(
  message = 'Sesión expirada. Vuelva a iniciar sesión.',
): void {
  clearAccessToken()
  for (const listener of sessionExpiredListeners) {
    listener(message)
  }
}
