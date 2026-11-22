"""MCP protocol-version negotiation for the Facebook Ads MCP (step 2 of the
MCP 2026-07-28 upgrade).

The 2026-07-28 revision of the Model Context Protocol is stateless: there is
no ``initialize`` handshake, every request names its protocol version both in
the ``MCP-Protocol-Version`` HTTP header and in ``params._meta`` under
``io.modelcontextprotocol/protocolVersion``, and ``server/discover`` lets a
client learn what a server supports before sending anything else. Requests
also carry ``Mcp-Method`` (and ``Mcp-Name`` for tool calls) so gateways can
route without parsing the body; the server must check they agree with it.

This module implements exactly that layer and nothing else. Legacy clients
(our own loop_agent client sends no version at all) keep working: a request
with no version anywhere is served as ``LEGACY_DEFAULT_VERSION`` per the spec's
backward-compatibility rule, and ``initialize`` is still answered.

Error codes (spec): ``-32020`` header/body disagreement, ``-32022``
unsupported protocol version (``data.supportedVersions`` lists what we speak).
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

LATEST_PROTOCOL_VERSION = "2026-07-28"
# Newest first. Every revision we can answer correctly for the methods we host.
SUPPORTED_PROTOCOL_VERSIONS: Tuple[str, ...] = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
# Spec: a request carrying no MCP-Protocol-Version header MAY be treated as a
# 2025-03-26 client. This is what keeps the current chat-side client working.
LEGACY_DEFAULT_VERSION = "2025-03-26"
# What ``initialize`` advertises when the caller asks for nothing / something
# unsupported (the SDK-installed baseline; newer than the old 2024-11-05).
INITIALIZE_FALLBACK_VERSION = "2025-06-18"

META_PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
HEADER_PROTOCOL_VERSION = "HTTP_MCP_PROTOCOL_VERSION"
HEADER_METHOD = "HTTP_MCP_METHOD"
HEADER_NAME = "HTTP_MCP_NAME"

ERR_HEADER_MISMATCH = -32020
ERR_UNSUPPORTED_VERSION = -32022

SERVER_NAME = "facebook-ads-mcp-server"
SERVER_VERSION = "2.1.0"
SERVER_TITLE = "Facebook Ads (Meta Marketing API)"
SERVER_DESCRIPTION = (
    "Facebook Ads (Meta Marketing API) only — tools are facebook_* . "
    "Not the WhatsApp MCP: use /api/mcp/bol7/ or /api/mcp/meta/ for send_message and templates."
)


def is_modern(version: str) -> bool:
    """True for the stateless 2026-07-28 revision and later."""
    return bool(version) and version >= LATEST_PROTOCOL_VERSION


def _clean(v: Any) -> str:
    return str(v or "").strip()


def body_protocol_version(body: Dict[str, Any]) -> str:
    """Version named inside ``params._meta`` (empty when absent)."""
    params = body.get("params") if isinstance(body, dict) else None
    if not isinstance(params, dict):
        return ""
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return ""
    return _clean(meta.get(META_PROTOCOL_VERSION_KEY))


def negotiate(request, body: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Resolve the protocol version for one request.

    Returns ``(version, None)`` on success or ``("", error)`` where ``error`` is
    a JSON-RPC error object (``{"code", "message", "data"?}``) the caller wraps.
    ``request`` may be ``None`` (stdio / SSE callers) — then only the body is
    consulted.
    """
    meta_hdrs = getattr(request, "META", {}) if request is not None else {}
    hdr_version = _clean(meta_hdrs.get(HEADER_PROTOCOL_VERSION))
    body_version = body_protocol_version(body)

    if hdr_version and body_version and hdr_version != body_version:
        return "", {
            "code": ERR_HEADER_MISMATCH,
            "message": (
                f"MCP-Protocol-Version header ({hdr_version}) does not match "
                f"params._meta.{META_PROTOCOL_VERSION_KEY} ({body_version})."
            ),
            "data": {"header": hdr_version, "body": body_version},
        }
    version = hdr_version or body_version or LEGACY_DEFAULT_VERSION
    if version not in SUPPORTED_PROTOCOL_VERSIONS:
        return "", {
            "code": ERR_UNSUPPORTED_VERSION,
            "message": f"Unsupported MCP protocol version {version!r}.",
            "data": {
                "requested": version,
                "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
                "preferredVersion": LATEST_PROTOCOL_VERSION,
            },
        }

    # Routing headers — validated whenever present (only modern clients send them).
    hdr_method = _clean(meta_hdrs.get(HEADER_METHOD))
    method = _clean(body.get("method")) if isinstance(body, dict) else ""
    if hdr_method and method and hdr_method != method:
        return "", {
            "code": ERR_HEADER_MISMATCH,
            "message": f"Mcp-Method header ({hdr_method}) does not match body method ({method}).",
            "data": {"header": hdr_method, "body": method},
        }
    hdr_name = _clean(meta_hdrs.get(HEADER_NAME))
    if hdr_name and method == "tools/call":
        params = body.get("params") if isinstance(body.get("params"), dict) else {}
        tool = _clean(params.get("name"))
        if tool and hdr_name != tool:
            return "", {
                "code": ERR_HEADER_MISMATCH,
                "message": f"Mcp-Name header ({hdr_name}) does not match params.name ({tool}).",
                "data": {"header": hdr_name, "body": tool},
            }
    return version, None


