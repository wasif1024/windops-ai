// MCP integrations and gateway policies (SRS 9.4), as built into this release. Both are platform
// configuration: a change is made by pull request, reviewed there, and deployed with a release,
// never from this console, so these pages show exactly what is in force and which release it is.
import { useCallback, useState } from "react";
import { useLoad } from "../../../api/useLoad";
import { Loading, Panel } from "../../../components/Panel";
import { StatusBadge } from "../../fleet/Freshness";
import { titleCase } from "../../fleet/format";
import type { AdminApi, GatewayRoute, McpServer, PlatformCatalog } from "../api";

const TAG = "bg-slate-100 text-slate-700 ring-1 ring-slate-200";
const WARN = "bg-amber-50 text-amber-900 ring-1 ring-amber-200";

function useCatalog(api: AdminApi) {
  return useLoad(useCallback(() => api.platformCatalog(), [api]));
}

function Release({ catalog }: { catalog: PlatformCatalog }) {
  return (
    <p className="text-sm text-slate-600" data-testid="release">
      As built into release <span className="font-mono">{catalog.release}</span>. These change only by pull request,
      reviewed there and deployed with a release: never from this console.
    </p>
  );
}

function Server({ server }: { server: McpServer }) {
  return (
    <Panel
      title={`${titleCase(server.server)} MCP`}
      actions={
        server.local_only ? <StatusBadge status="Development only" className={WARN} /> : <StatusBadge status="Deployed" className={TAG} />
      }
    >
      <div className="space-y-3 text-sm" data-testid={`mcp-${server.server}`}>
        <p className="text-slate-600">
          Calls {server.calls}. {server.boundary}.
        </p>
        <ul className="divide-y divide-slate-100" aria-label={`${server.server} tools`}>
          {server.tools.map((tool) => (
            <li key={tool.name} className="py-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="break-all font-mono text-slate-900">{tool.name}</span>
                <span className="flex flex-wrap gap-1">
                  {!tool.read_only && <StatusBadge status="Writes" className={WARN} />}
                  {tool.external && <StatusBadge status="External content" className={WARN} />}
                </span>
              </div>
              <p className="mt-0.5 break-words text-slate-700">{tool.description}</p>
              <p className="mt-0.5 text-xs text-slate-500">
                {tool.timeout_seconds} s timeout · {tool.per_minute} a minute per caller · {tool.per_day.toLocaleString()} a day per
                tenant · {tool.agents.length ? `agents: ${tool.agents.join(", ")}` : "no agent holds it"}
              </p>
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  );
}

export function McpIntegrationsPage({ api }: { api: AdminApi }) {
  const [state] = useCatalog(api);
  const [all, setAll] = useState(false);
  return (
    <Loading state={state}>
      {(catalog) => {
        const shown = catalog.mcp_servers.filter((s) => all || !s.local_only);
        const tools = catalog.mcp_servers.flatMap((s) => s.tools);
        return (
          <div className="space-y-4">
            <Release catalog={catalog} />
            <p className="text-sm text-slate-600">
              Agents reach the platform only through these servers, each calling as the person behind the agent, so it can never do
              more than they could. {tools.length} tools; {tools.filter((t) => !t.read_only).length} writes, a draft only.
            </p>
            <label className="inline-flex items-center gap-2 text-sm">
              <input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} />
              Show development-only servers
            </label>
            {shown.map((server) => (
              <Server key={server.server} server={server} />
            ))}
          </div>
        );
      }}
    </Loading>
  );
}

function RouteView({ route }: { route: GatewayRoute }) {
  const t = route.timeouts_seconds;
  return (
    <li className="py-3" data-testid={`route-${route.name}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="font-medium text-slate-900">{route.name}</span>
        <span className="break-all font-mono text-xs text-slate-500">{route.uris.join(", ")}</span>
      </div>
      <p className="mt-0.5 text-slate-700">{route.purpose}</p>
      <dl className="mt-1 grid gap-x-4 gap-y-0.5 text-xs sm:grid-cols-[auto_1fr]">
        <dt className="text-slate-500">Methods</dt>
        <dd>{route.methods.join(", ")}</dd>
        <dt className="text-slate-500">Sign-in</dt>
        <dd>{route.sign_in}</dd>
        <dt className="text-slate-500">Limit</dt>
        <dd>
          {route.limit
            ? `${route.limit.count} requests each ${route.limit.window_seconds} s per ${route.limit.per}, then ${route.limit.rejected_code}`
            : "none at the gateway"}
        </dd>
        <dt className="text-slate-500">Cross-origin</dt>
        <dd>
          {route.cors
            ? `${route.cors.origins}; ${route.cors.methods}; ${route.cors.credentials ? "with credentials" : "never with credentials"}`
            : "not allowed"}
        </dd>
        <dt className="text-slate-500">Upstream</dt>
        <dd className="break-all">
          {route.upstream.join(", ")}
          {route.upstream_tls ? ", over verified TLS" : ""}
          {route.health_checked ? ", health-checked" : ""}
        </dd>
        <dt className="text-slate-500">Timeouts</dt>
        <dd>
          connect {t.connect ?? "—"} s, send {t.send ?? "—"} s, read {t.read ?? "—"} s
        </dd>
      </dl>
    </li>
  );
}

export function GatewayPoliciesPage({ api }: { api: AdminApi }) {
  const [state] = useCatalog(api);
  return (
    <Loading state={state}>
      {(catalog) => (
        <div className="space-y-4">
          <Release catalog={catalog} />
          <Panel title="Gateway routes">
            <p className="text-sm text-slate-600">
              The API gateway is the only way in. Each route below is everything it lets through, and how.
            </p>
            <ul className="divide-y divide-slate-100 text-sm" aria-label="Gateway routes">
              {catalog.gateway_routes.map((route) => (
                <RouteView key={route.name} route={route} />
              ))}
            </ul>
          </Panel>
          <Panel title="Per-person API limits">
            <ul className="space-y-1 text-sm" aria-label="API limits" data-testid="api-limits">
              {catalog.api_limits.per_user.map((limit) => (
                <li key={limit.traffic}>
                  {titleCase(limit.traffic)}: {limit.requests} requests each {catalog.api_limits.window_seconds} s per person, in
                  each tenant
                </li>
              ))}
            </ul>
          </Panel>
        </div>
      )}
    </Loading>
  );
}
