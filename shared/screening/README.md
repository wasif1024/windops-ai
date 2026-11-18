# windops-screening

Finds what must not cross a trust boundary, in text going out of the platform or coming into it:

- **Credentials:** private keys, API keys, tokens and passwords, by their shape. A finding names what it is and its
  line, never the value.
- **Instructions to an AI:** text that tells its reader to ignore its instructions, claims to override the system, or
  addresses an assistant directly, as an injected instruction does (LLM-008). A finding says what the line does,
  never what it says.

It decides nothing: the Knowledge Service refuses an upload holding a credential and holds one addressed to an AI,
and the External Intelligence Service refuses a query holding a credential and flags web content addressed to an AI.

```bash
uv run pytest shared/screening/tests
```
