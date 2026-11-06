import { type FormEvent, useCallback, useState } from "react";
import { useChange } from "../../../api/useChange";
import { useLoad } from "../../../api/useLoad";
import { Button } from "../../../components/Button";
import { ChangeStatus } from "../../../components/ChangeStatus";
import { INPUT, LABEL, Loading, Panel } from "../../../components/Panel";
import type { AdminApi, BrandingFields, EmailTemplate, OwnBranding } from "../api";
import { contrastWithWhite, HEX, logoProblem, placeholdersIn } from "../brandingRules";

const FIELDS: { name: keyof BrandingFields; label: string; long?: boolean; max: number }[] = [
  { name: "display_name", label: "Name shown to users", max: 200 },
  { name: "login_title", label: "Sign-in page title", max: 200 },
  { name: "login_message", label: "Sign-in page message", long: true, max: 1000 },
  { name: "time_zone", label: "Time zone, such as Europe/Oslo", max: 64 },
  { name: "locale", label: "Language, such as en-GB", max: 35 },
];

function fieldsOf(branding: OwnBranding): BrandingFields {
  const { display_name, primary_color, accent_color, login_title, login_message, time_zone, locale } = branding;
  return { display_name, primary_color, accent_color, login_title, login_message, time_zone, locale };
}

function Look({ api, branding, onSaved }: { api: AdminApi; branding: OwnBranding; onSaved: (b: OwnBranding) => void }) {
  const [fields, setFields] = useState(() => fieldsOf(branding));
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const change = useChange();
  const set = (name: keyof BrandingFields, value: string) => {
    setSaved(false);
    setFields((current) => ({ ...current, [name]: value }));
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    const colors = { primary_color: fields.primary_color.toLowerCase(), accent_color: fields.accent_color.toLowerCase() };
    if (!HEX.test(colors.primary_color) || !HEX.test(colors.accent_color)) {
      setProblem("Colors are six-digit hex values, such as #0b5394.");
      return;
    }
    if (contrastWithWhite(colors.primary_color) < 4.5) {
      setProblem("White text on the primary color would be hard to read. Choose a darker primary color.");
      return;
    }
    setProblem(null);
    const result = await change.run((key) => api.updateBranding(key, { ...fields, ...colors }));
    if (result) {
      setSaved(true);
      onSaved(result);
    }
  };

  return (
    <Panel title="Look and sign-in page">
      <form onSubmit={(event) => void save(event)} className="grid gap-3 md:grid-cols-2">
        {FIELDS.map((field) => (
          <label key={field.name} className={`${LABEL} ${field.long ? "md:col-span-2" : ""}`}>
            {field.label}
            {field.long ? (
              <textarea
                className={`${INPUT} mt-1`}
                rows={3}
                maxLength={field.max}
                value={fields[field.name]}
                onChange={(event) => set(field.name, event.target.value)}
              />
            ) : (
              <input
                className={`${INPUT} mt-1`}
                maxLength={field.max}
                value={fields[field.name]}
                onChange={(event) => set(field.name, event.target.value)}
              />
            )}
          </label>
        ))}
        {(["primary_color", "accent_color"] as const).map((name) => (
          <label key={name} className={LABEL}>
            {name === "primary_color" ? "Primary color" : "Accent color"}
            <span className="mt-1 flex items-center gap-2">
              <input
                type="color"
                aria-label={`${name === "primary_color" ? "Primary" : "Accent"} color picker`}
                className="h-10 w-12 shrink-0 cursor-pointer rounded border border-slate-300"
                value={HEX.test(fields[name]) ? fields[name] : "#000000"}
                onChange={(event) => set(name, event.target.value)}
              />
              <input className={INPUT} maxLength={7} value={fields[name]} onChange={(e) => set(name, e.target.value)} />
            </span>
          </label>
        ))}
        <div className="md:col-span-2">
          {problem && <p className="mb-2 text-sm text-red-800">{problem}</p>}
          <Button type="submit" disabled={change.state.kind === "busy"}>
            Save branding
          </Button>
          {saved && (
            <span role="status" className="ml-3 text-sm text-emerald-700">
              Saved. Users see it at their next page load.
            </span>
          )}
          <ChangeStatus state={change.state} onDismiss={change.reset} />
        </div>
      </form>
    </Panel>
  );
}

function Logo({ api, branding, onSaved }: { api: AdminApi; branding: OwnBranding; onSaved: (b: OwnBranding) => void }) {
  const [problem, setProblem] = useState<string | null>(null);
  const change = useChange();
  const upload = async (file: File | undefined) => {
    if (!file) return;
    const refused = logoProblem(file);
    setProblem(refused);
    if (refused) return;
    const result = await change.run((key) => api.setLogo(key, file));
    if (result) onSaved(result);
  };
  return (
    <Panel title="Logo">
      <p className="text-sm text-slate-600">
        {branding.has_logo ? "Your tenant has a logo." : "No logo yet."} A PNG or JPEG of at most 256 KiB.
      </p>
      <label className={`${LABEL} mt-3`}>
        Choose a logo
        <input
          type="file"
          accept="image/png,image/jpeg"
          className="mt-1 block w-full text-sm"
          onChange={(event) => void upload(event.target.files?.[0])}
        />
      </label>
      {problem && <p className="mt-2 text-sm text-red-800">{problem}</p>}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </Panel>
  );
}

