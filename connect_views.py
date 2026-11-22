"""
Facebook Ads Connect API — save credentials, test connection, check status.
Endpoints: connect (POST), test (GET), status (GET).
Frontend calls these from Integrations.jsx and MultiAIChat.jsx.
"""
import json
import logging
import threading

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.http import HttpResponseRedirect, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import FacebookAdsCredential

logger = logging.getLogger("facebook_ads_mcp")

# Single source of truth for the Graph API version (see services.py).
from facebook_ads_mcp.services import (
    BASE_URL as FB_GRAPH_URL,
    META_GRAPH_API_VERSION,
    get_ad_accounts,
)


def _parse_body(request):
    """Parse JSON body from request."""
    data = {}
    if hasattr(request, "data") and request.data:
        data = dict(request.data)
    if not data and hasattr(request, "body") and request.body:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except Exception:
            pass
    return data or {}



def _login_required(view):
    from functools import wraps
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        from .auth_tokens import require_facebook_identity
        try:
            request.facebook_user_id = require_facebook_identity(request, _parse_body(request))
        except ValueError as exc:
            return JsonResponse({"ok": False, "error": str(exc)}, status=401)
        return view(request, *args, **kwargs)
    return wrapped

def _normalize(data):
    """Accept camelCase from frontend."""
    out = dict(data)
    mapping = {
        "accessToken": "access_token",
        "connectionName": "connection_name",
        "connectedEmail": "email",
        "userId": "user_id",
        "appId": "app_id",
        "appSecret": "app_secret",
    }
    for camel, snake in mapping.items():
        if camel in out and (snake not in out or not out.get(snake)):
            out[snake] = out.pop(camel)
    return out


def _send_notification_emails(email, connection_name):
    """Send connection confirmation emails in background thread."""
    try:
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", "noreply@localhost")
        admin_email = getattr(settings, "ADMIN_EMAIL", "")

        if email and "@" in email:
            try:
                send_mail(
                    subject="Facebook Ads Connected Successfully - AI Sales",
                    message=(
                        f"Hi,\n\n"
                        f"Your Facebook Ads account '{connection_name}' has been connected successfully.\n\n"
                        f"Your Facebook Ads MCP tools are now ready to use with AI Sales agent.\n\n"
                        f"Best regards,\n"
                        f"AI Sales Team"
                    ),
                    from_email=from_email,
                    recipient_list=[email],
                    fail_silently=True,
                )
            except Exception as exc:
                logger.warning("[FB Ads Connect] User email failed: %s", exc)

        if admin_email and "@" in admin_email:
            try:
                send_mail(
                    subject=f"New Facebook Ads Connection: {connection_name}",
                    message=(
                        f"A new Facebook Ads connection has been created.\n\n"
                        f"User Email: {email}\n"
                        f"Connection Name: {connection_name}\n"
                    ),
                    from_email=from_email,
                    recipient_list=[admin_email],
                    fail_silently=True,
                )
            except Exception as exc:
                logger.warning("[FB Ads Connect] Admin email failed: %s", exc)
    except Exception as exc:
        logger.warning("[FB Ads Connect] Email error: %s", exc)


