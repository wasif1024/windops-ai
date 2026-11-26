// The knowledge library's calls to Web BFF (M11.5). The Knowledge Service checks every one as the
// signed-in person: reading needs knowledge.read where it is read, and a document that applies to
// wind farms is shown only to someone who may read one of them (FR-KNW-004). Every change needs
// knowledge.manage and a recent second factor.
import { getJson, type ScopedPermission, sendJson } from "../../api/client";

export type DocumentStatus = "pending" | "indexed" | "held" | "not_indexed" | "failed";

export interface DocumentMetadata {
  document_id: string;
  revision: number;
  title: string;
  type: string;
  published: string;
  issued_by: string;
  applies_to: string[];
  classification: string;
  supersedes_document_id: string;
  supersedes_revision: number;
}

export interface KnowledgeDocument {
  metadata: DocumentMetadata;
  source: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  content_hash: string;
  ingested_at: string | null;
  ingested_by: string;
  status: DocumentStatus;
  status_reason: string;
  passages: number;
  embedding_provider: string;
  embedding_model: string;
  embedding_version: string;
  indexed_at: string | null;
  metadata_from_document: string[];
  screening: { kind: string; description: string; line: number }[];
  released_by: string;
}

export interface Passage {
  citation_id: string;
  document: DocumentMetadata;
  source: string;
  section: string;
  text: string;
  first_line: number;
  last_line: number;
  content_hash: string;
  current: boolean;
  superseded_by: string;
  embedding_model: string;
  embedding_version: string;
  score: number;
}

export interface DocumentPage {
  documents: KnowledgeDocument[];
  next_page_token: string;
}

export interface SearchResult {
  passages: Passage[];
  embedding_model: string;
  embedding_version: string;
}

export interface IngestionSettings {
  embedding_max_classification: string;
  updated_at: string | null;
  updated_by: string;
}

export const STATUSES: DocumentStatus[] = ["indexed", "pending", "held", "not_indexed", "failed"];
export const CLASSIFICATIONS = ["public", "internal", "confidential", "restricted"];
export const TYPES = ["manual", "bulletin", "procedure", "reference", "engineering"];

export const STATUS_STYLE: Record<DocumentStatus, { label: string; className: string }> = {
  indexed: { label: "Indexed", className: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200" },
  pending: { label: "Waiting to be indexed", className: "bg-sky-50 text-sky-800 ring-1 ring-sky-200" },
  held: { label: "Held for review", className: "bg-amber-50 text-amber-900 ring-1 ring-amber-200" },
  not_indexed: { label: "Kept, not searchable", className: "bg-slate-100 text-slate-700 ring-1 ring-slate-200" },
  failed: { label: "Indexing failed", className: "bg-red-50 text-red-800 ring-1 ring-red-200" },
};

function query(params: Record<string, string | number | boolean | string[] | undefined>): string {
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(params)) {
    if (Array.isArray(value)) value.forEach((item) => search.append(name, item));
    else if (value !== undefined && value !== "" && value !== false && value !== 0) search.set(name, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export function knowledgeApi(token: string) {
  return {
    documents: (farm: string, status: string, pageToken = "") =>
      getJson<DocumentPage>(`/knowledge/documents${query({ wind_farm_id: farm, status, page_token: pageToken })}`, token),
    document: (id: string, revision: number, farm: string) =>
      getJson<{ document: KnowledgeDocument; revisions: number[] }>(
        `/knowledge/documents/${encodeURIComponent(id)}${query({ revision, wind_farm_id: farm })}`,
        token,
      ),
    passages: (id: string, revision: number, farm: string) =>
      getJson<{ document: KnowledgeDocument; passages: Passage[] }>(
        `/knowledge/documents/${encodeURIComponent(id)}/passages${query({ revision, wind_farm_id: farm })}`,
        token,
      ),
    search: (q: string, farm: string, types: string[], includeSuperseded: boolean) =>
      getJson<SearchResult>(
        `/knowledge/search${query({ q, wind_farm_id: farm, types, include_superseded: includeSuperseded })}`,
        token,
      ),
    settings: () => getJson<IngestionSettings>("/knowledge/settings", token),
    upload: (key: string, body: { filename: string; content_type: string; content: string; source: string }) =>
      sendJson<KnowledgeDocument>("POST", "/knowledge/documents", token, key, body),
    release: (key: string, id: string, revision: number, reason: string) =>
      sendJson<KnowledgeDocument>(
        "POST",
        `/knowledge/documents/${encodeURIComponent(id)}/revisions/${revision}/release`,
        token,
        key,
        { reason },
      ),
    reindex: (key: string, documentId: string, reason: string) =>
      sendJson<{ queued: number }>("POST", "/knowledge/reindex", token, key, { document_id: documentId, reason }),
    setSettings: (key: string, classification: string, reason: string) =>
      sendJson<IngestionSettings>("PUT", "/knowledge/settings", token, key, {
        embedding_max_classification: classification,
        reason,
      }),
  };
}

export type KnowledgeApi = ReturnType<typeof knowledgeApi>;

/**
 * Where the person may browse: "" for the whole tenant, when they read there, or else each wind farm
 * they read. A turbine-scoped grant does not open the library: documents apply to farms and models.
 */
export function readableScopes(granted: ScopedPermission[]): string[] {
  const reads = granted.filter((g) => g.permission === "knowledge.read");
  if (reads.some((g) => !g.wind_farm_id && !g.turbine_id)) return [""];
  return [...new Set(reads.filter((g) => g.wind_farm_id && !g.turbine_id).map((g) => g.wind_farm_id))].sort();
}

/** The type an upload is sent as, from its name; anything else is not accepted. */
export function contentTypeOf(filename: string): string | null {
  if (/\.md$/i.test(filename)) return "text/markdown";
  if (/\.txt$/i.test(filename)) return "text/plain";
  return null;
}

/** "4.0 KB", from bytes. */
export function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
