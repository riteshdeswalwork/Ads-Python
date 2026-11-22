"""
Facebook Ads token management: long-lived token exchange, expiry tracking, auto-refresh from DB.
No env fallback — credentials must come from tool arguments or DB.

Priority for resolving tokens:
1. arguments.token (non-empty) — client-supplied access token
2. arguments.user_id — DB FacebookAdsCredential with auto-refresh

Long-lived refresh (fb_exchange_token) uses app credentials from:
1. arguments.facebook_app_id + arguments.facebook_app_secret (or client_id + client_secret)
2. DB-stored app_id / app_secret on FacebookAdsCredential
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta

import requests
from django.utils import timezone

logger = logging.getLogger("facebook_ads_mcp")

# Single source of truth for the Graph API version (see services.py).
# Imported here so token exchange and OAuth flows track the same version
# the rest of the connector uses.
from facebook_ads_mcp.services import BASE_URL as FB_GRAPH_URL


def require_facebook_identity(request, body=None):
    """Verify the existing encrypted login JWT; selectors never establish identity."""
    from app.authentication import EncryptedJWTAuthentication
    try:
        if request is None:
            raise ValueError("Login required")
        auth = EncryptedJWTAuthentication()
        result = auth.authenticate(request)
        if result is None:
            raise ValueError("Login required")
        _, validated = result
        uid = str(validated.get("user_id") or validated.get("sub") or "").strip()
        if not uid or uid == "default":
            raise ValueError("Missing identity")
    except Exception as exc:
        raise ValueError("A valid login bearer token is required for Facebook MCP.") from exc

    def check(mapping):
        if not isinstance(mapping, dict):
            return
        for key in ("user_id", "userId", "tenant_id", "credential_user_id"):
            value = mapping.get(key)
            if value is not None and str(value).strip() and str(value).strip() != uid:
                raise ValueError("Requested user does not match the authenticated user.")
        for key in ("params", "arguments", "_meta"):
            check(mapping.get(key))

    for key in ("HTTP_X_USER_ID", "HTTP_X_TENANT_ID"):
        value = request.META.get(key)
        if value and str(value).strip() != uid:
            raise ValueError("Requested user does not match the authenticated user.")
    for source in (request.GET, request.POST):
        for key in ("user_id", "userId", "tenant_id", "credential_user_id"):
            for value in source.getlist(key):
                check({key: value})
    check(body)
    return uid


def resolve_dynamic_app_credentials(args: dict | None) -> tuple[str, str]:
    """
    App id/secret for token exchange: from tool arguments only (multi-tenant). No env fallback.
    Accepts facebook_app_id / facebook_app_secret or client_id / client_secret.
    """
    args = args or {}
    aid = (args.get("facebook_app_id") or args.get("client_id") or "").strip()
    sec = (args.get("facebook_app_secret") or args.get("client_secret") or "").strip()
    return aid, sec


def exchange_for_long_lived_token(short_token: str, app_id: str, app_secret: str, timeout: int = 25) -> dict:
    """
    Exchange a short-lived token for a long-lived one (~60 days).
    Returns dict with access_token, token_type, expires_in.
    Raises ValueError on failure.
    """
    short_token = (short_token or "").strip()
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not short_token or not app_id or not app_secret:
        raise ValueError("short_token, app_id, and app_secret are required for long-lived token exchange")

    r = requests.get(
        f"{FB_GRAPH_URL}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=timeout,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        msg = data.get("error", {}).get("message") or data.get("error_description") or r.text or f"HTTP {r.status_code}"
        raise ValueError(f"Long-lived token exchange failed: {msg}")
    access = (data.get("access_token") or "").strip()
    if not access:
        raise ValueError("Token exchange returned no access_token")
    return data


def check_token_validity(
    token: str,
    timeout: int = 10,
    *,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> dict:
    """
    Check if a token is valid using Facebook's debug_token endpoint.
    Uses app_id|app_secret as access_token when provided or from settings; otherwise input_token (limited).
    """
    token = (token or "").strip()
    if not token:
        return {"is_valid": False, "error": "No token provided"}

    aid = (app_id or "").strip()
    sec = (app_secret or "").strip()
    access_for_debug = f"{aid}|{sec}" if aid and sec else token

    r = requests.get(
        f"{FB_GRAPH_URL}/debug_token",
        params={"input_token": token, "access_token": access_for_debug},
        timeout=timeout,
    )
    data = r.json() if r.content else {}
    # Meta sometimes returns ``data["error"]`` as a string (e.g. on
    # malformed input tokens) instead of the documented {message, code, ...}
    # dict shape. Coerce to a dict so the .get(...) chain below doesn't
    # raise AttributeError on the chat-side.
    if not isinstance(data, dict):
        data = {"error": {"message": str(data)}}
    err = data.get("error")
    if isinstance(err, str):
        err = {"message": err}
    elif not isinstance(err, dict):
        err = {}
    if r.status_code >= 400:
        return {"is_valid": False, "error": err.get("message", f"HTTP {r.status_code}")}

    debug_data = data.get("data") or {}
    if not isinstance(debug_data, dict):
        debug_data = {}
    return {
        "is_valid": debug_data.get("is_valid", False),
        "expires_at": debug_data.get("expires_at", 0),
        "scopes": debug_data.get("scopes", []),
        "app_id": debug_data.get("app_id", ""),
        "user_id": debug_data.get("user_id", ""),
    }


def _persist_refreshed_credential(user_id: str, access_token: str, expires_in: int, app_id: str, app_secret: str) -> None:
    from .models import FacebookAdsCredential

    uid = (user_id or "").strip()
    if not uid:
        return
    exp = timezone.now() + timedelta(seconds=int(expires_in)) if expires_in else None
    defaults = {
        "access_token": access_token,
        "token_type": "long_lived",
        "expires_at": exp,
    }
    if app_id:
        defaults["app_id"] = app_id
    if app_secret:
        defaults["app_secret"] = app_secret
    FacebookAdsCredential.objects.update_or_create(user_id=uid, defaults=defaults)


def ensure_refreshed_user_token(
    token: str,
    *,
    persist_user_id: str | None = None,
    force: bool = False,
    app_id: str | None = None,
    app_secret: str | None = None,
) -> tuple[str, dict]:
    """
    If FACEBOOK_AUTO_REFRESH_TOKEN and app credentials exist (from arguments or settings), call fb_exchange_token
    when the token is invalid or expiring within FACEBOOK_TOKEN_REFRESH_TTL_SECONDS.
    Persists to FacebookAdsCredential when persist_user_id is set.
    """
    from django.conf import settings

    token = (token or "").strip()
    meta: dict = {"token_refreshed": False}
    if not token:
        return token, meta
    if not getattr(settings, "FACEBOOK_AUTO_REFRESH_TOKEN", True):
        meta["token_refresh_skipped"] = "disabled"
        return token, meta

    aid = (app_id or "").strip()
    sec = (app_secret or "").strip()
    if not aid or not sec:
        meta["token_refresh_skipped"] = "no_app_credentials"
        return token, meta

    thresh = int(getattr(settings, "FACEBOOK_TOKEN_REFRESH_TTL_SECONDS", 7 * 24 * 3600) or 7 * 24 * 3600)
    dbg = check_token_validity(token, app_id=aid, app_secret=sec)
    ttl: int | None = None
    if dbg.get("is_valid") and dbg.get("expires_at"):
        try:
            ttl = int(dbg["expires_at"]) - int(time.time())
        except (TypeError, ValueError):
            ttl = None

    need = force or not dbg.get("is_valid")
    if not need and ttl is not None:
        need = ttl < thresh

    if not need:
        if ttl is not None:
            meta["token_ttl_seconds"] = ttl
        return token, meta

    try:
        data = exchange_for_long_lived_token(token, aid, sec)
        new_tok = (data.get("access_token") or "").strip()
        exp_in = int(data.get("expires_in") or 0)
        if not new_tok:
            meta["token_refresh_error"] = "exchange returned empty token"
            return token, meta
        meta["token_refreshed"] = True
        meta["expires_in"] = exp_in
        if persist_user_id:
            _persist_refreshed_credential(persist_user_id, new_tok, exp_in, aid, sec)
        return new_tok, meta
    except ValueError as e:
        meta["token_refresh_error"] = str(e)
        return token, meta


def resolve_mcp_access_token(args: dict | None, request=None) -> tuple[str, dict]:
    """
    Resolve token for MCP tools/call: arguments.token, arguments.user_id, or the
    request's ``X-User-Id`` header / JSON-RPC body user_id (resolved by
    :func:`app.tenant_http.mcp_resolve_user_id`). Then apply
    ensure_refreshed_user_token when app credentials exist.

    Optional arguments:
      facebook_app_id, facebook_app_secret — or client_id, client_secret (per-user app; no server env required)
      skip_token_refresh — if truthy, skip proactive exchange
      credential_user_id — when arguments.token is used, persist refreshed token under this user_id
    """
    args = args or {}
    if args.get("skip_token_refresh"):
        return resolve_access_token(args, request=request)

    token, meta = resolve_access_token(args, request=request)
    persist = (args.get("credential_user_id") or "").strip() or None
    if persist is None and meta.get("auth_source") in (
        "database.FacebookAdsCredential",
        "header.X-User-Id",
    ):
        persist = (meta.get("user_id") or "").strip() or None

    app_id, app_secret = resolve_dynamic_app_credentials(args)
    new_tok, rmeta = ensure_refreshed_user_token(
        token, persist_user_id=persist, app_id=app_id, app_secret=app_secret
    )
    return new_tok, {**meta, **rmeta}


def get_valid_token(user_id: str) -> str | None:
    """
    Fetch token from DB for user_id. If expired and app credentials available,
    attempt long-lived token exchange. Returns valid token or None.
    """
    from .models import FacebookAdsCredential

    user_id = (user_id or "").strip()
    if not user_id:
        return None

    try:
        cred = FacebookAdsCredential.objects.get(user_id=user_id)
    except FacebookAdsCredential.DoesNotExist:
        return None

    token = (cred.access_token or "").strip()
    if not token:
        return None

    # Check if token is expired (with 5-minute buffer)
    if cred.expires_at and cred.expires_at <= timezone.now() + timedelta(minutes=5):
        logger.info("Token expired for user_id=%s, attempting refresh", user_id)
        app_id = (cred.app_id or "").strip()
        app_secret = (cred.app_secret or "").strip()

        if app_id and app_secret:
            try:
                data = exchange_for_long_lived_token(token, app_id, app_secret)
                new_token = data.get("access_token", "")
                expires_in = data.get("expires_in", 0)
                from .services import invalidate_page_token_cache
                invalidate_page_token_cache(cred.access_token)
                cred.access_token = new_token
                cred.token_type = "long_lived"
                if expires_in:
                    cred.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
                cred.save()
                logger.info("Token refreshed for user_id=%s, expires_in=%s", user_id, expires_in)
                return new_token
            except (ValueError, Exception) as e:
                logger.warning("Token refresh failed for user_id=%s: %s", user_id, e)
                return None
        else:
            logger.warning("Cannot refresh token for user_id=%s: no app_id/app_secret", user_id)
            return None

    return token


def resolve_access_token(args: dict, request=None) -> tuple[str, dict]:
    """Resolve a provider token only after verifying the caller's login JWT.

    Explicit provider tokens are supported; saved credentials are selected solely
    by the authenticated identity. The login bearer is never a provider token.
    """
    args = args or {}
    uid = require_facebook_identity(request, args)
    direct = (args.get("token") or "").strip()
    if direct:
        return direct, {"auth_source": "arguments.token", "user_id": uid}
    token = get_valid_token(uid)
    if token:
        return token, {"auth_source": "database.FacebookAdsCredential", "user_id": uid}
    raise ValueError("No valid Facebook credential for the authenticated user. Connect Facebook first.")
