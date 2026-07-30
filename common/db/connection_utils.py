"""Small TigerGraph connection compatibility helpers."""

from __future__ import annotations

from typing import Any


def normalize_restpp_url(connection: Any) -> Any:
    """Apply the shared-port REST++ path used by TigerGraph Cloud.

    Savanna exposes GSQL and REST++ on the same HTTPS port. pyTigerGraph does
    not consistently append the REST++ routing prefix for every connection
    constructor/authentication mode, so normalize it once after construction.
    """

    if (
        getattr(connection, "restppPort", None)
        == getattr(connection, "gsPort", None)
        and "/restpp" not in getattr(connection, "restppUrl", "")
    ):
        connection.restppUrl = connection.restppUrl.rstrip("/") + "/restpp"
    return connection
