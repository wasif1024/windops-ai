# windops-auth

Holds the signed internal identity that every gRPC call carries (GRP-003, GRP-004), and decides whether a user's
token shows enough authentication. The MFA checks take claims that have **already been verified**, and do not check
signatures themselves.

| Check | Use it for | Requirement |
| --- | --- | --- |
| `require_mfa(claims)` | Administrative access | SEC-002 |
| `require_recent_mfa(claims)` | Sensitive administrative operations and approval-policy changes | SEC-003 |

```python
from windops_auth import StepUpRequiredError, require_recent_mfa

try:
    require_recent_mfa(claims)
except StepUpRequiredError:
    ...  # Refuse, and ask the frontend to sign in again with acr_values=mfa.
```

## Why `acr` is not enough

Keycloak keeps reporting `acr=mfa` on refreshed tokens after the level's 5-minute limit has passed, so checking
`acr` alone would accept a step-up from hours ago. `require_recent_mfa` also checks `auth_time`, which moves when
the user uses a second factor. `make mfa-check` proves both on the real server, so re-run it after upgrading
Keycloak.

## Limits

The Authorization, Identity and Tenant services call `require_recent_mfa` before every change. They read `acr`
and `auth_time` from the signed internal identity, which Web BFF copies from the user's verified Keycloak token.