def http_status_for_error(code: Any) -> int:
    """Protocol-negotiation faults are HTTP 400 per the 2026-07-28 transport
    rules; every other JSON-RPC error stays 200 (legacy behaviour)."""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return 200
    return 400 if c in (ERR_HEADER_MISMATCH, ERR_UNSUPPORTED_VERSION) else 200


def server_capabilities() -> Dict[str, Any]:
    return {"tools": {"listChanged": True}}


def server_info() -> Dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "title": SERVER_TITLE,
        "version": SERVER_VERSION,
        "description": SERVER_DESCRIPTION,
    }


def discover_result() -> Dict[str, Any]:
    """Result for ``server/discover`` (2026-07-28)."""
    return {
        "protocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "preferredProtocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": server_capabilities(),
        "serverInfo": server_info(),
    }


def initialize_version(requested: Any) -> str:
    """``initialize`` (legacy handshake): echo the requested version when we
    support it, else advertise the fallback."""
    r = _clean(requested)
    return r if r in SUPPORTED_PROTOCOL_VERSIONS else INITIALIZE_FALLBACK_VERSION


def stamp_result_meta(result: Dict[str, Any], version: str) -> Dict[str, Any]:
    """On modern requests, echo the negotiated version in ``result._meta`` so a
    stateless client can verify which revision answered. Legacy responses are
    left byte-for-byte unchanged."""
    if not is_modern(version) or not isinstance(result, dict):
        return result
    inner = result.get("result")
    if isinstance(inner, dict):
        meta = dict(inner.get("_meta") or {})
        meta.setdefault(META_PROTOCOL_VERSION_KEY, version)
        inner["_meta"] = meta
        # Every 2026-07-28 result is typed; we never need mid-call input, so
        # it is always "complete".
        inner.setdefault("resultType", "complete")
    return result


# ---------------------------------------------------------------------------
# Step 3 — tool definitions and result semantics (2026-07-28)
# ---------------------------------------------------------------------------
# tools/list cache hints: the catalog is user-independent and only changes on
# deploy, so clients may cache it (keeps their LLM prompt cache warm).
TOOLS_LIST_TTL_MS = 300_000
TOOLS_LIST_CACHE_SCOPE = "public"