@csrf_exempt
@require_http_methods(["POST"])
@_login_required
def connect_view(request):
    """
    POST /api/mcp/facebook-ads/connect/
    Body: { email (required), access_token (required), connection_name (optional), user_id (optional) }
    Saves FacebookAdsCredential and autowires BotFlowMCPConfig.
    """
    try:
        data = _normalize(_parse_body(request))

        email = (data.get("email") or data.get("connected_email") or "").strip()

        access_token = (data.get("access_token") or "").strip()
        if not access_token:
            return JsonResponse({"ok": False, "error": "access_token is required"}, status=400)

        connection_name = (data.get("connection_name") or "Facebook Ads").strip() or "Facebook Ads"
        app_id = (data.get("app_id") or "").strip()
        app_secret = (data.get("app_secret") or "").strip()

        # The decorator verifies the login and rejects conflicting selectors.
        body_user_id = (data.get("user_id") or "").strip()
        resolved_user_id = (
            request.facebook_user_id or ""
        ).strip()
        generated = False
        user_id = resolved_user_id

        logger.info(
            "[FB Ads Connect] email=%s body_user_id=%s resolved_user_id=%s final_user_id=%s "
            "generated=%s connection_name=%s has_token=%s",
            email, body_user_id, resolved_user_id, user_id, generated,
            connection_name, bool(access_token),
        )

        # --- Pre-validate the token with Meta (Addendum 10). Three outcomes:
        # (1) Meta explicitly rejects → HTTP 400, no DB write. Stops dead tokens
        #     from landing with connected=true in the status endpoint.
        # (2) Meta validates → prime expires_at + token_type on the row so the
        #     existing get_valid_token auto-refresh path can actually fire.
        # (3) Meta network / 5xx → log and continue; don't block users over
        #     Meta flakiness. Next tool call re-validates anyway.
        from datetime import datetime as _dt, timedelta, timezone as _tz
        from django.utils import timezone
        from .auth_tokens import (
            check_token_validity,
            exchange_for_long_lived_token,
        )

        validated: dict = {}
        validation_error = None
        try:
            validated = check_token_validity(
                access_token, app_id=app_id, app_secret=app_secret
            )
        except Exception as exc:  # noqa: BLE001 — Meta outage shouldn't block
            logger.warning("[FB Ads Connect] debug_token check failed: %s", exc)
            validated = {}
        else:
            if validated and "error" in validated and validated.get("is_valid") is False:
                validation_error = validated.get("error")

        if validation_error and validated.get("is_valid") is False:
            logger.info(
                "[FB Ads Connect] Meta rejected submitted token for user_id=%s: %s",
                user_id, validation_error,
            )
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        f"Token is invalid or expired per Meta: {validation_error}. "
                        "Generate a fresh access token (Graph API Explorer → Extend "
                        "Access Token, or a System User token) and try again."
                    ),
                },
                status=400,
            )

        # Derive expires_at + token_type from /debug_token. expires_at=0 means
        # never-expires (system user); keep DB expires_at NULL and mark long_lived.
        expires_at_dt = None
        resolved_token_type = None
        meta_exp_ts = validated.get("expires_at") if isinstance(validated, dict) else None
        if isinstance(meta_exp_ts, int) and meta_exp_ts > 0:
            expires_at_dt = _dt.fromtimestamp(meta_exp_ts, tz=_tz.utc)
            ttl = expires_at_dt - timezone.now()
            resolved_token_type = "long_lived" if ttl >= timedelta(days=7) else "short_lived"
        elif meta_exp_ts == 0:
            resolved_token_type = "long_lived"

        # Auto-exchange short-lived → long-lived when app creds are present.
        if app_id and app_secret and resolved_token_type == "short_lived":
            try:
                exchange = exchange_for_long_lived_token(access_token, app_id, app_secret)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[FB Ads Connect] long-lived exchange failed: %s", exc)
            else:
                new_token = (exchange.get("access_token") or "").strip()
                new_exp_in = exchange.get("expires_in") or 0
                if new_token:
                    access_token = new_token
                    resolved_token_type = "long_lived"
                    expires_at_dt = (
                        timezone.now() + timedelta(seconds=int(new_exp_in))
                        if new_exp_in else None
                    )
                    logger.info(
                        "[FB Ads Connect] exchanged short→long-lived for user_id=%s expires_in=%s",
                        user_id, new_exp_in,
                    )

        # Upsert: user_id is unique on FacebookAdsCredential
        cred = FacebookAdsCredential.objects.filter(user_id=user_id).first()

        if cred:
            from .services import invalidate_page_token_cache
            invalidate_page_token_cache(cred.access_token)
            cred.access_token = access_token
            if app_id:
                cred.app_id = app_id
            if app_secret:
                cred.app_secret = app_secret
            if resolved_token_type:
                cred.token_type = resolved_token_type
            if expires_at_dt is not None:
                cred.expires_at = expires_at_dt
            if (email or "").strip():
                cred.email = email.strip()
            cred.save()
            logger.info(
                "[FB Ads Connect] Updated existing for user_id=%s token_type=%s expires_at=%s",
                user_id, cred.token_type, cred.expires_at,
            )
        else:
            cred = FacebookAdsCredential.objects.create(
                user_id=user_id,
                email=(email or "").strip(),
                access_token=access_token,
                app_id=app_id,
                app_secret=app_secret,
                token_type=resolved_token_type or "short_lived",
                expires_at=expires_at_dt,
            )
            logger.info(
                "[FB Ads Connect] Created new for user_id=%s id=%s token_type=%s expires_at=%s",
                user_id, cred.id, cred.token_type, cred.expires_at,
            )

        # Autowire BotFlowMCPConfig
        autowire_result = {"bot_flow_wired": False, "tools_count": 0, "error": None}
        try:
            from app.bot_flow_autowire import autowire_bot_flow_from_connection
            autowire_result = autowire_bot_flow_from_connection(
                app_slug="facebook_ads",
                email=email,
                internal_user_id=user_id,
                request=request,
            )
        except Exception as exc:
            logger.warning("[FB Ads Connect] autowire failed: %s", exc)
            autowire_result["error"] = str(exc)

        # Email notifications in background
        threading.Thread(
            target=_send_notification_emails,
            args=(email, connection_name),
            daemon=True,
        ).start()

        base = request.build_absolute_uri("/").rstrip("/")
        return JsonResponse({
            "ok": True,
            "email": email,
            "user_id": user_id,
            "connection_name": connection_name,
            "status": "connected",
            "mcp_url": f"{base}/api/mcp/facebook-ads/",
            "bot_flow_wired": autowire_result.get("bot_flow_wired", False),
            "tools_count": autowire_result.get("tools_count", 0),
            "bot_flow_config_id": autowire_result.get("bot_flow_config_id"),
            "message": f"Facebook Ads '{connection_name}' connected successfully.",
        })

    except Exception as e:
        logger.exception("[FB Ads Connect] Unexpected error")
        return JsonResponse({"ok": False, "error": f"Internal error: {e}"}, status=500)


