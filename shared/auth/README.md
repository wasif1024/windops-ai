# windops-auth

Decides whether a token shows enough authentication. It takes claims that have **already been verified**, and
does not check signatures itself.

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

No service calls these yet. M3.5's Authorization Service does, once M3.7 passes these claims through.
