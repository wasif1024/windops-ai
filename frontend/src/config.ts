// The frontend's settings, which differ between environments (see .env.example), read once at
// start. In the image, the pod writes /config.json from windops-settings when it starts, so the
// same image works anywhere. On the dev server, scripts/dev.sh passes them as VITE_ variables.

export interface Config {
  /** WINDOPS_KEYCLOAK_REALM */
  realm: string;
  /** WINDOPS_WEB_CLIENT_ID */
  clientId: string;
}

// Plain names only, as the pod's start-up script also insists, so nothing odd reaches a URL.
const NAME = /^[A-Za-z0-9._-]{1,64}$/;

export function parseConfig(value: unknown): Config {
  const body = (value ?? {}) as Record<string, unknown>;
  const { realm, clientId } = body;
  if (typeof realm !== "string" || !NAME.test(realm) || typeof clientId !== "string" || !NAME.test(clientId)) {
    throw new Error("The frontend's settings are not valid");
  }
  return { realm, clientId };
}

export async function loadConfig(fetcher: typeof fetch = fetch): Promise<Config> {
  if (import.meta.env.DEV) {
    return parseConfig({
      realm: import.meta.env.VITE_KEYCLOAK_REALM || "windops",
      clientId: import.meta.env.VITE_WEB_CLIENT_ID || "windops-web",
    });
  }
  const response = await fetcher("/config.json", { credentials: "omit", cache: "no-cache" });
  if (!response.ok) throw new Error("The frontend's settings could not be read");
  return parseConfig(await response.json());
}