# Verb → annotation defaults. Derived from the tool NAME so a new tool gets
# sane hints without anyone maintaining a list (no-hardcoding rule); a tool
# dict may still carry an explicit ``annotations`` override.
_READ_ONLY_VERBS = frozenset({
    "get", "compare", "roas", "budget", "targeting", "batch", "verify",
    "preview", "validate", "check", "resolve",
})
_DESTRUCTIVE_VERBS = frozenset({"delete"})
# Verbs that change remote state but are safe to repeat with the same args
# (server-side idempotency cache or natural idempotence).
_IDEMPOTENT_MUTATING_VERBS = frozenset({"pause", "resume", "attach", "update", "exchange"})


def _verb(name: str) -> str:
    parts = str(name or "").split("_", 2)
    return parts[1] if len(parts) > 1 else ""


def tool_title(name: str) -> str:
    """Human display title from ``facebook_get_ad_sets`` → ``Get Ad Sets``."""
    rest = str(name or "")
    if rest.startswith("facebook_"):
        rest = rest[len("facebook_"):]
    words = [w for w in rest.split("_") if w]
    fixes = {"ad": "Ad", "adset": "Ad Set", "adsets": "Ad Sets", "roas": "ROAS", "id": "ID",
             "ids": "IDs", "cta": "CTA", "url": "URL", "api": "API", "ig": "IG", "dco": "DCO"}
    return " ".join(fixes.get(w, w.capitalize()) for w in words) or str(name)


def tool_annotations(name: str, *, idempotent_tools=frozenset(), explicit=None) -> Dict[str, Any]:
    """Spec ``annotations`` block: readOnlyHint / destructiveHint /
    idempotentHint / openWorldHint. Every tool talks to Meta's Graph API, so
    openWorldHint is always true."""
    if isinstance(explicit, dict) and explicit:
        return dict(explicit)
    v = _verb(name)
    # "batch" is only read-only for the *search* tools; the batch sync creates campaigns.
    read_only = v in _READ_ONLY_VERBS and not str(name).startswith("facebook_sync_")
    if v == "batch" and "sync" in str(name):
        read_only = False
    destructive = v in _DESTRUCTIVE_VERBS
    idempotent = read_only or v in _IDEMPOTENT_MUTATING_VERBS or name in idempotent_tools
    return {
        "readOnlyHint": bool(read_only),
        "destructiveHint": bool(destructive),
        "idempotentHint": bool(idempotent),
        "openWorldHint": True,
    }


def tools_list_result(tools: list, version: str) -> Dict[str, Any]:
    """Wrap the tool list; modern revisions get cache hints."""
    out: Dict[str, Any] = {"tools": tools}
    if is_modern(version):
        out["ttlMs"] = TOOLS_LIST_TTL_MS
        out["cacheScope"] = TOOLS_LIST_CACHE_SCOPE
    return out


def tool_call_result(data: Any, version: str, *, legacy_text: str) -> Dict[str, Any]:
    """Build a tools/call result.

    Legacy: ``{"content":[{"type":"text","text":<pretty json>}]}`` exactly as
    before (Meta errors buried in the text, no isError).
    Modern: compact text + ``structuredContent`` (the JSON object itself) +
    ``isError: true`` whenever the tool reported an ``error`` — so the model
    reads the failure and self-corrects instead of the run dying.
    """
    if not is_modern(version):
        return {"content": [{"type": "text", "text": legacy_text}]}
    import json as _json
    compact = _json.dumps(data, separators=(",", ":"), default=str)
    out: Dict[str, Any] = {"content": [{"type": "text", "text": compact}]}
    if isinstance(data, dict):
        out["structuredContent"] = data
        err = data.get("error")
        if err not in (None, "", False, {}):
            out["isError"] = True
        task = accepted_to_task(data)
        if task:
            # CreateTaskResult shape (tasks extension): client polls tasks/get.
            out["task"] = task
    return out


