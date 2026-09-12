from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from archbro.backend.api.provider_connections import ProviderMcpRuntimeRegistry
from archbro.backend.api.routes import build_router
from archbro.backend.core.authorization import PrincipalProvider
from archbro.backend.core.repository import ProjectRepositoryPort
from archbro.backend.llm.fake import FakeModelProvider
from archbro.backend.llm.gemini import GeminiProvider
from archbro.backend.llm.provider import ModelProvider
from archbro.backend.mcp.provider_credentials import ProviderCredentialStore
from archbro.platform.persistence.postgres import PostgresProjectRepository
from archbro.platform.persistence.provider_credentials import PostgresProviderCredentialStore
from archbro.platform.runtime.release_identity import (
    build_public_readiness_report,
    build_readiness_report,
    build_runtime_identity,
)

load_dotenv()

WEBMCP_SURFACE_VERSION = "archbro.semantic-webmcp.v4"
WEBMCP_DEFAULT_TOOL_COUNT = 14
WEBMCP_GATEWAY_TOOL_COUNT = 3
_FIREBASE_AUTH_DOMAIN_PATTERN = re.compile(
    r"(?=.{1,253}\Z)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
    re.IGNORECASE,
)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _firebase_auth_origin(auth_domain: str) -> str | None:
    """Return one safe CSP origin for Firebase's popup resolver iframe."""

    normalized = auth_domain.strip().lower()
    if not normalized:
        return None
    if _FIREBASE_AUTH_DOMAIN_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            "ARCHBRO_FIREBASE_AUTH_DOMAIN must be a hostname without a scheme, "
            "port, path, query, or fragment"
        )
    return f"https://{normalized}"


