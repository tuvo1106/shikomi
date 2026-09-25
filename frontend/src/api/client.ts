/**
 * The single HTTP client every part of the app uses to talk to the API
 * (DESIGN.md §6.4). It encapsulates the whole auth dance so callers just do
 * `api.get('/problems')` and never think about tokens.
 *
 * The model mirrors the backend: the **access token** lives in a module variable
 * (memory only — deliberately not localStorage, which XSS could read) and is sent
 * as a Bearer header. The **refresh token** is an httpOnly cookie the browser
 * attaches automatically (`credentials: 'include'`). When a request 401s because
 * the access token expired, we transparently refresh once and retry — so token
 * expiry is invisible to the UI.
 */
const API = '/api/v1'

let accessToken: string | null = null

/** Set (or clear, with null) the in-memory access token. Called by the auth layer. */
export function setAccessToken(token: string | null) {
  accessToken = token
}

/** The current in-memory access token, or null when signed out. */
export function getAccessToken() {
  return accessToken
}

/**
 * A failed API response, carrying the HTTP `status` and the backend's stable
 * `code` (e.g. `SUBMISSION_IN_FLIGHT`) so UI can branch on the machine code rather
 * than matching on human-readable message text.
 */
export class HttpError extends Error {
  status: number
  code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'HttpError'
    this.status = status
    this.code = code
  }
}

/**
 * De-dupe concurrent refreshes. Refresh-token rotation revokes the old token, so
 * two parallel `/auth/refresh` calls with the same cookie would look like token
 * *reuse* and the backend would kill the whole session (§3.6). This is a real
 * risk — React StrictMode double-fires effects in dev, and multiple 401s can land
 * at once. Sharing one in-flight promise means all callers await the same refresh.
 */
let refreshPromise: Promise<boolean> | null = null

/** Return the in-flight refresh if one is running, else start one. */
function tryRefresh(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = doRefresh().finally(() => {
      refreshPromise = null
    })
  }
  return refreshPromise
}

/**
 * Whether the readable `has_session` cookie (set by the server next to the
 * httpOnly refresh cookie) is present. Its presence means "a session may exist".
 * We can't read the refresh cookie itself (httpOnly), so this hint lets logged-out
 * visitors skip the refresh probe entirely — no guaranteed console 401 on landing.
 */
function hasSessionHint(): boolean {
  return document.cookie.split('; ').some((c) => c.startsWith('has_session='))
}

/** Expire the hint cookie locally (after a failed refresh) so we stop probing. */
function clearSessionHint(): void {
  document.cookie = 'has_session=; Max-Age=0; path=/'
}

/**
 * Restore a session on app startup: if the hint says one might exist, refresh to
 * get an access token before the first authed request (avoiding a guaranteed 401
 * on `/auth/me`). Resolves true if a session was restored.
 */
export function refreshSession(): Promise<boolean> {
  if (!hasSessionHint()) return Promise.resolve(false)
  return tryRefresh()
}

/** POST /auth/refresh: on success store the new access token; on failure sign out. */
async function doRefresh(): Promise<boolean> {
  const res = await fetch(`${API}/auth/refresh`, { method: 'POST', credentials: 'include' })
  if (!res.ok) {
    accessToken = null
    clearSessionHint() // stale hint (expired/revoked) — stop probing on future loads
    return false
  }
  const data = (await res.json()) as { access_token: string }
  accessToken = data.access_token
  return true
}

/**
 * The core request pipeline behind `api.*`: attach the token, send JSON, and on a
 * 401 refresh-and-retry exactly once (`retry=false` on the retry prevents an
 * infinite loop). Non-2xx becomes a thrown `HttpError`; 204 returns undefined.
 */
async function request<T>(method: string, path: string, body?: unknown, retry = true): Promise<T> {
  const res = await fetch(`${API}${path}`, {
    method,
    headers: {
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: 'include', // send/receive the refresh cookie
  })

  // Not for /auth/login*: a 401 there is a wrong password/code, not an expired token, and
  // replaying it would count one bad attempt twice toward the lockout.
  if (res.status === 401 && retry && !path.startsWith('/auth/login') && (await tryRefresh())) {
    return request<T>(method, path, body, false)
  }

  if (!res.ok) {
    const err = (await res.json().catch(() => ({ detail: res.statusText, code: 'ERROR' }))) as {
      detail: string
      code: string
    }
    throw new HttpError(res.status, err.code, err.detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/** The public HTTP surface: `api.get/post/put/del`, each returning parsed JSON. */
export const api = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
