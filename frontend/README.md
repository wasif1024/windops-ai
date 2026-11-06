# windops-frontend

The browser's application (SRS 4.11): React and TypeScript, in its own image and workload (DEP-001). The gateway
serves it at `WINDOPS_PUBLIC_URL`, `https://localhost:9443/` locally, on the same address as the API and Keycloak's sign-in pages.

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

## Phones, tablets and desktops (UX-001)

Every screen works from 360 pixels wide, without scrolling sideways. Below Tailwind's `md` breakpoint (768 pixels),
the sidebar becomes a menu button, which opens a slide-over menu. It closes on Escape, a tap outside, or a choice,
and returns focus to the button. The browser check runs at desktop size and at phone size, as a Pixel 7.

Write mobile first: the plain class is the phone, and `md:` and `lg:` add the larger layouts.

## Administrator console (SRS 9.4)

`features/admin/` is the console for a tenant's own administrators. It works in the signed-in user's tenant only.
Each tab shows only to someone holding its permission, and the services check again on every call.

| Tab | Needs | What |
| --- | --- | --- |
| Users | `users.manage` | Add users, who get a temporary password shown once. Disable or reactivate them, see their second factor and sessions, and end their sessions. With `roles.manage`, give and revoke roles for the tenant, a wind farm or a turbine |
| Role permissions | `permissions.manage` | Choose what each role gives in this tenant |
| Access denials | `audit.read` | Refused attempts, from the audit trail |
| Branding | `branding.manage` | The name, colors, sign-in text, time zone and language, the logo, the three emails, and the custom domain with the token to publish. Colors, the logo and placeholders are checked before sending |

Every change sends a new `Idempotency-Key`, and the same one if it is sent again after an outage, so it happens
once. When a change needs a recent second factor, the screen says so and offers **Verify now**, which goes to Keycloak
for a code only, then comes back to the same tab. The lists of roles and permissions to choose from are in
`catalog.ts`, which `tests/test_admin_catalog.py` keeps the same as the Authorization Service's policy.

The browser check signs in as a temporary Tenant Administrator at each size. `make frontend-check` sets up their
authenticator straight with Keycloak, because the gateway allows 5 sign-ins a minute from one address. In the
browser, the desktop journey verifies a real code, then adds a user, gives and revokes a role, and disables and
reactivates them. Every user it makes is deleted afterwards.

## Platform console (M3.10)

`features/platform/` is for the Platform Administrator only (`tenant.manage`), across tenants:

| Tab | What |
| --- | --- |
| Tenants | List and create tenants, open one to suspend it with a reason, or reactivate it, and view its branding and domain. Only the tenant's own administrators change branding (SRS 3.2) |
| Break-glass access | Start reasoned, time-limited, read-only access to one customer tenant, see your active grants, and end one early (RBAC-003) |

Every change asks for a second factor, as in the administrator console. Pieces both consoles use live in
`components/` (`Panel`, `ChangeStatus`) and `api/` (`useChange`, `useLoad`).

The browser check runs this journey as a temporary Platform Administrator too. The journeys share `e2e/helpers.ts`,
which signs in through Keycloak's pages and, when the gateway's limit of 5 sign-ins a minute refuses one, waits out
the minute and tries again.

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

`make dev` from the repository root runs this dev server with the backend, which is the easy way. See
[Run it and see it](../README.md#run-it-and-see-it). By hand, with the gateway forwarded (`make gateway-forward`):

```bash
cd frontend
npm ci
NODE_EXTRA_CA_CERTS=<the cluster CA> VITE_AUTH_ORIGIN=https://localhost:9443 VITE_API_TARGET=https://localhost:9443 npm run dev
npm run lint
npm run typecheck
npm test
```

On the dev server, `/api` goes through Vite's proxy to the gateway, and `VITE_AUTH_ORIGIN` tells the app that
Keycloak is at the gateway, not on port 5173. The image needs neither, since it is behind the gateway itself.

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
- **Screens are placeholders,** except Administration and Platform. Each says which phase builds it.
- **A change after step-up is made again by hand.** Verifying a second factor goes to Keycloak and back, which
  reloads the page, so the form is empty again. Nothing is kept in storage to replay it.
- **A logo cannot be removed,** only replaced, as the Tenant Service has no call to remove one.
- **Access denials are read oldest first.** The list reads up to 1,000 and shows them newest first. A tenant with
  more needs the audit explorer (Phase 11).
