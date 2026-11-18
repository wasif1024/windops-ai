"""Find what must not cross a trust boundary, in text going out or coming in.

- **Credentials:** a private key, an API key, a token or a password, by its shape. Text holding
  one is refused before it is kept or sent anywhere (SRS 9.6). A finding names what and where,
  never the value.
- **Instructions to an AI:** text that tells its reader to ignore its instructions, claims to
  override the system, or addresses an assistant directly, as an injected instruction does
  (LLM-008). A finding reports it, without the matched text; what to do is the caller's decision.

Used by the Knowledge Service, on uploads and queries, and the External Intelligence Service, on
outbound queries and the web content it brings back.
"""

import re
from dataclasses import dataclass

# Credentials, by their shape. Each is refused, wherever it appears.
CREDENTIALS = (
    ("a private key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("an AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("an API key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}")),
    # Tavily's, which the External Intelligence Service holds (M7.4).
    ("an API key", re.compile(r"\btvly-(?:dev-|prod-)?[A-Za-z0-9_-]{20,}")),
    ("a GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}")),
    ("a Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("a JSON web token",
     re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+")),
    ("a password", re.compile(r"(?i)\b(?:password|passwd|pwd)\s*[:=]\s*\S{4,}")),
)  # fmt: skip
# Text addressed to an AI, as an injected instruction is (LLM-008). A finding holds the document
# for a person; it does not decide anything about it.
INSTRUCTIONS_TO_AI = (
    ("It tells the reader to ignore earlier instructions",
     re.compile(r"(?i)\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b(?:previous|prior|earlier|above"
                r"|system)\b[^.\n]{0,20}\b(?:instructions?|prompts?|rules)\b")),
    ("It claims to override the system", re.compile(r"(?i)\bsystem\s+override\b")),
    ("It addresses AI assistants or models directly",
     re.compile(r"(?i)\b(?:note|message|instructions?)\s+(?:to|for)\s+(?:the\s+)?(?:ai|automated|"
                r"llm|language model|assistant)")),
    ("It tells the reader to hide something from the user",
     re.compile(r"(?i)\bdo not (?:mention|reveal|tell)\b[^.\n]{0,40}\b(?:this|the user|the "
                r"operator)\b")),
    ("It tells the reader what role to take", re.compile(r"(?i)\byou are now\b")),
)  # fmt: skip


@dataclass(frozen=True)
class Finding:
    kind: str
    description: str
    line: int


def credentials_in(text: str) -> list[tuple[str, int]]:
    """Each credential found, by what it is and its line. Never the credential itself."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for name, pattern in CREDENTIALS:
            if pattern.search(line):
                found.append((name, number))
    return found


def screen(text: str) -> tuple[Finding, ...]:
    """Each line addressing instructions to an AI, by what it does. Never the line itself."""
    findings = []
    for number, line in enumerate(text.splitlines(), start=1):
        for description, pattern in INSTRUCTIONS_TO_AI:
            if pattern.search(line):
                findings.append(Finding("instructions_to_ai", description, number))
    return tuple(findings)
