/**
 * API client: JSON in/out, RFC 9457 problem details → ApiError, transparent token refresh.
 *
 * Token storage (a deliberate trade-off, documented in docs/OBSERVABILITY.md):
 * - access token: **memory only**. Short-lived (15 min), gone on reload.
 * - refresh token: **sessionStorage**. It survives a reload but not closing the tab,
 *   and it's never sent automatically like a cookie, so there's no CSRF.
 *   XSS could still read it; the Phase 7 hardening moves it to an httpOnly
 *   cookie behind the same origin, and a strict CSP makes XSS much harder.
 *
 * Refresh is single-flight: ten requests failing with 401 at once trigger exactly one
 * refresh. That matters because the API rotates refresh tokens and treats reuse of an
 * old one as theft, revoking the whole session.
 */
import type { TokenResponse } from "./types";

const REFRESH_KEY = "nuroflow.refresh";
export const API_BASE = "/api/v1";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId?: string,
  ) {
    super(message);
  }
}

let accessToken: string | null = null;
let refreshing: Promise<boolean> | null = null;
const listeners = new Set<(signedIn: boolean) => void>();

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null; // private mode / blocked storage: still works, just not across reloads
  }
}

export const session = {
  set(tokens: TokenResponse) {
    accessToken = tokens.access_token;
    storage()?.setItem(REFRESH_KEY, tokens.refresh_token);
    listeners.forEach((fn) => fn(true));
  },
  clear() {
    accessToken = null;
    storage()?.removeItem(REFRESH_KEY);
    listeners.forEach((fn) => fn(false));
  },
  hasRefreshToken: () => Boolean(storage()?.getItem(REFRESH_KEY)),
  onChange(fn: (signedIn: boolean) => void) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
};

export async function refresh(): Promise<boolean> {
  const token = storage()?.getItem(REFRESH_KEY);
  if (!token) return false;
  refreshing ??= (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: token }),
      });
      if (!response.ok) {
        session.clear();
        return false;
      }
      session.set((await response.json()) as TokenResponse);
      return true;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

/** fetch() with auth and one retry after a refresh. Used directly by the SSE client. */
export async function authorizedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const send = () => {
    const headers = new Headers(init.headers);
    if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
    return fetch(`${API_BASE}${path}`, { ...init, headers });
  };
  if (!accessToken && session.hasRefreshToken()) await refresh();
  let response = await send();
  if (response.status === 401 && (await refresh())) response = await send();
  return response;
}

export async function toApiError(response: Response): Promise<ApiError> {
  let body: { code?: string; detail?: string; request_id?: string } = {};
  try {
    body = await response.json();
  } catch {
    /* non-JSON error (proxy page): fall back to the status text */
  }
  return new ApiError(
    response.status,
    body.code ?? "http_error",
    body.detail ?? response.statusText ?? "Request failed",
    body.request_id,
  );
}

export async function api<T>(
  path: string,
  { json, ...init }: RequestInit & { json?: unknown } = {},
): Promise<T> {
  const headers = new Headers(init.headers);
  if (json !== undefined) headers.set("Content-Type", "application/json");
  const response = await authorizedFetch(path, {
    ...init,
    headers,
    body: json !== undefined ? JSON.stringify(json) : init.body,
  });
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export async function login(email: string, password: string): Promise<void> {
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) throw await toApiError(response);
  session.set((await response.json()) as TokenResponse);
}

export async function logout(): Promise<void> {
  const token = storage()?.getItem(REFRESH_KEY);
  try {
    if (token) await api("/auth/logout", { method: "POST", json: { refresh_token: token } });
  } finally {
    session.clear();
  }
}
