// The knowledge library (SRS 4.11, M11.5): browse the documents a person may read, read one passage
// by passage with each citation, search them, and, with knowledge.manage, upload a revision,
// release a held one, index again and choose what may be embedded. The Knowledge Service decides
// every call; this screen only hides what the person's roles do not include.
import { useCallback, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getJson, type Me, type Permissions } from "../../../api/client";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel, when } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import {
  CLASSIFICATIONS,
  contentTypeOf,
  type KnowledgeApi,
  knowledgeApi,
  type KnowledgeDocument,
  type Passage,
  readableScopes,
  size,
  STATUS_STYLE,
  STATUSES,
  TYPES,
} from "../api";

type Params = ReturnType<typeof useSearchParams>;

function Status({ document }: { document: KnowledgeDocument }) {
  const style = STATUS_STYLE[document.status] ?? STATUS_STYLE.pending;
  return <StatusBadge status={style.label} className={style.className} />;
}

function docLink(params: URLSearchParams, id: string, revision = 0, passage?: string): string {
  const next = new URLSearchParams(params);
  next.set("doc", id);
  if (revision) next.set("rev", String(revision));
  else next.delete("rev");
  return `?${next.toString()}${passage ? `#${encodeURIComponent(passage)}` : ""}`;
}

function SearchPanel({ api, farm }: { api: KnowledgeApi; farm: string }) {
  const [params] = useSearchParams();
  const [question, setQuestion] = useState("");
  const [types, setTypes] = useState<string[]>([]);
  const [older, setOlder] = useState(false);
  const [asked, setAsked] = useState<{ q: string; types: string[]; older: boolean } | null>(null);
  const load = useCallback(
    () => (asked ? api.search(asked.q, farm, asked.types, asked.older) : Promise.resolve(null)),
    [api, asked, farm],
  );
  const [state] = useLoad(load);
  return (
    <Panel title="Search">
      <form
        className="space-y-3"
        aria-label="Search the library"
        onSubmit={(event) => {
          event.preventDefault();
          if (question.trim()) setAsked({ q: question.trim(), types, older });
        }}
      >
        <div className="flex flex-col gap-2 sm:flex-row">
          <label className="sr-only" htmlFor="knowledge-question">
            Question
          </label>
          <input
            id="knowledge-question"
            className={INPUT}
            maxLength={1000}
            placeholder="What do you want to find, such as gearbox oil sampling?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
          />
          <Button type="submit" disabled={!question.trim()}>
            Search
          </Button>
        </div>
        <fieldset className="flex flex-wrap gap-x-4 gap-y-1 text-sm text-slate-700">
          <legend className="sr-only">Document types</legend>
          {TYPES.map((type) => (
            <label key={type} className="inline-flex items-center gap-1">
              <input
                type="checkbox"
                checked={types.includes(type)}
                onChange={(e) => setTypes(e.target.checked ? [...types, type] : types.filter((t) => t !== type))}
              />
              {titleCase(type)}
            </label>
          ))}
          <label className="inline-flex items-center gap-1">
            <input type="checkbox" checked={older} onChange={(e) => setOlder(e.target.checked)} />
            Replaced revisions too
          </label>
        </fieldset>
      </form>
      {asked && (
        <div className="mt-4">
          <Loading state={state}>
            {(result) =>
              !result || result.passages.length === 0 ? (
                <p className="text-sm text-slate-500" data-testid="no-passages">
                  Nothing you may read answers that.
                </p>
              ) : (
                <ol className="space-y-3" aria-label="Search results" data-testid="search-results">
                  {result.passages.map((p) => (
                    <li key={p.citation_id} className="rounded-md border border-slate-200 p-3">
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <Link
                          to={docLink(params, p.document.document_id, p.document.revision, p.citation_id)}
                          className="font-medium text-brand-primary hover:underline"
                        >
                          {p.document.title}
                        </Link>
                        <span className="font-mono text-xs text-slate-500">{p.citation_id}</span>
                      </div>
                      <p className="mt-1 break-words text-xs text-slate-500">
                        {p.section} · lines {p.first_line} to {p.last_line} · closeness {p.score.toFixed(2)}
                        {!p.current && ` · replaced by ${p.superseded_by}`}
                      </p>
                      <p className="mt-2 whitespace-pre-wrap break-words text-sm text-slate-800">{p.text}</p>
                    </li>
                  ))}
                </ol>
              )
            }
          </Loading>
        </div>
      )}
    </Panel>
  );
}

