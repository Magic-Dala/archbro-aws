from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from deploy.release.local_entrypoint import runtime_environment


def overrides() -> dict[str, str]:
    digest = "sha256:" + "a" * 64
    return {
        "ARCHBRO_IMAGE_REFERENCE": "archbro-final@" + digest,
        "ARCHBRO_IMAGE_MANIFEST_DIGEST": digest,
        "ARCHBRO_DB_HOST": "host.docker.internal",
        "ARCHBRO_DB_PORT": "55432",
        "ARCHBRO_ACCEPTANCE_TARGET_ORIGIN": "https://archbro-jim.magicdala.com",
    }


def config(tmp_path: Path, dsn: str) -> Path:
    path = tmp_path / "runtime.env"
    path.write_text("# Existing service config\nDATABASE_URL=" + dsn +
                    "\nARCHBRO_AUTH_MODE=firebase\nGEMINI_API_KEY=opaque=a=b\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("dsn", [
    "host=127.0.0.1 port=5432 dbname=archbro user=archbro password='opaque value' sslmode=prefer",
    "postgresql://archbro:opaque%20value@localhost:5432/archbro?sslmode=prefer",
])
def test_existing_credentials_preserved_while_explicit_endpoint_changes(tmp_path, dsn):
    path = config(tmp_path, dsn)
    before = path.read_bytes()
    values = runtime_environment(path, overrides())
    database = conninfo_to_dict(values["DATABASE_URL"])
    assert database == {**conninfo_to_dict(dsn), "host": "host.docker.internal", "port": "55432"}
    assert values["ARCHBRO_AUTH_MODE"] == "firebase"
    assert values["GEMINI_API_KEY"] == "opaque=a=b"
    assert path.read_bytes() == before


def test_deployment_identity_overrides_stale_service_config(tmp_path):
    path = config(tmp_path, "dbname=archbro")
    with path.open("a") as handle:
        handle.write("ARCHBRO_ACCEPTANCE_TARGET_ORIGIN=http://127.0.0.1:8013\n")
    values = runtime_environment(path, overrides())
    assert values["ARCHBRO_ACCEPTANCE_TARGET_ORIGIN"] == overrides()["ARCHBRO_ACCEPTANCE_TARGET_ORIGIN"]
    assert "ARCHBRO_IMAGE_ID" not in values  # Never invent an unobserved image ID.


@pytest.mark.parametrize("change", [
    {"ARCHBRO_DB_HOST": ""}, {"ARCHBRO_DB_PORT": ""}, {"ARCHBRO_DB_PORT": "65536"},
    {"ARCHBRO_IMAGE_REFERENCE": "archbro-final:latest"},
    {"ARCHBRO_IMAGE_MANIFEST_DIGEST": "sha256:" + "b" * 64},
])
def test_incomplete_deployment_configuration_fails_before_app_start(tmp_path, change):
    with pytest.raises(ValueError):
        runtime_environment(config(tmp_path, "dbname=archbro"), {**overrides(), **change})


def test_missing_or_invalid_service_config_fails_without_echoing_content(tmp_path):
    with pytest.raises(FileNotFoundError):
        runtime_environment(tmp_path / "missing", overrides())
    path = tmp_path / "runtime.env"
    path.write_text("secret content without equals", encoding="utf-8")
    with pytest.raises(ValueError, match="line 1") as error:
        runtime_environment(path, overrides())
    assert "secret content" not in str(error.value)
