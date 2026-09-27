// Typed client for the doc-intelligence REST API (see planning/04-api-and-frontend.md).
// Every call is same-origin: in dev, next.config.ts rewrites /api/* to localhost:8000;
// in production the static export is served by the same FastAPI process that serves /api.

export type AccessLevel = "employee" | "manager";

export interface AuthUser {
  username: string;
  level: AccessLevel;
}

export type DocumentStatus = "processing" | "ready" | "failed";

// Field names match the MCP tools' shapes (app/rag/kb/models.py), not a REST-only variant.
export interface DocumentRecord {
  document_id: string;
  name: string;
  tags: string[];
  status: DocumentStatus;
  page_count: number | null;
  chunk_count: number | null;
  uploaded_at: string;
  error: string | null;
  required_level?: AccessLevel;
}

export interface UploadAccepted {
  document_id: string;
  status: "processing";
}

export interface UploadDuplicate {
  already_present: true;
  document_id: string;
  filename?: string;
}

export type UploadResult = UploadAccepted | UploadDuplicate;

export interface Tag {
  tag: string;
  description: string;
  document_count: number;
}

export class ApiError extends Error {
  status: number;
  body: unknown;

  constructor(status: number, body: unknown) {
    super(extractDetail(body) ?? `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

function extractDetail(body: unknown): string | undefined {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  if (typeof body === "string" && body.length > 0) return body;
  return undefined;
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, { credentials: "include", ...init });
  const text = await res.text();
  const data = text.length > 0 ? safeJsonParse(text) : undefined;
  if (!res.ok) {
    throw new ApiError(res.status, data);
  }
  return data as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

// --- auth -------------------------------------------------------------

export function login(username: string, password: string): Promise<AuthUser> {
  return request<AuthUser>("/api/auth/login", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ username, password }),
  });
}

export function logout(): Promise<void> {
  return request<void>("/api/auth/logout", { method: "POST" });
}

export function me(): Promise<AuthUser> {
  return request<AuthUser>("/api/auth/me");
}

// --- documents ----------------------------------------------------------

export function listDocuments(): Promise<DocumentRecord[]> {
  return request<DocumentRecord[]>("/api/documents");
}

export function uploadDocument(
  file: File,
  tags: string[],
  requiredLevel?: AccessLevel
): Promise<UploadResult> {
  const form = new FormData();
  form.append("file", file);
  for (const tag of tags) form.append("tags", tag);
  if (requiredLevel) form.append("required_level", requiredLevel);
  return request<UploadResult>("/api/documents", { method: "POST", body: form });
}

export function deleteDocument(id: string): Promise<void> {
  return request<void>(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
}

// --- tags -----------------------------------------------------------------

export function listTags(): Promise<Tag[]> {
  return request<Tag[]>("/api/tags");
}

export function createTag(name: string, description: string): Promise<Tag> {
  return request<Tag>("/api/tags", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({ name, description }),
  });
}

export function updateTagDescription(name: string, description: string): Promise<Tag> {
  return request<Tag>(`/api/tags/${encodeURIComponent(name)}`, {
    method: "PATCH",
    headers: JSON_HEADERS,
    body: JSON.stringify({ description }),
  });
}

export function deleteTag(name: string): Promise<void> {
  return request<void>(`/api/tags/${encodeURIComponent(name)}`, { method: "DELETE" });
}