def create_app(
    repository: ProjectRepositoryPort | None = None,
    provider: ModelProvider | None = None,
    *,
    web_dir: str | Path | None = None,
    principal_provider: PrincipalProvider | None = None,
    provider_credential_store: ProviderCredentialStore | None = None,
) -> FastAPI:
    """Max-owned runtime composition root.

    This is the only layer that chooses concrete persistence/model providers and
    binds Shaun's frontend to Jim's API. Backend modules stay independent of
    deployment/runtime details.
    """

    database_url = (os.getenv("DATABASE_URL") or "").strip()
    selected_repository = repository
    if selected_repository is None:
        persistence_mode = os.getenv("ARCHBRO_PERSISTENCE", "postgres").strip().lower()
        if persistence_mode != "postgres":
            raise ValueError("ARCHBRO_PERSISTENCE must be 'postgres'")

        if not database_url:
            raise ValueError(
                "DATABASE_URL is required when ARCHBRO_PERSISTENCE=postgres"
            )
        selected_repository = PostgresProjectRepository(database_url)

    environment = os.getenv("ARCHBRO_ENV", "local").strip().lower()
    if environment not in {"local", "test", "production"}:
        raise ValueError("ARCHBRO_ENV must be 'local', 'test', or 'production'")


    selected_provider_credential_store = provider_credential_store
    provider_credential_key = os.getenv("ARCHBRO_PROVIDER_CREDENTIAL_KEY", "").strip()
    provider_oauth_configured = any(
        os.getenv(name, "").strip()
        for name in (
            "ARCHBRO_GITHUB_OAUTH_CLIENT_ID",
            "ARCHBRO_GOOGLE_DRIVE_OAUTH_CLIENT_ID",
            "ARCHBRO_SLACK_OAUTH_CLIENT_ID",
            "ARCHBRO_MICROSOFT_TEAMS_CLIENT_ID",
        )
    )
    if selected_provider_credential_store is None and provider_credential_key:
        if not database_url:
            raise ValueError(
                "DATABASE_URL is required when ARCHBRO_PROVIDER_CREDENTIAL_KEY is configured"
            )
        selected_provider_credential_store = PostgresProviderCredentialStore(
            database_url,
            provider_credential_key,
        )
    if (
        environment == "production"
        and provider_oauth_configured
        and selected_provider_credential_store is None
    ):
        raise ValueError(
            "ARCHBRO_PROVIDER_CREDENTIAL_KEY is required when first-party provider OAuth "
            "is enabled in production"
        )
    provider_mcp_runtime = ProviderMcpRuntimeRegistry(selected_provider_credential_store)

    edge_guard_mode = os.getenv("ARCHBRO_EDGE_GUARD", "off").strip().lower()
    if edge_guard_mode not in {"off", "required"}:
        raise ValueError("ARCHBRO_EDGE_GUARD must be 'off' or 'required'")
    edge_token = os.getenv("ARCHBRO_EDGE_TOKEN", "").strip()
    if edge_guard_mode == "required" and not edge_token:
        raise ValueError("ARCHBRO_EDGE_TOKEN is required when ARCHBRO_EDGE_GUARD=required")

    auth_mode = os.getenv("ARCHBRO_AUTH_MODE", "local").strip().lower()
    if auth_mode not in {"local", "firebase"}:
        raise ValueError("ARCHBRO_AUTH_MODE must be 'local' or 'firebase'")

    selected_principal_provider = principal_provider
    firebase_project_id = (
        os.getenv("FIREBASE_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or ""
    ).strip()
    if selected_principal_provider is None:
        if auth_mode == "firebase":
            if not firebase_project_id:
                raise ValueError(
                    "FIREBASE_PROJECT_ID or GOOGLE_CLOUD_PROJECT is required when "
                    "ARCHBRO_AUTH_MODE=firebase"
                )
            from archbro.integrations.firebase import FirebasePrincipalProvider

            selected_principal_provider = FirebasePrincipalProvider(firebase_project_id)
        elif environment == "production":
            raise ValueError(
                "Production Archbro must use ARCHBRO_AUTH_MODE=firebase; "
                "the local development principal is disabled in production."
            )

    public_firebase_config: dict[str, str] | None = None
    firebase_auth_origin: str | None = None
    if auth_mode == "firebase":
        public_firebase_config = {
            "apiKey": os.getenv("ARCHBRO_FIREBASE_API_KEY", "").strip(),
            "authDomain": os.getenv("ARCHBRO_FIREBASE_AUTH_DOMAIN", "").strip(),
            "projectId": firebase_project_id,
            "appId": os.getenv("ARCHBRO_FIREBASE_APP_ID", "").strip(),
        }
        # Google sign-in opens https://<authDomain>/__/auth/handler, so authDomain
        # became load-bearing the moment that button started working: without it
        # the SDK raises auth/auth-domain-config-required when someone clicks,
        # which is a forgotten deployment setting reaching real users as a broken
        # button on a server that booted cleanly. appId stays optional; it serves
        # Analytics and installations, neither of which sign-in touches.
        required_public_keys = ("apiKey", "projectId", "authDomain")
        missing = [key for key in required_public_keys if not public_firebase_config[key]]
        if missing and environment == "production":
            raise ValueError(
                "Production Firebase browser configuration is incomplete: "
                + ", ".join(missing)
            )
        firebase_auth_origin = _firebase_auth_origin(
            public_firebase_config["authDomain"]
        )

    selected_provider = provider
    if selected_provider is None:
        provider_name = (os.getenv("ARCHBRO_PROVIDER") or os.getenv("HUMAN_AGENT_PROVIDER") or "gemini").lower()
        selected_provider = (
            FakeModelProvider()
            if provider_name == "fake"
            else GeminiProvider(
                model_id=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
                checkpoint_repository=selected_repository,
            )
        )

    goal_timeout = float(
        os.getenv("ARCHBRO_GOAL_REQUEST_TIMEOUT_SECONDS")
        or os.getenv("HUMAN_AGENT_GOAL_REQUEST_TIMEOUT_SECONDS")
        or "30"
    )
    if goal_timeout <= 0:
        raise ValueError("ARCHBRO_GOAL_REQUEST_TIMEOUT_SECONDS must be greater than zero")

    architecture_model = str(
        getattr(selected_provider, "system_map_model_id", None)
        or getattr(selected_provider, "model_id", selected_provider.__class__.__name__)
    ).strip() or selected_provider.__class__.__name__
    try:
        architecture_total_timeout_seconds = float(
            getattr(selected_provider, "architecture_total_timeout_seconds", 45.0)
        )
    except (TypeError, ValueError):
        architecture_total_timeout_seconds = 45.0
    if not math.isfinite(architecture_total_timeout_seconds) or architecture_total_timeout_seconds <= 0:
        architecture_total_timeout_seconds = 45.0
    try:
        architecture_queue_timeout_seconds = float(
            getattr(selected_provider, "architecture_queue_timeout_seconds", 0.0)
        )
    except (TypeError, ValueError):
        architecture_queue_timeout_seconds = 0.0
    if (
        not math.isfinite(architecture_queue_timeout_seconds)
        or architecture_queue_timeout_seconds < 0
    ):
        architecture_queue_timeout_seconds = 0.0
    # The browser deadline must include admission wait as well as the planner's
    # own global deadline. Otherwise a queued request can be aborted by the UI
    # while the backend is still safely waiting to begin its first paid call.
    architecture_request_timeout_ms = max(
        60_000,
        round(
            (
                architecture_queue_timeout_seconds
                + architecture_total_timeout_seconds
                + 15.0
            )
            * 1000
        ),
    )

    frontend_dir = Path(web_dir) if web_dir is not None else _project_root() / "frontend" / "web"
    if not frontend_dir.exists():
        raise RuntimeError(f"Archbro frontend directory not found: {frontend_dir}")
    static_asset_versions = {
        f"/static/{asset.relative_to(frontend_dir).as_posix()}": hashlib.sha256(
            asset.read_bytes()
        ).hexdigest()[:16]
        for asset in frontend_dir.rglob("*")
        if asset.is_file()
    }
    webmcp_asset_sha256 = hashlib.sha256(
        (frontend_dir / "archbro-webmcp.js").read_bytes()
    ).hexdigest()
    runtime_identity_payload = build_runtime_identity(app_root=_project_root())

    def connected_mcp_gateway_configured() -> bool:
        raw_connected_mcp = os.getenv("ARCHBRO_MCP_SERVERS_JSON", "").strip()
        if not raw_connected_mcp:
            return False
        try:
            return bool(json.loads(raw_connected_mcp))
        except (json.JSONDecodeError, TypeError):
            return False

    def webmcp_manifest_payload() -> dict[str, object]:
        gateway_configured = connected_mcp_gateway_configured()
        return {
            "surface": "archbro-webmcp",
            "surface_version": WEBMCP_SURFACE_VERSION,
            "asset_sha256": webmcp_asset_sha256,
            "connected_mcp_gateway_configured": gateway_configured,
            # The discovery/call tools are always registered. They can expose either
            # deployment-bound MCP servers or the current user's authorized provider hub.
            "expected_tool_count": WEBMCP_DEFAULT_TOOL_COUNT + WEBMCP_GATEWAY_TOOL_COUNT,
        }

    def runtime_config_script() -> str:
        manifest = webmcp_manifest_payload()
        payload = {
            "auth_mode": auth_mode,
            "firebase": public_firebase_config,
            "architecture_model": architecture_model,
            "architecture_request_timeout_ms": architecture_request_timeout_ms,
            "connected_mcp_gateway_configured": manifest["connected_mcp_gateway_configured"],
            "webmcp_surface_version": manifest["surface_version"],
            "webmcp_asset_sha256": manifest["asset_sha256"],
            "webmcp_expected_tool_count": manifest["expected_tool_count"],
            "webmcp_manifest_url": "/webmcp-manifest.json",
        }
        return "window.__ARCHBRO_RUNTIME_CONFIG__ = " + json.dumps(payload) + ";\n"

    def runtime_config_version() -> str:
        return hashlib.sha256(runtime_config_script().encode("utf-8")).hexdigest()[:16]

    index_template = (frontend_dir / "index.html").read_text(encoding="utf-8")
    runtime_config_marker = 'src="/runtime-config.js"'
    if index_template.count(runtime_config_marker) != 1:
        raise RuntimeError("Archbro index must reference runtime-config.js exactly once")

    def runtime_index_html() -> str:
        return index_template.replace(
            runtime_config_marker,
            f'src="/runtime-config.js?v={runtime_config_version()}"',
        )

    app = FastAPI(title="Archbro")
    app.include_router(
        build_router(
            selected_repository,
            selected_provider,
            goal_request_timeout_seconds=goal_timeout,
            principal_provider=selected_principal_provider,
            provider_mcp_runtime=provider_mcp_runtime,
        )
    )

    @app.middleware("http")
    async def edge_origin_guard(request, call_next):
        # Probe endpoints are deliberately non-sensitive and must be callable
        # from inside the container where the edge token is unavailable. They do
        # not grant an API principal or expose secret configuration.
        probe_paths = {"/healthz", "/readyz", "/runtime-identity"}
        if request.url.path == "/internal/readyz":
            client_host = request.client.host if request.client is not None else ""
            if client_host not in {"127.0.0.1", "::1", "localhost"}:
                return JSONResponse(status_code=403, content={"detail": "internal readiness is loopback-only"})
            return await call_next(request)
        if edge_guard_mode == "required" and request.url.path not in probe_paths:
            presented = request.headers.get("X-ArchBro-Edge-Token", "")
            if not presented or not hmac.compare_digest(presented, edge_token):
                return JSONResponse(status_code=403, content={"detail": "direct origin access is forbidden"})
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        script_sources = ["'self'", "https://www.gstatic.com"]
        frame_sources = ["'none'"]
        if auth_mode == "firebase":
            # Firebase Auth 12.2.1 dynamically loads Google's popup bridge from
            # https://apis.google.com/js/api.js during Google sign-in.
            script_sources.append("https://apis.google.com")
            frame_sources = ["https://*.firebaseapp.com"]
            if firebase_auth_origin not in {None, "https://*.firebaseapp.com"}:
                frame_sources.append(firebase_auth_origin)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src {' '.join(script_sources)}; "
            "style-src 'self'; img-src 'self' data:; "
            "connect-src 'self' https://identitytoolkit.googleapis.com "
            "https://securetoken.googleapis.com https://www.googleapis.com; "
            f"frame-src {' '.join(frame_sources)}; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'"
        )
        if environment == "production":
            response.headers["Strict-Transport-Security"] = "max-age=86400"
        versioned_static = (
            static_asset_versions.get(request.url.path) is not None
            and request.query_params.get("v") == static_asset_versions[request.url.path]
        )
        versioned_runtime_config = (
            request.url.path == "/runtime-config.js"
            and request.query_params.get("v") == runtime_config_version()
        )
        if response.status_code in {200, 206, 304} and (
            versioned_static or versioned_runtime_config
        ):
            # Static asset URLs in index.html are content/version keyed. Keeping
            # those responses no-store forces the browser to download the same
            # 300KB+ modules on every F5. A changed asset always gets a new URL,
            # so versioned static resources are safe to cache immutably while
            # direct unversioned asset reads below remain fail-safe no-store.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            if "Pragma" in response.headers:
                del response.headers["Pragma"]
            if "Expires" in response.headers:
                del response.headers["Expires"]
        elif request.url.path.startswith("/static/") or request.url.path in {
            "/",
            "/healthz",
            "/readyz",
            "/internal/readyz",
            "/runtime-identity",
            "/runtime-config.js",
            "/webmcp-manifest.json",
        }:
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # Container liveness probe. Deliberately does not touch persistence: a probe
    # that fails during a transient database outage makes the orchestrator
    # restart the app while the database is still recovering, turning a short
    # outage into a restart storm. Readiness is reported independently below.
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        # Public readiness is intentionally cheap and cannot be used to trigger
        # database/schema work from outside the runtime boundary.
        report, ready = build_public_readiness_report(
            environment=environment,
            auth_mode=auth_mode,
            public_firebase_config=public_firebase_config,
            principal_provider=selected_principal_provider,
        )
        return JSONResponse(status_code=200 if ready else 503, content=report)

    @app.get("/internal/readyz", include_in_schema=False)
    async def internal_readyz():
        # Deep dependency readiness is loopback-only and bounded to one schema
        # query. It never calls Gemini/Strands or any paid/live model.
        report, ready = build_readiness_report(
            repository=selected_repository,
            environment=environment,
            auth_mode=auth_mode,
            public_firebase_config=public_firebase_config,
            principal_provider=selected_principal_provider,
        )
        return JSONResponse(status_code=200 if ready else 503, content=report)

    @app.get("/runtime-identity", include_in_schema=False)
    async def runtime_identity():
        return JSONResponse(
            content=runtime_identity_payload,
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/", include_in_schema=False)
    async def web_app():
        return Response(content=runtime_index_html(), media_type="text/html")

    @app.get("/webmcp-manifest.json", include_in_schema=False)
    async def webmcp_manifest():
        return JSONResponse(
            content=webmcp_manifest_payload(),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/runtime-config.js", include_in_schema=False)
    async def runtime_config():
        return Response(
            content=runtime_config_script(),
            media_type="application/javascript",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")
    return app


# Kept as a tiny compatibility alias for tests and QA harnesses.
build_app = create_app
