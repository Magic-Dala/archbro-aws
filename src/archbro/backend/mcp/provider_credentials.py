from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol


PROVIDER_CREDENTIAL_SCHEMA = "archbro.provider-credential.v1"


@dataclass(frozen=True, repr=False)
class StoredProviderCredential:
    """One OAuth-backed provider connection safe to restore after a restart.

    The object contains secrets and must only cross the backend persistence
    boundary. Implementations are responsible for authenticated encryption at
    rest and must never return it to browser or WebMCP surfaces.
    """

    connection_id: str
    provider: str
    name: str
    url: str
    auth_type: str
    access_token: str
    refresh_token: str | None
    expires_at: float | None
    token_url: str
    client_id: str
    client_secret: str
    display_endpoint: str | None = None
    tool_count: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": PROVIDER_CREDENTIAL_SCHEMA,
            **asdict(self),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "StoredProviderCredential":
        if payload.get("schema") != PROVIDER_CREDENTIAL_SCHEMA:
            raise ValueError("unsupported provider credential schema")
        required_text = (
            "connection_id",
            "provider",
            "name",
            "url",
            "auth_type",
            "access_token",
            "token_url",
            "client_id",
        )
        normalized: dict[str, Any] = {}
        for field_name in required_text:
            value = str(payload.get(field_name) or "").strip()
            if not value and field_name not in {"client_id"}:
                raise ValueError(f"provider credential {field_name} is required")
            normalized[field_name] = value
        normalized["client_secret"] = str(payload.get("client_secret") or "")
        refresh_token = payload.get("refresh_token")
        normalized["refresh_token"] = (
            str(refresh_token).strip() if refresh_token not in {None, ""} else None
        )
        expires_at = payload.get("expires_at")
        normalized["expires_at"] = float(expires_at) if expires_at is not None else None
        display_endpoint = payload.get("display_endpoint")
        normalized["display_endpoint"] = (
            str(display_endpoint).strip() if display_endpoint not in {None, ""} else None
        )
        tool_count = payload.get("tool_count")
        normalized["tool_count"] = int(tool_count) if tool_count is not None else None
        return cls(**normalized)


class ProviderCredentialStore(Protocol):
    """Encrypted, principal-scoped storage for first-party OAuth connections."""

    @property
    def persistent(self) -> bool: ...

    def list_for_user(self, user_id: str) -> list[StoredProviderCredential]: ...

    def upsert(self, user_id: str, credential: StoredProviderCredential) -> None: ...

    def delete(self, user_id: str, provider: str) -> None: ...
