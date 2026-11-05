# Keycloak

`windops-keycloak` handles login, credentials and sessions (SEC-006). The Authorization Service decides what
a signed-in user may do, once M3.5 adds it. Keycloak keeps its data in its own database, `windops_keycloak`.

## Set it up

```bash
make secrets         # creates its administrator password and database login
make images          # builds windops-keycloak:local
make infra           # deploys it
make identity-seed   # creates the realm, tenants, client and demo users
```

`make identity-seed` is safe to repeat. It updates settings to match, and keeps existing users' passwords.

## What the seed creates

| Item | Value |
| --- | --- |
| Realm | `windops`, with Organizations turned on |
| Tenants | Organizations `north-sea` and `atlas-wind`, which are synthetic |
| Client | `windops-web`: a public client using authorization code with PKCE, and no password grant |
| Demo users | `operator@north-sea.example` and `operator@atlas-wind.example` |

Demo passwords are generated into the Secret `windops-identity-demo-users`. To read one:

```bash
kubectl --context windops --namespace windops-system get secret windops-identity-demo-users \
  --output jsonpath='{.data.north-sea}' | base64 --decode
```

## Tokens and rules

| Rule | Setting | Requirement |
| --- | --- | --- |
| Signed, short-lived access tokens | RS256, 5 minutes | SEC-001 |
| Refresh tokens rotate | Each can be used once | SEC-001 |
| Password policy | At least 12 characters, with upper and lower case, a digit and a symbol. Not the username, and not one of the last 5 | SEC-004 |
| Account lockout | After 5 failed attempts, with a wait that grows to 15 minutes | SEC-004 |
| Session revocation | An administrator can end a user's sessions, and their refresh tokens stop working | SEC-004 |
| One tenant model | Organizations in one realm | SEC-005 |
| Verified tenant | The `tenant_id` claim holds the user's organization alias, and comes only from Keycloak | SEC-005 |

## Vendor check: Organizations as the tenant model

SRS 13 requires this to be confirmed. **Result on 2026-11-05, on Keycloak 26.8.1: confirmed, so the model stays
as planned, and realm-per-tenant is not needed.** `make identity-check` repeats the test.

| User | `tenant_id` in the token |
| --- | --- |
| A member of one organization | That organization's alias |
| A member of no organization | None |
| A member of two organizations | None, because Keycloak does not guess |

Keycloak adds a tenant only when the `organization` scope applies, and that scope is optional by default. So the
seed makes it a default for `windops-web`, and a login cannot leave the tenant out. This must be repeated for any
new client.

## Reach the admin console

```bash
kubectl --context windops --namespace windops-system port-forward service/windops-keycloak 8081:8080
```

Open <http://localhost:8081>, and sign in as `windops_admin`. The password is in
`infrastructure/minikube/secrets.local.keycloak.env`.

## Back up and reset

Its data is the `windops_keycloak` database, so [back it up as PostgreSQL](../../data/postgres/README.md). To
reset it, delete its Deployment, drop that database, then run `make infra` and `make identity-seed`.

## When Keycloak is down

New logins and token exchange fail, and nothing falls back to weaker access (SRS 7.7). Tokens already issued
keep working until they expire.

## Limits

- Nothing requires `tenant_id` yet: a user with no organization, or two, gets a valid token with no tenant. M3.7's
  gateway must refuse those (SEC-005).
- MFA arrives in M3.2. Roles belong to the Authorization Service, so M3.5 seeds them (SEC-006).
- It uses plain HTTP and a flexible hostname until M3.7. Until then, a token's issuer follows the address used
  to reach it.
- One replica, so a restart interrupts login.
