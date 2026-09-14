from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _first_nonempty(source: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = source.get(name, "").strip()
        if value:
            return value
    return None


def _parse_boolean(
    source: Mapping[str, str],
    name: str,
    *,
    default: bool = False,
) -> bool:
    raw = source.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off"
    )


@dataclass(frozen=True, slots=True)
class GoogleGenAIClientFactory:
    """Build invocation-scoped Google Gen AI clients from one auth policy.

    Vertex AI mode uses Application Default Credentials. Developer API mode
    keeps the existing API-key and optional custom-gateway behavior.
    """

    use_vertex_ai: bool
    api_key: str | None = field(default=None, repr=False)
    project: str | None = None
    location: str | None = None
    base_url: str | None = None

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> GoogleGenAIClientFactory:
        source = os.environ if env is None else env
        use_vertex_ai = _parse_boolean(
            source,
            "GOOGLE_GENAI_USE_VERTEXAI",
            default=False,
        )
        configured_base_url = _first_nonempty(
            source,
            "GEMINI_BASE_URL",
            "GOOGLE_GEMINI_BASE_URL",
        )
        base_url = (
            configured_base_url.rstrip("/")
            if configured_base_url is not None
            else None
        )

        if use_vertex_ai:
            if base_url is not None:
                raise ValueError(
                    "GEMINI_BASE_URL/GOOGLE_GEMINI_BASE_URL cannot be combined "
                    "with GOOGLE_GENAI_USE_VERTEXAI=true"
                )
            project = _first_nonempty(source, "GOOGLE_CLOUD_PROJECT")
            if project is None:
                raise RuntimeError(
                    "GOOGLE_CLOUD_PROJECT is required when "
                    "GOOGLE_GENAI_USE_VERTEXAI=true"
                )
            location = _first_nonempty(source, "GOOGLE_CLOUD_LOCATION") or "global"
            return cls(
                use_vertex_ai=True,
                project=project,
                location=location,
            )

        return cls.for_developer_api(
            api_key=_first_nonempty(
                source,
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
            ),
            base_url=base_url,
        )

    @classmethod
    def for_developer_api(
        cls,
        *,
        api_key: str | None,
        base_url: str | None = None,
    ) -> GoogleGenAIClientFactory:
        normalized_key = api_key.strip() if api_key else ""
        if not normalized_key:
            raise RuntimeError(
                "Gemini credentials are not configured: set "
                "GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT, "
                "or set GEMINI_API_KEY/GOOGLE_API_KEY"
            )
        normalized_base_url = base_url.rstrip("/") if base_url else None
        return cls(
            use_vertex_ai=False,
            api_key=normalized_key,
            base_url=normalized_base_url,
        )

    @property
    def transport(self) -> str:
        if self.use_vertex_ai:
            return "vertex"
        return "gateway" if self.base_url else "google"

    def create_client(self, *, http_timeout_ms: int):
        if http_timeout_ms <= 0:
            raise ValueError("Google Gen AI HTTP timeout must be greater than zero")

        from google import genai
        from google.genai import types as genai_types

        http_options: dict[str, object] = {
            "timeout": http_timeout_ms,
            "retry_options": genai_types.HttpRetryOptions(attempts=1),
        }
        if self.base_url is not None:
            http_options["base_url"] = self.base_url

        client_args: dict[str, Any] = {
            "http_options": genai_types.HttpOptions(**http_options),
        }
        if self.use_vertex_ai:
            client_args.update(
                {
                    "vertexai": True,
                    "project": self.project,
                    "location": self.location,
                }
            )
        else:
            client_args["api_key"] = self.api_key
        return genai.Client(**client_args)
