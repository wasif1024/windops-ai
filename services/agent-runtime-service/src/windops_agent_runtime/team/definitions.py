"""The agents and skills, as defined in the repository's agents/ and skills/ folders (SRS 11.5).

Each agent is a folder: agent.yaml says its role, tools, skills and delegates, and prompt.md is
its stable system prompt. Each skill is a folder with a SKILL.md. All are checked when loaded:

- an agent's role is in the Claude policy, which alone sets its model and effort (LLM-001);
- its prompt holds nothing that changes between runs, so it can be cached (LLM-007);
- its skills and delegates exist.

Each prompt, tool list and skill has a content hash, which a run pins (AGT-004): a run resumed
after any of them changed ends as stale. Whether each agent's tools match its allowlist in the
Identity Service is checked by a repository test (ADR 0020).
"""

import hashlib
import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from windops_claude import Policy, load
from windops_claude.conversation import check_stable

# The repository root locally; the image copies agents/ and skills/ to /app.
ROOT = Path(os.environ.get("WINDOPS_AGENTS_ROOT", Path(__file__).resolve().parents[5]))
SUPERVISOR = "supervisor"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class Skill:
    name: str
    version: int
    description: str
    content: str

    @property
    def content_hash(self) -> str:
        return _hash(self.content)


@dataclass(frozen=True)
class Agent:
    name: str
    version: int
    role: str
    description: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    delegates: tuple[str, ...]
    prompt: str

    @property
    def prompt_hash(self) -> str:
        return _hash(self.prompt)

    @property
    def tools_hash(self) -> str:
        return _hash("\n".join(sorted(self.tools)))


@dataclass(frozen=True)
class Team:
    agents: dict[str, Agent]
    skills: dict[str, Skill]

    def pins(self) -> dict[str, Any]:
        """What a run is pinned to: each prompt, tool list and skill, by version and hash."""
        return {
            "prompts": {n: f"{a.version}:{a.prompt_hash}" for n, a in sorted(self.agents.items())},
            "tools": {n: a.tools_hash for n, a in sorted(self.agents.items())},
            "skills": {n: f"{s.version}:{s.content_hash}" for n, s in sorted(self.skills.items())},
        }


def _frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---\n"):
        raise ValueError("A SKILL.md starts with its frontmatter")
    header = text[4 : text.index("\n---", 4)]
    found: dict[str, Any] = yaml.safe_load(header)
    return found


def load_skill(folder: Path) -> Skill:
    content = (folder / "SKILL.md").read_text()
    header = _frontmatter(content)
    if header.get("name") != folder.name:
        raise ValueError(f"The skill in {folder.name} names itself {header.get('name')!r}")
    return Skill(
        name=folder.name, version=int(header["version"]), description=str(header["description"]),
        content=content,
    )  # fmt: skip


def load_agent(folder: Path) -> Agent:
    spec = yaml.safe_load((folder / "agent.yaml").read_text())
    if spec.get("name") != folder.name:
        raise ValueError(f"The agent in {folder.name} names itself {spec.get('name')!r}")
    prompt = (folder / "prompt.md").read_text().strip()
    check_stable(prompt)
    return Agent(
        name=folder.name, version=int(spec["version"]), role=str(spec["role"]),
        description=str(spec["description"]), tools=tuple(spec.get("tools") or ()),
        skills=tuple(spec.get("skills") or ()), delegates=tuple(spec.get("delegates") or ()),
        prompt=prompt,
    )  # fmt: skip


def load_team(root: Path = ROOT, policy: Policy | None = None) -> Team:
    policy = policy or load()
    skills = {f.name: load_skill(f) for f in sorted((root / "skills").iterdir()) if f.is_dir()}
    agents = {f.name: load_agent(f) for f in sorted((root / "agents").iterdir()) if f.is_dir()}
    if SUPERVISOR not in agents:
        raise ValueError("There is no supervisor")
    for agent in agents.values():
        if agent.role not in policy.roles:
            raise ValueError(f"{agent.name}'s role {agent.role!r} is not in the Claude policy")
        missing = [s for s in agent.skills if s not in skills]
        if missing:
            raise ValueError(f"{agent.name} names skills that do not exist: {missing}")
        strangers = [d for d in agent.delegates if d not in agents or d == SUPERVISOR]
        if strangers:
            raise ValueError(f"{agent.name} delegates to agents that do not exist: {strangers}")
    return Team(agents=agents, skills=skills)


@cache
def team() -> Team:
    return load_team()
