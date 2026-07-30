"""Resolve environment-variable references in JSON configuration values."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

_ENV_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def resolve_environment_placeholders(
    value: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Return a copy with exact ``${NAME}`` values read from the environment.

    Only whole-string references are expanded. This keeps configuration
    behavior explicit and prevents accidental shell-style interpolation of
    arbitrary text.
    """

    source = os.environ if environ is None else environ
    if isinstance(value, dict):
        return {
            key: resolve_environment_placeholders(item, environ=source)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_environment_placeholders(item, environ=source)
            for item in value
        ]
    if isinstance(value, str):
        match = _ENV_REFERENCE.fullmatch(value)
        if match:
            name = match.group(1)
            if name not in source or not source[name]:
                raise ValueError(
                    f"Required environment variable {name} is not set"
                )
            return source[name]
    return value
