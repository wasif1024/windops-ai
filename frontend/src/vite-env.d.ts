/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Only for the dev server: Keycloak's address, the gateway's https://localhost:9443. */
  readonly VITE_AUTH_ORIGIN?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
