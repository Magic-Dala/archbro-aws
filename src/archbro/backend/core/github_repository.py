"""Non-secret project repository identity, independent of OAuth session lifetime."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://github\.com/)?(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def normalize_github_repository(value: str) -> str:
    value = value.strip()
    match = _GITHUB_REPOSITORY.fullmatch(value)
    if not match or any(match.group(key) in {".", ".."} for key in ("owner", "repo")):
        raise ValueError("repository must be owner/repo or an https://github.com/owner/repo URL")
    if len(match.group('owner')) > 100 or len(match.group('repo')) > 100:
        raise ValueError("repository name is too long")
    return f"{match.group('owner')}/{match.group('repo')}"


def normalize_repository_branch(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    value = value.strip()
    if (len(value) > 200 or re.search(r"[\s\x00-\x1f\x7f~^:?*\[\\]", value)
            or '..' in value or '@{' in value or value == '@'
            or value.startswith(('/', '-')) or value.endswith(('/', '.'))
            or any(not part or part.startswith('.') or part.endswith('.lock') for part in value.split('/'))):
        raise ValueError("branch must be a valid Git branch/ref without spaces")
    return value


class GitHubRepositoryBinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Literal['github'] = 'github'
    full_name: str = Field(min_length=3, max_length=300)
    branch: str | None = Field(default=None, max_length=200)
    repository_id: int | None = Field(default=None, gt=0)
    selected_by_user_id: str = Field(min_length=1, max_length=200)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    _repository = field_validator('full_name')(normalize_github_repository)
    _branch = field_validator('branch')(normalize_repository_branch)

    @property
    def owner(self) -> str:
        return self.full_name.split('/', 1)[0]

    @property
    def repo(self) -> str:
        return self.full_name.split('/', 1)[1]


def require_matching_repository(binding: GitHubRepositoryBinding | None, repository: str) -> None:
    if binding and normalize_github_repository(repository).casefold() != binding.full_name.casefold():
        raise ValueError("repository_scope_mismatch: use this project's selected GitHub repository")
