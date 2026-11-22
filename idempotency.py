"""Persist successful tools/call results by idempotency key (SHA-256 of tool name + client key)."""
from __future__ import annotations

import hashlib
import json
import logging

from django.db import IntegrityError

from .models import FacebookAdsMcpIdempotency

logger = logging.getLogger("facebook_ads_mcp")


def storage_key(tool_name: str, raw: str) -> str:
    return hashlib.sha256(f"{tool_name}\n{raw}".encode("utf-8")).hexdigest()


def get_cached_result(tool_name: str, raw: str) -> dict | None:
    if not raw or not tool_name:
        return None
    key = storage_key(tool_name, raw)
    row = FacebookAdsMcpIdempotency.objects.filter(idempotency_key=key, tool_name=tool_name).first()
    if not row:
        return None
    try:
        return json.loads(row.response_json)
    except json.JSONDecodeError:
        logger.warning("idempotency corrupt JSON tool=%s", tool_name)
        return None


def save_success(tool_name: str, raw: str, data: dict) -> None:
    """Store only successful tool payloads (no top-level error key) to allow retries after failures."""
    if not raw or not tool_name or not isinstance(data, dict) or data.get("error"):
        return
    key = storage_key(tool_name, raw)
    text = json.dumps(data, default=str)
    try:
        FacebookAdsMcpIdempotency.objects.create(idempotency_key=key, tool_name=tool_name, response_json=text)
    except IntegrityError:
        pass
