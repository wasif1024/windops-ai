# windops-frontend

The browser's application (SRS 4.11): React and TypeScript, in its own image and workload (DEP-001). The gateway
serves it at `https://localhost:9443/`, on the same address as the API and Keycloak's sign-in pages.

## What the shell does

| Part | How |
| --- | --- |
| Sign-in | Keycloak's authorization code flow with PKCE, through the gateway, using `oidc-client-ts` |
| Tokens | Kept in memory only, never in `localStorage` or `sessionStorage`, so script on the page cannot read them back from storage. A reload asks Keycloak again, which signs the user straight back in while their session lasts |
| Branding | The tenant's name, colors and logo, before sign-in from `?tenant=`, and after it from the user's own tenant. Colors from the API must be hex, or the defaults are used |
| Navigation | Only the SRS 4.11 screens the user's permissions include, from `/api/v1/me/permissions`. Each screen says which phase builds it |
| API answers | RFC 9457 problems, with a stable `code`. `step_up_required` means the user must use their second factor again |

Hiding a screen is a convenience, not protection. Every operation is still checked by the services.

## Structure

```text
src/
  main.tsx          entry point
  app/              the shell: App, routes, navigation, sign-in and placeholder pages
  auth/             sign-in with Keycloak, and tokens
  api/              the Web BFF client, its errors and response types
  branding/         a tenant's colors, title and logo
  components/       building blocks more than one feature uses, such as Button and Logo
  features/         one folder per SRS 4.11 area, as each is built
  styles/           Tailwind, and the brand color variables
  assets/           images the code imports, which Vite fingerprints
e2e/                browser journeys, with Playwright
```

A feature owns its folder, so adding or removing one touches one place:

```text
features/admin/
  pages/            UsersPage.tsx, one per route
  components/       UserTable.tsx, used only here
  api.ts            this feature's calls to Web BFF
  *.test.tsx        tests beside the code they test
```

A component moves to `components/` only once a second feature needs it.

## Styling

[Tailwind CSS](https://tailwindcss.com) 4, with utility classes in the markup. It writes one static stylesheet at
build time, so the strict Content Security Policy needs no inline styles.

A tenant's branding sets two CSS variables, `--brand-primary` and `--brand-accent`, and Tailwind's theme maps them to
`bg-brand-primary`, `border-brand-accent` and the rest, so every screen follows the tenant's colors with no code of
its own. Tests find elements by role, label or `data-testid`, never by a styling class.

## Security headers

nginx sends these on every page, from one file, so no location can lose them:

- **Content Security Policy:** everything comes from this one address, with no inline script, `frame-ancestors
  'none'`, and `base-uri 'none'`
- **No content sniffing:** `X-Content-Type-Options: nosniff`
- **No referrer:** `Referrer-Policy: no-referrer`
- **Window isolation:** `Cross-Origin-Opener-Policy: same-origin`
- **No device access:** a Permissions Policy that turns off the camera, microphone, location and payment

## Work on it

```bash
cd frontend
npm ci
npm run dev        # http://localhost:5173, which Keycloak also accepts
npm run lint
npm run typecheck
npm test
```

`make frontend-test` runs what CI runs. `make frontend-check` signs in through a real Chromium, through the gateway,
and checks the branding, the navigation, the headers and that no token reaches browser storage. The browser trusts
only the gateway's own key, so certificate checks stay on.

## Dependencies

Every version is exact, and `package-lock.json` holds the rest. `npm audit` must find nothing of moderate severity
or above. ESLint 10 still asks for `minimatch` 10, which has a known denial-of-service bug (GHSA-g3hv-75wg-m249), so
`overrides` sets the patched 11.0.5. ESLint works with it, which CI proves on every build.

## Limits

- **Plain HTTP from the gateway.** The gateway reaches this pod over plain HTTP. It serves the same public files to
  everyone, and tokens never pass through it.
- **A reload means a sign-in redirect.** Tokens are not kept across a reload. Keycloak's session cookie makes the
  sign-in instant while the session lasts.
- **Screens are placeholders.** Each says which phase builds it.
- **Step-up is not wired up yet.** The administrator console (M3.9) asks for a second factor when the API needs one.