function DocumentList({ api, farm }: { api: KnowledgeApi; farm: string }) {
  const [params, setParams] = useSearchParams();
  const status = params.get("status") ?? "";
  const [pages, setPages] = useState<string[]>([""]);
  const load = useCallback(
    () => Promise.all(pages.map((token) => api.documents(farm, status, token))),
    [api, farm, status, pages],
  );
  const [state] = useLoad(load);
  return (
    <Panel
      title="Documents"
      actions={
        <select
          aria-label="Status"
          className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          value={status}
          onChange={(e) => {
            const next = new URLSearchParams(params);
            if (e.target.value) next.set("status", e.target.value);
            else next.delete("status");
            setPages([""]);
            setParams(next);
          }}
        >
          <option value="">Every status</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {STATUS_STYLE[s].label}
            </option>
          ))}
        </select>
      }
    >
      <Loading state={state}>
        {(loaded) => {
          const documents = loaded.flatMap((page) => page.documents);
          const more = loaded[loaded.length - 1]?.next_page_token ?? "";
          if (documents.length === 0) return <p className="text-sm text-slate-500">No documents you may read here.</p>;
          return (
            <div>
              <ul className="divide-y divide-slate-100" aria-label="Documents" data-testid="documents">
                {documents.map((d) => (
                  <li key={d.metadata.document_id} className="py-2">
                    <Link to={docLink(params, d.metadata.document_id)} className="block rounded-md hover:bg-slate-50">
                      <span className="flex flex-wrap items-center justify-between gap-2">
                        <span className="font-medium text-slate-900">{d.metadata.title}</span>
                        <Status document={d} />
                      </span>
                      <span className="mt-0.5 block break-words text-xs text-slate-500">
                        {d.metadata.document_id} revision {d.metadata.revision} · {titleCase(d.metadata.type)} ·{" "}
                        {d.metadata.classification} · applies to {d.metadata.applies_to.join(", ") || "—"} ·{" "}
                        {d.passages} passages
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
              {more && (
                <Button variant="outline" className="mt-3 text-brand-primary" onClick={() => setPages([...pages, more])}>
                  Show more
                </Button>
              )}
            </div>
          );
        }}
      </Loading>
    </Panel>
  );
}

function ReasonForm({
  label,
  action,
  onSend,
}: {
  label: string;
  action: string;
  onSend: (key: string, reason: string) => Promise<unknown>;
}) {
  const change = useChange();
  const [reason, setReason] = useState("");
  const [done, setDone] = useState(false);
  const id = `reason-${action.replace(/\W+/g, "-").toLowerCase()}`;
  return (
    <form
      className="space-y-2"
      aria-label={action}
      onSubmit={(event) => {
        event.preventDefault();
        void change.run((key) => onSend(key, reason.trim())).then((made) => {
          if (made !== undefined) {
            setReason("");
            setDone(true);
          }
        });
      }}
    >
      <label className={LABEL} htmlFor={id}>
        {label}
      </label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <input id={id} className={INPUT} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
        <Button type="submit" disabled={!reason.trim() || change.state.kind === "busy"}>
          {action}
        </Button>
      </div>
      {done && change.state.kind === "idle" && (
        <p role="status" className="text-sm text-emerald-800">
          Done.
        </p>
      )}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </form>
  );
}

function PassageView({ passage }: { passage: Passage }) {
  return (
    <li id={passage.citation_id} className="scroll-mt-20 border-t border-slate-100 py-3 target:bg-amber-50">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="text-sm font-medium text-slate-700">{passage.section || "—"}</span>
        <span className="font-mono text-xs text-slate-500">{passage.citation_id}</span>
      </div>
      <p className="mt-1 whitespace-pre-wrap break-words text-sm text-slate-800">{passage.text}</p>
      <p className="mt-1 text-xs text-slate-400">
        Lines {passage.first_line} to {passage.last_line}
      </p>
    </li>
  );
}

function DocumentView({
  api,
  farm,
  id,
  revision,
  manage,
  me,
}: {
  api: KnowledgeApi;
  farm: string;
  id: string;
  revision: number;
  manage: boolean;
  me: string;
}) {
  const [params] = useSearchParams();
  const load = useCallback(
    () => Promise.all([api.passages(id, revision, farm), api.document(id, revision, farm)]),
    [api, id, revision, farm],
  );
  const [state, reload] = useLoad(load);
  const back = new URLSearchParams(params);
  back.delete("doc");
  back.delete("rev");
  return (
    <section className="space-y-4">
      <Link to={`?${back.toString()}`} className="text-sm text-brand-primary hover:underline">
        Back to the library
      </Link>
      <Loading state={state}>
        {([text, { revisions }]) => {
          const d = text.document;
          const m = d.metadata;
          const replaced = text.passages.find((p) => !p.current)?.superseded_by;
          return (
            <div className="space-y-4">
              <Panel title={m.title} actions={<Status document={d} />}>
                <div className="space-y-3 text-sm" data-testid="document">
                  {d.status_reason && <p className="text-slate-700">{d.status_reason}.</p>}
                  {replaced && (
                    <p role="note" className="rounded-md bg-amber-50 p-2 text-amber-900">
                      Replaced by {replaced}. Its citations still read as they were.
                    </p>
                  )}
                  <dl className="grid gap-x-4 gap-y-1 sm:grid-cols-[auto_1fr]">
                    <dt className="text-slate-500">Document</dt>
                    <dd className="break-all">
                      {m.document_id}, revision {m.revision}
                      {m.supersedes_document_id && ` (replaces ${m.supersedes_document_id} revision ${m.supersedes_revision})`}
                    </dd>
                    <dt className="text-slate-500">Type</dt>
                    <dd>{titleCase(m.type)}</dd>
                    <dt className="text-slate-500">Issued by</dt>
                    <dd className="break-words">
                      {m.issued_by}, published {m.published}
                    </dd>
                    <dt className="text-slate-500">Applies to</dt>
                    <dd className="break-words">{m.applies_to.join(", ") || "—"}</dd>
                    <dt className="text-slate-500">Classification</dt>
                    <dd>{m.classification}</dd>
                    <dt className="text-slate-500">Source</dt>
                    <dd className="break-words">
                      {d.source} · {d.filename} · {size(d.size_bytes)}
                    </dd>
                    <dt className="text-slate-500">Ingested</dt>
                    <dd className="break-words">
                      {when(d.ingested_at)} by {d.ingested_by}
                      {d.released_by && `, released by ${d.released_by}`}
                    </dd>
                    <dt className="text-slate-500">Indexed</dt>
                    <dd className="break-words">
                      {d.indexed_at ? `${when(d.indexed_at)} with ${d.embedding_model} (${d.embedding_version})` : "—"}
                    </dd>
                    <dt className="text-slate-500">Hash</dt>
                    <dd className="break-all font-mono text-xs">{d.content_hash}</dd>
                  </dl>
                  {revisions.length > 1 && (
                    <nav aria-label="Revisions" className="flex flex-wrap gap-2">
                      {revisions.map((r) => (
                        <Link
                          key={r}
                          to={docLink(params, m.document_id, r)}
                          aria-current={r === m.revision ? "page" : undefined}
                          className={`rounded px-2 py-0.5 text-xs ring-1 ${r === m.revision ? "bg-sky-50 ring-sky-300" : "ring-slate-200 hover:bg-slate-50"}`}
                        >
                          Revision {r}
                        </Link>
                      ))}
                    </nav>
                  )}
                  {d.screening.length > 0 && (
                    <div role="note" className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900">
                      <p className="font-medium">The screening found:</p>
                      <ul className="mt-1 list-disc pl-5" aria-label="Screening findings">
                        {d.screening.map((f) => (
                          <li key={`${f.kind}-${f.line}`}>
                            {f.description}, line {f.line}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              </Panel>
              {manage && d.status === "held" && d.ingested_by === me && (
                <p role="note" className="rounded-md border border-slate-200 bg-white p-3 text-sm text-slate-600">
                  You uploaded it, so someone else with the library to manage reads it and releases it.
                </p>
              )}
              {manage && d.status === "held" && d.ingested_by !== me && (
                <Panel title="Release for indexing">
                  <p className="mb-2 text-sm text-slate-600">
                    Read it first. Someone other than its uploader releases it, saying why it is safe to index.
                  </p>
                  <ReasonForm
                    label="Why it is safe"
                    action="Release"
                    onSend={(key, reason) => api.release(key, m.document_id, m.revision, reason).then((r) => (reload(), r))}
                  />
                </Panel>
              )}
              {manage && (d.status === "indexed" || d.status === "failed") && (
                <Panel title="Index again">
                  <ReasonForm
                    label="Why, such as a new embedding model"
                    action="Index this document again"
                    onSend={(key, reason) => api.reindex(key, m.document_id, reason).then((r) => (reload(), r))}
                  />
                </Panel>
              )}
              <Panel title={`Passages (${text.passages.length})`}>
                <ol aria-label="Passages" data-testid="passages">
                  {text.passages.map((p) => (
                    <PassageView key={p.citation_id} passage={p} />
                  ))}
                </ol>
              </Panel>
            </div>
          );
        }}
      </Loading>
    </section>
  );
}

function UploadForm({ api, onMade }: { api: KnowledgeApi; onMade: (d: KnowledgeDocument) => void }) {
  const change = useChange();
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const type = file ? contentTypeOf(file.name) : null;
  const tooBig = file !== null && file.size > 1024 * 1024;
  return (
    <form
      className="space-y-3"
      aria-label="Upload a document"
      onSubmit={(event) => {
        event.preventDefault();
        if (!file || !type) return;
        void file
          .text()
          .then((content) =>
            change.run((key) => api.upload(key, { filename: file.name, content_type: type, content, source: source.trim() })),
          )
          .then((made) => made && onMade(made));
      }}
    >
      <div>
        <label className={LABEL} htmlFor="knowledge-file">
          Markdown or plain text, at most 1 MB
        </label>
        <input
          id="knowledge-file"
          type="file"
          accept=".md,.txt,text/markdown,text/plain"
          className="mt-1 block w-full text-sm"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
        {file && !type && <p className="mt-1 text-sm text-red-800">Only .md and .txt files are accepted.</p>}
        {tooBig && <p className="mt-1 text-sm text-red-800">This file is larger than 1 MB.</p>}
      </div>
      <div>
        <label className={LABEL} htmlFor="knowledge-source">
          Where it came from
        </label>
        <input
          id="knowledge-source"
          className={INPUT}
          maxLength={500}
          placeholder="Such as the manufacturer's portal, March 2026"
          value={source}
          onChange={(e) => setSource(e.target.value)}
        />
      </div>
      <p className="text-xs text-slate-500">
        The document's own header gives its ID, revision, title and classification. It is checked before anything is kept, and
        one that addresses an AI is held until someone else releases it.
      </p>
      <Button type="submit" disabled={!file || !type || tooBig || !source.trim() || change.state.kind === "busy"}>
        Upload
      </Button>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </form>
  );
}

function SettingsPanel({ api }: { api: KnowledgeApi }) {
  const load = useCallback(() => api.settings(), [api]);
  const [state, reload] = useLoad(load);
  const [wanted, setWanted] = useState("");
  return (
    <Panel title="What may be embedded">
      <Loading state={state}>
        {(settings) => (
          <div className="space-y-3 text-sm" data-testid="ingestion-settings">
            <p>
              Passages up to <strong>{settings.embedding_max_classification}</strong> go to the embedding provider. A document above
              it is kept, but not searchable.
              {settings.updated_by && ` Set by ${settings.updated_by} ${when(settings.updated_at)}.`}
            </p>
            <div>
              <label className={LABEL} htmlFor="knowledge-classification">
                Highest classification
              </label>
              <select
                id="knowledge-classification"
                className={INPUT}
                value={wanted || settings.embedding_max_classification}
                onChange={(e) => setWanted(e.target.value)}
              >
                {CLASSIFICATIONS.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
            {wanted && wanted !== settings.embedding_max_classification && (
              <ReasonForm
                label="Why"
                action="Change"
                onSend={(key, reason) =>
                  api.setSettings(key, wanted, reason).then((r) => {
                    setWanted("");
                    reload();
                    return r;
                  })
                }
              />
            )}
          </div>
        )}
      </Loading>
    </Panel>
  );
}

function Uploaded({ document: d }: { document: KnowledgeDocument }) {
  return (
    <div role="status" className="mt-3 space-y-1 rounded-md border border-slate-200 p-3 text-sm" data-testid="uploaded">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium">{d.metadata.title}</span>
        <Status document={d} />
      </div>
      <p className="break-words text-xs text-slate-500">
        {d.metadata.document_id} revision {d.metadata.revision} · {d.passages} passages
      </p>
      {d.status_reason && <p className="text-slate-700">{d.status_reason}.</p>}
      {d.screening.length > 0 && (
        <ul className="list-disc pl-5 text-amber-900" aria-label="Screening findings">
          {d.screening.map((f) => (
            <li key={`${f.kind}-${f.line}`}>
              {f.description}, line {f.line}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ManagePanels({
  api,
  params,
  setParams,
  reads,
}: {
  api: KnowledgeApi;
  params: URLSearchParams;
  setParams: Params[1];
  reads: boolean;
}) {
  const [made, setMade] = useState<KnowledgeDocument | null>(null);
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Panel title="Upload a revision">
        <UploadForm
          api={api}
          onMade={(d) =>
            reads
              ? setParams(new URLSearchParams(docLink(params, d.metadata.document_id, d.metadata.revision).slice(1)))
              : setMade(d)
          }
        />
        {made && <Uploaded document={made} />}
      </Panel>
      <SettingsPanel api={api} />
      <Panel title="Index everything again">
        <ReasonForm label="Why, such as a new embedding model" action="Index all again" onSend={(key, reason) => api.reindex(key, "", reason)} />
      </Panel>
    </div>
  );
}

export function KnowledgeScreen({ token, held }: { token: string; held: ReadonlySet<string> }) {
  const api = useMemo(() => knowledgeApi(token), [token]);
  const [params, setParams] = useSearchParams();
  const loadScopes = useCallback(
    () =>
      Promise.all([getJson<Permissions>("/me/permissions", token), getJson<Me>("/me", token)]).then(([p, me]) => ({
        farms: readableScopes(p.permissions),
        me: me.user_id,
      })),
    [token],
  );
  const [scopes] = useLoad(loadScopes);
  const manage = held.has("knowledge.manage");
  const doc = params.get("doc");
  const revision = Number(params.get("rev") ?? 0) || 0;
  return (
    <section className="space-y-4">
      <h2 className="text-xl font-semibold">Knowledge library</h2>
      <Loading state={scopes}>
        {({ farms, me }) => {
          if (farms.length === 0) {
            return (
              <div className="space-y-4">
                <p className="text-sm text-slate-500">
                  Your roles include managing the library, not reading it, so its documents are not shown.
                </p>
                {manage && <ManagePanels api={api} params={params} setParams={setParams} reads={false} />}
              </div>
            );
          }
          const farm = farms.includes(params.get("farm") ?? "") ? (params.get("farm") ?? "") : (farms[0] ?? "");
          return (
            <div className="space-y-4">
              {farms.length > 1 || farms[0] !== "" ? (
                <div className="max-w-xs">
                  <label className={LABEL} htmlFor="knowledge-farm">
                    Wind farm
                  </label>
                  <select
                    id="knowledge-farm"
                    className={INPUT}
                    value={farm}
                    onChange={(e) => {
                      const next = new URLSearchParams(params);
                      next.set("farm", e.target.value);
                      setParams(next);
                    }}
                  >
                    {farms.map((f) => (
                      <option key={f} value={f}>
                        {f}
                      </option>
                    ))}
                  </select>
                </div>
              ) : null}
              {doc ? (
                <DocumentView key={`${doc}@${revision}@${farm}`} api={api} farm={farm} id={doc} revision={revision} manage={manage} me={me} />
              ) : (
                <>
                  <SearchPanel key={farm} api={api} farm={farm} />
                  <DocumentList key={`list-${farm}`} api={api} farm={farm} />
                  {manage && <ManagePanels api={api} params={params} setParams={setParams} reads />}
                </>
              )}
            </div>
          );
        }}
      </Loading>
    </section>
  );
}
