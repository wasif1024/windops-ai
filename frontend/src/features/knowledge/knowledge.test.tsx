import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { contentTypeOf, type KnowledgeDocument, type Passage, readableScopes, size } from "./api";
import { KnowledgeScreen } from "./pages/KnowledgeScreen";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const grant = (permission: string, wind_farm_id = "", turbine_id = "") => ({ permission, wind_farm_id, turbine_id });

const METADATA = {
  document_id: "WS-PRC-GBX-012",
  revision: 3,
  title: "Gearbox oil inspection",
  type: "procedure",
  published: "2026-03-01",
  issued_by: "WindSim",
  applies_to: ["WS-8.0-167"],
  classification: "internal",
  supersedes_document_id: "",
  supersedes_revision: 0,
};

function documentOf(overrides: Partial<KnowledgeDocument> = {}): KnowledgeDocument {
  return {
    metadata: METADATA,
    source: "synthetic-documents-1",
    filename: "ws-prc-gbx-012-r3.md",
    content_type: "text/markdown",
    size_bytes: 4096,
    content_hash: "sha256:ab",
    ingested_at: "2026-11-26T06:00:00Z",
    ingested_by: "u-admin",
    status: "indexed",
    status_reason: "",
    passages: 1,
    embedding_provider: "openai",
    embedding_model: "text-embedding-3-large",
    embedding_version: "adapter-1",
    indexed_at: "2026-11-26T06:01:00Z",
    metadata_from_document: [],
    screening: [],
    released_by: "",
    ...overrides,
  };
}

const PASSAGE: Passage = {
  citation_id: "kn:WS-PRC-GBX-012@3#0",
  document: METADATA,
  source: "synthetic-documents-1",
  section: "Gearbox oil inspection > Before you start",
  text: "Lock the rotor before opening the gearbox.",
  first_line: 3,
  last_line: 5,
  content_hash: "sha256:cd",
  current: true,
  superseded_by: "",
  embedding_model: "text-embedding-3-large",
  embedding_version: "adapter-1",
  score: 0,
};

/** A fake Web BFF: each path prefix answers with its body, and every URL asked is kept. */
function serve(routes: [string, unknown][]) {
  const asked: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      asked.push(url);
      const found = routes.find(([prefix]) => url.includes(prefix));
      return Promise.resolve(new Response(JSON.stringify(found ? found[1] : {}), { status: found ? 200 : 404 }));
    }),
  );
  return asked;
}

function show(entry: string, held: string[]) {
  render(
    <MemoryRouter initialEntries={[entry]}>
      <KnowledgeScreen token="token" held={new Set(held)} />
    </MemoryRouter>,
  );
}

describe("where a person may browse the library", () => {
  it("is the whole tenant when they read there, or else each farm they read", () => {
    expect(readableScopes([grant("knowledge.read"), grant("knowledge.read", "dogger-east")])).toEqual([""]);
    expect(
      readableScopes([grant("knowledge.read", "hornsea-west"), grant("knowledge.read", "dogger-east"), grant("knowledge.read", "dogger-east", "t1")]),
    ).toEqual(["dogger-east", "hornsea-west"]);
    expect(readableScopes([grant("knowledge.manage")])).toEqual([]);
  });

  it("accepts only Markdown and plain text, and says how big a file is", () => {
    expect(contentTypeOf("notes.MD")).toBe("text/markdown");
    expect(contentTypeOf("notes.txt")).toBe("text/plain");
    expect(contentTypeOf("manual.pdf")).toBeNull();
    expect(size(4096)).toBe("4.0 KB");
  });
});

describe("the knowledge library", () => {
  it("lists a farm-scoped reader's documents where they read, and searches there", async () => {
    const asked = serve([
      ["/me/permissions", { roles: [], permissions: [grant("knowledge.read", "dogger-east")] }],
      ["/me", { user_id: "u-1" }],
      ["/knowledge/documents", { documents: [documentOf()], next_page_token: "" }],
      ["/knowledge/search", { passages: [{ ...PASSAGE, score: 0.82 }], embedding_model: "m", embedding_version: "v" }],
    ]);
    show("/", ["knowledge.read"]);
    expect((await screen.findByTestId("documents")).textContent).toContain("Gearbox oil inspection");
    expect(asked.some((u) => u.includes("/knowledge/documents?wind_farm_id=dogger-east"))).toBe(true);
    expect(screen.queryByRole("form", { name: "Upload a document" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Question"), { target: { value: "oil" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    const results = await screen.findByTestId("search-results");
    expect(results.textContent).toContain("kn:WS-PRC-GBX-012@3#0");
    expect(results.textContent).toContain("closeness 0.82");
    expect(asked.some((u) => u.includes("/knowledge/search?q=oil&wind_farm_id=dogger-east"))).toBe(true);
  });

  it("reads a held document passage by passage, and never offers its uploader the release", async () => {
    const held = documentOf({
      status: "held",
      status_reason: "It holds text addressed to an AI",
      ingested_by: "u-1",
      screening: [{ kind: "instructions_to_ai", description: "Text addressed to an AI", line: 12 }],
    });
    serve([
      ["/me/permissions", { roles: [], permissions: [grant("knowledge.read"), grant("knowledge.manage")] }],
      ["/me", { user_id: "u-1" }],
      ["/passages", { document: held, passages: [PASSAGE] }],
      ["/knowledge/documents/WS-PRC-GBX-012", { document: held, revisions: [2, 3] }],
    ]);
    show("/?doc=WS-PRC-GBX-012", ["knowledge.read", "knowledge.manage"]);
    expect((await screen.findByTestId("passages")).textContent).toContain("Lock the rotor");
    const document = screen.getByTestId("document");
    expect(screen.getByText("Held for review")).toBeTruthy();
    expect(document.textContent).toContain("Text addressed to an AI, line 12");
    expect(screen.getByRole("navigation", { name: "Revisions" }).textContent).toContain("Revision 2");
    expect(screen.getByText(/You uploaded it/)).toBeTruthy();
    expect(screen.queryByRole("form", { name: "Release" })).toBeNull();
  });

  it("lets someone who manages without reading upload and see the settings, not the documents", async () => {
    const asked = serve([
      ["/me/permissions", { roles: [], permissions: [grant("knowledge.manage")] }],
      ["/me", { user_id: "u-ta" }],
      ["/knowledge/settings", { embedding_max_classification: "confidential", updated_at: null, updated_by: "" }],
    ]);
    show("/", ["knowledge.manage"]);
    expect((await screen.findByTestId("ingestion-settings")).textContent).toContain("confidential");
    expect(screen.getByText(/managing the library, not reading it/)).toBeTruthy();
    expect(screen.getByRole("form", { name: "Upload a document" })).toBeTruthy();
    expect(asked.some((u) => u.includes("/knowledge/documents"))).toBe(false);
  });
});
