// The single HTTP layer of the admin UI: a fetch wrapper with bearer auth
// from localStorage and normalized errors. No axios, no query libraries.

const TOKEN_KEY = 'admin_token';

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let onUnauthorized: () => void = () => {};

// App registers a single handler that switches the UI to the token screen.
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (init.body !== undefined) headers['Content-Type'] = 'application/json';

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch {
    throw new ApiError(0, 'Server is unreachable');
  }
  if (response.status === 401) {
    onUnauthorized();
    throw new ApiError(401, 'Admin token required');
  }
  if (!response.ok) {
    throw new ApiError(response.status, await errorMessage(response));
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

async function errorMessage(response: Response): Promise<string> {
  // FastAPI errors are {"detail": "..."}; 422 validation errors put a list there.
  try {
    const body = await response.json();
    if (typeof body.detail === 'string') return body.detail;
    if (body.detail !== undefined) return JSON.stringify(body.detail);
  } catch {
    // non-JSON body — fall through to the generic message
  }
  return `HTTP ${response.status}`;
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(`/api${path}`);
}

export function apiSend<T>(
  method: 'POST' | 'PUT' | 'PATCH' | 'DELETE',
  path: string,
  body?: unknown,
): Promise<T> {
  return request<T>(`/api${path}`, {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// /health lives outside /api and needs no auth.
export interface Health {
  status: string;
  redis: boolean;
}

export function fetchHealth(): Promise<Health> {
  return request<Health>('/health');
}

export interface SetupStatus {
  configured: boolean;
  missing: string[];
  sections: Record<string, unknown>;
}

export function fetchSetupStatus(): Promise<SetupStatus> {
  return apiGet<SetupStatus>('/setup/status');
}
