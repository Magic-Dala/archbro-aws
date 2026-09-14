"""Start the Jim candidate using its existing, read-only service configuration."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from psycopg.conninfo import make_conninfo


def runtime_environment(config_path: Path, overrides: Mapping[str, str]) -> dict[str, str]:
    # runtime.env is the existing literal KEY=value format; do not interpolate
    # credentials, emit their values, or change the shared volume permissions.
    values: dict[str, str] = {}
    for number, raw in enumerate(config_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid runtime configuration at line {number}")
        values[key] = value
    values.update(overrides)

    digest = values.get("ARCHBRO_IMAGE_MANIFEST_DIGEST", "")
    reference = values.get("ARCHBRO_IMAGE_REFERENCE", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or not reference.endswith("@" + digest):
        raise ValueError("Supply the inspected immutable image reference and matching manifest digest")

    host = values.get("ARCHBRO_DB_HOST", "")
    port = values.get("ARCHBRO_DB_PORT", "")
    if not host or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("Explicit ARCHBRO_DB_HOST and ARCHBRO_DB_PORT are required")
    if not values.get("DATABASE_URL"):
        raise ValueError("Existing service configuration must contain DATABASE_URL")
    values["DATABASE_URL"] = make_conninfo(values["DATABASE_URL"], host=host, port=port)
    return values


def main() -> None:
    values = runtime_environment(
        Path(os.environ.get("ARCHBRO_RUNTIME_ENV_FILE", "/run/archbro-config/runtime.env")),
        os.environ,
    )
    os.environ.update(values)
    # Load the app only after the existing credentials and explicit deployment
    # overrides are in place. This path does not create a Firebase principal.
    from archbro.platform.runtime.app import create_app
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(values.get("PORT", "8080")))


if __name__ == "__main__":
    main()
