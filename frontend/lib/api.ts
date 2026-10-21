import { supabase } from "./supabase";

const configuredBase = process.env.NEXT_PUBLIC_API_URL ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000";
export const API_URL = configuredBase.endsWith("/api/v1") ? configuredBase : `${configuredBase.replace(/\/$/, "")}/api/v1`;

const ACCESS_KEY = "aura_access_token";
const REFRESH_KEY = "aura_refresh_token";
const PROVIDER_KEY = "aura_auth_provider"; // "local" | "supabase"
const USER_KEY = "aura_user";

export type AuthProvider = "local" | "supabase";
export type SessionTokens = { access_token: string; refresh_token?: string | null };
export type SavedSession = { tokens: SessionTokens; user: unknown };

export function saveSession(session: SavedSession, provider: AuthProvider = "local") {
  localStorage.setItem(ACCESS_KEY, session.tokens.access_token);
  if (session.tokens.refresh_token) localStorage.setItem(REFRESH_KEY, session.tokens.refresh_token);
  else localStorage.removeItem(REFRESH_KEY);
  localStorage.setItem(USER_KEY, JSON.stringify(session.user));
  localStorage.setItem(PROVIDER_KEY, provider);
}

export function clearSession() {
  for (const key of [ACCESS_KEY, REFRESH_KEY, PROVIDER_KEY, USER_KEY]) localStorage.removeItem(key);
}

export function getAccessToken(): string | null {
  return typeof window === "undefined" ? null : localStorage.getItem(ACCESS_KEY);
}

function redirectToLogin() {
  clearSession();
  if (typeof window !== "undefined") window.location.href = "/login";
}

// Shared promise so concurrent 401s trigger exactly one refresh, not a storm.
let refreshInFlight: Promise<boolean> | null = null;

async function refreshTokens(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    try {
      // Supabase-issued sessions carry no backend refresh token (the backend
      // says so explicitly); refresh stays the Supabase browser client's job.
      if (localStorage.getItem(PROVIDER_KEY) === "supabase") {
        if (!supabase) return false;
        const { data, error } = await supabase.auth.refreshSession();
        const session = data?.session;
        if (error || !session) return false;
        localStorage.setItem(ACCESS_KEY, session.access_token);
        return true;
      }
      const refreshToken = localStorage.getItem(REFRESH_KEY);
      if (!refreshToken) return false;
      // Direct fetch, never api(): a failing refresh must not recurse into
      // another 401 → redirect loop.
      const res = await fetch(`${API_URL}/auth/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!res.ok) return false;
      const session = (await res.json()) as SavedSession;
      if (!session?.tokens?.access_token) return false;
      // Backend rotates: replace BOTH tokens, never keep the old refresh one.
      saveSession(session, "local");
      return true;
    } catch {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const doFetch = async (): Promise<Response> => {
    const token = getAccessToken();
    const headers = new Headers(options.headers);
    if (!(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
    if (token) headers.set("Authorization", `Bearer ${token}`);
    let response: Response;
    try { response = await fetch(`${API_URL}${path}`, { ...options, headers, cache: "no-store" }); }
    catch { throw new Error("AURA API is unavailable. Start the backend or set NEXT_PUBLIC_API_BASE_URL in frontend/.env.local."); }
    return response;
  };

  let response = await doFetch();
  if (response.status === 401 && typeof window !== "undefined") {
    // One silent-refresh attempt before the session is considered dead.
    const refreshed = await refreshTokens();
    if (refreshed) {
      response = await doFetch();
      if (response.status === 401) { redirectToLogin(); throw new Error("Your session expired. Please sign in again."); }
    } else {
      redirectToLogin();
      throw new Error("Your session expired. Please sign in again.");
    }
  }
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    // The backend's error envelope is { error: { code, message, detail?,
    // hint?, fields? } }, plus FastAPI's { detail } shape as a fallback.
    const err = body?.error;
    let message =
      err?.message ??
      (typeof body?.detail === "object" ? body.detail?.message : body?.detail) ??
      `Request failed (${response.status})`;
    if (err?.fields && typeof err.fields === "object") {
      const fieldText = Object.entries(err.fields as Record<string, string>)
        .map(([field, problem]) => `${field}: ${problem}`)
        .join(". ");
      if (fieldText) message = `${message} ${fieldText}`;
    }
    if (err?.hint) message = `${message} ${err.hint}`;
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}
