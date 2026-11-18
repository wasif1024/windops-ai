# windops-claude

The Claude model policy (SRS 5.2, LLM-001 to LLM-010): every call to Claude goes through it. The decisions are in
[ADR 0017](../../docs/adr/0017-the-claude-policy-as-versioned-configuration.md).

| Module | What it does |
| --- | --- |
| `claude-policy.yaml` | Each role's model, effort and output limit, the prices, and each tenant's budget. Versioned |
| `policy.py` | Loads and checks the policy, and gives its version and content hash, which runs pin |
| `models.py` | Makes a role's chat model from the policy, with refusal fallback where it is taken, and structured outputs |
| `responses.py` | Checks the stop reason before content is read, and notices fallbacks |
| `pricing.py` | What a call cost, from its usage and the policy's prices |
| `budget.py` | A run's token and cost budget, checked before and after each call |
| `conversation.py` | The cached stable prompt, the append-only history guard, and dropping private reasoning |
| `client.py` | One call, in order: budget, model, history, call, record, budget, stop reason |

## Changing the policy

Edit `claude-policy.yaml`, raise `version`, then:

```bash
make claude-policy-lock
```

A test fails until the new version's hash is locked. A model or effort change must also pass the Section 10 gates
before release (LLM-002). Runs queued under the old version end as `stale` when the new one is deployed.

## Using it

```python
from windops_claude import Claude, Meter, MemoryLedger, Budget, load, stable_system

claude = Claude(load(), Meter(budget, ledger), api_key=read_key)
result = await claude.invoke("scada-analyst", [stable_system(INSTRUCTIONS), case_message])
```

`result.message` may be read: its stop reason was checked. `result.call` holds the model that served it, any
fallback, the tokens and the cost. In the Agent Runtime, each workflow's context gives a client already metered
against the run's budget.
