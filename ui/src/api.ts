// The single HTTP layer of the admin UI: a fetch wrapper with bearer auth
// from localStorage and normalized errors. No axios, no query libraries.

const TOKEN_KEY = 'admin_token';

// The project's own repository. Lives here rather than in one screen because
// two of them link into it — the layout's build stamp and the telemetry
// section's "what is collected", which points at a file in `docs/`.
export const REPO_URL = 'https://github.com/knowitop/itop-ai-assistant';

export const TELEMETRY_DOC_URL = `${REPO_URL}/blob/main/docs/telemetry.md`;

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
  // Per-field messages of a 422, keyed by field name. The empty string keys
  // what the server said about the request as a whole — a rule about the
  // relation between fields belongs to no single input.
  fields: Record<string, string>;

  constructor(status: number, message: string, fields: Record<string, string> = {}) {
    super(message);
    this.status = status;
    this.fields = fields;
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
    throw await apiFailure(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

interface FieldError {
  field: string;
  message: string;
}

function isFieldErrors(detail: unknown): detail is FieldError[] {
  return (
    Array.isArray(detail) &&
    detail.length > 0 &&
    detail.every((item) => typeof item?.field === 'string' && typeof item?.message === 'string')
  );
}

async function apiFailure(response: Response): Promise<ApiError> {
  // FastAPI errors are {"detail": "..."}; config validation puts a list of
  // {field, message} there instead, so a form can place each one.
  try {
    const body = await response.json();
    if (typeof body.detail === 'string') return new ApiError(response.status, body.detail);
    if (isFieldErrors(body.detail)) {
      const fields: Record<string, string> = {};
      for (const item of body.detail) {
        // Several failures on one field would only fit one input anyway;
        // the first is the one the server checked first.
        if (!(item.field in fields)) fields[item.field] = item.message;
      }
      // The message stays readable for callers that show errors as one line.
      const summary = body.detail
        .map((item: FieldError) => (item.field ? `${item.field}: ${item.message}` : item.message))
        .join('\n');
      return new ApiError(response.status, summary, fields);
    }
    if (body.detail !== undefined) return new ApiError(response.status, JSON.stringify(body.detail));
  } catch {
    // non-JSON body — fall through to the generic message
  }
  return new ApiError(response.status, `HTTP ${response.status}`);
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

// /version is public for the same reason as /health: it is read before a token exists.
export interface BuildInfo {
  version: string;
  commit: string | null;
  built_at: string | null;
}

export function fetchVersion(): Promise<BuildInfo> {
  return request<BuildInfo>('/version');
}

export interface SetupStatus {
  configured: boolean;
  missing: string[];
  // Installation-wide dry run: runs happen, nothing is written to iTop.
  // Top-level rather than dug out of `sections` — every screen shows it.
  dry_run: boolean;
  // This installation's own anonymous id, generated once at first start. Shown
  // on the wizard's welcome screen and on System; it is what a "delete my
  // data" request names.
  install_id: string;
  sections: Record<string, unknown>;
}

export function fetchSetupStatus(): Promise<SetupStatus> {
  return apiGet<SetupStatus>('/setup/status');
}
