"""
Facebook Ads MCP audit: stderr (stdio MCP must keep stdout JSON-only) + server logs, secrets redacted.
"""
from __future__ import annotations

import copy
import json
import logging
import sys
import time
import uuid

logger = logging.getLogger("facebook_ads_mcp.audit")

_REDACT_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "refresh",
    "bearer",
    "access_token",
    "client_secret",
)


def redact_for_log(obj, depth: int = 0):
    """Deep-copy style redaction for safe printing."""
    if depth > 14:
        return "***MAX_DEPTH***"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if any(s in kl for s in _REDACT_SUBSTRINGS):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact_for_log(v, depth + 1)
        return out
    if isinstance(obj, list):
        return [redact_for_log(x, depth + 1) for x in obj[:300]]
    return obj


def safe_json_dumps(obj, max_len: int = 16000) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception as e:
        s = f"<json error: {e}>"
    if len(s) > max_len:
        s = s[:max_len] + f"...(truncated +{len(s) - max_len} chars)"
    return s


def summarize_jsonrpc_response_for_log(method: str | None, response_dict: dict) -> dict:
    """Shrink large tools/list and tools/call payloads for logs."""
    if not isinstance(response_dict, dict):
        return {"raw": str(response_dict)[:500]}
    err = response_dict.get("error")
    if err:
        return {"error": err}
    res = response_dict.get("result")
    m = (method or "").strip()
    if m == "tools/list" and isinstance(res, dict):
        tools = res.get("tools") or []
        names = [t.get("name") for t in tools if isinstance(t, dict)]
        return {
            "result": {
                "tools_count": len(names),
                "tool_names": names[:80],
            }
        }
    if m == "tools/call" and isinstance(res, dict):
        content = res.get("content") or []
        if content and isinstance(content[0], dict):
            text = content[0].get("text") or ""
            return {
                "result": {
                    "type": content[0].get("type"),
                    "text_preview": text[:2000],
                    "text_length": len(text),
                }
            }
        return {"result": res}
    if m == "initialize" and isinstance(res, dict):
        si = res.get("serverInfo") or {}
        return {
            "result": {
                "protocolVersion": res.get("protocolVersion"),
                "serverInfo": {"name": si.get("name"), "version": si.get("version")},
            }
        }
    return redact_for_log(copy.deepcopy(response_dict) if isinstance(response_dict, dict) else response_dict)


def generate_request_id() -> str:
    """Generate a unique request ID for tracing."""
    return uuid.uuid4().hex[:12]


def emit_fb_ads_mcp_audit(
    phase: str,
    *,
    request=None,
    body: dict | None = None,
    response: dict | None = None,
    method: str | None = None,
    tool_name: str | None = None,
    transport: str = "http",
    extra: str | None = None,
    request_id: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """stderr (safe with MCP stdio — never print audit lines to stdout) + logger.info."""
    parts = [f"[FB Ads MCP] {phase}", f"transport={transport}"]
    if request_id:
        parts.append(f"req_id={request_id}")
    if method:
        parts.append(f"jsonrpc_method={method}")
    if tool_name:
        parts.append(f"tool={tool_name}")
    if duration_ms is not None:
        parts.append(f"duration_ms={duration_ms}")
    if request is not None:
        parts.append(f"path={getattr(request, 'path', '?')}")
        meta = getattr(request, "META", {}) or {}
        uid = meta.get("HTTP_X_USER_ID") or meta.get("HTTP_X_TENANT_ID") or "-"
        parts.append(f"X-User-Id={uid}")
        parts.append(f"http_method={getattr(request, 'method', '?')}")
    line = " | ".join(parts)
    print(line, file=sys.stderr, flush=True)
    logger.info(line)

    if body is not None:
        rb = redact_for_log(copy.deepcopy(body) if isinstance(body, dict) else body)
        sj = safe_json_dumps(rb)
        print(f"[FB Ads MCP] REQUEST_PAYLOAD {sj}", file=sys.stderr, flush=True)
        logger.info("[FB Ads MCP] REQUEST_PAYLOAD %s", sj)

    if response is not None and isinstance(response, dict):
        summ = summarize_jsonrpc_response_for_log(method, response)
        sj = safe_json_dumps(summ)
        print(f"[FB Ads MCP] RESPONSE_SUMMARY {sj}", file=sys.stderr, flush=True)
        logger.info("[FB Ads MCP] RESPONSE_SUMMARY %s", sj)

    if extra:
        print(f"[FB Ads MCP] {extra}", file=sys.stderr, flush=True)
        logger.info("[FB Ads MCP] %s", extra)


def save_audit_to_db(
    *,
    request_id: str,
    user_id: str = "",
    method: str = "",
    tool_name: str = "",
    request_payload: str = "",
    response_summary: str = "",
    status: str = "success",
    duration_ms: int = 0,
) -> None:
    """Persist audit log entry to database (best-effort, never raises)."""
    try:
        from .models import FacebookAdsMcpAuditLog

        FacebookAdsMcpAuditLog.objects.create(
            request_id=request_id,
            user_id=user_id or "",
            method=method,
            tool_name=tool_name,
            request_payload=request_payload[:16000],
            response_summary=response_summary[:16000],
            status=status,
            duration_ms=duration_ms,
        )
    except Exception as e:
        logger.warning("Failed to save audit log: %s", e)