def tool_input_error_result(message: str, version: str, *, data: Any = None) -> Optional[Dict[str, Any]]:
    """Modern revisions return input-validation failures as a normal result
    with ``isError: true`` (SEP-1303). Returns None on legacy so the caller
    keeps raising the JSON-RPC -32602 error the old client expects."""
    if not is_modern(version):
        return None
    payload: Dict[str, Any] = {"error": {"code": ERR_INVALID_PARAMS, "message": message}}
    if data is not None:
        payload["error"]["data"] = data
    import json as _json
    return {
        "content": [{"type": "text", "text": _json.dumps(payload, separators=(",", ":"), default=str)}],
        "structuredContent": payload,
        "isError": True,
    }


ERR_INVALID_PARAMS = -32602


# ---------------------------------------------------------------------------
# Step 4 — transport hygiene + tasks extension (2026-07-28)
# ---------------------------------------------------------------------------
import os as _os
from urllib.parse import urlsplit as _urlsplit

TASKS_EXTENSION = "io.modelcontextprotocol/tasks"
TASK_POLL_INTERVAL_MS = 2000
# FacebookAdsMcpTaskResult.status → spec task status
_TASK_STATUS_MAP = {
    "pending": "working", "running": "working", "started": "working",
    "success": "completed", "failed": "failed", "cancelled": "cancelled", "canceled": "cancelled",
}


def allowed_origins() -> Tuple[str, ...]:
    """Extra Origins (scheme://host[:port]) allowed to POST, from
    MCP_ALLOWED_ORIGINS (comma-separated). Same-origin is always allowed."""
    raw = _os.environ.get("MCP_ALLOWED_ORIGINS", "") or ""
    return tuple(o.strip().rstrip("/").lower() for o in raw.split(",") if o.strip())


def origin_allowed(request) -> bool:
    """2026-07-28 transport rule: validate ``Origin`` (DNS-rebinding guard).
    No Origin header (server-to-server, curl, our chat client) → allowed.
    Same host as the request → allowed. Else must be listed in
    MCP_ALLOWED_ORIGINS."""
    meta = getattr(request, "META", {}) or {}
    origin = str(meta.get("HTTP_ORIGIN") or "").strip().rstrip("/").lower()
    if not origin or origin == "null":
        return not origin  # "null" origin (sandboxed iframe / file://) is refused
    host = str(meta.get("HTTP_HOST") or "").strip().lower()
    o_host = _urlsplit(origin).netloc.lower()
    if host and o_host == host:
        return True
    if origin in allowed_origins():
        return True
    # Browser origins the service already trusts for CORS (settings
    # CORS_ALLOWED_ORIGINS / CORS_ALLOWED_ORIGIN_REGEXES) must not be blocked
    # by the MCP layer — same rule as app.mcp_protocol_core.
    try:
        from app.mcp_cors_middleware import _origin_allowed as _cors_origin_allowed
        return bool(_cors_origin_allowed(origin))
    except Exception:  # noqa: BLE001 — CORS helper is optional
        return False


def task_status_from_row(status: str) -> str:
    return _TASK_STATUS_MAP.get(str(status or "").lower(), "working")


def task_descriptor(task_id: str, row_status: str, *, created_at=None) -> Dict[str, Any]:
    """Spec ``Task`` object (tasks/get result and CreateTaskResult.task)."""
    st = task_status_from_row(row_status)
    d: Dict[str, Any] = {"taskId": str(task_id), "status": st}
    if st == "working":
        d["pollInterval"] = TASK_POLL_INTERVAL_MS
    if created_at is not None:
        try:
            d["createdAt"] = created_at.isoformat()
        except Exception:  # noqa: BLE001
            pass
    return d


def accepted_to_task(data: Any) -> Optional[Dict[str, Any]]:
    """Our tools signal a queued job as ``{"accepted": true, "task_id": ...}``.
    Translate to the spec task descriptor for modern clients; None otherwise."""
    if not isinstance(data, dict) or data.get("accepted") is not True or not data.get("task_id"):
        return None
    d = task_descriptor(str(data["task_id"]), str(data.get("status") or "pending"))
    try:
        d["pollInterval"] = max(200, int(data.get("poll_after_ms") or TASK_POLL_INTERVAL_MS))
    except (TypeError, ValueError):
        pass
    return d
