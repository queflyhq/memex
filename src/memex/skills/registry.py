"""
Registry client — fetch skills from a remote git-hosted registry.

Default registry: `queflyhq/memex-skills` on the `main` branch. Override via
`MEMEX_SKILL_REGISTRY` env var (`<owner>/<repo>`) or by passing
`owner_repo` directly.

Skills are fetched over HTTPS from `raw.githubusercontent.com`. No git
client is needed — just `httpx`. Versions are resolved via the same URL
prefix: `<owner>/<repo>/<ref>/skills/<name>/...`. `<ref>` may be `main`,
a tag (`v0.1.0`), or a branch.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from memex.skills.loader import Skill

log = logging.getLogger(__name__)


DEFAULT_OWNER_REPO = "queflyhq/memex-skills"
DEFAULT_REF = "main"


class SkillNotFoundError(LookupError):
    """Raised when a skill name is not present in the registry."""


class RegistryFetchError(RuntimeError):
    """Raised when a registry HTTP fetch fails after retries."""


@dataclass(slots=True)
class RegistryEntry:
    name: str
    version: str
    description: str
    path: str
    scope: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> RegistryEntry:
        return cls(
            name=d["name"],
            version=d.get("version", "0.0.0"),
            description=d.get("description", ""),
            path=d["path"],
            scope=d.get("scope", ""),
        )


class RegistryClient:
    """HTTPS-based skill registry client.

    Stateless — each method does its own HTTP fetch. Suitable for CLI use
    where the process is short-lived.
    """

    def __init__(
        self,
        owner_repo: str = DEFAULT_OWNER_REPO,
        ref: str = DEFAULT_REF,
        timeout: float = 15.0,
    ):
        self.owner_repo = owner_repo
        self.ref = ref
        self.timeout = timeout

    @property
    def base_url(self) -> str:
        return f"https://raw.githubusercontent.com/{self.owner_repo}/{self.ref}"

    def _get_text(self, path: str) -> str:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
        except httpx.HTTPError as e:
            raise RegistryFetchError(f"GET {url} failed: {e}") from e
        if resp.status_code == 404:
            raise SkillNotFoundError(f"not found in registry: {path}")
        if resp.status_code >= 400:
            raise RegistryFetchError(f"GET {url} returned {resp.status_code}: {resp.text[:200]}")
        return resp.text

    def fetch_index(self) -> list[RegistryEntry]:
        """Return the list of skills declared in `registry.json`."""
        text = self._get_text("registry.json")
        data = json.loads(text)
        return [RegistryEntry.from_dict(s) for s in data.get("skills", [])]

    def find_entry(self, name: str) -> RegistryEntry:
        for entry in self.fetch_index():
            if entry.name == name:
                return entry
        raise SkillNotFoundError(
            f"skill `{name}` not found in registry {self.owner_repo}@{self.ref}"
        )

    def fetch_skill(self, name: str) -> Skill:
        """Fetch a skill bundle (manifest + concepts.jsonl) and return a Skill."""
        entry = self.find_entry(name)
        manifest = json.loads(self._get_text(f"{entry.path}/manifest.json"))
        try:
            concepts_text = self._get_text(f"{entry.path}/concepts.jsonl")
        except SkillNotFoundError:
            concepts_text = ""
        try:
            edges_text = self._get_text(f"{entry.path}/edges.jsonl")
        except SkillNotFoundError:
            edges_text = ""

        concepts = [json.loads(line) for line in concepts_text.splitlines() if line.strip()]
        edges = [json.loads(line) for line in edges_text.splitlines() if line.strip()]
        return Skill(
            name=manifest["name"],
            version=manifest.get("version", "0.0.0"),
            description=manifest.get("description", ""),
            concepts=concepts,
            edges=edges,
        )


def parse_target(target: str) -> tuple[str, str, str]:
    """Parse `skill:NAME[@REF]` or `skill:OWNER/REPO/NAME[@REF]` into (owner_repo, ref, name).

    Examples:
      'aws-iam-essentials'              -> (DEFAULT_OWNER_REPO, 'main', 'aws-iam-essentials')
      'aws-iam-essentials@v0.1.0'       -> (DEFAULT_OWNER_REPO, 'v0.1.0', 'aws-iam-essentials')
      'org/repo/skill-name'             -> ('org/repo', 'main', 'skill-name')
      'org/repo/skill-name@feature-x'   -> ('org/repo', 'feature-x', 'skill-name')
    """
    target = target.strip()
    if "@" in target:
        target, _, ref = target.partition("@")
    else:
        ref = DEFAULT_REF
    parts = target.split("/")
    if len(parts) == 1:
        return DEFAULT_OWNER_REPO, ref, parts[0]
    if len(parts) >= 3:
        owner = parts[0]
        repo = parts[1]
        name = "/".join(parts[2:])
        return f"{owner}/{repo}", ref, name
    raise ValueError(
        f"invalid skill target `{target}`: expected NAME, OWNER/REPO/NAME, or those with @REF"
    )