function Template({ api, template }: { api: AdminApi; template: EmailTemplate }) {
  const [subject, setSubject] = useState(template.subject);
  const [body, setBody] = useState(template.body);
  const [customized, setCustomized] = useState(template.customized);
  const [problem, setProblem] = useState<string | null>(null);
  const change = useChange();
  const save = async (event: FormEvent) => {
    event.preventDefault();
    const unknown = placeholdersIn(`${subject} ${body}`).filter((name) => !template.placeholders.includes(name));
    if (unknown.length) {
      setProblem(`Not allowed in this email: ${[...new Set(unknown)].join(", ")}.`);
      return;
    }
    setProblem(null);
    const result = await change.run((key) => api.setTemplate(key, template.template, subject, body));
    if (result) setCustomized(result.customized);
  };
  return (
    <form onSubmit={(event) => void save(event)} className="grid gap-2 border-t border-slate-100 pt-4 first:border-0 first:pt-0">
      <h4 className="font-medium">
        {template.template.replace("_", " ")}
        <span className="ml-2 text-xs font-normal text-slate-500">{customized ? "changed" : "the standard text"}</span>
      </h4>
      <label className={LABEL}>
        Subject
        <input className={`${INPUT} mt-1`} maxLength={200} value={subject} onChange={(e) => setSubject(e.target.value)} />
      </label>
      <label className={LABEL}>
        Text
        <textarea
          className={`${INPUT} mt-1 font-mono`}
          rows={4}
          maxLength={10000}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </label>
      <p className="text-xs text-slate-500">
        May use: {template.placeholders.map((name) => `{{${name}}}`).join(", ")}
      </p>
      {problem && <p className="text-sm text-red-800">{problem}</p>}
      <div>
        <Button type="submit" variant="outline" className="text-brand-primary" disabled={change.state.kind === "busy"}>
          Save this email
        </Button>
      </div>
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </form>
  );
}

function Emails({ api }: { api: AdminApi }) {
  const [state] = useLoad(useCallback(() => api.templates(), [api]));
  return (
    <Panel title="Emails">
      <Loading state={state}>
        {({ templates }) => (
          <div className="grid gap-4">
            {templates.map((template) => (
              <Template key={template.template} api={api} template={template} />
            ))}
          </div>
        )}
      </Loading>
    </Panel>
  );
}

function Domain({ api, branding, onChanged }: { api: AdminApi; branding: OwnBranding; onChanged: () => void }) {
  const [domain, setDomain] = useState("");
  const change = useChange();
  const current = branding.custom_domain;
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (await change.run((key) => api.setDomain(key, domain.trim().toLowerCase()))) {
      setDomain("");
      onChanged();
    }
  };
  const remove = async () => {
    // A removal answers with no body, so success is the absence of an error.
    const removed = await change.run(async (key) => {
      await api.removeDomain(key);
      return true;
    });
    if (removed) onChanged();
  };
  return (
    <Panel title="Custom domain">
      {current ? (
        <div className="grid gap-2 text-sm">
          <p>
            <span className="font-medium">{current.domain}</span>{" "}
            <span className="text-amber-800">({current.status})</span>
          </p>
          <p className="text-slate-600">To prove you control it, publish this value in a DNS TXT record:</p>
          <code className="block break-all rounded bg-slate-100 px-3 py-2 font-mono" data-testid="domain-token">
            {current.verification_token}
          </code>
          <p className="text-xs text-slate-500">Checking the record, and serving WindOps there, come in a later release.</p>
          <div>
            <Button variant="outline" className="text-red-700" onClick={() => void remove()}>
              Remove domain
            </Button>
          </div>
        </div>
      ) : (
        <form onSubmit={(event) => void save(event)} className="flex flex-col gap-2 sm:flex-row sm:items-end">
          <label className={`${LABEL} grow`}>
            Domain, such as ops.example.com
            <input
              className={`${INPUT} mt-1`}
              required
              minLength={3}
              maxLength={253}
              value={domain}
              onChange={(event) => setDomain(event.target.value)}
            />
          </label>
          <Button type="submit" disabled={change.state.kind === "busy"}>
            Set domain
          </Button>
        </form>
      )}
      <ChangeStatus state={change.state} onDismiss={change.reset} />
    </Panel>
  );
}

/** A tenant's own branding, which only its administrators change (SRS 3.2, FR-TEN-002). */
export function BrandingPage({ api }: { api: AdminApi }) {
  const [state, reload] = useLoad(useCallback(() => api.branding(), [api]));
  const [latest, setLatest] = useState<OwnBranding | null>(null);
  return (
    <Loading state={state}>
      {(loaded) => {
        const branding = latest ?? loaded;
        return (
          <div className="grid gap-4">
            <Look api={api} branding={branding} onSaved={setLatest} />
            <div className="grid gap-4 lg:grid-cols-2">
              <Logo api={api} branding={branding} onSaved={setLatest} />
              <Domain
                api={api}
                branding={branding}
                onChanged={() => {
                  setLatest(null);
                  reload();
                }}
              />
            </div>
            <Emails api={api} />
          </div>
        );
      }}
    </Loading>
  );
}