_STATUS_VALIDATION_CACHE_TTL_S = 60


def _mcp_base_url(request) -> str:
    return request.build_absolute_uri("/").rstrip("/")


def _status_payload(cred, base_url, *, extra=None):
    """Success envelope for a healthy, connected credential row."""
    exp_ts = 0
    if cred.expires_at:
        try:
            exp_ts = int(cred.expires_at.timestamp())
        except Exception:  # noqa: BLE001
            exp_ts = 0
    out = {
        "connected": True,
        "connection_name": "Facebook Ads",
        "email": "",
        "user_id": cred.user_id,
        "mcp_url": f"{base_url}/api/mcp/facebook-ads/",
        "updated_at": cred.updated_at.isoformat() if cred.updated_at else "",
        "expires_at": exp_ts,
        "token_type": cred.token_type or "short_lived",
        "app_credentials_present": bool(
            (cred.app_id or "").strip() and (cred.app_secret or "").strip()
        ),
    }
    if extra:
        out.update(extra)
    return out


@csrf_exempt
@require_http_methods(["GET"])
@_login_required
def status_view(request):
    """
    GET /api/mcp/facebook-ads/status/
    Returns connection status for the requesting user.

    Addendum 10: live-validates the stored token against Meta when
    ``expires_at`` is missing or within 5 minutes of now. Healthy long-lived
    tokens stay on the fast DB-only path. Positive/negative validation results
    are cached for 60 s per user_id to keep status probes cheap.
    """
    from datetime import datetime as _dt, timedelta, timezone as _tz
    from django.core.cache import cache
    from django.utils import timezone

    user_id = request.facebook_user_id
    uid = (user_id or "").strip()
    if not uid:
        return JsonResponse({"connected": False, "connection_name": "", "email": ""})

    cred = FacebookAdsCredential.objects.filter(user_id=uid).first()
    if not cred or not (cred.access_token or "").strip():
        return JsonResponse({"connected": False, "connection_name": "", "email": ""})

    base = _mcp_base_url(request)
    skip_validate = (request.GET.get("validate") or "").strip() == "0"

    # Fast path: populated, comfortably-future expiry → trust DB.
    if cred.expires_at and cred.expires_at > timezone.now() + timedelta(minutes=5):
        return JsonResponse(_status_payload(cred, base))

    if skip_validate:
        return JsonResponse(_status_payload(cred, base, extra={"validated": False}))

    # Slow path: expired / unknown → hit Meta (per-user 60s cache).
    cache_key = f"fb_ads:status_validated:{uid}"
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return JsonResponse(cached)

    from .auth_tokens import check_token_validity
    try:
        result = check_token_validity(
            cred.access_token,
            app_id=(cred.app_id or None),
            app_secret=(cred.app_secret or None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[FB Ads Status] debug_token call failed for %s: %s", uid, exc)
        return JsonResponse(
            _status_payload(
                cred, base,
                extra={"validated": False, "validation_error": "meta_unreachable"},
            )
        )

    if result.get("is_valid"):
        # Opportunistically refresh DB expiry so next probe fast-paths.
        meta_exp = result.get("expires_at") or 0
        if isinstance(meta_exp, int) and meta_exp > 0:
            try:
                cred.expires_at = _dt.fromtimestamp(meta_exp, tz=_tz.utc)
                ttl = cred.expires_at - timezone.now()
                cred.token_type = "long_lived" if ttl >= timedelta(days=7) else "short_lived"
                cred.save(update_fields=["expires_at", "token_type", "updated_at"])
            except Exception as exc:  # noqa: BLE001
                logger.debug("[FB Ads Status] could not persist refreshed expiry: %s", exc)
        elif meta_exp == 0:
            cred.expires_at = None
            cred.token_type = "long_lived"
            try:
                cred.save(update_fields=["expires_at", "token_type", "updated_at"])
            except Exception:  # noqa: BLE001
                pass
        payload = _status_payload(cred, base, extra={"validated": True})
        cache.set(cache_key, payload, _STATUS_VALIDATION_CACHE_TTL_S)
        return JsonResponse(payload)

    # Meta says the stored token is dead.
    reason = "token_invalid"
    err_msg = result.get("error") or "Token is no longer valid on Meta's side."
    if "expired" in str(err_msg).lower():
        reason = "token_expired"
    payload = {
        "connected": False,
        "reason": reason,
        "message": (
            "Stored Facebook Ads token has been revoked or expired on Meta's "
            "side. Open the Integrations page and reconnect with a fresh "
            "(preferably long-lived or System User) token."
        ),
        "detail": err_msg,
        "user_id": cred.user_id,
        "mcp_url": f"{base}/api/mcp/facebook-ads/",
        "updated_at": cred.updated_at.isoformat() if cred.updated_at else "",
        "app_credentials_present": bool(
            (cred.app_id or "").strip() and (cred.app_secret or "").strip()
        ),
    }
    cache.set(cache_key, payload, _STATUS_VALIDATION_CACHE_TTL_S)
    return JsonResponse(payload)


@csrf_exempt
@require_http_methods(["GET"])
@_login_required
def test_view(request):
    """
    GET /api/mcp/facebook-ads/test/
    Tests the saved access token by calling Facebook Graph API /me.
    """
    user_id = request.facebook_user_id
    uid = (user_id or "").strip()
    if not uid:
        return JsonResponse({"ok": False, "error": "user_id is required"}, status=400)

    cred = FacebookAdsCredential.objects.filter(user_id=uid).first()
    if not cred or not (cred.access_token or "").strip():
        return JsonResponse({"ok": False, "error": "No saved credentials found"}, status=404)

    try:
        r = requests.get(
            f"{FB_GRAPH_URL}/me",
            params={"access_token": cred.access_token, "fields": "id,name"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return JsonResponse({
                "ok": True,
                "message": "Connection is valid.",
                "facebook_user": data.get("name", ""),
                "facebook_id": data.get("id", ""),
            })
        else:
            err = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            msg = err.get("error", {}).get("message", r.text[:200])
            return JsonResponse({"ok": False, "error": f"Facebook API error: {msg}"}, status=400)
    except requests.Timeout:
        return JsonResponse({"ok": False, "error": "Facebook API timeout"}, status=504)
    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Test failed: {e}"}, status=500)


# ===========================================================================
# OAuth ("Connect with Facebook") — server-side single-app flow.
#
# Why a DB-backed state row instead of the session: Facebook's redirect to
# oauth_callback is a top-level browser GET that won't carry our X-User-Id
# header or a session cookie reliably. So user_id (resolved at oauth_start) and
# the exact redirect_uri are persisted on FacebookAdsOAuthState and recovered
# via the opaque `state` token in the callback.
# ===========================================================================

# Permissions requested during Facebook Login are configured via the
# FACEBOOK_OAUTH_SCOPES env var (see .env.mcp), NOT hardcoded here. This minimal
# fallback only applies if the env var is unset, so the flow never breaks.
_FB_OAUTH_SCOPES_DEFAULT = "email,public_profile"
FB_OAUTH_SCOPES = (
    (getattr(settings, "FACEBOOK_OAUTH_SCOPES", "") or "").strip()
    or _FB_OAUTH_SCOPES_DEFAULT
)


def _fb_app_credentials():
    """Server-registered Facebook app credentials (single-app pattern)."""
    return (
        (getattr(settings, "FACEBOOK_APP_ID", "") or "").strip(),
        (getattr(settings, "FACEBOOK_APP_SECRET", "") or "").strip(),
    )


def _fb_oauth_redirect_uri(request):
    """The exact redirect_uri used in BOTH the authorize URL and the token
    exchange — and which must be registered in the FB app dashboard. Uses the
    configured prod value if set, else derives from the request host (local)."""
    configured = (getattr(settings, "FACEBOOK_OAUTH_REDIRECT_URI", "") or "").strip()
    if configured:
        return configured
    base = request.build_absolute_uri("/").rstrip("/")
    return f"{base}/api/mcp/facebook-ads/oauth/callback/"


def _finish_oauth(success_url, params):
    """Redirect back to the frontend with status query params, or return JSON
    when no success_url was provided (e.g. direct API testing)."""
    from urllib.parse import urlencode
    if success_url:
        sep = "&" if "?" in success_url else "?"
        return HttpResponseRedirect(f"{success_url}{sep}{urlencode(params)}")
    return JsonResponse({"ok": params.get("facebook_ads") == "connected", **params})


@csrf_exempt
@require_http_methods(["GET", "POST"])
@_login_required
def oauth_start(request):
    """
    GET|POST /api/mcp/facebook-ads/oauth/start/
    Begins Facebook Login. Resolves user_id NOW (it can't be recovered from
    Facebook's redirect), persists FacebookAdsOAuthState, and returns the
    authorize URL for the frontend to navigate to.
    Body/query: { user_id?, email?, connection_name?, success_url? }
    """
    import secrets as _secrets
    from urllib.parse import urlencode
    from .models import FacebookAdsOAuthState

    try:
        data = _normalize(_parse_body(request))
        for k in ("user_id", "email", "connection_name", "success_url"):
            if not data.get(k) and request.GET.get(k):
                data[k] = request.GET.get(k)

        app_id, app_secret = _fb_app_credentials()
        if not app_id or not app_secret:
            return JsonResponse({
                "ok": False,
                "error": ("Facebook app credentials are not configured on the server. "
                          "Set FACEBOOK_APP_ID and FACEBOOK_APP_SECRET in the MCP env."),
            }, status=503)

        resolved_user_id = (request.facebook_user_id or "").strip()
        user_id = resolved_user_id

        success_url = (
            data.get("success_url")
            or getattr(settings, "FACEBOOK_OAUTH_SUCCESS_URL", "")
            or ""
        ).strip()
        email = (data.get("email") or "").strip()
        redirect_uri = _fb_oauth_redirect_uri(request)
        state = _secrets.token_urlsafe(24)

        # One in-flight handshake per user.
        FacebookAdsOAuthState.objects.filter(user_id=user_id).delete()
        FacebookAdsOAuthState.objects.create(
            state=state,
            user_id=user_id,
            redirect_uri=redirect_uri,
            success_url=success_url,
            connected_email=email,
        )

        authorize_url = (
            f"https://www.facebook.com/{META_GRAPH_API_VERSION}/dialog/oauth?"
            + urlencode({
                "client_id": app_id,
                "redirect_uri": redirect_uri,
                "state": state,
                "response_type": "code",
                "scope": FB_OAUTH_SCOPES,
            })
        )
        logger.info("[FB Ads OAuth] start user_id=%s redirect_uri=%s", user_id, redirect_uri)
        return JsonResponse({
            "ok": True,
            "mode": "oauth_kickoff",
            "user_id": user_id,
            "authorize_url": authorize_url,
            "state": state,
            "instruction": "Navigate the browser to authorize_url (window.location = authorize_url).",
        })
    except Exception as e:
        logger.exception("[FB Ads OAuth] start failed")
        return JsonResponse({"ok": False, "error": f"Internal error: {e}"}, status=500)


@csrf_exempt
@require_http_methods(["GET"])
def oauth_callback(request):
    """
    GET /api/mcp/facebook-ads/oauth/callback/?code=...&state=...
    Facebook redirects here. Validates state, exchanges code → long-lived
    token, stores the credential, captures ad accounts, then redirects back to
    the frontend success_url with ?facebook_ads=connected (or =error).
    """
    from datetime import timedelta
    from django.utils import timezone
    from .models import FacebookAdsOAuthState
    from .auth_tokens import exchange_for_long_lived_token

    code = (request.GET.get("code") or "").strip()
    state = (request.GET.get("state") or "").strip()
    err = (request.GET.get("error") or request.GET.get("error_reason") or "").strip()
    err_desc = (request.GET.get("error_description") or "").strip()

    st = FacebookAdsOAuthState.objects.filter(state=state).first() if state else None
    success_url = (st.success_url if st else "") or ""

    # User denied consent or provider returned an error.
    if err or not code:
        reason = err or "missing_code"
        logger.info("[FB Ads OAuth] callback error reason=%s desc=%s state_found=%s",
                    reason, err_desc, bool(st))
        if st:
            st.delete()
        return _finish_oauth(success_url, {"facebook_ads": "error", "reason": reason})

    if not st:
        return _finish_oauth("", {"facebook_ads": "error", "reason": "unknown_or_expired_state"})

    app_id, app_secret = _fb_app_credentials()
    if not app_id or not app_secret:
        st.delete()
        return JsonResponse(
            {"ok": False, "error": "Facebook app credentials not configured on server."},
            status=503,
        )

    user_id = st.user_id
    redirect_uri = st.redirect_uri  # MUST byte-match the authorize URL

    try:
        # 1) code -> short-lived token
        r = requests.get(
            f"{FB_GRAPH_URL}/oauth/access_token",
            params={
                "client_id": app_id,
                "client_secret": app_secret,
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=25,
        )
        token_data = r.json() if r.content else {}
        if r.status_code >= 400 or not token_data.get("access_token"):
            msg = (token_data.get("error", {}) or {}).get("message") or r.text[:200]
            logger.warning("[FB Ads OAuth] code exchange failed: %s", msg)
            st.delete()
            return _finish_oauth(success_url, {"facebook_ads": "error", "reason": "code_exchange_failed"})
        short_token = token_data["access_token"].strip()

        # 2) short -> long-lived (~60 days); fall back to short token on failure
        access_token = short_token
        expires_in = int(token_data.get("expires_in") or 0)
        token_type = "short_lived"
        try:
            ll = exchange_for_long_lived_token(short_token, app_id, app_secret)
            if ll.get("access_token"):
                access_token = ll["access_token"].strip()
                expires_in = int(ll.get("expires_in") or 0)
                token_type = "long_lived"
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FB Ads OAuth] long-lived exchange failed (keeping short token): %s", exc)

        expires_at = timezone.now() + timedelta(seconds=expires_in) if expires_in else None

        # 3) persist credential (reuses model + get_valid_token auto-refresh path)
        cred_defaults = {
            "access_token": access_token,
            "token_type": token_type,
            "expires_at": expires_at,
            "app_id": app_id,
            "app_secret": app_secret,
        }
        # Persist the connected email (captured at oauth_start) so it survives the
        # state-row deletion below and is visible in the admin. Don't blank an
        # existing email if this handshake didn't carry one.
        if (st.connected_email or "").strip():
            cred_defaults["email"] = st.connected_email.strip()
        FacebookAdsCredential.objects.update_or_create(
            user_id=user_id,
            defaults=cred_defaults,
        )

        # 4) capture ad accounts (+ default when exactly one)
        accounts = []
        default_ad_account_id = ""
        try:
            acc = get_ad_accounts(access_token)
            accounts = acc.get("data", []) if isinstance(acc, dict) else []
            if len(accounts) == 1:
                default_ad_account_id = (accounts[0].get("id") or "").strip()
                if default_ad_account_id:
                    FacebookAdsCredential.objects.filter(user_id=user_id).update(
                        default_ad_account_id=default_ad_account_id
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FB Ads OAuth] ad-account fetch failed: %s", exc)

        # 5) best-effort autowire (never fail connect over this)
        try:
            from app.bot_flow_autowire import autowire_bot_flow_from_connection
            autowire_bot_flow_from_connection(
                app_slug="facebook_ads",
                email=st.connected_email,
                internal_user_id=user_id,
                request=request,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[FB Ads OAuth] autowire failed: %s", exc)

        st.delete()
        logger.info("[FB Ads OAuth] connected user_id=%s token_type=%s accounts=%s",
                    user_id, token_type, len(accounts))
        return _finish_oauth(success_url, {
            "facebook_ads": "connected",
            "user_id": user_id,
            "ad_accounts": len(accounts),
            "default_ad_account_id": default_ad_account_id,
        })
    except requests.Timeout:
        st.delete()
        return _finish_oauth(success_url, {"facebook_ads": "error", "reason": "timeout"})
    except Exception:
        logger.exception("[FB Ads OAuth] callback failed")
        try:
            st.delete()
        except Exception:  # noqa: BLE001
            pass
        return _finish_oauth(success_url, {"facebook_ads": "error", "reason": "internal_error"})


@csrf_exempt
@require_http_methods(["GET", "POST"])
@_login_required
def adaccounts_view(request):
    """
    GET  /api/mcp/facebook-ads/adaccounts/?user_id=...  → list the connected
         user's ad accounts (powers the UI account picker).
    POST same with { default_ad_account_id }            → persist chosen default.
    The token is user-level and spans all ad accounts; ad_account_id stays a
    per-tool-call argument.
    """
    uid = (request.facebook_user_id or "").strip()
    if not uid:
        return JsonResponse({"ok": False, "error": "user_id is required"}, status=400)

    cred = FacebookAdsCredential.objects.filter(user_id=uid).first()
    if not cred or not (cred.access_token or "").strip():
        return JsonResponse({"ok": False, "error": "not connected"}, status=404)

    if request.method == "POST":
        data = _normalize(_parse_body(request))
        chosen = (data.get("default_ad_account_id") or "").strip()
        cred.default_ad_account_id = chosen
        cred.save(update_fields=["default_ad_account_id", "updated_at"])
        return JsonResponse({"ok": True, "user_id": uid, "default_ad_account_id": chosen})

    from .auth_tokens import get_valid_token
    token = get_valid_token(uid)
    if not token:
        return JsonResponse(
            {"ok": False, "error": "stored token invalid or expired; reconnect"},
            status=400,
        )
    result = get_ad_accounts(token)
    if isinstance(result, dict) and result.get("error"):
        return JsonResponse({"ok": False, "error": result["error"]}, status=400)
    return JsonResponse({
        "ok": True,
        "user_id": uid,
        "default_ad_account_id": cred.default_ad_account_id or "",
        "ad_accounts": result.get("data", []) if isinstance(result, dict) else [],
    })
