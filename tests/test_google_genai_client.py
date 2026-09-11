import pytest

from archbro.backend.llm.google_genai_client import GoogleGenAIClientFactory


def _capture_client(monkeypatch):
    from google import genai

    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(genai, "Client", FakeClient)
    return captured


def test_developer_api_factory_builds_keyed_gateway_client(monkeypatch):
    captured = _capture_client(monkeypatch)
    factory = GoogleGenAIClientFactory.from_env(
        {
            "GOOGLE_GENAI_USE_VERTEXAI": "false",
            "GEMINI_API_KEY": "developer-test-key",
            "GEMINI_BASE_URL": "http://127.0.0.1:8080/gemini/",
        }
    )

    factory.create_client(http_timeout_ms=2500)

    assert factory.transport == "gateway"
    assert factory.base_url == "http://127.0.0.1:8080/gemini"
    assert "developer-test-key" not in repr(factory)
    assert captured["api_key"] == "developer-test-key"
    assert "vertexai" not in captured
    http_options = captured["http_options"]
    assert http_options.timeout == 2500
    assert http_options.retry_options.attempts == 1
    assert http_options.base_url == "http://127.0.0.1:8080/gemini"


def test_vertex_factory_uses_adc_fields_and_ignores_stale_api_key(monkeypatch):
    captured = _capture_client(monkeypatch)
    factory = GoogleGenAIClientFactory.from_env(
        {
            "GOOGLE_GENAI_USE_VERTEXAI": "TRUE",
            "GOOGLE_CLOUD_PROJECT": "magic-dala",
            "GOOGLE_CLOUD_LOCATION": "global",
            "GEMINI_API_KEY": "stale-key-must-not-be-forwarded",
        }
    )

    factory.create_client(http_timeout_ms=90000)

    assert factory.transport == "vertex"
    assert factory.api_key is None
    assert captured["vertexai"] is True
    assert captured["project"] == "magic-dala"
    assert captured["location"] == "global"
    assert "api_key" not in captured
    http_options = captured["http_options"]
    assert http_options.timeout == 90000
    assert http_options.retry_options.attempts == 1


def test_vertex_factory_defaults_location_to_global():
    factory = GoogleGenAIClientFactory.from_env(
        {
            "GOOGLE_GENAI_USE_VERTEXAI": "yes",
            "GOOGLE_CLOUD_PROJECT": "example-project",
        }
    )

    assert factory.location == "global"


def test_vertex_factory_requires_explicit_project():
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        GoogleGenAIClientFactory.from_env(
            {"GOOGLE_GENAI_USE_VERTEXAI": "true"}
        )


def test_vertex_factory_rejects_custom_developer_gateway():
    with pytest.raises(ValueError, match="cannot be combined"):
        GoogleGenAIClientFactory.from_env(
            {
                "GOOGLE_GENAI_USE_VERTEXAI": "true",
                "GOOGLE_CLOUD_PROJECT": "example-project",
                "GEMINI_BASE_URL": "http://127.0.0.1:8080/gemini",
            }
        )


def test_developer_api_factory_requires_a_key():
    with pytest.raises(RuntimeError, match="Gemini credentials are not configured"):
        GoogleGenAIClientFactory.from_env(
            {"GOOGLE_GENAI_USE_VERTEXAI": "false"}
        )


def test_vertex_flag_rejects_ambiguous_values():
    with pytest.raises(ValueError, match="GOOGLE_GENAI_USE_VERTEXAI"):
        GoogleGenAIClientFactory.from_env(
            {
                "GOOGLE_GENAI_USE_VERTEXAI": "sometimes",
                "GEMINI_API_KEY": "unused",
            }
        )
