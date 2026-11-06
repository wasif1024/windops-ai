/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Only for the dev server: Keycloak's address, the gateway's https://localhost:9443. */
  readonly VITE_AUTH_ORIGIN?: string;
  /** Only for the dev server: WINDOPS_KEYCLOAK_REALM and WINDOPS_WEB_CLIENT_ID. */
  readonly VITE_KEYCLOAK_REALM?: string;
  readonly VITE_WEB_CLIENT_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
