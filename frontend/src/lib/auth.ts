// Lightweight auth helper. Stores JWT in localStorage and injects it into
// fetch() calls as Authorization: Bearer <token>. On 401 we clear the token
// and reload, which sends the user back to the login screen.

const TOKEN_KEY = "afds_token";
const ROLE_KEY = "afds_role";
const USER_KEY = "afds_username";

export interface LoginResult {
  access_token: string;
  token_type: string;
  role: string;
  expires_in: number;
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function getRole(): string | null {
  return localStorage.getItem(ROLE_KEY);
}

export function getUsername(): string | null {
  return localStorage.getItem(USER_KEY);
}

export function isAuthenticated(): boolean {
  return !!getToken();
}

export async function login(username: string, password: string): Promise<LoginResult> {
  const res = await fetch("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Login failed: ${res.status}`);
  }
  const data: LoginResult = await res.json();
  localStorage.setItem(TOKEN_KEY, data.access_token);
  localStorage.setItem(ROLE_KEY, data.role);
  localStorage.setItem(USER_KEY, username);
  return data;
}

export function logout(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(ROLE_KEY);
  localStorage.removeItem(USER_KEY);
  window.location.reload();
}

/** fetch wrapper that injects Bearer token and handles 401s. */
export async function authFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers || {});
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(input, { ...init, headers });
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(ROLE_KEY);
    localStorage.removeItem(USER_KEY);
    // Trigger re-render by reloading; App will show <Login />.
    window.location.reload();
  }
  return res;
}

/**
 * Monkey-patch window.fetch so every request to /api/v1/* automatically gets
 * the Bearer token and any 401 forces a logout. This keeps existing components
 * (which call plain fetch) working without touching them.
 */
export function installAuthFetch(): void {
  const originalFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const isApi = url.startsWith("/api/") || url.includes(window.location.origin + "/api/");
    if (!isApi) return originalFetch(input, init);

    const token = getToken();
    const headers = new Headers(init.headers || {});
    if (token && !headers.has("Authorization")) {
      headers.set("Authorization", `Bearer ${token}`);
    }
    const res = await originalFetch(input, { ...init, headers });
    if (res.status === 401 && !url.includes("/auth/login")) {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(ROLE_KEY);
      localStorage.removeItem(USER_KEY);
      window.location.reload();
    }
    return res;
  };
}
