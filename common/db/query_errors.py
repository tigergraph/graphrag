# Copyright (c) 2024-2026 TigerGraph, Inc.
#
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
# See the LICENSE file or <https://www.gnu.org/licenses/agpl-3.0.html>
# for the full license text.

"""Helpers for interpreting TigerGraph query create/install results and errors.

The query REST API (``createQuery``) distinguishes a TigerGraph query error
(the body failed type/semantic checks and was saved as a draft) from an
HTTP/transport failure, and GSQL error blobs need compressing for display.
These helpers centralize that interpretation so every caller (the Migration
Assistant, the ECC rebuild, …) shares one implementation.

For detecting whether a ``conn.gsql()`` result string reports failure, use
``common.db.schema_utils.gsql_output_error`` (the established helper) — not a
second copy here.
"""


def concise_gsql_error(text) -> str:
    """Reduce a GSQL / exception blob to its key message for display. Drops the
    ``Using graph ...`` shell preamble, the ``Saved as draft`` trailer, and
    stack-trace noise, keeping the meaningful reason. Full detail should stay in
    the server logs.

    GSQL emits ``<Type|Semantic> Check Error in query X (CODE): line N, col M``
    and puts the human-readable reason on the FOLLOWING line, so the header and
    that reason are returned together.
    """
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    skip = ("using graph", "saved as draft", "traceback", "file \"", "during handling", "^^^", "raise ")
    lines = [ln for ln in lines if not any(ln.lower().startswith(s) for s in skip)]
    if not lines:
        return str(text)[:300]
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "error" in low and "line" in low and "col" in low:
            # Location header + the reason GSQL puts on the following line.
            if i + 1 < len(lines):
                return f"{ln} — {lines[i + 1]}"[:400]
            return ln[:300]
    for key in ("does not exist", "failed", "error", "exception"):
        hit = next((ln for ln in lines if key in ln.lower()), None)
        if hit:
            return hit[:300]
    return lines[0][:300]


def create_response_error(res) -> str | None:
    """Return an error message when a ``createQuery`` response indicates the
    query was NOT created — TigerGraph saved it as a draft (``isDraft``) or
    flagged it (``error``) because the body failed type/semantic checks. This is
    a *TigerGraph query error* (definitive — retrying won't help), distinct from
    an HTTP/transport failure. Returns None when the response looks successful."""
    if isinstance(res, dict) and (res.get("isDraft") or res.get("error")):
        return str(res.get("message") or res)
    return None


def http_error_response_body(exc):
    """Best-effort parse of the TigerGraph response body carried by a raised
    HTTP error, so a TG query error hidden inside a 500 can be distinguished
    from a transport failure. Returns a dict / str, or None if no body."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return getattr(resp, "text", None)
