"""
Facebook Marketing API (Graph API) service layer — real API calls.
All functions accept token; account_id can be numeric or act_XXX.
Required: Facebook App with Marketing API, access token with ads_read permission.
"""
import base64
import binascii
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("facebook_ads_mcp")

# Meta Graph API version. Single source of truth across the connector
# (services.py, auth_tokens.py, connect_views.py all read this). Configurable
# via the META_GRAPH_API_VERSION env var.
#
# Current default: v26.0 (released Jul 29 2026). v24.0 retires 2026-10-06,
# v25.0 TBD — running on the latest gives us the longest support window.
# v26 behaviour that applies to ALL versions from 2026-10-27: Messenger
# Stories + Instagram Explore placements removed, HEC special-category ad
# sets need an explicit targeting_automation.advantage_audience, Shop ads
# default to WEBSITE_AND_SHOP (opt-out via destination_spec). See:
# https://developers.facebook.com/docs/graph-api/changelog/version26.0
META_GRAPH_API_VERSION = os.environ.get("META_GRAPH_API_VERSION", "v26.0").strip() or "v26.0"
BASE_URL = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"
DEFAULT_TIMEOUT = 30
MAX_LIMIT = 500  # Graph API typical max for list endpoints

# Process-local cache of page-scoped access tokens harvested from
# /me/accounts and /me/businesses → owned_pages / client_pages responses.
# Populated as a side-effect of get_pages(); read by get_page_access_token()
# before any Meta API call. This avoids a second user-token GET to
# /{page_id}?fields=access_token which itself fails (#200) for
# cross-business pages where the user-token's per-page grant is narrow.
# Entries are keyed by (credential grant, page id) — the grant is a hash of
# the caller's own user token — so a page token harvested under tenant A can
# never be returned to tenant B, even when both select the same public Page
# ID on the same worker. Entries expire after FACEBOOK_PAGE_TOKEN_CACHE_TTL
# seconds and are dropped when the owning user token is replaced/disconnected
# (see invalidate_page_token_cache). Dict assignment in CPython is atomic.
_PAGE_TOKEN_CACHE: dict = {}


def _grant_id(user_token) -> str:
    import hashlib
    return hashlib.sha256(str(user_token or "").encode("utf-8")).hexdigest()[:32]


def _page_cache_key(user_token, page_id) -> tuple:
    return (_grant_id(user_token), str(page_id or "").strip())


def _page_cache_ttl() -> int:
    try:
        from django.conf import settings as _s
        return int(getattr(_s, "FACEBOOK_PAGE_TOKEN_CACHE_TTL", 3600))
    except Exception:  # noqa: BLE001
        return 3600


def _page_cache_get(user_token, page_id):
    import time as _t
    entry = _PAGE_TOKEN_CACHE.get(_page_cache_key(user_token, page_id))
    if not entry:
        return None
    tok, exp = entry
    if exp <= _t.monotonic():
        _PAGE_TOKEN_CACHE.pop(_page_cache_key(user_token, page_id), None)
        return None
    return tok


def _page_cache_set(user_token, page_id, page_token) -> None:
    import time as _t
    _PAGE_TOKEN_CACHE[_page_cache_key(user_token, page_id)] = (page_token, _t.monotonic() + _page_cache_ttl())


def invalidate_page_token_cache(user_token=None) -> int:
    """Drop cached page tokens for one user token (or all when None). Returns count removed."""
    if user_token is None:
        n = len(_PAGE_TOKEN_CACHE); _PAGE_TOKEN_CACHE.clear(); return n
    g = _grant_id(user_token)
    keys = [k for k in _PAGE_TOKEN_CACHE if k[0] == g]
    for k in keys:
        _PAGE_TOKEN_CACHE.pop(k, None)
    return len(keys)

# Map user-friendly / doc event names to Meta API custom_event_type values.
# Meta's promoted_object[custom_event_type] enum (as of v19): AD_IMPRESSION,
# RATE, TUTORIAL_COMPLETION, CONTACT, CUSTOMIZE_PRODUCT, DONATE, FIND_LOCATION,
# SCHEDULE, START_TRIAL, SUBMIT_APPLICATION, SUBSCRIBE, ADD_TO_CART,
# ADD_TO_WISHLIST, INITIATED_CHECKOUT, ADD_PAYMENT_INFO, PURCHASE, LEAD,
# COMPLETE_REGISTRATION, CONTENT_VIEW, SEARCH, SERVICE_BOOKING_REQUEST,
# MESSAGING_CONVERSATION_STARTED_7D, LEVEL_ACHIEVED, ACHIEVEMENT_UNLOCKED,
# SPENT_CREDITS, LISTING_INTERACTION, D2_RETENTION, D7_RETENTION, OTHER.
#
# Anything outside this list must be aliased to a valid enum or stripped.
# Map common brief-style names (PAGE_VIEW, VIDEO_VIEW, etc.) so Meta accepts
# them. Note: VIDEO_VIEW is NOT a valid custom_event_type for promoted_object
# — for video-views goals the pixel doesn't attach the same way; closest
# equivalent is CONTENT_VIEW (or strip the pixel entirely; see
# _NO_PIXEL_ATTACH_GOALS for video/awareness handling).
PIXEL_EVENT_TYPE_MAP = {
    # PAGE_VIEW family → CONTENT_VIEW
    "PAGE_VIEW": "CONTENT_VIEW",
    "PAGEVIEW": "CONTENT_VIEW",
    "PageView": "CONTENT_VIEW",
    "page_view": "CONTENT_VIEW",
    "VIEW_CONTENT": "CONTENT_VIEW",
    "CONTENT_VIEW": "CONTENT_VIEW",
    "VIEW": "CONTENT_VIEW",
    # VIDEO_VIEW family → CONTENT_VIEW (video views aren't a valid pixel
    # custom_event_type on promoted_object; CONTENT_VIEW is the closest
    # accepted value for "viewed media" intent).
    "VIDEO_VIEW": "CONTENT_VIEW",
    "VIDEO_VIEWS": "CONTENT_VIEW",
    "VideoView": "CONTENT_VIEW",
    "video_view": "CONTENT_VIEW",
    "THRUPLAY": "CONTENT_VIEW",
    # Lead/conversion variants
    "PURCHASE": "PURCHASE",
    "ADD_TO_CART": "ADD_TO_CART",
    "ATC": "ADD_TO_CART",
    "LEAD": "LEAD",
    "LEAD_GENERATION": "LEAD",
    "COMPLETE_REGISTRATION": "COMPLETE_REGISTRATION",
    "REGISTRATION": "COMPLETE_REGISTRATION",
    "SIGN_UP": "COMPLETE_REGISTRATION",
    "SIGNUP": "COMPLETE_REGISTRATION",
    "ADD_PAYMENT_INFO": "ADD_PAYMENT_INFO",
    "PAYMENT_INFO": "ADD_PAYMENT_INFO",
    "ADD_TO_WISHLIST": "ADD_TO_WISHLIST",
    "WISHLIST": "ADD_TO_WISHLIST",
    "INITIATED_CHECKOUT": "INITIATED_CHECKOUT",
    "INITIATE_CHECKOUT": "INITIATED_CHECKOUT",
    "CHECKOUT": "INITIATED_CHECKOUT",
    "SEARCH": "SEARCH",
    "SUBSCRIBE": "SUBSCRIBE",
    "SUBSCRIPTION": "SUBSCRIBE",
    "START_TRIAL": "START_TRIAL",
    "TRIAL": "START_TRIAL",
    "CONTACT": "CONTACT",
    "CUSTOMIZE_PRODUCT": "CUSTOMIZE_PRODUCT",
    "DONATE": "DONATE",
    "DONATION": "DONATE",
    "FIND_LOCATION": "FIND_LOCATION",
    "LOCATION": "FIND_LOCATION",
    "SCHEDULE": "SCHEDULE",
    "BOOKING": "SCHEDULE",
    "BOOK": "SCHEDULE",
    "SUBMIT_APPLICATION": "SUBMIT_APPLICATION",
    "APPLICATION": "SUBMIT_APPLICATION",
    "APPLY": "SUBMIT_APPLICATION",
    # Other Meta-accepted enums (pass-through)
    "AD_IMPRESSION": "AD_IMPRESSION",
    "RATE": "RATE",
    "TUTORIAL_COMPLETION": "TUTORIAL_COMPLETION",
    "SERVICE_BOOKING_REQUEST": "SERVICE_BOOKING_REQUEST",
    "MESSAGING_CONVERSATION_STARTED_7D": "MESSAGING_CONVERSATION_STARTED_7D",
    "LEVEL_ACHIEVED": "LEVEL_ACHIEVED",
    "ACHIEVEMENT_UNLOCKED": "ACHIEVEMENT_UNLOCKED",
    "SPENT_CREDITS": "SPENT_CREDITS",
    "LISTING_INTERACTION": "LISTING_INTERACTION",
    "D2_RETENTION": "D2_RETENTION",
    "D7_RETENTION": "D7_RETENTION",
    "OTHER": "OTHER",
}

# Valid geo location types returned by Meta targeting search
_GEO_LOCATION_TYPES = frozenset({
    "country", "region", "city", "zip", "geo_market",
    "electoral_district", "country_group", "subcity",
    "neighborhood", "place",
})

# BOL7 Social Growth doc — exactly these 10 cities, in order (Meta adgeolocation keys).
# Verified via Graph type=adgeolocation, location_types=["city"], country_code=IN (Delhi = primary NCR / New Delhi key).
GEO_CITIES_BOL7_TOP_10_IN = [
    {"key": "2727942", "radius": 40, "distance_unit": "kilometer"},  # Delhi
    {"key": "1035921", "radius": 40, "distance_unit": "kilometer"},  # Mumbai
    {"key": "1017930", "radius": 40, "distance_unit": "kilometer"},  # Bangalore
    {"key": "1027234", "radius": 40, "distance_unit": "kilometer"},  # Hyderabad
    {"key": "1021534", "radius": 40, "distance_unit": "kilometer"},  # Chennai
    {"key": "1039952", "radius": 40, "distance_unit": "kilometer"},  # Pune
    {"key": "1020734", "radius": 40, "distance_unit": "kilometer"},  # Kolkata
    {"key": "1015741", "radius": 40, "distance_unit": "kilometer"},  # Ahmedabad
    {"key": "1027633", "radius": 40, "distance_unit": "kilometer"},  # Jaipur (Rajasthan)
    {"key": "1021145", "radius": 40, "distance_unit": "kilometer"},  # Chandigarh
]

GEO_CITIES_PRESETS = {
    "bol7_top_10_in": GEO_CITIES_BOL7_TOP_10_IN,
}


def _account_id(account_id):
    """Ensure account_id is in act_XXX format."""
    if not account_id:
        return None
    s = str(account_id).strip()
    if s.startswith("act_"):
        return s
    return f"act_{s}"


def _attach_why_failed(resp):
    """Add human-readable why_failed AND a generic structuredError envelope.

    ``why_failed`` is kept for backward compat. ``structuredError`` is the new
    connector-agnostic envelope the loop_agent recognises when allocating
    truncation budget — so the recovery hints are never clipped by the default
    char cap. Shape:
        {
          "structuredError": {
            "code": int|None, "subcode": int|None,
            "summary": str, "likely_causes": [str,...],
            "what_to_try": [str,...], "raw": <original error dict>
          }
        }
    """
    if isinstance(resp, dict) and isinstance(resp.get("error"), dict) and resp["error"]:
        try:
            from . import api_errors

            why = api_errors.explain_graph_error(resp["error"])
            resp["why_failed"] = why
            err = resp["error"]
            resp["structuredError"] = {
                "code": err.get("code"),
                "subcode": err.get("error_subcode"),
                "summary": (why or {}).get("summary") or err.get("message") or "",
                "likely_causes": (why or {}).get("likely_causes") or [],
                "what_to_try": (why or {}).get("what_to_try") or [],
                "raw": err,
            }
        except Exception:
            pass
    return resp


def _is_rate_limit_error(resp: Any) -> bool:
    """True when Meta returned a transient rate-limit / throttling error.

    Meta surfaces these as error code 17 (User request limit reached) or
    error code 4 (App-level rate limit) with subcodes like 2446079. The
    ``message`` field also contains 'request limit' or 'limit reached'
    strings on rare variants — we match on both for robustness.
    """
    if not isinstance(resp, dict):
        return False
    err = resp.get("error") or {}
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    try:
        code_i = int(code) if code is not None else None
    except (TypeError, ValueError):
        code_i = None
    if code_i in (17, 4):
        return True
    msg = str(err.get("message") or "").lower()
    return ("request limit" in msg) or ("rate limit" in msg) or ("temporarily blocked" in msg)


def _get(url, params, timeout=DEFAULT_TIMEOUT):
    """GET request; returns parsed JSON. On HTTP error returns dict with error from API."""
    logger.info("GET %s", url)
    try:
        r = requests.get(url, params=params, timeout=timeout)
        data = r.json()
        if r.status_code >= 400:
            logger.warning("GET %s -> %s | error: %s", url, r.status_code, data.get("error", {}).get("message", ""))
            if isinstance(data, dict) and "error" in data:
                return _attach_why_failed(data)
            return _attach_why_failed({"error": {"message": f"HTTP {r.status_code}", "code": r.status_code, "body": data}})
        logger.info("GET %s -> %s OK", url, r.status_code)
        return data
    except requests.RequestException as e:
        logger.error("GET %s -> RequestException: %s", url, e)
        return _attach_why_failed({"error": {"message": str(e), "type": "request_error"}})
    except ValueError:
        logger.error("GET %s -> Invalid JSON response", url)
        return _attach_why_failed({"error": {"message": "Invalid JSON response from API", "type": "parse_error"}})


def _is_reduce_data_error(resp) -> bool:
    """True for Meta's generic code-1 "Please reduce the amount of data you're
    asking for, then retry your request" — a server-side timeout/complexity
    error, NOT a permissions or shape error. Pages with a large post history
    (e.g. BOL7 1876788849233669) return it for /posts and /published_posts
    even at modest page sizes when ``message``/``attachments`` are requested.
    The only cure is a smaller ``limit``."""
    if not isinstance(resp, dict):
        return False
    err = resp.get("error")
    if not isinstance(err, dict):
        return False
    msg = str(err.get("message") or "").lower()
    if "reduce the amount of data" in msg:
        return True
    # Bare code 1 without a subcode is the same "unknown/timeout" family.
    return err.get("code") == 1 and not err.get("error_subcode")


_SHRINK_LIMITS = (100, 50, 25, 10, 5)


def _get_shrinking(url, params, limits=_SHRINK_LIMITS):
    """``_get`` that retries with a progressively smaller ``limit`` when Meta
    answers "Please reduce the amount of data". Starts from ``params['limit']``
    (or the first value in ``limits``) and only ever shrinks. Returns the
    first non-reduce-data response, or the last error. Positional ``_get``
    call so unit tests that stub ``_get(url, params)`` keep working."""
    p = dict(params or {})
    try:
        start = int(p.get("limit") or limits[0])
    except (TypeError, ValueError):
        start = limits[0]
    ladder = [start] + [l for l in limits if l < start]
    last = None
    for lim in ladder:
        p["limit"] = lim
        last = _get(url, p)
        if not _is_reduce_data_error(last):
            return last
        logger.warning("GET %s limit=%s -> Meta 'reduce the amount of data'; shrinking", url, lim)
    return last


def _get_with_rate_limit_retry(url, params, timeout=DEFAULT_TIMEOUT, max_retries=3, base_delay=1.5):
    """``_get`` with exponential-backoff retry on Meta rate-limit errors.

    Meta's targeting_search endpoint regularly throttles when many city /
    interest queries fan out in parallel (e.g. building a 10-city + 8-interest
    document). Without retry the first miss aborts the whole resolver
    pipeline — we've seen "User request limit reached" land mid-flight.

    Retries up to ``max_retries`` times with ``base_delay * 2**attempt``
    seconds between attempts. Non-rate-limit errors are returned immediately
    (no point retrying a 400 Bad Request)."""
    import time as _time
    last = None
    for attempt in range(max_retries + 1):
        last = _get(url, params, timeout=timeout)
        if not _is_rate_limit_error(last):
            return last
        if attempt < max_retries:
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "GET %s rate-limited (attempt %s/%s); sleeping %.1fs before retry",
                url, attempt + 1, max_retries, delay,
            )
            _time.sleep(delay)
    return last


def _post(url, data, timeout=DEFAULT_TIMEOUT):
    """POST request (form body); returns parsed JSON. For create/update. Requires ads_management."""
    logger.info("POST %s", url)
    try:
        r = requests.post(url, data=data, timeout=timeout)
        resp = r.json()
        if r.status_code >= 400:
            logger.warning("POST %s -> %s | error: %s", url, r.status_code, resp.get("error", {}).get("message", ""))
            if isinstance(resp, dict) and "error" in resp:
                return _attach_why_failed(resp)
            return _attach_why_failed({"error": {"message": f"HTTP {r.status_code}", "code": r.status_code, "body": resp}})
        logger.info("POST %s -> %s OK", url, r.status_code)
        return resp
    except requests.RequestException as e:
        logger.error("POST %s -> RequestException: %s", url, e)
        return _attach_why_failed({"error": {"message": str(e), "type": "request_error"}})
    except ValueError:
        logger.error("POST %s -> Invalid JSON response", url)
        return _attach_why_failed({"error": {"message": "Invalid JSON response from API", "type": "parse_error"}})


def _post_with_rate_limit_retry(url, data, timeout=DEFAULT_TIMEOUT, max_retries=2, base_delay=2.0):
    """``_post`` with exponential-backoff retry on Meta rate-limit errors.

    Mirrors ``_get_with_rate_limit_retry`` for create/update flows in
    production_flow. Defaults to 2 retries (3 attempts total) with delays
    2s/8s — keeps end-to-end latency bounded while surviving the typical
    transient throttle bursts. Non-rate-limit errors return immediately.
    """
    import time as _time
    last = None
    for attempt in range(max_retries + 1):
        last = _post(url, data, timeout=timeout)
        if not _is_rate_limit_error(last):
            return last
        if attempt < max_retries:
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "POST %s rate-limited (attempt %s/%s); sleeping %.1fs before retry",
                url, attempt + 1, max_retries, delay,
            )
            _time.sleep(delay)
    return last


def _post_multipart(url, data, files, timeout=DEFAULT_TIMEOUT):
    """POST multipart/form-data (e.g. adimages bytes). data = form fields, files = requests files dict."""
    logger.info("POST (multipart) %s", url)
    try:
        r = requests.post(url, data=data, files=files, timeout=timeout)
        resp = r.json() if r.content else {}
        if r.status_code >= 400:
            logger.warning(
                "POST (multipart) %s -> %s | error: %s",
                url,
                r.status_code,
                resp.get("error", {}).get("message", "") if isinstance(resp, dict) else "",
            )
            if isinstance(resp, dict) and "error" in resp:
                return _attach_why_failed(resp)
            return _attach_why_failed({"error": {"message": f"HTTP {r.status_code}", "code": r.status_code, "body": resp}})
        logger.info("POST (multipart) %s -> %s OK", url, r.status_code)
        return resp
    except requests.RequestException as e:
        logger.error("POST (multipart) %s -> RequestException: %s", url, e)
        return _attach_why_failed({"error": {"message": str(e), "type": "request_error"}})
    except ValueError:
        logger.error("POST (multipart) %s -> Invalid JSON response", url)
        return _attach_why_failed({"error": {"message": "Invalid JSON response from API", "type": "parse_error"}})


def _delete(url, params, timeout=DEFAULT_TIMEOUT):
    """DELETE request; returns parsed JSON."""
    logger.info("DELETE %s", url)
    try:
        r = requests.delete(url, params=params, timeout=timeout)
        if r.status_code in (200, 204) or (r.status_code >= 400 and r.content):
            try:
                data = r.json()
            except ValueError:
                data = {}
        else:
            data = r.json() if r.content else {}
        if r.status_code >= 400:
            logger.warning("DELETE %s -> %s | error: %s", url, r.status_code, data.get("error", {}).get("message", "") if isinstance(data, dict) else "")
            if isinstance(data, dict) and "error" in data:
                return _attach_why_failed(data)
            return _attach_why_failed({"error": {"message": f"HTTP {r.status_code}", "code": r.status_code, "body": data}})
        logger.info("DELETE %s -> %s OK", url, r.status_code)
        return data if data else {"success": True}
    except requests.RequestException as e:
        logger.error("DELETE %s -> RequestException: %s", url, e)
        return _attach_why_failed({"error": {"message": str(e), "type": "request_error"}})
    except ValueError:
        logger.error("DELETE %s -> Invalid JSON response", url)
        return _attach_why_failed({"error": {"message": "Invalid JSON response from API", "type": "parse_error"}})


def _post_json(url, json_body, params=None, timeout=DEFAULT_TIMEOUT):
    """POST with JSON body (e.g. Conversions API). params typically access_token."""
    logger.info("POST (JSON) %s", url)
    try:
        r = requests.post(url, json=json_body, params=params or {}, timeout=timeout)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            logger.warning("POST (JSON) %s -> %s | error: %s", url, r.status_code, data.get("error", {}).get("message", "") if isinstance(data, dict) else "")
            if isinstance(data, dict) and "error" in data:
                return data
            return {"error": {"message": f"HTTP {r.status_code}", "code": r.status_code, "body": data}}
        logger.info("POST (JSON) %s -> %s OK", url, r.status_code)
        return data
    except requests.RequestException as e:
        logger.error("POST (JSON) %s -> RequestException: %s", url, e)
        return {"error": {"message": str(e), "type": "request_error"}}
    except ValueError:
        logger.error("POST (JSON) %s -> Invalid JSON response", url)
        return {"error": {"message": "Invalid JSON response from API", "type": "parse_error"}}


def get_ad_accounts(token, limit=100):
    """List ad accounts for the authenticated user. Real API: GET /me/adaccounts. Requires ads_read."""
    url = f"{BASE_URL}/me/adaccounts"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "access_token": token,
        "fields": "id,name,account_id,account_status,currency,timezone_name",
        "limit": limit,
    }
    return _get(url, params)


def get_all_ad_accounts_from_facebook(token, limit=500):
    """
    Get all ad accounts from Facebook (live API only, no database).
    Aggregates: GET /me/adaccounts + for each business GET owned_ad_accounts and client_ad_accounts.
    Use this when you want the list to come from Facebook account only. Requires ads_read and optionally business_management.
    """
    all_accounts = []
    seen = set()
    limit = min(max(1, int(limit) if limit else 500), MAX_LIMIT)

    # 1) User's direct ad accounts
    r = get_ad_accounts(token, limit=limit)
    if "data" in r:
        for a in r["data"]:
            aid = a.get("id") or a.get("account_id")
            if aid and aid not in seen:
                seen.add(aid)
                all_accounts.append({**a, "source": "user_adaccounts"})
    if "error" in r and not all_accounts:
        return r

    # 2) Via businesses: owned + client
    biz = get_businesses(token, limit=limit)
    if "data" in biz:
        for b in biz["data"]:
            bid = b.get("id")
            if not bid:
                continue
            bname = b.get("name", "")
            for acc_list, src in [
                (get_business_owned_ad_accounts(bid, token, limit=limit), "business_owned"),
                (get_business_client_ad_accounts(bid, token, limit=limit), "business_client"),
            ]:
                if "data" not in acc_list:
                    continue
                for a in acc_list["data"]:
                    aid = a.get("id") or a.get("account_id")
                    if aid and aid not in seen:
                        seen.add(aid)
                        all_accounts.append({**a, "source": src, "business_name": bname})

    return {"source": "facebook", "total": len(all_accounts), "ad_accounts": all_accounts}


def get_businesses(token, limit=100):
    """List businesses the user has access to. Real API: GET /me/businesses. Requires business_management or ads_read."""
    url = f"{BASE_URL}/me/businesses"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": "id,name,created_time", "limit": limit}
    return _get(url, params)


def get_pages(token, limit=100):
    """List ALL Facebook Pages the user has access to across every Business
    Manager they belong to. Returns ``{"data": [...]}`` shaped like Meta's
    own response — callers (and the agent) see a single union list.

    Why this isn't just ``GET /me/accounts``:
        ``/me/accounts`` returns Pages the user owns directly (typically only
        the default Business Manager's pages). In multi-tenant setups where
        the same user has admin access to multiple Businesses — or where a
        Page is owned by a different Business that the user has been added
        to — those Pages are invisible to ``/me/accounts``. The agent then
        silently falls back to the wrong Page.

    Discovery order (universal across any Business Manager setup):
        1. ``GET /me/accounts`` — direct-owned Pages (fastest).
        2. ``GET /me/businesses`` — list every Business the user belongs to.
        3. For each Business, ``GET /<bid>/owned_pages`` and
           ``GET /<bid>/client_pages``. Owned = the Business owns the Page;
           Client = the Page is added to the Business but owned elsewhere.

    Pages are deduped by id; first occurrence wins (so direct-owned Pages
    keep their page-scoped access_token from /me/accounts).

    Real API: see https://developers.facebook.com/docs/graph-api/reference/business
    Requires scopes: ``pages_show_list``, ``business_management``.
    """
    fields = "id,name,access_token,category"
    cap = min(max(1, int(limit) if limit else 100), MAX_LIMIT)

    pages: list = []
    seen_ids: set = set()

    def _add(rows):
        if not isinstance(rows, list):
            return
        for r in rows:
            if not isinstance(r, dict):
                continue
            pid = str(r.get("id") or "").strip()
            if not pid or pid in seen_ids:
                continue
            seen_ids.add(pid)
            pages.append(r)
            # Side-effect: harvest any page-scoped access_token Meta gave
            # us so the retry path in get_page_access_token can use it
            # instead of making a second user-token call that may itself
            # fail with #200 on cross-business pages.
            page_tok = r.get("access_token")
            if isinstance(page_tok, str) and page_tok.strip():
                _page_cache_set(token, pid, page_tok.strip())
            if len(pages) >= cap:
                break

    # Step 1: direct-owned Pages (cheap, gives page access tokens).
    direct = _get(
        f"{BASE_URL}/me/accounts",
        {"access_token": token, "fields": fields, "limit": cap},
    )
    if isinstance(direct, dict) and not direct.get("error"):
        _add(direct.get("data") or [])

    if len(pages) >= cap:
        return {"data": pages[:cap]}

    # Step 2: Discover every Business the user belongs to. If this fails (no
    # business_management scope, or Meta is flaky), we fall back to whatever
    # /me/accounts gave us — never raise.
    businesses_resp = _get(
        f"{BASE_URL}/me/businesses",
        {"access_token": token, "fields": "id,name", "limit": MAX_LIMIT},
    )
    biz_rows = (businesses_resp.get("data") if isinstance(businesses_resp, dict) else []) or []

    # Step 3: For each business, ask Meta for both Pages the business owns
    # and Pages it has been granted client access to. Order: owned first
    # (the agent should prefer those when name-matching) then client.
    for b in biz_rows:
        if len(pages) >= cap:
            break
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or "").strip()
        if not bid:
            continue
        for endpoint in ("owned_pages", "client_pages"):
            if len(pages) >= cap:
                break
            res = _get(
                f"{BASE_URL}/{bid}/{endpoint}",
                {"access_token": token, "fields": fields, "limit": MAX_LIMIT},
            )
            if isinstance(res, dict) and not res.get("error"):
                _add(res.get("data") or [])

    return {"data": pages[:cap]}


def get_instagram_accounts(page_id, token):
    """Get Instagram business account(s) linked to a Facebook Page. Use the
    returned id as instagram_actor_id when creating creatives.
    GET /{page_id}?fields=instagram_business_account,instagram_accounts.
    Prefer instagram_business_account.id for ad creatives.

    Auto-retries with the page-scoped access token when Meta rejects the
    user-token call. Covers TWO error families (both bypassed by a
    page-scoped token from /me/accounts → access_token):

    - ``(#190)`` "This method must be called with a Page Access Token" —
      classical wrong-token-type rejection.
    - ``(#200)`` "Your access token does not have pages_read_engagement
      permissions or your page role is not permitted to create ads for
      the page" — observed in production (May 2026) for cross-business
      pages where the user-token's per-page grant differs but the
      page-scoped token returned by get_pages has the right grant.
      Confirmed via direct Meta API curl: same /{page_id} fields read
      that returns 200 with the user token returns the data successfully
      with the page-scoped token.

    Both fall through to the same fix: swap to the page-scoped token and
    retry once. Mirrors the retry pattern in
    ``get_page_backed_instagram_accounts``."""
    if not page_id:
        return {"error": {"message": "page_id is required"}}
    url = f"{BASE_URL}/{str(page_id).strip()}"
    params = {"access_token": token, "fields": "id,name,instagram_business_account{id,username},instagram_accounts{id,username}"}
    res = _get(url, params)
    if isinstance(res, dict):
        err = res.get("error") or {}
        if isinstance(err, dict) and err.get("code") in (190, 200):
            page_tok = get_page_access_token(page_id, token)
            if page_tok and page_tok != token:
                params["access_token"] = page_tok
                res = _get(url, params)
    return res


def get_page_access_token(page_id, user_token):
    """Resolve the Page-scoped access token for a page.

    Resolution order (each step short-circuits on success):
      1. ``_PAGE_TOKEN_CACHE`` (populated by ``get_pages``) — no Meta call.
      2. ``GET /{page_id}?fields=access_token`` with the user token —
         works for primary-business pages where the user-token's per-page
         grant is full.
      3. **Last-resort fallback**: call ``get_pages(user_token)`` which
         hits ``/me/accounts`` + every ``/{biz}/owned_pages`` and
         ``/{biz}/client_pages``, harvesting every accessible page-scoped
         access_token into ``_PAGE_TOKEN_CACHE``. Re-read the cache once
         done. This handles two production realities:
           - Cross-business pages where step 2 itself returns ``(#200)
             pages_read_engagement missing`` (Rekha Gupta Fans Club, May
             2026).
           - Multi-worker gunicorn deployments where ``facebook_get_pages``
             populated the cache in worker A but the current request
             landed on worker B with an empty cache.

    Returns the token string or None if none of the three paths yield one.
    """
    if not page_id:
        return None
    pid = str(page_id).strip()
    if not user_token:
        return None
    cached = _page_cache_get(user_token, pid)
    if cached:
        return cached
    url = f"{BASE_URL}/{pid}"
    params = {"access_token": user_token, "fields": "access_token"}
    res = _get(url, params)
    if isinstance(res, dict) and not res.get("error"):
        tok = res.get("access_token")
        if tok and isinstance(tok, str) and tok.strip():
            tok = tok.strip()
            # Backfill the cache (scoped to this user token) so subsequent
            # calls in this worker skip the network round-trip too.
            _page_cache_set(user_token, pid, tok)
            return tok
    # Step 3: cross-business / cross-worker fallback. Run get_pages so
    # this worker harvests page tokens via the same enumeration the
    # agent's facebook_get_pages call would do.
    try:
        get_pages(user_token, limit=MAX_LIMIT)
    except Exception:
        return None
    return _page_cache_get(user_token, pid)


def get_page_backed_instagram_accounts(page_id, token):
    """Get Page-backed Instagram account(s). Use the returned id as
    instagram_actor_id for ad creatives (often required for 'valid
    Instagram account id').
    GET /{page_id}/page_backed_instagram_accounts. Requires a Page
    Access Token — we transparently swap the user token for the
    page-scoped token when needed.

    Auto-retries on both ``(#190)`` ("Page Access Token required") and
    ``(#200)`` ("pages_read_engagement / page role not permitted") for
    the same reason documented on ``get_instagram_accounts``: the
    page-scoped token from get_pages succeeds in both cases (verified
    May 2026 against cross-business pages)."""
    if not page_id:
        return {"error": {"message": "page_id is required"}}
    # Try with the user token first; if Meta rejects with #190 or #200,
    # retry with the page-scoped token. Saves one GET when the user
    # token already has page scope.
    url = f"{BASE_URL}/{str(page_id).strip()}/page_backed_instagram_accounts"
    params = {"access_token": token, "fields": "id,username"}
    res = _get(url, params)
    if isinstance(res, dict):
        err = res.get("error") or {}
        if isinstance(err, dict) and err.get("code") in (190, 200):
            page_tok = get_page_access_token(page_id, token)
            if page_tok and page_tok != token:
                params["access_token"] = page_tok
                res = _get(url, params)
    return res


def get_saved_audiences(account_id, token, limit=100):
    """List saved audiences for an ad account. Real API: GET /act_{id}/saved_audiences. Apply via sync_adset_targeting_from_saved_audience (saved-audience ids are not valid in targeting.custom_audiences)."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/saved_audiences"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": "id,name", "limit": limit}
    return _get(url, params)


def get_custom_audiences(account_id, token, limit=100):
    """List custom audiences for an ad account. Real API: GET /act_{id}/customaudiences.

    Distinct from saved_audiences (which are reusable targeting specs):
    custom audiences are user-list audiences (uploaded emails/phones/IDs,
    website visitors, app activity, etc.) and are the IDs you put inside
    ``targeting.custom_audiences`` for an ad set."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/customaudiences"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "access_token": token,
        "fields": "id,name,subtype,description,approximate_count_lower_bound,approximate_count_upper_bound,delivery_status,operation_status,time_created,time_updated",
        "limit": limit,
    }
    return _get(url, params)


_CA_SCHEMA_ALIASES = {
    "EMAIL": "EMAIL_SHA256",
    "EMAIL_SHA256": "EMAIL_SHA256",
    "PHONE": "PHONE_SHA256",
    "PHONE_NUMBER": "PHONE_SHA256",
    "PHONE_SHA256": "PHONE_SHA256",
    "MOBILE": "PHONE_SHA256",
}


def _normalize_email_for_hash(s: str) -> str:
    return str(s or "").strip().lower()


def _normalize_phone_for_hash(s: str) -> str:
    """Strip everything but digits — Meta requires E.164-style digits-only.

    Callers should pass numbers with country code (e.g. +91...). We don't
    auto-prepend a country code; if the brief is missing one Meta will
    silently undercount the audience match rate and the user can re-upload."""
    return re.sub(r"\D", "", str(s or ""))


def _hash_users_for_custom_audience(values: List[str], schema: str) -> List[List[str]]:
    """SHA-256 hash a list of email/phone strings per Meta's requirement.

    Meta wants lowercase-trimmed for emails and digits-only for phones,
    then SHA-256 hex (lower). Returns the [[hash], [hash], ...] data
    payload shape that ``POST /{ca_id}/users`` expects."""
    import hashlib
    out: List[List[str]] = []
    for v in values or []:
        if v is None:
            continue
        if schema == "EMAIL_SHA256":
            norm = _normalize_email_for_hash(v)
        elif schema == "PHONE_SHA256":
            norm = _normalize_phone_for_hash(v)
        else:
            norm = str(v).strip()
        if not norm:
            continue
        out.append([hashlib.sha256(norm.encode("utf-8")).hexdigest()])
    return out


def create_custom_audience(account_id, token, name, schema=None, description=None,
                           users_email=None, users_phone=None, users_url=None, customer_file_source="USER_PROVIDED_ONLY"):
    """Create a custom audience and (optionally) seed it with hashed users in one call.

    Two-step Meta API:
      1. POST /act_{id}/customaudiences  → creates the empty audience
      2. POST /{audience_id}/users       → uploads SHA-256 hashed users

    schema: ``EMAIL`` / ``EMAIL_SHA256`` / ``PHONE`` / ``PHONE_SHA256``.
    users_email / users_phone: list of plain-text emails/phones (we hash).
    users_url: optional public URL to a UTF-8 ``.txt`` file with one
        email/phone per line (server fetches and hashes). Useful for the
        common ``.txt`` ingestion path that Ads Manager supports.

    Returns the created audience plus ``_users_added`` count when seeded.
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    if not name or not str(name).strip():
        return {"error": {"message": "name is required"}}
    # Resolve schema from input or from which users_* arg was provided.
    schema_norm = ""
    if schema is not None:
        schema_norm = _CA_SCHEMA_ALIASES.get(str(schema).strip().upper(), "")
    if not schema_norm and users_email:
        schema_norm = "EMAIL_SHA256"
    if not schema_norm and users_phone:
        schema_norm = "PHONE_SHA256"
    # Step 1: create the container.
    url = f"{BASE_URL}/{act}/customaudiences"
    data = {
        "access_token": token,
        "name": str(name).strip(),
        "subtype": "CUSTOM",
        "customer_file_source": str(customer_file_source or "USER_PROVIDED_ONLY").strip(),
    }
    if description:
        data["description"] = str(description).strip()
    create_resp = _post(url, data)
    if not isinstance(create_resp, dict) or "error" in create_resp:
        return create_resp
    ca_id = str(create_resp.get("id") or "").strip()
    if not ca_id:
        return create_resp
    # Step 2: optional user upload. Resolve raw values from any of the three input shapes.
    raw_values: List[str] = []
    if users_email is not None:
        if isinstance(users_email, str):
            try:
                users_email = json.loads(users_email)
            except (TypeError, ValueError):
                users_email = [s.strip() for s in users_email.splitlines() if s.strip()]
        if isinstance(users_email, list):
            raw_values.extend(str(v) for v in users_email)
            if not schema_norm:
                schema_norm = "EMAIL_SHA256"
    if users_phone is not None:
        if isinstance(users_phone, str):
            try:
                users_phone = json.loads(users_phone)
            except (TypeError, ValueError):
                users_phone = [s.strip() for s in users_phone.splitlines() if s.strip()]
        if isinstance(users_phone, list):
            raw_values.extend(str(v) for v in users_phone)
            if not schema_norm:
                schema_norm = "PHONE_SHA256"
    if users_url and isinstance(users_url, str) and users_url.startswith(("http://", "https://")):
        try:
            from app.tenant_http import fetch_public_bytes
            _body, _ct, _ = fetch_public_bytes(
                users_url, max_bytes=20 * 1024 * 1024, timeout=20,
                content_types=("text/", "application/json", "application/csv", "application/octet-stream"),
            )
            text = _body.decode("utf-8", errors="replace") or ""
        except Exception as exc:
            create_resp["_users_url_fetch_error"] = f"Could not fetch users_url: {exc}"
            return create_resp
        for line in text.splitlines():
            line = line.strip()
            if line:
                raw_values.append(line)
        if not schema_norm:
            # Heuristic — line containing '@' implies email; else phone.
            schema_norm = "EMAIL_SHA256" if any("@" in v for v in raw_values) else "PHONE_SHA256"
    if raw_values and schema_norm:
        hashed = _hash_users_for_custom_audience(raw_values, schema_norm)
        if hashed:
            users_url_endpoint = f"{BASE_URL}/{ca_id}/users"
            payload = {
                "schema": [schema_norm],
                "data": hashed,
            }
            users_data = {"access_token": token, "payload": json.dumps(payload)}
            users_resp = _post(users_url_endpoint, users_data)
            create_resp["_users_added"] = len(hashed)
            create_resp["_users_response"] = users_resp
    elif raw_values and not schema_norm:
        create_resp["_users_skipped"] = (
            "users provided but no schema could be resolved — pass schema=EMAIL or PHONE."
        )
    return create_resp


def create_lookalike_audience(account_id, token, name, origin_audience_id, country, ratio=0.01, description=None):
    """Create a lookalike audience based on an existing custom audience.

    POST /act_{id}/customaudiences with subtype=LOOKALIKE.

    Required:
      - origin_audience_id: ID of the source custom audience (the "seed").
        Get one via facebook_get_custom_audiences.
      - country: ISO 3166-1 alpha-2 country code where the lookalike will be
        modeled (e.g. "IN", "US"). Meta builds the lookalike from users in
        that country who resemble your seed.
      - ratio: lookalike size as a fraction of the country's population.
        0.01 = top 1% (most similar; ~smallest, highest quality). Meta accepts
        0.01, 0.02, ... up to 0.10. Default 0.01.

    Returns the created audience id.
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    if not name or not str(name).strip():
        return {"error": {"message": "name is required"}}
    if not origin_audience_id or not str(origin_audience_id).strip():
        return {"error": {"message": "origin_audience_id is required (the seed custom audience)"}}
    if not country or len(str(country).strip()) != 2:
        return {"error": {"message": "country must be a 2-letter ISO code (e.g. 'IN', 'US')"}}
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        r = 0.01
    if r < 0.01 or r > 0.20:
        r = max(0.01, min(0.20, r))
    url = f"{BASE_URL}/{act}/customaudiences"
    lookalike_spec = {
        "type": "similarity" if r <= 0.10 else "reach",
        "country": str(country).strip().upper(),
        "ratio": round(r, 2),
        "origin": [{"id": str(origin_audience_id).strip(), "type": "custom_audience"}],
    }
    data = {
        "access_token": token,
        "name": str(name).strip(),
        "subtype": "LOOKALIKE",
        "lookalike_spec": json.dumps(lookalike_spec),
    }
    if description:
        data["description"] = str(description).strip()
    return _post(url, data)


def get_political_disclaimers(page_id, token, limit=50):
    """List political ad disclaimers ('paid for by' bylines) registered for a Page.

    Pages running ads in the ISSUES_ELECTIONS_POLITICS special category MUST
    attach a registered disclaimer to each ad. Disclaimers cannot be created
    via the public Marketing API — registration happens in the Page's
    Trust+Transparency section in Ads Manager. This tool surfaces the existing
    disclaimer ids so an LLM can pass the right id into the ad payload.

    Endpoint: GET /{page_id}/disclaimers (also exposed as
    `/political_ad_disclaimers` on some accounts; we try the canonical first).
    Auto-retries with the page-scoped access token on Meta error #190.
    """
    if not page_id:
        return {"error": {"message": "page_id is required"}}
    url = f"{BASE_URL}/{str(page_id).strip()}/disclaimers"
    limit = min(max(1, int(limit) if limit else 50), MAX_LIMIT)
    params = {
        "access_token": token,
        "fields": "id,byline,paid_for_by,status,created_time",
        "limit": limit,
    }
    res = _get(url, params)
    if isinstance(res, dict):
        err = res.get("error") or {}
        if isinstance(err, dict) and err.get("code") == 190:
            page_tok = get_page_access_token(page_id, token)
            if page_tok and page_tok != token:
                params["access_token"] = page_tok
                res = _get(url, params)
    return res


def get_lead_forms(page_id, token, limit=100):
    """List lead (Instant) forms on a Page. Real API: GET /{page_id}/leadgen_forms.

    Use the returned form id as ``lead_gen_form_id`` inside the creative
    ``call_to_action.value`` when building lead-ad creatives. Useful for
    avoiding duplicate-name collisions when re-running a brief that says
    "use existing form: <name>" — list first, then bind by id.

    Auto-retries with the page-scoped access token on Meta error #190
    ("This method must be called with a Page Access Token")."""
    if not page_id:
        return {"error": {"message": "page_id is required"}}
    url = f"{BASE_URL}/{str(page_id).strip()}/leadgen_forms"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "access_token": token,
        "fields": "id,name,status,locale,questions,privacy_policy_url,created_time",
        "limit": limit,
    }
    res = _get(url, params)
    if isinstance(res, dict):
        err = res.get("error") or {}
        if isinstance(err, dict) and err.get("code") == 190:
            page_tok = get_page_access_token(page_id, token)
            if page_tok and page_tok != token:
                params["access_token"] = page_tok
                res = _get(url, params)
    return res


_PAGE_POSTS_HARD_CAP = 10000


def get_page_posts(page_id, token, fields=None, caption_query=None):
    """List ALL posts published by a Page. Real API: GET /{page_id}/posts. Use id as object_story_id (format page_id_post_id) for existing-post creatives.

    Always walks every paging.next until exhausted (hard-capped at
    _PAGE_POSTS_HARD_CAP posts as a runaway safety). Per-request page size
    is fixed at MAX_LIMIT to minimize round-trips.

    Meta's "new Pages experience" requires a Page Access Token for this
    endpoint (error code 190, subcode 2069032 when called with a user token).
    Try the supplied token first; on 190, transparently swap for the
    page-scoped token via ``get_page_access_token`` and retry. Same pattern
    used by get_instagram_accounts, get_page_backed_instagram_accounts,
    get_lead_forms.

    When ``caption_query`` is provided, returns only posts whose ``message``
    contains the query (case-insensitive substring). The response keeps
    Meta's native shape but the returned ``data`` is filtered, and a
    ``search`` summary is included.
    """
    if not page_id:
        return {"error": {"message": "page_id is required"}}
    url = f"{BASE_URL}/{str(page_id).strip()}/posts"
    if fields is None:
        # ``message`` is the caption a brief means by "first N words of the
        # caption"; ``is_eligible_for_promotion`` lets the agent see boostability
        # in the SAME call it reads ids from, instead of guessing either.
        fields = "id,message,created_time,permalink_url,is_eligible_for_promotion"
    # Meta /page/posts caps per-request size at 100 (error #100 otherwise).
    # We still fetch ALL posts via paging.next — this only changes how many
    # round-trips it takes, not the total returned to the caller.
    params = {"access_token": token, "fields": fields, "limit": 100}
    # Large Pages answer code 1 "Please reduce the amount of data" at 100
    # (live: BOL7 Page, 2026-09-08 — the tool was unusable for caption
    # search there). _get_shrinking steps the page size down on that error.
    res = _get_shrinking(url, params)
    if isinstance(res, dict):
        err = res.get("error") or {}
        if isinstance(err, dict) and err.get("code") == 190:
            page_tok = get_page_access_token(page_id, token)
            if page_tok and page_tok != token:
                params["access_token"] = page_tok
                res = _get_shrinking(url, params)

    if not isinstance(res, dict) or res.get("error"):
        return res

    query_low = str(caption_query).strip().lower() if caption_query else ""
    all_posts: list = []
    matches: list = []
    posts_scanned = 0
    pages_scanned = 0
    current = res
    last_paging = current.get("paging") or {}

    while True:
        pages_scanned += 1
        data = current.get("data") or []
        posts_scanned += len(data)
        for p in data:
            if not isinstance(p, dict):
                continue
            all_posts.append(p)
            if query_low:
                msg = (p.get("message") or "").lower()
                if query_low in msg:
                    matches.append(p)
        if posts_scanned >= _PAGE_POSTS_HARD_CAP:
            break
        paging_obj = current.get("paging") or {}
        next_url = paging_obj.get("next")
        next_res = None
        if next_url:
            # Meta's paging.next is a fully-formed URL with the access_token
            # already embedded. Hit it directly via _get (no extra params).
            next_res = _get(next_url, None)
            if _is_reduce_data_error(next_res):
                # paging.next carries the original limit; rebuild the request
                # from the cursor with a shrinking page size instead.
                after_cursor = ((paging_obj.get("cursors") or {}).get("after")
                                if isinstance(paging_obj.get("cursors"), dict) else None)
                if after_cursor:
                    cursor_params = dict(params)
                    cursor_params["after"] = after_cursor
                    cursor_params["limit"] = max(len(data), 1)
                    next_res = _get_shrinking(url, cursor_params)
        else:
            # Fallback: some endpoints return paging.cursors.after without
            # a paging.next URL. Build the next-page request manually.
            after_cursor = ((paging_obj.get("cursors") or {}).get("after")
                            if isinstance(paging_obj.get("cursors"), dict) else None)
            if not after_cursor:
                break
            cursor_params = dict(params)
            cursor_params["after"] = after_cursor
            next_res = _get(url, cursor_params)
        if not isinstance(next_res, dict) or next_res.get("error"):
            break
        if not (next_res.get("data") or []):
            last_paging = next_res.get("paging") or last_paging
            break
        current = next_res
        last_paging = current.get("paging") or last_paging

    if query_low:
        return {
            "data": matches,
            "paging": last_paging,
            "search": {
                "query": str(caption_query).strip(),
                "pages_scanned": pages_scanned,
                "posts_scanned": posts_scanned,
                "matched": bool(matches),
            },
        }
    return {"data": all_posts, "paging": last_paging}


_LEADFORM_QUESTION_TYPE_ALIASES = {
    # Meta accepts PHONE / WHATSAPP_NUMBER, NOT PHONE_NUMBER. Auto-rewrite the
    # historical/intuitive aliases so a brief that says "phone number" works.
    "PHONE_NUMBER": "PHONE",
    "PHONE_NO":     "PHONE",
    "MOBILE":       "PHONE",
    "MOBILE_NUMBER": "PHONE",
    "WHATSAPP":     "WHATSAPP_NUMBER",
    "WHATSAPP_NO":  "WHATSAPP_NUMBER",
}


def _normalize_leadform_questions(q_in: Any) -> Tuple[Any, List[str]]:
    """Rewrite a list of lead-form question dicts into the exact shape Meta accepts.

    Returns (questions, fixes). Fixes is a list of human-readable lines describing
    every transformation, so callers can surface them in auto_fixes.

    Transforms applied:
      - "type" aliases (PHONE_NUMBER -> PHONE, WHATSAPP -> WHATSAPP_NUMBER, etc.)
      - For CUSTOM questions, "options" entries that are plain strings are
        rewritten to {"key": "<lowercased-snake>", "value": "<original>"} objects.
      - For CUSTOM questions, ensure "key" is present (lowercased label) since
        Meta uses it as the field id in the lead payload.
    """
    fixes: List[str] = []
    if not isinstance(q_in, list):
        return q_in, fixes
    out = []
    for q in q_in:
        if not isinstance(q, dict):
            out.append(q)
            continue
        new_q = dict(q)
        t_raw = str(new_q.get("type") or "").strip().upper()
        if t_raw in _LEADFORM_QUESTION_TYPE_ALIASES:
            corrected = _LEADFORM_QUESTION_TYPE_ALIASES[t_raw]
            new_q["type"] = corrected
            fixes.append(f"Lead form: rewrote question type {t_raw!r} -> {corrected!r} (Meta requires this).")
        elif t_raw:
            new_q["type"] = t_raw
        if str(new_q.get("type", "")).upper() == "CUSTOM":
            if not str(new_q.get("key") or "").strip():
                label = str(new_q.get("label") or new_q.get("question_text") or "").strip()
                if label:
                    auto_key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "custom_q"
                    new_q["key"] = auto_key
                    fixes.append(f"Lead form: auto-generated key {auto_key!r} for custom question {label!r}.")
            opts = new_q.get("options")
            if isinstance(opts, list) and opts:
                rewrote_any = False
                new_opts = []
                for opt in opts:
                    if isinstance(opt, str):
                        v = opt
                        k = re.sub(r"[^a-z0-9]+", "_", v.lower()).strip("_") or v.lower()
                        new_opts.append({"key": k, "value": v})
                        rewrote_any = True
                    elif isinstance(opt, dict):
                        # Already-structured option, ensure both key and value are present.
                        v = opt.get("value") or opt.get("label") or opt.get("text")
                        k = opt.get("key") or (re.sub(r"[^a-z0-9]+", "_", str(v).lower()).strip("_") if v else None)
                        if v is not None and k:
                            new_opts.append({"key": str(k), "value": str(v)})
                        else:
                            new_opts.append(opt)
                    else:
                        new_opts.append(opt)
                if rewrote_any:
                    fixes.append(
                        f"Lead form: rewrote {len(new_opts)} string option(s) for custom question "
                        f"{(new_q.get('label') or new_q.get('key') or 'custom')!r} into "
                        "{key,value} object form (Meta rejects plain strings)."
                    )
                new_q["options"] = new_opts
        out.append(new_q)
    return out, fixes


def create_lead_form(page_id, token, name, questions=None, privacy_policy_url=None,
                     context_card_title=None, context_card_body=None,
                     thank_you_title=None, thank_you_body=None, follow_up_url=None):
    """Create a Facebook Lead Form (Instant Form) on a Page. POST /{page_id}/leadgen_forms.
    questions: list of dicts. Standard types: FULL_NAME, EMAIL, PHONE, WHATSAPP_NUMBER,
    COMPANY_NAME, JOB_TITLE, CITY, STATE, ZIP, COUNTRY, DOB, GENDER, plus CUSTOM (with
    label + options). PHONE_NUMBER / WHATSAPP / MOBILE aliases are auto-rewritten."""
    if not page_id:
        return {"error": {"message": "page_id is required"}}
    if not name:
        return {"error": {"message": "name is required"}}
    url = f"{BASE_URL}/{str(page_id).strip()}/leadgen_forms"
    default_questions = [{"type": "FULL_NAME"}, {"type": "PHONE"}, {"type": "EMAIL"}]
    q = questions if questions else default_questions
    if isinstance(q, str):
        try:
            q = json.loads(q)
        except (TypeError, ValueError):
            q = default_questions
    q, _q_fixes = _normalize_leadform_questions(q)
    data = {
        "access_token": token,
        "name": name,
        "questions": json.dumps(q),
    }
    if privacy_policy_url:
        data["privacy_policy"] = json.dumps({"url": str(privacy_policy_url).strip(), "link_text": "Privacy Policy"})
    thank_you = {}
    if thank_you_title:
        thank_you["title"] = thank_you_title
    if thank_you_body:
        thank_you["body"] = thank_you_body
    if follow_up_url:
        thank_you["button_type"] = "VIEW_WEBSITE"
        thank_you["button_text"] = "Visit Website"
        thank_you["website_url"] = follow_up_url
    if thank_you:
        data["thank_you_page"] = json.dumps(thank_you)
    if context_card_title or context_card_body:
        ctx = {"style": "PARAGRAPH_STYLE"}
        if context_card_title:
            ctx["title"] = context_card_title
        if context_card_body:
            ctx["content"] = [context_card_body]
        data["context_card"] = json.dumps(ctx)
    resp = _post(url, data)
    # Surface the question-normalization fixes on the response so the agent /
    # production_flow can echo them in auto_fixes. Never put them in the form
    # body — Meta rejects unknown fields.
    if isinstance(resp, dict) and _q_fixes:
        resp.setdefault("_auto_fixes", []).extend(_q_fixes)
    return resp


def get_reach_estimate(account_id, token, targeting_spec, optimization_goal=None):
    """Get audience size / reach estimate for a targeting spec. GET /act_{id}/reachestimate."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/reachestimate"
    params = {"access_token": token}
    if isinstance(targeting_spec, str):
        params["targeting_spec"] = targeting_spec
    else:
        params["targeting_spec"] = json.dumps(targeting_spec)
    if optimization_goal:
        params["optimization_goal"] = optimization_goal
    return _get(url, params)


def get_pixels(account_id, token, limit=100):
    """List pixels for an ad account. Real API: GET /act_{id}/adspixels. Requires ads_read.

    ``last_fired_time`` is requested so callers (notably the auto-resolve
    phase in production_flow) can pick the most-recently-fired pixel when the
    brief omits ``tracking.pixel_id``.
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/adspixels"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "access_token": token,
        "fields": "id,name,last_fired_time,creativity_tool,code",
        "limit": limit,
    }
    return _get(url, params)


def _apply_breakdowns(params: dict, breakdowns) -> None:
    """Meta insights ``breakdowns`` pass-through (platform split etc.).

    Accepts a comma-separated string or a list — e.g. ``publisher_platform``
    (facebook vs instagram vs audience_network), ``platform_position``,
    ``device_platform``, ``age,gender``, ``country``. Free-form: Meta
    validates the values, so new breakdowns need no code change here.
    """
    if not breakdowns:
        return
    if isinstance(breakdowns, (list, tuple)):
        breakdowns = ",".join(str(b).strip() for b in breakdowns if str(b).strip())
    b = str(breakdowns).strip()
    if b:
        params["breakdowns"] = b


def get_insights(account_id, token, date_preset="last_30d", fields=None, limit=100, time_start=None, time_end=None, breakdowns=None):
    """Get insights (performance metrics) for an ad account. Real API: GET /act_{id}/insights.
    Use date_preset (last_7d, last_30d, this_month, last_90d) OR time_start+time_end (YYYY-MM-DD) for custom range up to 3 years.
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/insights"
    if fields is None:
        fields = "campaign_name,impressions,clicks,spend,cpc,cpm,ctr"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "fields": fields,
        "limit": limit,
        "access_token": token,
    }
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset
    _apply_breakdowns(params, breakdowns)
    return _get(url, params)


def get_campaigns(account_id, token, fields=None, limit=100, filtering=None):
    """List campaigns for an ad account. Real API: GET /act_{id}/campaigns."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/campaigns"
    if fields is None:
        fields = "id,name,status,objective,daily_budget,lifetime_budget,created_time,start_time,stop_time"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "fields": fields,
        "limit": limit,
        "access_token": token,
    }
    if filtering:
        params["filtering"] = filtering if isinstance(filtering, str) else str(filtering)
    return _get(url, params)


def create_campaign(account_id, token, name, objective, status="PAUSED", special_ad_categories=None,
                    special_ad_category_country=None,
                    is_adset_budget_sharing_enabled=False, daily_budget=None, lifetime_budget=None,
                    bid_strategy=None, strict=False):
    """Create a campaign. POST /act_{id}/campaigns. Set daily_budget/lifetime_budget at campaign level for CBO (Advantage Campaign Budget).
    special_ad_category_country: ISO country codes e.g. [\"IN\"] required with ISSUES_ELECTIONS_OR_POLITICS in many regions.
    Smart CBO: if is_adset_budget_sharing_enabled=True fails, auto-retries two-step (create with false + budget, then update bid_strategy).
    If strict=True, no CBO two-step retry and no post-create bid_strategy update."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    if not name or not objective:
        return {"error": {"message": "name and objective are required"}}
    url = f"{BASE_URL}/{act}/campaigns"

    def _build_data(cbo_flag):
        d = {
            "access_token": token,
            "name": name,
            "objective": objective,
            "status": status or "PAUSED",
            "is_adset_budget_sharing_enabled": "true" if cbo_flag else "false",
        }
        # NOTE: do NOT set ``smart_promotion_type`` here. Meta v22 only accepts
        # ``GUIDED_CREATION`` or ``SMART_APP_PROMOTION`` for that field — both
        # are UI-flow markers, not ODAX hints. Setting any other value (incl.
        # the campaign objective) returns error #100 and blocks every
        # OUTCOME_* campaign create. Conversion Location is derived from
        # ad set ``destination_type`` automatically; if the Ads Manager panel
        # still renders empty for a fresh campaign, that's cosmetic and does
        # not affect delivery.
        if daily_budget is not None:
            d["daily_budget"] = str(int(daily_budget))
        if lifetime_budget is not None:
            d["lifetime_budget"] = str(int(lifetime_budget))
        # Set bid_strategy at create time. Setting it post-hoc via update_campaign
        # often fails after Meta has assigned a CBO default that requires
        # bid_amount, leaving the adset to inherit a strategy it can't satisfy.
        if bid_strategy and str(bid_strategy).strip():
            d["bid_strategy"] = str(bid_strategy).strip()
        elif (daily_budget is not None or lifetime_budget is not None) and not strict:
            # CBO campaign with no explicit strategy: pin Meta's documented
            # default (highest volume). Live-observed 2026-08-14: LIFETIME-budget
            # CBO campaigns left without bid_strategy get a Meta-assigned default
            # that demands bid_amount, so every child ad set fails with subcode
            # 1815857 ("Bid amount required") — daily-budget CBO does not.
            d["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
        if isinstance(special_ad_categories, list):
            d["special_ad_categories"] = json.dumps(special_ad_categories)
        elif special_ad_categories in (True, False) or special_ad_categories is None:
            d["special_ad_categories"] = "[]"
        else:
            d["special_ad_categories"] = json.dumps(special_ad_categories) if special_ad_categories else "[]"
        if special_ad_category_country is not None:
            sac = special_ad_category_country
            if isinstance(sac, str) and sac.strip():
                try:
                    sac = json.loads(sac)
                except (TypeError, ValueError):
                    sac = [c.strip() for c in sac.split(",") if c.strip()]
            if isinstance(sac, list) and sac:
                d["special_ad_category_country"] = json.dumps([str(c).strip().upper() for c in sac if str(c).strip()])
        return d

    resp = _post_with_rate_limit_retry(url, _build_data(is_adset_budget_sharing_enabled))

    # Smart CBO fallback: if CBO=true failed and we have budget, retry with false + update bid_strategy
    if (not strict
            and isinstance(resp, dict) and "error" in resp
            and is_adset_budget_sharing_enabled
            and (daily_budget is not None or lifetime_budget is not None)):
        logger.info("CBO create failed, retrying two-step: create with false + update bid_strategy")
        resp2 = _post_with_rate_limit_retry(url, _build_data(False))
        if isinstance(resp2, dict) and "error" not in resp2:
            campaign_id = str(resp2.get("id") or "").strip()
            auto_fixes = ["CBO two-step: created campaign with is_adset_budget_sharing_enabled=false + budget"]
            if campaign_id:
                bs = str(bid_strategy).strip() if bid_strategy else "LOWEST_COST_WITHOUT_CAP"
                u = update_campaign(campaign_id, token, bid_strategy=bs)
                if isinstance(u, dict) and "error" not in u:
                    auto_fixes.append(f"Set bid_strategy={bs} via update_campaign")
                else:
                    auto_fixes.append(f"bid_strategy update failed: {u.get('error', {}).get('message', '') if isinstance(u, dict) else u}")
            resp2["_auto_fixes"] = auto_fixes
            return resp2
        return resp

    # If bid_strategy provided and campaign created OK without it already set
    # (legacy path — most calls now set it inline in _build_data), set it via
    # update. We only enter this path when the create succeeded but didn't
    # carry bid_strategy (e.g. older callers).
    if (not strict
            and isinstance(resp, dict) and "error" not in resp and bid_strategy
            and "bid_strategy" not in (_build_data(is_adset_budget_sharing_enabled))):
        campaign_id = str(resp.get("id") or "").strip()
        if campaign_id:
            import time as _t
            last_err: Any = None
            u: Any = None
            for _attempt in range(4):
                _t.sleep(0.4 * (2 ** _attempt))
                u = update_campaign(campaign_id, token, bid_strategy=str(bid_strategy).strip())
                if isinstance(u, dict) and "error" not in u:
                    break
                err_obj = u.get("error", {}) if isinstance(u, dict) else {}
                err_msg = err_obj.get("message") if isinstance(err_obj, dict) else str(u)
                last_err = err_msg
                # Only retry the eventual-consistency failure mode. Other errors
                # (auth, invalid param) are deterministic — bail.
                if not (err_msg and ("not a valid" in str(err_msg).lower()
                                     or "does not exist" in str(err_msg).lower()
                                     or err_obj.get("error_subcode") == 1815148)):
                    break
            if isinstance(u, dict) and "error" not in u:
                resp["_auto_fixes"] = [f"Set bid_strategy={bid_strategy} via update_campaign"]
            else:
                resp["_auto_fixes"] = [
                    f"Campaign created but bid_strategy update failed after retries: {last_err}"
                ]

    return resp


def update_campaign(
    campaign_id,
    token,
    name=None,
    status=None,
    daily_budget=None,
    lifetime_budget=None,
    bid_strategy=None,
    objective=None,
    skip_objective_when_adsets_exist=True,
):
    """Update a campaign. POST /{campaign_id}. Supports name, status, daily_budget (CBO), lifetime_budget, bid_strategy, objective.

    Meta blocks changing ``objective`` once the campaign has at least one ad set (error_subcode 1885073).
    When ``skip_objective_when_adsets_exist`` is True (default), a requested objective change is omitted
    if the campaign already has ad sets and the objective differs from the current one; other fields are
    still sent. Response may include ``_objective_change_skipped`` and ``_fix_objective_instructions``.
    Set ``skip_objective_when_adsets_exist`` to False to attempt the objective update anyway (Meta may error).
    To use a different objective in production, create a **new** campaign (e.g. document sync) — duplicate names are allowed."""
    if not campaign_id or not campaign_id.strip():
        return {"error": {"message": "campaign_id is required"}}
    cid = campaign_id.strip()
    url = f"{BASE_URL}/{cid}"
    meta_notes = {}

    requested_objective = None
    if objective is not None and str(objective).strip():
        requested_objective = str(objective).strip()

    if requested_objective and skip_objective_when_adsets_exist:
        adsets_probe = _get(
            f"{BASE_URL}/{cid}/adsets",
            {"access_token": token, "fields": "id", "limit": 1},
        )
        if isinstance(adsets_probe, dict) and adsets_probe.get("error"):
            return adsets_probe
        has_adsets = bool((adsets_probe or {}).get("data"))
        if has_adsets:
            cur = get_marketing_object(cid, token, "objective")
            if isinstance(cur, dict) and cur.get("error"):
                return cur
            current_obj = str((cur or {}).get("objective") or "").strip()
            if requested_objective.upper() != current_obj.upper():
                meta_notes["_objective_change_skipped"] = True
                meta_notes["_objective_change_reason"] = (
                    "Meta does not allow changing campaign objective after at least one ad set exists "
                    "(Marketing API error_subcode 1885073). Objective was omitted from this request."
                )
                meta_notes["_fix_objective_instructions"] = (
                    "Create a new campaign with the desired objective using your document "
                    "(facebook_sync_document_campaign) or facebook_create_campaign. "
                    "Same campaign name is allowed on Meta; pause or delete the old campaign if needed."
                )
                requested_objective = None

    data = {"access_token": token}
    if name is not None:
        data["name"] = name
    if status is not None:
        data["status"] = status
    if daily_budget is not None:
        data["daily_budget"] = str(int(daily_budget))
    if lifetime_budget is not None:
        data["lifetime_budget"] = str(int(lifetime_budget))
    if bid_strategy is not None and str(bid_strategy).strip():
        data["bid_strategy"] = str(bid_strategy).strip()
    if requested_objective:
        data["objective"] = requested_objective

    if len(data) == 1:
        if meta_notes.get("_objective_change_skipped"):
            return {
                "success": False,
                "error": {
                    "message": (
                        "Objective change was skipped because this campaign has ad sets "
                        "(Meta does not allow objective changes after ad sets exist). "
                        "No other fields were provided to update. "
                        "To update name, status, daily_budget, lifetime_budget, or bid_strategy, "
                        "call this tool again with those fields (without objective). "
                        "To use a different objective, create a new campaign."
                    ),
                    "type": "McpObjectiveOnlyUpdateBlocked",
                    "code": 400,
                },
                "_other_fields_still_updatable": True,
                "_updatable_fields": ["name", "status", "daily_budget", "lifetime_budget", "bid_strategy"],
                **meta_notes,
            }
        return {"error": {"message": "Provide at least one field to update"}}

    resp = _post(url, data)
    if meta_notes and isinstance(resp, dict) and "error" not in resp:
        resp = {
            **resp,
            **meta_notes,
            "_note": (
                "Other fields were updated successfully. Objective change was skipped "
                "because ad sets exist. To change objective, create a new campaign."
            ),
        }
    elif meta_notes and isinstance(resp, dict):
        err = resp.get("error") if isinstance(resp.get("error"), dict) else {}
        sub = err.get("error_subcode")
        if sub == 1885073 or (isinstance(err.get("message"), str) and "objective" in err["message"].lower() and "advert set" in err["message"].lower()):
            resp = {**resp, **meta_notes, "_meta_original_error": err}
    return resp


def pause_campaign(campaign_id, token):
    """Set campaign status to PAUSED. Real API: POST /{campaign_id} with status=PAUSED."""
    return update_campaign(campaign_id, token, status="PAUSED")


def resume_campaign(campaign_id, token):
    """Set campaign status to ACTIVE. Real API: POST /{campaign_id} with status=ACTIVE."""
    return update_campaign(campaign_id, token, status="ACTIVE")


def delete_campaign(campaign_id, token):
    """Delete a campaign. Real API: DELETE /{campaign_id}. Requires ads_management."""
    if not campaign_id or not str(campaign_id).strip():
        return {"error": {"message": "campaign_id is required"}}
    url = f"{BASE_URL}/{str(campaign_id).strip()}"
    return _delete(url, {"access_token": token})


def get_campaign_insights(campaign_id, token, date_preset="lifetime", fields=None, time_start=None, time_end=None, breakdowns=None):
    """Get insights for a single campaign. Real API: GET /{campaign_id}/insights. Only campaign_id required; date optional (default lifetime)."""
    if not campaign_id or not str(campaign_id).strip():
        return {"error": {"message": "campaign_id is required"}}
    url = f"{BASE_URL}/{str(campaign_id).strip()}/insights"
    if fields is None:
        fields = "campaign_name,impressions,clicks,spend,cpc,cpm,ctr"
    params = {"access_token": token, "fields": fields}
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset
    _apply_breakdowns(params, breakdowns)
    return _get(url, params)


def get_ad_sets(account_id, token, fields=None, limit=100, filtering=None):
    """List ad sets for an ad account. Real API: GET /act_{id}/adsets."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/adsets"
    if fields is None:
        fields = "id,name,status,campaign_id,daily_budget,lifetime_budget,optimization_goal,billing_event,created_time"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "fields": fields,
        "limit": limit,
        "access_token": token,
    }
    if filtering:
        params["filtering"] = filtering if isinstance(filtering, str) else str(filtering)
    return _get(url, params)


def targeting_search(token, search_type, q=None, location_types=None, country_code=None, region_id=None, limit=50):
    """Search targeting options. GET /search. search_type: adgeolocation (locations), adinterest (interests), adTargetingCategory (demographics). For adgeolocation use location_types: country, region, city, zip, country_group, geo_market, electoral_district."""
    url = f"{BASE_URL}/search"
    params = {"access_token": token, "type": search_type, "limit": min(max(1, int(limit) if limit else 50), 200)}
    if q is not None and str(q).strip():
        params["q"] = str(q).strip()
    if location_types is not None:
        if isinstance(location_types, str):
            params["location_types"] = location_types.strip()
        else:
            params["location_types"] = json.dumps(location_types)
    if country_code is not None and str(country_code).strip():
        params["country_code"] = str(country_code).strip()
    if region_id is not None:
        params["region_id"] = str(region_id).strip()
    return _get_with_rate_limit_retry(url, params)


def targeting_search_ad_account(account_id, token, search_type, q=None, location_types=None, country_code=None, region_id=None, limit=50):
    """Search targeting scoped to ad account. GET /act_{id}/targetingsearch (preferred for production). Same params as /search."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/targetingsearch"
    params = {"access_token": token, "type": search_type, "limit": min(max(1, int(limit) if limit else 50), 200)}
    if q is not None and str(q).strip():
        params["q"] = str(q).strip()
    if location_types is not None:
        if isinstance(location_types, str):
            params["location_types"] = location_types.strip()
        else:
            params["location_types"] = json.dumps(location_types)
    if country_code is not None and str(country_code).strip():
        params["country_code"] = str(country_code).strip()
    if region_id is not None:
        params["region_id"] = str(region_id).strip()
    return _get_with_rate_limit_retry(url, params)


def targeting_search_with_fallback(account_id, token, search_type, q=None, location_types=None, country_code=None, region_id=None, limit=50):
    """Try act-scoped targetingsearch first; on error or empty/invalid data fall back to global /search.
    For adgeolocation: validates that results contain geo fields (key + type); if not, falls back to global."""
    primary = targeting_search_ad_account(account_id, token, search_type, q=q, location_types=location_types, country_code=country_code, region_id=region_id, limit=limit)
    if "error" not in primary:
        data = primary.get("data")
        if data is not None and len(data) > 0:
            # Validate geo results — account-scoped search sometimes returns non-geo data
            if search_type == "adgeolocation":
                has_valid_geo = any(
                    row.get("key") and str(row.get("type", "")).lower() in _GEO_LOCATION_TYPES
                    for row in data if isinstance(row, dict)
                )
                if not has_valid_geo:
                    logger.warning("adgeolocation search returned non-geo data from account scope, falling back to global")
                    fallback = targeting_search(token, search_type, q=q, location_types=location_types, country_code=country_code, region_id=region_id, limit=limit)
                    if "error" not in fallback and fallback.get("data"):
                        return {**fallback, "_source": "global_search_geo_fallback"}
            return {**primary, "_source": "ad_account_targetingsearch"}
        # Some accounts return empty list — try global search
    fallback = targeting_search(token, search_type, q=q, location_types=location_types, country_code=country_code, region_id=region_id, limit=limit)
    if "error" in fallback:
        return primary if "error" in primary else fallback
    return {**fallback, "_source": "global_search"}


def _strip_paren_suffix(name: str) -> str:
    """Drop a trailing ``(...)`` qualifier from a Meta interest/category name.

    Briefs sometimes carry the qualifier ("Lead generation (marketing)"),
    sometimes don't ("Lead generation"). Meta's ``/search`` is sensitive
    to which form is sent. We use this to retry queries that missed on
    the first pass.
    """
    if not name:
        return ""
    s = re.sub(r"\s*\([^)]*\)\s*$", "", str(name)).strip()
    return s


def _pick_plausible_interest_row(rows: List[Any]) -> Optional[Dict[str, Any]]:
    """Filter Meta /search rows to plausible interest matches (id 11-15 digits, type=interests)."""
    plausible: List[Dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        iid = str(row.get("id") or "").strip()
        if not iid.isdigit() or not (11 <= len(iid) <= 15):
            continue
        row_type = str(row.get("type") or "").strip().lower()
        if row_type and row_type not in ("interests", "adinterest"):
            continue
        plausible.append(row)
    return plausible


# Minimum SequenceMatcher ratio for a non-exact interest match to be accepted
# as "found". Below this we send the query to ``missing`` so the caller can
# decide (drop the interest, search again, or surface to the user) rather than
# binding e.g. "Business email" to "Marketing services and organisations".
_INTEREST_MATCH_MIN_RATIO = 0.6


def _best_interest_match(plausible: List[Dict[str, Any]], query: str) -> Tuple[Optional[Dict[str, Any]], float]:
    """Return (row, ratio). Prefers exact case-insensitive name match (ratio=1.0).
    Otherwise returns the highest-ratio row by ``difflib.SequenceMatcher``."""
    from difflib import SequenceMatcher
    if not plausible:
        return None, 0.0
    q = (query or "").strip().lower()
    best: Optional[Dict[str, Any]] = None
    best_ratio = 0.0
    for row in plausible:
        nm = str(row.get("name") or "").strip().lower()
        if nm and nm == q:
            return row, 1.0
        ratio = SequenceMatcher(None, q, nm).ratio() if nm else 0.0
        if ratio > best_ratio:
            best_ratio = ratio
            best = row
    return best, best_ratio


def batch_interest_search(account_id, token, interest_names):
    """Search multiple interests in batch. Returns {found: [{id, name, query, score}], missing: [query]}.

    Picks the best result per query with multiple safety filters because Meta's
    ``/search?type=adinterest`` sometimes leaks cross-type matches (education
    schools, demographics) into the response when no real interest matches the
    query string. Without filtering, ``data[0]`` can be e.g. ``{id: 4, name:
    "High school leaver"}`` for a query like "Business school (higher
    education)" — that gets swapped into ``flexible_spec.interests`` and the
    fabricated-ID guard later refuses to create the campaign.

    Filters applied (in order):
      1. Drop results whose ``id`` isn't a 13-15 digit number (real Meta
         interest IDs are always in that range — never 1-12 digits).
      2. Drop results whose ``type`` isn't ``interests`` (some Meta accounts
         return the field, others don't; lenient when missing).
      3. Prefer an exact case-insensitive name match (score=1.0).
      4. Otherwise accept the highest-ratio row IF the SequenceMatcher score
         is at least ``_INTEREST_MATCH_MIN_RATIO`` (0.6). Lower-ratio matches
         go to ``missing[]`` instead of polluting ``flexible_spec.interests``
         with semantically unrelated targeting (e.g. "Business email" was
         binding to "Marketing services and organisations" before this guard).

    Two-pass search: when the first pass misses (no plausible row), retry
    with a paren-suffix-stripped query — briefs frequently carry an
    explanatory qualifier like "(marketing)" that Meta's search treats as
    part of the literal query and fails to match.
    """
    results = {"found": [], "missing": []}
    for raw_name in (interest_names or []):
        name = str(raw_name).strip()
        if not name:
            continue
        chosen: Optional[Dict[str, Any]] = None
        chosen_ratio = 0.0
        # Pass 1: literal query.
        r = targeting_search_with_fallback(account_id, token, "adinterest", q=name, limit=10)
        rows = r.get("data") if isinstance(r, dict) and "error" not in r else None
        plausible = _pick_plausible_interest_row(rows or [])
        chosen, chosen_ratio = _best_interest_match(plausible, name)
        # Pass 2: retry with paren stripped if the first pass missed or scored low.
        if chosen is None or chosen_ratio < _INTEREST_MATCH_MIN_RATIO:
            stripped = _strip_paren_suffix(name)
            if stripped and stripped != name:
                r2 = targeting_search_with_fallback(account_id, token, "adinterest", q=stripped, limit=10)
                rows2 = r2.get("data") if isinstance(r2, dict) and "error" not in r2 else None
                plausible2 = _pick_plausible_interest_row(rows2 or [])
                cand, cand_ratio = _best_interest_match(plausible2, stripped)
                if cand is not None and cand_ratio > chosen_ratio:
                    chosen = cand
                    chosen_ratio = cand_ratio
        if chosen is None or chosen_ratio < _INTEREST_MATCH_MIN_RATIO:
            results["missing"].append(name)
            # Include up to 3 near-miss alternatives so the LLM can self-correct
            # next turn instead of silently dropping the brief item. We collect
            # plausible rows from BOTH passes (literal + paren-stripped) and
            # surface the top-ranked names. Empty `suggestions` means Meta has
            # no nearby concept at all.
            near_misses: List[str] = []
            for _row in (plausible or [])[:5]:
                _nm = str(_row.get("name") or "").strip()
                if _nm and _nm not in near_misses:
                    near_misses.append(_nm)
            if near_misses:
                results.setdefault("suggestions", {})[name] = near_misses[:3]
            continue
        results["found"].append({
            "id": str(chosen["id"]),
            "name": chosen.get("name") or name,
            "query": name,  # always the original brief name so the caller can match
            "score": round(chosen_ratio, 3),
        })
    return results


def batch_targeting_category_search(account_id, token, category_names):
    """Search multiple behaviors/demographics in batch.

    Returns ``{found: [{id, name, path, query}], missing: [query]}``. Mirrors
    ``batch_interest_search`` but hits Meta's ``adTargetingCategory`` search
    type — the endpoint that backs both the "Behaviors" and "Demographics"
    panes in Ads Manager. Meta returns each row with a ``path`` array (e.g.
    ``["Behaviors", "Digital activities"]``, ``["Demographics", "Education"]``)
    so callers can route results to the correct ``flexible_spec`` axis.

    Filters applied (in order, first match wins):
      1. Drop results whose ``id`` isn't a 13-15 digit number — protects
         against the same cross-type-leak failure mode that bit
         ``batch_interest_search`` (id=4 / "High school leaver" etc.).
      2. Require ``path[0]`` to be ``Behaviors`` or ``Demographics``. When
         ``path`` is missing on a row we accept on length alone since some
         Meta accounts predate consistent path tagging.
      3. Prefer an exact case-insensitive name match if any result has one.
      4. Otherwise pick the first surviving result.
    """
    def _filter_category_rows(rows: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            iid = str(row.get("id") or "").strip()
            if not iid.isdigit() or not (11 <= len(iid) <= 15):
                continue
            row_path = row.get("path")
            if isinstance(row_path, list) and row_path:
                first = str(row_path[0] or "").strip().lower()
                if first not in ("behaviors", "demographics"):
                    continue
            out.append(row)
        return out

    results: Dict[str, List[Any]] = {"found": [], "missing": []}
    for raw_name in (category_names or []):
        name = str(raw_name).strip()
        if not name:
            continue
        chosen: Optional[Dict[str, Any]] = None
        # Pass 1: literal query.
        r = targeting_search_with_fallback(account_id, token, "adTargetingCategory", q=name, limit=10)
        rows = r.get("data") if isinstance(r, dict) and "error" not in r else None
        plausible = _filter_category_rows(rows)
        if plausible:
            nm_l = name.lower()
            exact = next(
                (row for row in plausible if str(row.get("name") or "").strip().lower() == nm_l),
                None,
            )
            chosen = exact or plausible[0]
        # Pass 2: paren-stripped retry when the first pass missed.
        if chosen is None:
            stripped = _strip_paren_suffix(name)
            if stripped and stripped != name:
                r2 = targeting_search_with_fallback(account_id, token, "adTargetingCategory", q=stripped, limit=10)
                rows2 = r2.get("data") if isinstance(r2, dict) and "error" not in r2 else None
                plausible2 = _filter_category_rows(rows2)
                if plausible2:
                    nm_l = stripped.lower()
                    exact = next(
                        (row for row in plausible2 if str(row.get("name") or "").strip().lower() == nm_l),
                        None,
                    )
                    chosen = exact or plausible2[0]
        # Pass 3: cross-type fallback to adinterest. Some briefs categorize
        # terms as behaviors that Meta only has in the interest taxonomy
        # ("Entrepreneurs", "Marketing Professionals"). We try adinterest as a
        # last resort. To prevent low-quality fuzzy matches from leaking in,
        # we only accept an EXACT name match here (not just first plausible)
        # — production_flow's strict-name guard would drop fuzzy ones anyway,
        # so requiring exact match upfront keeps the API call lean and the
        # results predictable.
        if chosen is None:
            for q_try in (name, _strip_paren_suffix(name)):
                if not q_try:
                    continue
                r3 = targeting_search_with_fallback(account_id, token, "adinterest", q=q_try, limit=10)
                rows3 = r3.get("data") if isinstance(r3, dict) and "error" not in r3 else None
                plausible3 = _pick_plausible_interest_row(rows3 or [])
                if plausible3:
                    nm_l = q_try.lower()
                    exact = next(
                        (row for row in plausible3 if str(row.get("name") or "").strip().lower() == nm_l),
                        None,
                    )
                    if exact:
                        # Synthesize a path so the caller's path[0] routing
                        # still works. Treat cross-type-from-adinterest as
                        # "Behaviors" (closer to behaviors than demographics
                        # in Meta's UI taxonomy).
                        candidate = dict(exact)
                        candidate["path"] = candidate.get("path") or ["Behaviors"]
                        chosen = candidate
                        break
        if chosen is None:
            results["missing"].append(name)
            # Same near-miss logic as batch_interest_search: surface the top
            # plausible names from the literal-query pass so the LLM can pick
            # a real Meta concept instead of dropping the brief item.
            near_misses: List[str] = []
            for _row in (plausible or [])[:5]:
                _nm = str(_row.get("name") or "").strip()
                if _nm and _nm not in near_misses:
                    near_misses.append(_nm)
            if near_misses:
                results.setdefault("suggestions", {})[name] = near_misses[:3]
            continue
        path_val = chosen.get("path") if isinstance(chosen.get("path"), list) else []
        results["found"].append({
            "id": str(chosen["id"]),
            "name": chosen.get("name") or name,
            "path": path_val,
            "query": name,
        })
    return results


def get_marketing_object(object_id, token, fields):
    """GET /{id}?fields=... for campaign, adset, ad, creative, etc."""
    oid = (object_id or "").strip()
    if not oid:
        return {"error": {"message": "object_id is required"}}
    if not fields or not str(fields).strip():
        return {"error": {"message": "fields is required"}}
    url = f"{BASE_URL}/{oid}"
    return _get(url, {"access_token": token, "fields": str(fields).strip()})


# Radius in km: Facebook allows 1-80 km for custom_locations, 17-80 km for cities. We clamp to 2-80 km as standard.
def _clamp_radius_km(km, default=10):
    if km is None:
        return default
    try:
        v = float(km)
        return max(2, min(80, v))
    except (TypeError, ValueError):
        return default


# Meta API floor for city radius is 1 km outside US (1 mile US). Hard 17 km
# clamp used to be defensive — but it wrongly forced "Delhi +2 km" briefs to
# 17 km, silently distorting audience size. Per spec §13, clamp to [1, 70] km
# and let Meta surface error_subcode=1487110 for low-density rejections so the
# agent can react (typically by omitting radius to use the city polygon).
def _clamp_city_radius_km(km, default=25):
    if km is None:
        return default
    try:
        v = float(km)
        clamped = max(1.0, min(70.0, v))
        if v < 1.0 or v > 70.0:
            logger.info("city radius %r clamped to %r (Meta floor 1 km, ceiling 70 km)", v, clamped)
        return clamped
    except (TypeError, ValueError):
        return default


def build_targeting_spec(
    geo_countries=None,
    geo_regions=None,
    geo_cities=None,
    geo_custom_locations=None,
    geo_country_groups=None,
    geo_location_types=None,
    geo_radius_km=None,
    age_min=None,
    age_max=None,
    genders=None,
    flexible_spec=None,
    custom_audiences=None,
    excluded_geo_locations=None,
    excluded_custom_audiences=None,
    targeting_override=None,
    publisher_platforms=None,
    device_platforms=None,
    facebook_positions=None,
    instagram_positions=None,
    messenger_positions=None,
    audience_network_positions=None,
    threads_positions=None,
    user_os=None,
):
    """Build Facebook targeting spec (JSON-serializable dict) from structured params. Standard: radius 2-80 km. Use targeting_override to merge or pass raw targeting.
    publisher_platforms: e.g. ['facebook','instagram','messenger']. device_platforms: e.g. ['mobile'].
    facebook_positions / instagram_positions: e.g. ['feed','story','reels'].
    audience_network_positions / threads_positions: manual placements when those publisher platforms are included.
    messenger_positions: e.g. ['messenger_home'] (Messenger home / inbox-style placement in Ads Manager). Include 'messenger' in publisher_platforms.
    user_os: e.g. ['Android'] or ['iOS'] — restricts to specific mobile OS. Auto-preserved from targeting_override if device_platforms is set."""
    spec = {}
    if targeting_override is not None:
        if isinstance(targeting_override, str):
            try:
                spec = json.loads(targeting_override)
            except (TypeError, ValueError):
                spec = {}
        elif isinstance(targeting_override, dict):
            spec = dict(targeting_override)

    geo = spec.get("geo_locations") or {}
    if geo_countries is not None:
        if isinstance(geo_countries, str):
            try:
                geo_countries = json.loads(geo_countries)
            except (TypeError, ValueError):
                geo_countries = [c.strip() for c in geo_countries.split(",") if c.strip()]
        if isinstance(geo_countries, list) and geo_countries:
            geo["countries"] = [str(c).strip() for c in geo_countries if str(c).strip()]
    if geo_country_groups is not None:
        if isinstance(geo_country_groups, str):
            try:
                geo_country_groups = json.loads(geo_country_groups)
            except (TypeError, ValueError):
                geo_country_groups = [g.strip() for g in geo_country_groups.split(",") if g.strip()]
        if isinstance(geo_country_groups, list) and geo_country_groups:
            geo["country_groups"] = [str(g).strip() for g in geo_country_groups if str(g).strip()]
    if geo_regions is not None:
        if isinstance(geo_regions, str):
            try:
                geo_regions = json.loads(geo_regions)
            except (TypeError, ValueError):
                geo_regions = []
        if isinstance(geo_regions, list) and geo_regions:
            geo["regions"] = [{"key": str(r.get("key", r)).strip()} if isinstance(r, dict) else {"key": str(r).strip()} for r in geo_regions]
    if geo_cities is not None:
        if isinstance(geo_cities, str):
            try:
                geo_cities = json.loads(geo_cities)
            except (TypeError, ValueError):
                geo_cities = []
        if isinstance(geo_cities, list) and geo_cities:
            # City targeting has a 17 km Meta minimum; clamp via the
            # city-specific helper so a brief that says "10km radius" doesn't
            # POST a value Meta rejects with error_subcode 1487110.
            radius_km = _clamp_city_radius_km(geo_radius_km, 25)
            out = []
            for c in geo_cities:
                if isinstance(c, dict):
                    key = str(c.get("key", "")).strip()
                    r = c.get("radius")
                    du = (c.get("distance_unit") or "kilometer").lower()
                    if "mile" in du:
                        du = "mile"
                    else:
                        du = "kilometer"
                    if not key:
                        continue
                    out.append({"key": key, "radius": _clamp_city_radius_km(r, radius_km) if r is not None else radius_km, "distance_unit": du})
                else:
                    continue
            if out:
                geo["cities"] = out
    if geo_custom_locations is not None:
        if isinstance(geo_custom_locations, str):
            try:
                geo_custom_locations = json.loads(geo_custom_locations)
            except (TypeError, ValueError):
                geo_custom_locations = []
        if isinstance(geo_custom_locations, list) and geo_custom_locations:
            radius_km = _clamp_radius_km(geo_radius_km, 10)
            out = []
            for loc in geo_custom_locations:
                if not isinstance(loc, dict):
                    continue
                lat = loc.get("latitude")
                lon = loc.get("longitude")
                addr = loc.get("address_string")
                if (lat is not None and lon is not None) or addr:
                    entry = {}
                    if addr:
                        entry["address_string"] = str(addr).strip()
                    else:
                        try:
                            entry["latitude"] = float(lat)
                            entry["longitude"] = float(lon)
                        except (TypeError, ValueError):
                            continue
                    r = loc.get("radius")
                    du = (loc.get("distance_unit") or "kilometer").lower()
                    if "mile" in du:
                        du = "mile"
                    else:
                        du = "kilometer"
                    entry["radius"] = _clamp_radius_km(r, radius_km) if r is not None else radius_km
                    entry["distance_unit"] = du
                    out.append(entry)
            if out:
                geo["custom_locations"] = out
    if geo_location_types is not None:
        if isinstance(geo_location_types, str):
            geo_location_types = [s.strip() for s in geo_location_types.split(",") if s.strip()]
        if isinstance(geo_location_types, list) and geo_location_types:
            allowed = {"home", "recent"}
            geo["location_types"] = [t for t in geo_location_types if str(t).strip().lower() in allowed]
    if geo:
        spec["geo_locations"] = geo

    if excluded_geo_locations is not None:
        if isinstance(excluded_geo_locations, str):
            try:
                excluded_geo_locations = json.loads(excluded_geo_locations)
            except (TypeError, ValueError):
                excluded_geo_locations = {}
        if isinstance(excluded_geo_locations, dict) and excluded_geo_locations:
            spec["excluded_geo_locations"] = excluded_geo_locations

    if age_min is not None:
        try:
            v = int(age_min)
            spec["age_min"] = max(13, min(65, v))
        except (TypeError, ValueError):
            pass
    if age_max is not None:
        try:
            v = int(age_max)
            spec["age_max"] = max(13, min(65, v))
        except (TypeError, ValueError):
            pass
    if genders is not None:
        if isinstance(genders, str):
            try:
                genders = json.loads(genders)
            except (TypeError, ValueError):
                genders = [int(g) for g in str(genders).replace(",", " ").split() if str(g).strip().isdigit()]
        if isinstance(genders, list) and genders:
            spec["genders"] = [int(g) for g in genders if g in (1, 2)]

    if flexible_spec is not None:
        if isinstance(flexible_spec, str):
            try:
                flexible_spec = json.loads(flexible_spec)
            except (TypeError, ValueError):
                flexible_spec = []
        if isinstance(flexible_spec, list) and flexible_spec:
            spec["flexible_spec"] = flexible_spec

    if custom_audiences is not None:
        if isinstance(custom_audiences, str):
            try:
                custom_audiences = json.loads(custom_audiences)
            except (TypeError, ValueError):
                custom_audiences = []
        if isinstance(custom_audiences, list) and custom_audiences:
            spec["custom_audiences"] = [{"id": str(a.get("id", a))} if isinstance(a, dict) else {"id": str(a)} for a in custom_audiences]

    if excluded_custom_audiences is not None:
        if isinstance(excluded_custom_audiences, str):
            try:
                excluded_custom_audiences = json.loads(excluded_custom_audiences)
            except (TypeError, ValueError):
                excluded_custom_audiences = []
        if isinstance(excluded_custom_audiences, list) and excluded_custom_audiences:
            spec["excluded_custom_audiences"] = [{"id": str(a.get("id", a))} if isinstance(a, dict) else {"id": str(a)} for a in excluded_custom_audiences]

    if publisher_platforms is not None and isinstance(publisher_platforms, list) and publisher_platforms:
        spec["publisher_platforms"] = [str(p).strip().lower() for p in publisher_platforms if str(p).strip()]
    if device_platforms is not None and isinstance(device_platforms, list) and device_platforms:
        spec["device_platforms"] = [str(d).strip().lower() for d in device_platforms if str(d).strip()]
        # Auto-preserve user_os from targeting_override when device_platforms is explicitly set
        # (e.g. if caller passes device_platforms=["mobile"] but forgets user_os=["Android"],
        # keep the existing user_os from the override so it doesn't get dropped).
        if "user_os" not in spec and isinstance(targeting_override, dict) and targeting_override.get("user_os"):
            spec["user_os"] = targeting_override["user_os"]
    # Explicit user_os parameter (e.g. ["Android"], ["iOS"], ["Android", "iOS"])
    if user_os is not None:
        if isinstance(user_os, str):
            try:
                user_os = json.loads(user_os)
            except (TypeError, ValueError):
                user_os = [o.strip() for o in user_os.split(",") if o.strip()]
        if isinstance(user_os, list) and user_os:
            spec["user_os"] = [str(o).strip() for o in user_os if str(o).strip()]
    if facebook_positions is not None and isinstance(facebook_positions, list) and facebook_positions:
        spec["facebook_positions"] = [str(f).strip().lower() for f in facebook_positions if str(f).strip()]
    if instagram_positions is not None and isinstance(instagram_positions, list) and instagram_positions:
        spec["instagram_positions"] = [str(i).strip().lower() for i in instagram_positions if str(i).strip()]
    if audience_network_positions is not None and isinstance(audience_network_positions, list) and audience_network_positions:
        spec["audience_network_positions"] = [
            str(p).strip().lower() for p in audience_network_positions if str(p).strip()
        ]
    if threads_positions is not None and isinstance(threads_positions, list) and threads_positions:
        spec["threads_positions"] = [str(p).strip().lower() for p in threads_positions if str(p).strip()]

    # Messenger: use publisher_platforms including "messenger" + messenger_positions (e.g. messenger_home).
    # v26 (all versions from 2026-10-27): "story" (Messenger Stories) is gone —
    # Meta silently drops it today and rejects it from October. Strip here.
    _ALLOWED_MESSENGER = frozenset({"messenger_home", "sponsored_messages"})
    _MESSENGER_ALIASES = {"messenger_inbox": "messenger_home", "inbox": "messenger_home"}
    if messenger_positions is not None and isinstance(messenger_positions, list) and messenger_positions:
        out_m = []
        for m in messenger_positions:
            t = str(m).strip().lower()
            t = _MESSENGER_ALIASES.get(t, t)
            if t in _ALLOWED_MESSENGER:
                out_m.append(t)
        if out_m:
            spec["messenger_positions"] = out_m
        else:
            spec.pop("messenger_positions", None)
    elif "messenger_positions" in spec:
        raw = spec.get("messenger_positions")
        if isinstance(raw, list):
            out_m = []
            for m in raw:
                t = str(m).strip().lower()
                t = _MESSENGER_ALIASES.get(t, t)
                if t in _ALLOWED_MESSENGER:
                    out_m.append(t)
            if out_m:
                spec["messenger_positions"] = out_m
            else:
                spec.pop("messenger_positions", None)

    return spec


# Optimization goals that benefit from a default `LOWEST_COST_WITHOUT_CAP` bid
# strategy. Without this Meta rejects with error_subcode 2490487 demanding a
# bid_amount or bid_constraints (which we can't infer from the brief).
_LEAD_OPTIMIZATION_GOALS = frozenset({
    "LEAD_GENERATION", "OFFSITE_CONVERSIONS", "QUALITY_LEAD", "QUALITY_CALL",
    "CONVERSATIONS",
})


def _apply_adset_safe_defaults(data: dict, optimization_goal: Optional[str] = None) -> List[str]:
    """Mutate ``data`` (the about-to-POST adset payload) with Meta's required
    defaults so single-call ad set creation doesn't bounce with 4xx errors.

    Returns a list of human-readable strings describing each default applied,
    so the caller can append them to ``response['_applied_defaults']``.

    Covers the three error subcodes that broke the May 2026 BOL7 lead-ads
    session in one round-trip:
      - 2490487 (bid_amount/bid_constraints required) → default bid_strategy
      - 1870227 (advantage_audience flag required)    → default to 0
      - 1870189 (age_max < 65 with advantage_audience=1) → auto-disable AA
    """
    fixes: List[str] = []
    og = (optimization_goal or data.get("optimization_goal") or "")
    og = str(og).upper().strip()
    # 1) bid_strategy default
    if og in _LEAD_OPTIMIZATION_GOALS and not str(data.get("bid_strategy") or "").strip():
        data["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
        fixes.append(
            f"bid_strategy defaulted to LOWEST_COST_WITHOUT_CAP "
            f"(Meta requires it for optimization_goal={og})."
        )
    # 2/3) targeting_automation.advantage_audience: default + age conflict
    targ_raw = data.get("targeting")
    targ: Optional[Dict[str, Any]] = None
    if isinstance(targ_raw, str) and targ_raw.strip():
        try:
            targ = json.loads(targ_raw)
        except (TypeError, ValueError):
            targ = None
    elif isinstance(targ_raw, dict):
        targ = dict(targ_raw)
    if isinstance(targ, dict):
        ta = dict(targ.get("targeting_automation") or {})
        aa = ta.get("advantage_audience")
        if aa is None:
            ta["advantage_audience"] = 0
            fixes.append(
                "targeting_automation.advantage_audience defaulted to 0 "
                "(Meta requires the flag explicitly when optimization is set)."
            )
        else:
            try:
                aa_i = int(aa)
            except (TypeError, ValueError):
                aa_i = 0
            if aa_i == 1:
                age_max_raw = targ.get("age_max")
                try:
                    age_max_i = int(age_max_raw) if age_max_raw is not None else None
                except (TypeError, ValueError):
                    age_max_i = None
                if age_max_i is not None and age_max_i < 65:
                    ta["advantage_audience"] = 0
                    fixes.append(
                        f"advantage_audience auto-disabled (was 1) because age_max="
                        f"{age_max_i} < 65; Meta rejects this combination "
                        "(error_subcode 1870189). Widen age_max to 65 in Ads "
                        "Manager and re-enable Advantage+ audience if you want it."
                    )
        targ["targeting_automation"] = ta
        data["targeting"] = json.dumps(targ)
    return fixes


def validate_adset_spec(account_id, token, spec, *, campaign_id=None):
    """Server-side validate-only probe of an ad set spec.

    Meta supports ``execution_options=["validate_only"]`` on POST
    ``/act_{id}/adsets``: the server runs full validation but commits
    nothing. We use this to detect 2490408 ("Performance goal isn't
    available") and similar per-account×objective×goal lockouts BEFORE
    create_campaign and create_adset write anything. Zero account-side
    writes on a rejected goal.

    ``spec`` is the same dict that ``create_adset`` would POST; we just
    inject ``execution_options=["validate_only"]``. ``campaign_id`` is
    optional — when present we point the probe at that real campaign;
    when absent Meta still validates the spec shape but may report a
    missing-campaign error in addition to anything else (caller should
    decide whether to surface that).

    Returns the raw Meta response dict. Caller inspects ``error`` field
    for blocking subcodes (specifically 2490408 + blame_field_specs
    pointing at optimization_goal).
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/adsets"
    # spec may be dict or stringified JSON; normalize to flat form fields
    data = dict(spec) if isinstance(spec, dict) else {}
    if campaign_id and not data.get("campaign_id"):
        data["campaign_id"] = str(campaign_id).strip()
    data["access_token"] = token
    # JSON-encode any nested values (targeting, promoted_object, etc.)
    for k in list(data.keys()):
        v = data[k]
        if isinstance(v, (dict, list)):
            data[k] = json.dumps(v)
    data["execution_options"] = json.dumps(["validate_only"])
    return _post(url, data)


def create_adset(account_id, token, campaign_id, name, status="PAUSED", daily_budget=None, lifetime_budget=None,
                 billing_event="IMPRESSIONS", optimization_goal=None, targeting=None,
                 bid_strategy=None, bid_amount=None, bid_constraints=None,
                 promoted_object=None, page_id=None, destination_type=None,
                 start_time=None, end_time=None,
                 is_dynamic_creative=None, product_set_id=None,
                 frequency_control_specs=None):
    """Create an ad set. Real API: POST /act_{id}/adsets. Requires ads_management. Provide daily_budget or lifetime_budget (cents).
    For bid cap: set bid_strategy (e.g. LOWEST_COST_WITH_BID_CAP, COST_CAP) and bid_amount (cents).
    For ROAS: set optimization_goal VALUE and bid_constraints e.g. {\"ROAS_AVERAGE_FLOOR\": 150}.
    start_time/end_time: ISO 8601 (e.g. 2025-03-13T00:00:00+0000). targeting may include publisher_platforms, device_platforms, facebook_positions, instagram_positions.

    Auto-applies Meta defaults that briefs commonly omit (bid_strategy for
    LEAD_GENERATION, targeting_automation.advantage_audience flag, age-cap
    conflict resolution). The applied defaults are reported in
    ``response['_applied_defaults']`` so the caller can surface them."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    if not campaign_id or not name:
        return {"error": {"message": "campaign_id and name are required"}}
    # Budget is optional when using Campaign Budget Optimization (CBO)
    url = f"{BASE_URL}/{act}/adsets"
    data = {"access_token": token, "campaign_id": str(campaign_id).strip(), "name": name, "status": status or "PAUSED", "billing_event": billing_event or "IMPRESSIONS"}
    if daily_budget is not None:
        data["daily_budget"] = str(int(daily_budget))
    if lifetime_budget is not None:
        data["lifetime_budget"] = str(int(lifetime_budget))
    if start_time:
        data["start_time"] = str(start_time).strip()
    if end_time:
        data["end_time"] = str(end_time).strip()
    if optimization_goal:
        data["optimization_goal"] = optimization_goal
    if targeting:
        data["targeting"] = targeting if isinstance(targeting, str) else json.dumps(targeting)
    if bid_strategy:
        data["bid_strategy"] = str(bid_strategy).strip()
    if bid_amount is not None:
        data["bid_amount"] = str(int(bid_amount))
    if bid_constraints is not None:
        data["bid_constraints"] = bid_constraints if isinstance(bid_constraints, str) else json.dumps(bid_constraints)
    if promoted_object:
        data["promoted_object"] = promoted_object if isinstance(promoted_object, str) else json.dumps(promoted_object)
    elif product_set_id and str(product_set_id).strip():
        # Dynamic Product Ads / Catalog: bind the adset to a product set so
        # OUTCOME_SALES + LINK_CLICKS / OFFSITE_CONVERSIONS optimizes against
        # the catalog. Combine with page_id when available so Meta has both
        # the brand identity AND the product source.
        po: Dict[str, Any] = {"product_set_id": str(product_set_id).strip()}
        if page_id:
            po["page_id"] = str(page_id).strip()
        data["promoted_object"] = json.dumps(po)
    elif page_id:
        data["promoted_object"] = json.dumps({"page_id": str(page_id).strip()})
    if destination_type:
        data["destination_type"] = str(destination_type).strip()
    # Frequency cap (e.g. max 3 impressions / 1 day). Meta accepts a JSON array
    # of {event, interval_days, max_frequency}. Must be set at create time — Meta
    # forbids editing frequency_control_specs once the ad set has started
    # (subcode 3858722), so it is never patched after the fact.
    if frequency_control_specs:
        data["frequency_control_specs"] = (
            frequency_control_specs
            if isinstance(frequency_control_specs, str)
            else json.dumps(frequency_control_specs)
        )
    # Dynamic Creative Optimization: must be enabled at the adset level for the
    # creative's asset_feed_spec to take effect. Meta returns error if the
    # creative has asset_feed_spec but the adset doesn't set this flag.
    if is_dynamic_creative is not None:
        truthy = (is_dynamic_creative is True) or (str(is_dynamic_creative).strip().lower() in ("true", "1", "yes"))
        data["is_dynamic_creative"] = "true" if truthy else "false"
    fixes = _apply_adset_safe_defaults(data, optimization_goal)
    resp = _post_with_rate_limit_retry(url, data)
    if isinstance(resp, dict) and fixes:
        resp.setdefault("_applied_defaults", []).extend(fixes)
    return resp


def update_adset_replace_geo_cities_from_preset(
    adset_id,
    token,
    preset_key="bol7_top_10_in",
    name=None,
    status=None,
    daily_budget=None,
    lifetime_budget=None,
    bid_strategy=None,
    optimization_goal=None,
    destination_type=None,
    billing_event=None,
):
    """
    Fetch current ad set targeting, replace geo_locations.cities with a named preset (exact doc list),
    remove whole-country/region geo so targeting is only those cities. Other targeting keys unchanged.
    """
    adset_id = str(adset_id or "").strip()
    preset_key = str(preset_key or "").strip()
    cities = GEO_CITIES_PRESETS.get(preset_key)
    if not cities:
        return {"error": {"message": f"Unknown geo_cities_preset: {preset_key}. Use: bol7_top_10_in"}}
    obj = get_marketing_object(adset_id, token, "targeting")
    if isinstance(obj, dict) and "error" in obj:
        return obj
    t = obj.get("targeting") if isinstance(obj, dict) else None
    if isinstance(t, str) and t.strip():
        try:
            t = json.loads(t)
        except (TypeError, ValueError):
            return {"error": {"message": "Could not parse ad set targeting JSON"}}
    if not isinstance(t, dict):
        t = {}
    t = dict(t)
    geo = dict(t.get("geo_locations") or {})
    geo["cities"] = [
        {"key": str(c["key"]), "radius": int(c["radius"]), "distance_unit": str(c["distance_unit"]).lower()}
        for c in cities
    ]
    geo.pop("countries", None)
    geo.pop("regions", None)
    geo.pop("zips", None)
    t["geo_locations"] = geo
    return update_adset(
        adset_id,
        token,
        name=name,
        status=status,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        bid_strategy=bid_strategy,
        optimization_goal=optimization_goal,
        destination_type=destination_type,
        billing_event=billing_event,
        targeting=t,
    )


_PLACEMENT_TARGETING_KEYS = (
    "publisher_platforms",
    "facebook_positions",
    "instagram_positions",
    "messenger_positions",
    "audience_network_positions",
    "threads_positions",
    "device_platforms",
)


def _placement_slice_from_targeting(targeting) -> dict:
    """Subset of targeting used for placements (for meta_diff)."""
    t = targeting if isinstance(targeting, dict) else _targeting_as_dict(targeting)
    if not t:
        return {}
    return {k: t[k] for k in _PLACEMENT_TARGETING_KEYS if k in t}


def _normalize_skipped_entry(field: str, reason: str) -> dict:
    return {"field": str(field), "reason": str(reason)}


def _publisher_platforms_list(placement_slice: dict | None) -> list:
    if not placement_slice:
        return []
    pp = placement_slice.get("publisher_platforms")
    if isinstance(pp, list):
        return sorted(str(x).lower() for x in pp)
    return []


def _build_adset_meta_diff(
    before_adset: dict,
    post_payload: dict,
    after_adset: dict,
) -> dict:
    """
    Compare before vs after refetch; show what was requested in POST vs what Meta stored.
    """
    before_t = before_adset.get("targeting")
    after_t = after_adset.get("targeting")
    req_t = post_payload.get("targeting")
    before_pl = _placement_slice_from_targeting(before_t)
    after_pl = _placement_slice_from_targeting(after_t)
    req_pl = _placement_slice_from_targeting(req_t) if req_t is not None else None
    req_list = _publisher_platforms_list(req_pl)
    act_list = _publisher_platforms_list(after_pl)
    meta_diff = {
        "placements": {
            "before": before_pl,
            "requested": req_list,
            "actual": act_list,
            "detail": {
                "requested": req_pl,
                "actual": after_pl,
            },
        },
    }
    pl = meta_diff["placements"]
    if req_pl is not None:
        pl["requested_matches_actual"] = _canonical_json(req_pl) == _canonical_json(after_pl)
    else:
        pl["requested_matches_actual"] = None
    # Other POST fields (non-targeting): surface if Meta returned different values after refresh
    field_snap = {}
    for key in post_payload:
        if key == "targeting":
            continue
        b, a = before_adset.get(key), after_adset.get(key)
        if key in ("promoted_object", "bid_constraints"):
            same = _canonical_json(b) == _canonical_json(a)
        elif key in ("daily_budget", "lifetime_budget", "bid_amount"):
            try:
                same = int(b) == int(a) if b is not None and a is not None else b == a
            except (TypeError, ValueError):
                same = str(b) == str(a)
        else:
            same = str(b or "").strip() == str(a or "").strip() if key not in ("start_time", "end_time") else str(b) == str(a)
        field_snap[key] = {
            "requested_in_post": post_payload.get(key),
            "before": b,
            "after": a,
            "unchanged_after_refresh": same,
        }
    if field_snap:
        meta_diff["fields"] = field_snap
    if pl.get("requested_matches_actual") is False:
        logger.info(
            "facebook_ads_mcp: placements requested != actual after adset update (see meta_diff.placements.detail)",
        )
    return meta_diff


def _targeting_as_dict(t):
    if t is None:
        return {}
    if isinstance(t, dict):
        return dict(t)
    if isinstance(t, str) and t.strip():
        try:
            return dict(json.loads(t))
        except (TypeError, ValueError):
            return {}
    return {}


def merge_saved_audience_targeting_with_placements(
    token,
    saved_audience_id,
    placement_template_targeting,
    advantage_audience=0,
):
    """
    GET saved audience targeting and overlay placement/device fields from the document-built template.
    Used so the first ad set create includes full geo/audience (avoids empty-location errors before post-hoc sync).
    Returns merged targeting dict, or {"error": ...} on failure.
    """
    sid = str(saved_audience_id or "").strip()
    if not sid:
        return {"error": {"message": "saved_audience_id is required"}}
    sav = get_marketing_object(sid, token, "id,name,targeting")
    if isinstance(sav, dict) and sav.get("error"):
        return sav
    st = _targeting_as_dict(sav.get("targeting"))
    if not st:
        return {"error": {"message": "Saved audience has no targeting; cannot pre-merge."}}
    merged = dict(st)
    pt = placement_template_targeting if isinstance(placement_template_targeting, dict) else {}
    for k in _PLACEMENT_TARGETING_KEYS:
        if k in pt:
            merged[k] = pt[k]
    # Preserve the document's AUDIENCE keys (custom audiences, lookalikes,
    # interest specs, exclusions) — otherwise a brief that combines a saved
    # audience WITH a lookalike/custom audience silently drops the latter,
    # because the saved audience's targeting (geo-only) replaces everything.
    # The saved audience's own value wins if it already defines the key.
    _AUDIENCE_MERGE_KEYS = (
        "custom_audiences",
        "excluded_custom_audiences",
        "flexible_spec",
        "exclusions",
        "interests",
        "behaviors",
    )
    for k in _AUDIENCE_MERGE_KEYS:
        if k in pt and pt[k] and not merged.get(k):
            merged[k] = pt[k]
    try:
        aa = int(advantage_audience)
    except (TypeError, ValueError):
        aa = 0
    merged["targeting_automation"] = {"advantage_audience": aa}
    return merged


def sync_adset_targeting_from_saved_audience(
    adset_id,
    saved_audience_id,
    token,
    preserve_placements=True,
    name=None,
    status=None,
    daily_budget=None,
    lifetime_budget=None,
    start_time=None,
    end_time=None,
    optimization_goal=None,
    bid_strategy=None,
    destination_type=None,
    billing_event=None,
    promoted_object=None,
    publisher_platforms=None,
    device_platforms=None,
    facebook_positions=None,
    instagram_positions=None,
    messenger_positions=None,
    audience_network_positions=None,
    threads_positions=None,
    advantage_audience=None,
):
    """
    Apply a saved audience's targeting to an ad set (GET /{saved_audience_id}?fields=targeting).
    Saved-audience IDs are not valid in targeting.custom_audiences; use this instead of pasting the id there.
    When preserve_placements is True, keeps publisher_platforms, position arrays, device_platforms,
    and targeting_automation from the current ad set — unless explicit placement args are passed; those override.
    """
    aid = str(adset_id or "").strip()
    sid = str(saved_audience_id or "").strip()
    if not aid or not sid:
        return {"error": {"message": "adset_id and saved_audience_id are required"}}
    cur = get_marketing_object(aid, token, "targeting")
    if isinstance(cur, dict) and cur.get("error"):
        return cur
    sav = get_marketing_object(sid, token, "id,name,targeting")
    if isinstance(sav, dict) and sav.get("error"):
        return sav
    ct = _targeting_as_dict(cur.get("targeting"))
    st = _targeting_as_dict(sav.get("targeting"))
    if not st:
        return {"error": {"message": "Saved audience has no targeting; cannot sync."}}
    merged = dict(st)
    if preserve_placements:
        for k in _PLACEMENT_TARGETING_KEYS:
            if k in ct:
                merged[k] = ct[k]
        if "targeting_automation" in ct:
            merged["targeting_automation"] = ct["targeting_automation"]

    has_placement_override = any(
        x is not None
        for x in (
            publisher_platforms,
            device_platforms,
            facebook_positions,
            instagram_positions,
            messenger_positions,
            audience_network_positions,
            threads_positions,
        )
    )
    if has_placement_override:
        spec = build_targeting_spec(
            targeting_override=merged,
            publisher_platforms=publisher_platforms,
            device_platforms=device_platforms,
            facebook_positions=facebook_positions,
            instagram_positions=instagram_positions,
            messenger_positions=messenger_positions,
            audience_network_positions=audience_network_positions,
            threads_positions=threads_positions,
        )
        merged = spec
    if advantage_audience is not None:
        try:
            merged["targeting_automation"] = {"advantage_audience": int(advantage_audience)}
        except (TypeError, ValueError):
            pass

    return update_adset(
        aid,
        token,
        name=name,
        status=status,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        start_time=start_time,
        end_time=end_time,
        optimization_goal=optimization_goal,
        bid_strategy=bid_strategy,
        destination_type=destination_type,
        billing_event=billing_event,
        promoted_object=promoted_object,
        targeting=merged,
    )


def update_adset(adset_id, token, name=None, status=None, daily_budget=None, lifetime_budget=None, promoted_object=None, start_time=None, end_time=None, optimization_goal=None, bid_strategy=None, bid_amount=None, bid_constraints=None, destination_type=None, billing_event=None, targeting=None):
    """Update an ad set. Real API: POST /{adset_id}. Requires ads_management. promoted_object: JSON or dict. start_time/end_time: ISO 8601. optimization_goal: e.g. AD_RECALL_LIFT for Maximize Ad Recall Lift. bid_amount: minor currency units when using bid cap strategies."""
    if not adset_id or not str(adset_id).strip():
        return {"error": {"message": "adset_id is required"}}
    url = f"{BASE_URL}/{str(adset_id).strip()}"
    data = {"access_token": token}
    if name is not None:
        data["name"] = name
    if status is not None:
        data["status"] = status
    if daily_budget is not None:
        data["daily_budget"] = str(int(daily_budget))
    if lifetime_budget is not None:
        data["lifetime_budget"] = str(int(lifetime_budget))
    if promoted_object is not None:
        data["promoted_object"] = promoted_object if isinstance(promoted_object, str) else json.dumps(promoted_object)
    if start_time is not None:
        data["start_time"] = str(start_time).strip()
    if end_time is not None:
        data["end_time"] = str(end_time).strip()
    if optimization_goal is not None:
        data["optimization_goal"] = str(optimization_goal).strip()
    if bid_strategy is not None and str(bid_strategy).strip():
        data["bid_strategy"] = str(bid_strategy).strip()
    if bid_amount is not None:
        data["bid_amount"] = str(int(bid_amount))
    if bid_constraints is not None:
        data["bid_constraints"] = bid_constraints if isinstance(bid_constraints, str) else json.dumps(bid_constraints)
    if destination_type is not None and str(destination_type).strip():
        data["destination_type"] = str(destination_type).strip()
    if billing_event is not None and str(billing_event).strip():
        data["billing_event"] = str(billing_event).strip()
    if targeting is not None:
        data["targeting"] = targeting if isinstance(targeting, str) else json.dumps(targeting)
    if len(data) == 1:
        return {"error": {"message": "Provide at least one field to update"}}
    return _post(url, data)


_ADSET_FIELDS_FOR_DIFF = (
    "name,status,targeting,promoted_object,optimization_goal,destination_type,"
    "daily_budget,lifetime_budget,billing_event,bid_strategy,bid_amount,bid_constraints,start_time,end_time"
)


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def is_post_engagement_on_post(optimization_goal, destination_type) -> bool:
    og = str(optimization_goal or "").strip().upper()
    dt = str(destination_type or "").strip().upper()
    return og == "POST_ENGAGEMENT" and dt == "ON_POST"


def _strip_pixel_from_promoted_object(po):
    if not isinstance(po, dict):
        return {}
    out = {k: v for k, v in po.items() if k not in ("pixel_id", "custom_event_type")}
    return out


def _parse_json_if_string(val):
    if isinstance(val, str) and val.strip():
        try:
            return json.loads(val)
        except (TypeError, ValueError):
            return val
    return val


def _adset_field_values_equal(field, current_val, desired_val) -> bool:
    if field in ("daily_budget", "lifetime_budget", "bid_amount"):
        try:
            return int(current_val) == int(desired_val)
        except (TypeError, ValueError):
            return str(current_val or "").strip() == str(desired_val or "").strip()
    if field in ("optimization_goal", "destination_type", "billing_event", "bid_strategy", "status"):
        return str(current_val or "").strip().upper() == str(desired_val or "").strip().upper()
    if field == "bid_constraints":
        cur = _parse_json_if_string(current_val)
        new = _parse_json_if_string(desired_val)
        return _canonical_json(cur) == _canonical_json(new)
    if field == "targeting":
        cur = current_val if isinstance(current_val, dict) else _parse_json_if_string(current_val)
        new = desired_val if isinstance(desired_val, dict) else _parse_json_if_string(desired_val)
        if not isinstance(cur, dict):
            cur = {}
        if not isinstance(new, dict):
            new = {}
        return _canonical_json(cur) == _canonical_json(new)
    if field == "promoted_object":
        cur = current_val if isinstance(current_val, dict) else _parse_json_if_string(current_val)
        new = desired_val if isinstance(desired_val, dict) else _parse_json_if_string(desired_val)
        if not isinstance(cur, dict):
            cur = {}
        if not isinstance(new, dict):
            new = {}
        return _canonical_json(cur) == _canonical_json(new)
    return str(current_val or "").strip() == str(desired_val or "").strip()


def _serialize_adset_post_value(field, value):
    if field in ("daily_budget", "lifetime_budget", "bid_amount"):
        return str(int(value))
    if field in ("promoted_object", "targeting", "bid_constraints"):
        if isinstance(value, str):
            return value
        return json.dumps(value)
    if field in ("start_time", "end_time", "name", "status", "optimization_goal", "bid_strategy", "destination_type", "billing_event"):
        return str(value).strip()
    return value


def update_adset_from_payload(adset_id, token, payload: dict):
    """POST only keys present in payload (non-empty dict of API field -> native values)."""
    if not adset_id or not str(adset_id).strip():
        return {"error": {"message": "adset_id is required"}}
    if not payload:
        return {"error": {"message": "Provide at least one field to update"}}
    url = f"{BASE_URL}/{str(adset_id).strip()}"
    data = {"access_token": token}
    for k, v in payload.items():
        if v is None:
            continue
        data[k] = _serialize_adset_post_value(k, v)
    if len(data) == 1:
        return {"error": {"message": "Provide at least one field to update"}}
    return _post(url, data)


def update_adset_with_smart_diff(
    adset_id,
    token,
    explicit_request: dict,
    merged_targeting: dict | None = None,
    *,
    dry_run: bool = False,
    force_update: bool = False,
):
    """
    Fetch current ad set, merge intent with objective rules, diff, POST only changed fields.

    explicit_request: only top-level ad set fields the caller explicitly set (name, status, budgets,
    optimization_goal, billing_event, promoted_object as dict, etc.).
    merged_targeting: full targeting dict to apply (from merged existing + placements/geo), or None to leave targeting unchanged.
    force_update: if True, skip equality diff and POST all built desired fields (still applies engagement pixel strip).

    Returns success with changes_applied, skipped (list of {field, reason}), meta_diff after POST; or no-op; or API error.
    """
    skipped = []
    changes_applied = []
    aid = str(adset_id).strip()
    if not aid:
        return {"error": {"message": "adset_id is required"}}

    cur = get_marketing_object(aid, token, _ADSET_FIELDS_FOR_DIFF)
    if "error" in cur:
        return cur
    before_snapshot = {k: cur.get(k) for k in ("targeting", "promoted_object", "optimization_goal", "destination_type", "billing_event", "name", "status", "daily_budget", "lifetime_budget", "bid_strategy", "bid_amount", "bid_constraints", "start_time", "end_time")}

    # --- Smart auto-fix: start_time / end_time ---
    # If the ad set has already started, Meta rejects start_time edits.
    # Auto-skip start_time to prevent the error.
    from datetime import datetime, timezone
    _now = datetime.now(timezone.utc)
    cur_start_str = cur.get("start_time") or ""
    cur_end_str = cur.get("end_time") or ""
    if "start_time" in explicit_request and cur_start_str:
        try:
            from dateutil.parser import parse as _dtparse
            cur_start_dt = _dtparse(cur_start_str)
            if cur_start_dt <= _now:
                skipped.append({"field": "start_time", "reason": "Ad set already started — start_time cannot be edited (auto-skipped)"})
                del explicit_request["start_time"]
        except Exception:
            pass
    # If the ad set's end_time is in the past, Meta rejects ALL edits.
    # Never extend the schedule implicitly: a preview must make zero provider
    # mutations, and a real edit must carry an explicitly approved future
    # end_time. The ONLY provider write allowed here is that approved end_time.
    schedule_expired = False
    if cur_end_str:
        try:
            from dateutil.parser import parse as _dtparse
            schedule_expired = _dtparse(cur_end_str) <= _now
        except Exception:
            schedule_expired = False
    if schedule_expired:
        from datetime import timedelta
        proposed_end = (_now + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59+0000")
        new_end_raw = explicit_request.get("end_time")
        new_end_ok = False
        if new_end_raw:
            try:
                from dateutil.parser import parse as _dtparse
                new_end_ok = _dtparse(str(new_end_raw)) > _now
            except Exception:
                new_end_ok = False
        if dry_run or not new_end_ok:
            return {
                "success": False,
                "dry_run": bool(dry_run),
                "schedule_expired": True,
                "current_end_time": cur_end_str,
                "proposed_end_time": proposed_end,
                "changes_applied": [],
                "skipped": skipped,
                "mutations_made": [],
                "error": {
                    "message": (
                        "Ad set has expired (end_time in the past). Meta rejects edits on expired ad sets. "
                        "Explicitly approve a new end_time before editing — re-run with end_time set to a future "
                        f"timestamp (for example {proposed_end})."
                        + (" This was a dry run; nothing was changed." if dry_run else "")
                    ),
                    "type": "adset_expired",
                },
            }
        # Caller explicitly approved a future end_time: apply ONLY that first so Meta
        # unlocks the ad set, then continue with the normal diff for the other fields.
        _ext = _post(f"{BASE_URL}/{aid}", {"access_token": token, "end_time": str(new_end_raw)})
        if "error" in _ext:
            return _ext
        changes_applied.append("end_time")
        logger.info("Applied caller-approved end_time for expired adset %s -> %s", aid, new_end_raw)
        cur = get_marketing_object(aid, token, _ADSET_FIELDS_FOR_DIFF)
        if "error" in cur:
            return cur

    eff_og = explicit_request["optimization_goal"] if "optimization_goal" in explicit_request else cur.get("optimization_goal")
    eff_dt = explicit_request["destination_type"] if "destination_type" in explicit_request else cur.get("destination_type")
    engage_on_post = is_post_engagement_on_post(eff_og, eff_dt)

    desired = {}
    for key in (
        "name",
        "status",
        "daily_budget",
        "lifetime_budget",
        "start_time",
        "end_time",
        "optimization_goal",
        "bid_strategy",
        "bid_amount",
        "bid_constraints",
        "destination_type",
        "billing_event",
    ):
        if key not in explicit_request:
            continue
        val = explicit_request[key]
        if val is None and key not in ("bid_amount", "daily_budget", "lifetime_budget"):
            continue
        if key in (
            "optimization_goal",
            "bid_strategy",
            "destination_type",
            "billing_event",
            "status",
            "name",
            "start_time",
            "end_time",
        ):
            if val is not None and not str(val).strip():
                continue
        if key in ("daily_budget", "lifetime_budget", "bid_amount") and val is not None:
            try:
                desired[key] = int(val)
            except (TypeError, ValueError):
                desired[key] = val
        elif key == "bid_constraints" and val is not None:
            desired[key] = _parse_json_if_string(val)
        else:
            desired[key] = val

    if "promoted_object" in explicit_request:
        po = explicit_request["promoted_object"]
        if isinstance(po, str) and po.strip():
            po = _parse_json_if_string(po)
        if isinstance(po, dict):
            if engage_on_post:
                if po.get("pixel_id"):
                    skipped.append(
                        _normalize_skipped_entry(
                            "pixel_id",
                            "Not allowed for POST_ENGAGEMENT + ON_POST",
                        )
                    )
                if po.get("custom_event_type"):
                    skipped.append(
                        _normalize_skipped_entry(
                            "custom_event_type",
                            "Not allowed for POST_ENGAGEMENT + ON_POST",
                        )
                    )
                po = _strip_pixel_from_promoted_object(po)
            if po:
                desired["promoted_object"] = po
        elif po is not None:
            desired["promoted_object"] = po

    if merged_targeting is not None and isinstance(merged_targeting, dict):
        desired["targeting"] = merged_targeting

    # Drop promoted_object if identical to current (unless force_update — Meta sometimes needs a no-op refresh re-sent)
    if "promoted_object" in desired and not force_update:
        cur_po = cur.get("promoted_object")
        cur_cmp = _strip_pixel_from_promoted_object(cur_po) if isinstance(cur_po, dict) else {}
        new_cmp = desired["promoted_object"] if isinstance(desired["promoted_object"], dict) else {}
        if isinstance(new_cmp, dict) and _canonical_json(cur_cmp) == _canonical_json(new_cmp):
            del desired["promoted_object"]

    post_payload = {}
    if force_update and desired:
        post_payload = dict(desired)
        changes_applied = list(post_payload.keys())
    else:
        for field, new_val in desired.items():
            cur_val = cur.get(field)
            if _adset_field_values_equal(field, cur_val, new_val):
                continue
            post_payload[field] = new_val
            changes_applied.append(field)

    if not post_payload:
        out = {
            "success": True,
            "message": "No changes needed",
            "changes_applied": [],
            "skipped": skipped,
            "force_update": force_update,
        }
        if engage_on_post and skipped:
            out["reason"] = "POST_ENGAGEMENT + ON_POST: pixel/conversion fields omitted; no other diffs vs current ad set"
        return out

    if dry_run:
        preview = {k: _serialize_adset_post_value(k, v) for k, v in post_payload.items()}
        return {
            "success": True,
            "dry_run": True,
            "would_apply": preview,
            "changes_applied": list(changes_applied),
            "skipped": skipped,
            "force_update": force_update,
        }

    resp = update_adset_from_payload(aid, token, post_payload)
    if "error" in resp:
        return resp
    after = get_marketing_object(aid, token, _ADSET_FIELDS_FOR_DIFF)
    meta_diff = None
    if "error" not in after:
        meta_diff = _build_adset_meta_diff(before_snapshot, post_payload, after)
        try:
            pl = (meta_diff.get("placements") or {})
            if pl.get("requested") is not None and pl.get("requested_matches_actual") is False:
                logger.info(
                    "facebook_ads_mcp adset update: placements requested != actual after POST adset_id=%s meta_diff=%s",
                    aid,
                    json.dumps(pl, default=str)[:2000],
                )
        except (TypeError, ValueError):
            pass
    resp["success"] = True
    resp["changes_applied"] = list(changes_applied)
    resp["skipped"] = skipped
    resp["force_update"] = force_update
    if meta_diff is not None:
        resp["meta_diff"] = meta_diff
    if skipped:
        resp["reason"] = "Some fields were skipped before update (see skipped)"
    return resp


def attach_pixel_to_adset(adset_id, token, pixel_id, custom_event_type=None, page_id=None):
    """Merge pixel into ad set promoted_object when Meta allows it. Skips API call for POST_ENGAGEMENT + ON_POST."""
    if not pixel_id or not str(pixel_id).strip():
        return {"error": {"message": "pixel_id is required"}}
    cur = get_marketing_object(
        str(adset_id).strip(),
        token,
        "promoted_object,optimization_goal,destination_type",
    )
    if "error" in cur:
        return cur
    if is_post_engagement_on_post(cur.get("optimization_goal"), cur.get("destination_type")):
        rsn = "Not allowed for POST_ENGAGEMENT + ON_POST engagement ad sets"
        return {
            "success": True,
            "message": "No changes needed",
            "changes_applied": [],
            "skipped": [
                _normalize_skipped_entry("pixel_id", rsn),
                _normalize_skipped_entry("custom_event_type", rsn),
            ],
            "reason": rsn,
            "_pixel_not_attached": True,
        }
    po = cur.get("promoted_object")
    if not isinstance(po, dict):
        po = {}
    else:
        po = dict(po)
    po["pixel_id"] = str(pixel_id).strip()
    if custom_event_type and str(custom_event_type).strip():
        raw_evt = str(custom_event_type).strip()
        po["custom_event_type"] = PIXEL_EVENT_TYPE_MAP.get(raw_evt, raw_evt.upper())
    if page_id and str(page_id).strip():
        po["page_id"] = str(page_id).strip()
    return update_adset_with_smart_diff(
        str(adset_id).strip(),
        token,
        {"promoted_object": po},
        merged_targeting=None,
        dry_run=False,
    )


def verify_ad_setup(token, campaign_id=None, adset_id=None, ad_id=None, expected=None):
    """Fetch campaign, ad set, ad (and creative when possible) and report gaps vs optional expected dict (pixel_id, destination_type, has_link, uses_existing_post, etc.)."""
    expected = expected if isinstance(expected, dict) else {}
    issues = []
    fetched = {}
    if campaign_id:
        c = get_marketing_object(campaign_id, token, "id,name,status,objective,daily_budget,lifetime_budget,bid_strategy")
        fetched["campaign"] = c
        if "error" in c:
            issues.append({"level": "campaign", "message": c.get("error", {}).get("message", str(c.get("error")))})
    if adset_id:
        a = get_marketing_object(
            adset_id, token,
            "id,name,status,campaign_id,targeting,promoted_object,optimization_goal,destination_type,daily_budget,lifetime_budget,billing_event,bid_strategy,start_time,end_time",
        )
        fetched["adset"] = a
        if "error" in a:
            issues.append({"level": "adset", "message": a.get("error", {}).get("message", str(a.get("error")))})
        else:
            exp_pix = expected.get("pixel_id")
            if exp_pix:
                po = a.get("promoted_object") or {}
                if str(po.get("pixel_id", "")).strip() != str(exp_pix).strip():
                    issues.append({"level": "adset", "message": "promoted_object.pixel_id missing or mismatch", "expected_pixel_id": exp_pix, "got": po.get("pixel_id")})
            exp_dest = expected.get("destination_type")
            if exp_dest and str(a.get("destination_type") or "").upper() != str(exp_dest).strip().upper():
                issues.append({"level": "adset", "message": "destination_type mismatch", "expected": exp_dest, "got": a.get("destination_type")})
            targ = a.get("targeting")
            if expected.get("require_targeting") and (not targ or not isinstance(targ, dict) or not any(targ.get(k) for k in ("geo_locations", "flexible_spec", "custom_audiences"))):
                issues.append({"level": "adset", "message": "targeting appears empty or missing geo/flexible/custom"})
    if ad_id:
        ad = get_marketing_object(ad_id, token, "id,name,status,adset_id,creative{id,object_story_id,object_story_spec,name}")
        fetched["ad"] = ad
        if "error" in ad:
            issues.append({"level": "ad", "message": ad.get("error", {}).get("message", str(ad.get("error")))})
        else:
            cr = ad.get("creative") or {}
            has_story = bool(cr.get("object_story_id"))
            spec = cr.get("object_story_spec")
            if isinstance(spec, str):
                try:
                    spec = json.loads(spec)
                except (TypeError, ValueError):
                    spec = None
            link_data = (spec or {}).get("link_data") if isinstance(spec, dict) else None
            has_link = bool(link_data and link_data.get("link"))
            if expected.get("expect_existing_post") and not has_story:
                issues.append({"level": "creative", "message": "Expected existing post creative (object_story_id); not found on creative"})
            if expected.get("expect_link_ad") and not has_link and not has_story:
                issues.append({"level": "creative", "message": "Expected link ad (link_data.link); missing"})
            cta = (link_data or {}).get("call_to_action") or {}
            if (
                expected.get("cta_type")
                and not (expected.get("expect_existing_post") and has_story)
                and str(cta.get("type", "")).upper() != str(expected["cta_type"]).strip().upper()
            ):
                issues.append({"level": "creative", "message": "CTA type mismatch", "expected": expected.get("cta_type"), "got": cta.get("type")})
    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "fetched": fetched,
    }


def pause_adset(adset_id, token):
    """Set ad set status to PAUSED."""
    return update_adset(adset_id, token, status="PAUSED")


def resume_adset(adset_id, token):
    """Set ad set status to ACTIVE."""
    return update_adset(adset_id, token, status="ACTIVE")


def delete_adset(adset_id, token):
    """Delete an ad set. Real API: DELETE /{adset_id}. Requires ads_management."""
    if not adset_id or not str(adset_id).strip():
        return {"error": {"message": "adset_id is required"}}
    url = f"{BASE_URL}/{str(adset_id).strip()}"
    return _delete(url, {"access_token": token})


def update_budget(adset_id, token, daily_budget=None, lifetime_budget=None):
    """Update ad set budget. Real API: POST /{adset_id} with daily_budget or lifetime_budget (cents)."""
    return update_adset(adset_id, token, daily_budget=daily_budget, lifetime_budget=lifetime_budget)


def get_ads(account_id, token, fields=None, limit=100, filtering=None):
    """List ads for an ad account. Real API: GET /act_{id}/ads."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/ads"
    if fields is None:
        fields = "id,name,status,creative{id,name},adset_id,campaign_id,created_time"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {
        "fields": fields,
        "limit": limit,
        "access_token": token,
    }
    if filtering:
        params["filtering"] = filtering if isinstance(filtering, str) else str(filtering)
    return _get(url, params)


def create_ad(account_id, token, adset_id, name, creative_id=None, status="PAUSED", tracking_specs=None):
    """Create an ad. Real API: POST /act_{id}/ads. Requires ads_management. Provide creative_id (existing creative).

    ``tracking_specs`` is an optional list (or JSON string) of action specs Meta
    fires on this ad — typically a pixel + action.type pair for funnel
    attribution separate from the ad set's optimization goal.
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    if not adset_id or not name:
        return {"error": {"message": "adset_id and name are required"}}
    if not creative_id:
        return {"error": {"message": "creative_id is required"}}
    url = f"{BASE_URL}/{act}/ads"
    data = {"access_token": token, "adset_id": str(adset_id).strip(), "name": name, "creative": json.dumps({"creative_id": str(creative_id)}), "status": status or "PAUSED"}
    if tracking_specs:
        data["tracking_specs"] = (
            tracking_specs if isinstance(tracking_specs, str) else json.dumps(tracking_specs)
        )
    return _post_with_rate_limit_retry(url, data)


def update_ad(ad_id, token, name=None, status=None, adset_id=None, creative_id=None, tracking_specs=None):
    """Update an ad. Real API: POST /{ad_id}. Requires ads_management. Pass creative_id to switch to another creative (e.g. with Instagram)."""
    if not ad_id or not str(ad_id).strip():
        return {"error": {"message": "ad_id is required"}}
    url = f"{BASE_URL}/{str(ad_id).strip()}"
    data = {"access_token": token}
    if name is not None:
        data["name"] = name
    if status is not None:
        data["status"] = status
    if adset_id is not None:
        data["adset_id"] = str(adset_id)
    if creative_id is not None:
        data["creative"] = json.dumps({"creative_id": str(creative_id).strip()})
    if tracking_specs is not None:
        data["tracking_specs"] = tracking_specs if isinstance(tracking_specs, str) else json.dumps(tracking_specs)
    if len(data) == 1:
        return {"error": {"message": "Provide at least one field to update"}}
    return _post(url, data)


_AD_FIELDS_FOR_DIFF = "id,name,status,adset_id,creative{id},tracking_specs"


def update_ad_from_payload(ad_id, token, payload: dict):
    """POST only keys in payload (name, status, adset_id, creative JSON string, tracking_specs JSON string)."""
    if not ad_id or not str(ad_id).strip():
        return {"error": {"message": "ad_id is required"}}
    if not payload:
        return {"error": {"message": "Provide at least one field to update"}}
    url = f"{BASE_URL}/{str(ad_id).strip()}"
    data = {"access_token": token}
    if "name" in payload:
        data["name"] = str(payload["name"])
    if "status" in payload:
        data["status"] = str(payload["status"])
    if "adset_id" in payload:
        data["adset_id"] = str(payload["adset_id"])
    if "creative" in payload:
        c = payload["creative"]
        data["creative"] = c if isinstance(c, str) else json.dumps(c)
    if "tracking_specs" in payload:
        ts = payload["tracking_specs"]
        data["tracking_specs"] = ts if isinstance(ts, str) else json.dumps(ts)
    if len(data) == 1:
        return {"error": {"message": "Provide at least one field to update"}}
    return _post(url, data)


def _build_ad_meta_diff(before_ad: dict, post_payload: dict, after_ad: dict) -> dict:
    """Before vs requested POST vs after refetch for ad-level fields."""
    meta_diff = {}
    if "tracking_specs" in post_payload:
        req = post_payload.get("tracking_specs")
        act = after_ad.get("tracking_specs")
        meta_diff["tracking_specs"] = {
            "before": before_ad.get("tracking_specs"),
            "requested": req,
            "actual": act,
            "requested_matches_actual": _canonical_json(req) == _canonical_json(act),
        }
        if not meta_diff["tracking_specs"]["requested_matches_actual"]:
            logger.info(
                "facebook_ads_mcp ad update: tracking_specs requested != actual after POST ad_id=%s",
                before_ad.get("id", ""),
            )
    other_keys = [k for k in post_payload if k not in ("tracking_specs", "creative")]
    snap = {}
    for key in other_keys:
        b, a = before_ad.get(key), after_ad.get(key)
        snap[key] = {
            "requested_in_post": post_payload.get(key),
            "before": b,
            "after": a,
            "unchanged_after_refresh": str(b or "") == str(a or ""),
        }
    if "creative" in post_payload:
        old_c = (before_ad.get("creative") or {}).get("id")
        new_c = (after_ad.get("creative") or {}).get("id")
        snap["creative_id"] = {
            "requested_in_post": post_payload.get("creative"),
            "before_creative_id": old_c,
            "after_creative_id": new_c,
            "unchanged_after_refresh": str(old_c or "") == str(new_c or ""),
        }
    if snap:
        meta_diff["fields"] = snap
    return meta_diff


def update_ad_with_smart_diff(
    ad_id,
    token,
    explicit_request: dict,
    *,
    dry_run: bool = False,
    force_update: bool = False,
):
    """
    Fetch current ad, diff explicit fields (name, status, adset_id, creative_id, tracking_specs), POST only changes.
    force_update: send all explicit fields even if equal to current (Meta refresh).
    """
    aid = str(ad_id).strip()
    if not aid:
        return {"error": {"message": "ad_id is required"}}
    cur = get_marketing_object(aid, token, _AD_FIELDS_FOR_DIFF)
    if "error" in cur:
        return cur
    before_ad = dict(cur)

    post_payload = {}
    if force_update:
        if "name" in explicit_request:
            post_payload["name"] = str(explicit_request["name"])
        if "status" in explicit_request:
            post_payload["status"] = explicit_request["status"]
        if "adset_id" in explicit_request:
            post_payload["adset_id"] = str(explicit_request["adset_id"]).strip()
        if "creative_id" in explicit_request:
            new_crid = str(explicit_request["creative_id"]).strip()
            if new_crid:
                post_payload["creative"] = json.dumps({"creative_id": new_crid})
        if "tracking_specs" in explicit_request and explicit_request["tracking_specs"] is not None:
            ts = explicit_request["tracking_specs"]
            if isinstance(ts, str) and ts.strip():
                ts = _parse_json_if_string(ts)
            post_payload["tracking_specs"] = ts
    else:
        if "name" in explicit_request:
            nv = str(explicit_request["name"])
            if str(cur.get("name") or "").strip() != nv.strip():
                post_payload["name"] = nv
        if "status" in explicit_request:
            sv = str(explicit_request["status"]).strip().upper()
            if str(cur.get("status") or "").strip().upper() != sv:
                post_payload["status"] = explicit_request["status"]
        if "adset_id" in explicit_request:
            av = str(explicit_request["adset_id"]).strip()
            if str(cur.get("adset_id") or "").strip() != av:
                post_payload["adset_id"] = av
        if "creative_id" in explicit_request:
            new_crid = str(explicit_request["creative_id"]).strip()
            old_crid = str((cur.get("creative") or {}).get("id") or "").strip()
            if new_crid and new_crid != old_crid:
                post_payload["creative"] = json.dumps({"creative_id": new_crid})
        if "tracking_specs" in explicit_request and explicit_request["tracking_specs"] is not None:
            ts = explicit_request["tracking_specs"]
            if isinstance(ts, str) and ts.strip():
                ts = _parse_json_if_string(ts)
            cur_ts = cur.get("tracking_specs")
            if _canonical_json(cur_ts) != _canonical_json(ts):
                post_payload["tracking_specs"] = ts

    changes_applied = []
    for k in post_payload:
        changes_applied.append("creative_id" if k == "creative" else k)

    if not post_payload:
        return {
            "success": True,
            "message": "No changes needed",
            "changes_applied": [],
            "skipped": [],
            "force_update": force_update,
        }
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "would_apply": dict(post_payload),
            "changes_applied": list(changes_applied),
            "skipped": [],
            "force_update": force_update,
        }
    resp = update_ad_from_payload(aid, token, post_payload)
    if "error" in resp:
        return resp
    after = get_marketing_object(aid, token, _AD_FIELDS_FOR_DIFF)
    meta_diff = None
    if "error" not in after:
        meta_diff = _build_ad_meta_diff(before_ad, post_payload, after)
    resp["success"] = True
    resp["changes_applied"] = list(changes_applied)
    resp["skipped"] = []
    resp["force_update"] = force_update
    if meta_diff is not None:
        resp["meta_diff"] = meta_diff
    return resp


def pause_ad(ad_id, token):
    """Set ad status to PAUSED."""
    return update_ad(ad_id, token, status="PAUSED")


def resume_ad(ad_id, token):
    """Set ad status to ACTIVE."""
    return update_ad(ad_id, token, status="ACTIVE")


def delete_ad(ad_id, token):
    """Delete an ad. Real API: DELETE /{ad_id}. Requires ads_management."""
    if not ad_id or not str(ad_id).strip():
        return {"error": {"message": "ad_id is required"}}
    url = f"{BASE_URL}/{str(ad_id).strip()}"
    return _delete(url, {"access_token": token})


def get_ad_creatives(account_id, token, fields=None, limit=100):
    """List creatives for an ad account. Real API: GET /act_{id}/adcreatives."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/adcreatives"
    if fields is None:
        fields = "id,name,object_story_spec,effective_object_story_id"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": fields, "limit": limit}
    return _get(url, params)


def get_spend(account_id, token, date_preset="last_30d", time_start=None, time_end=None):
    """Get spend summary for an ad account (from insights). Use date_preset OR time_start+time_end (YYYY-MM-DD)."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/insights"
    params = {
        "fields": "spend,impressions,clicks",
        "access_token": token,
    }
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset
    data = _get(url, params)
    if "data" in data and data["data"]:
        total_spend = 0
        total_impressions = 0
        total_clicks = 0
        for row in data["data"]:
            total_spend += float(row.get("spend") or 0)
            total_impressions += int(row.get("impressions") or 0)
            total_clicks += int(row.get("clicks") or 0)
        out = {
            "account_id": act,
            "spend": round(total_spend, 2),
            "impressions": total_impressions,
            "clicks": total_clicks,
            "raw_data": data.get("data"),
        }
        if time_start and time_end:
            out["time_range"] = {"since": time_start, "until": time_end}
        else:
            out["date_preset"] = date_preset
        return out
    return data


# --- Reporting (performance by level) ---
def get_campaign_performance(account_id, token, date_preset="last_30d", time_start=None, time_end=None, limit=100, breakdowns=None):
    """Get campaign-level performance (insights broken down by campaign). Real API: GET /act_{id}/insights with level=campaign."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/insights"
    params = {"access_token": token, "level": "campaign", "fields": "campaign_id,campaign_name,impressions,clicks,spend,cpc,cpm,ctr", "limit": min(max(1, int(limit)), MAX_LIMIT)}
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset
    _apply_breakdowns(params, breakdowns)
    return _get(url, params)


def get_adset_performance(account_id, token, date_preset="last_30d", time_start=None, time_end=None, limit=100, breakdowns=None):
    """Get ad set-level performance. Real API: GET /act_{id}/insights with level=adset."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/insights"
    params = {"access_token": token, "level": "adset", "fields": "adset_id,adset_name,impressions,clicks,spend,cpc,cpm,ctr", "limit": min(max(1, int(limit)), MAX_LIMIT)}
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset
    _apply_breakdowns(params, breakdowns)
    return _get(url, params)


def get_ad_performance(account_id, token, date_preset="last_30d", time_start=None, time_end=None, limit=100, breakdowns=None):
    """Get ad-level performance. Real API: GET /act_{id}/insights with level=ad."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/insights"
    params = {"access_token": token, "level": "ad", "fields": "ad_id,ad_name,impressions,clicks,spend,cpc,cpm,ctr", "limit": min(max(1, int(limit)), MAX_LIMIT)}
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset
    _apply_breakdowns(params, breakdowns)
    return _get(url, params)


def get_ad_level_data_from_all_active_accounts(token, date_preset="last_30d", time_start=None, time_end=None, limit_per_account=100):
    """
    Get ad-level performance data from all active ad accounts (account_status == 1).
    Returns a list of ad rows with account_id, account_name, ad_id, ad_name, impressions, clicks, spend, cpc, cpm, ctr.
    Uses get_all_ad_accounts_from_facebook then get_ad_performance for each active account.
    """
    r = get_all_ad_accounts_from_facebook(token, limit=500)
    if "error" in r:
        return r
    accounts = r.get("ad_accounts") or []
    active = [a for a in accounts if a.get("account_status") == 1]
    if not active:
        return {"source": "facebook", "active_accounts": 0, "ad_level_data": [], "message": "No active ad accounts found."}
    all_ads = []
    for acc in active:
        account_id = acc.get("id") or acc.get("account_id")
        account_name = acc.get("name") or ""
        if not account_id:
            continue
        perf = get_ad_performance(
            account_id, token,
            date_preset=date_preset,
            time_start=time_start,
            time_end=time_end,
            limit=limit_per_account,
        )
        if "error" in perf:
            all_ads.append({"account_id": account_id, "account_name": account_name, "error": perf["error"].get("message", perf)})
            continue
        rows = perf.get("data") or []
        for row in rows:
            all_ads.append({
                "account_id": account_id,
                "account_name": account_name,
                "ad_id": row.get("ad_id"),
                "ad_name": row.get("ad_name"),
                "impressions": row.get("impressions"),
                "clicks": row.get("clicks"),
                "spend": row.get("spend"),
                "cpc": row.get("cpc"),
                "cpm": row.get("cpm"),
                "ctr": row.get("ctr"),
            })
    return {
        "source": "facebook",
        "active_accounts": len(active),
        "date_preset": date_preset,
        "time_range": {"since": time_start, "until": time_end} if (time_start and time_end) else None,
        "total_ads": len(all_ads),
        "ad_level_data": all_ads,
    }


def get_spend_report(account_id, token, date_preset="last_30d", time_start=None, time_end=None):
    """Get spend report (same as get_spend). Use date_preset OR time_start+time_end (YYYY-MM-DD)."""
    return get_spend(account_id, token, date_preset=date_preset, time_start=time_start, time_end=time_end)


def get_clicks_impressions(account_id, token, date_preset="last_30d", time_start=None, time_end=None):
    """Get clicks and impressions summary for an ad account. Real API: GET /act_{id}/insights."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/insights"
    params = {"access_token": token, "fields": "impressions,clicks"}
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset
    data = _get(url, params)
    if "data" in data and data["data"]:
        total_impressions = sum(int(row.get("impressions") or 0) for row in data["data"])
        total_clicks = sum(int(row.get("clicks") or 0) for row in data["data"])
        out = {"account_id": act, "impressions": total_impressions, "clicks": total_clicks}
        if time_start and time_end:
            out["time_range"] = {"since": time_start, "until": time_end}
        else:
            out["date_preset"] = date_preset
        return out
    return data


# --- Creative & Media ---
def _decode_base64_image(image_base64: str) -> bytes | None:
    """Decode data-URL or raw base64 PNG/JPEG bytes. Returns None on failure.

    Tolerant: strips data-URL prefix, whitespace, URL-safe chars, and re-pads
    to a multiple of 4 (the most common reason agent-pasted base64 fails to
    decode is truncated / missing trailing '=' padding).
    """
    s = (image_base64 or "").strip()
    if not s:
        return None
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1].strip()
    # Strip any whitespace / newlines that survived JSON serialization.
    s = "".join(s.split())
    # Translate URL-safe base64 (- _) to standard alphabet (+ /).
    s = s.replace("-", "+").replace("_", "/")
    # Re-pad to multiple of 4. Missing padding is the most common LLM mistake.
    rem = len(s) % 4
    if rem:
        s = s + ("=" * (4 - rem))
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        try:
            return base64.b64decode(s)
        except (binascii.Error, ValueError):
            return None


def _validate_image_bytes(raw: bytes, source_label: str = "image") -> Optional[Dict[str, Any]]:
    """Reject obviously-corrupt or truncated image payloads BEFORE sending to Meta.

    Meta's /adimages rejects with subcode 2446496 ("Invalid image format") when
    bytes are corrupt or truncated. The agent (especially LLM tool calls) is
    prone to truncating long base64 strings — the symptom in production was a
    57-byte payload pasted as image_base64. This guard fails fast with a clear
    message so the caller (agent) knows to re-attach the image.

    Returns None if bytes look valid, else a structured error dict.
    """
    if not raw:
        return {"error": {"message": f"Decoded {source_label} is empty",
                          "type": "image_validation_error"}}
    n = len(raw)
    # Real ad creatives are at minimum a few KB. Anything under 1 KB is
    # essentially guaranteed to be a truncated base64 string or a placeholder.
    if n < 1024:
        return {"error": {
            "message": (
                f"Decoded {source_label} is only {n} bytes — likely a truncated "
                "base64 payload. Meta requires a real image (≥1 KB). Re-attach "
                "the original image and try again."
            ),
            "type": "image_validation_error",
            "size": n,
        }}
    # Magic-byte sniff. Meta accepts JPEG, PNG, GIF, BMP, WebP.
    head = raw[:12]
    is_jpeg = head[:3] == b"\xff\xd8\xff"
    is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
    is_gif = head[:6] in (b"GIF87a", b"GIF89a")
    is_bmp = head[:2] == b"BM"
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (is_jpeg or is_png or is_gif or is_bmp or is_webp):
        return {"error": {
            "message": (
                f"Decoded {source_label} is not a recognized image format "
                f"(first bytes: {head[:8].hex()}). Meta accepts JPEG, PNG, GIF, "
                "BMP, or WebP. The base64 was likely truncated or the URL did "
                "not point to an image."
            ),
            "type": "image_validation_error",
            "first_bytes_hex": head[:8].hex(),
            "size": n,
        }}
    return None


def upload_image(account_id, token, url_or_bytes=None, filename=None, image_base64=None):
    """Upload an image to the ad account. Real API: POST /act_{id}/adimages.
    Pass public image URL (url_or_bytes), or image_base64 (raw or data-URL) for multipart upload."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    api_url = f"{BASE_URL}/{act}/adimages"
    if image_base64:
        raw = _decode_base64_image(image_base64)
        if not raw:
            in_len = len(image_base64) if isinstance(image_base64, str) else 0
            preview = (image_base64[:60] + "…") if isinstance(image_base64, str) and in_len > 60 else (image_base64 or "")
            return {"error": {
                "message": (
                    f"image_base64 could not be decoded (input length={in_len}, "
                    f"starts with: {preview!r}). The string is not valid base64 — "
                    "common causes: truncated/cut-off bytes, double JSON-escaping, "
                    "or non-base64 characters mixed in. Re-attach the image so the "
                    "chat backend can re-encode the full bytes, or paste a public "
                    "URL via the `url` field instead."
                ),
                "type": "image_base64_decode_error",
                "input_length": in_len,
            }}
        if len(raw) > 30 * 1024 * 1024:
            return {"error": {"message": "image_base64 exceeds 30MB limit"}}
        bad = _validate_image_bytes(raw, source_label="image_base64")
        if bad:
            return bad
        fname = (filename or "creative.png").strip() or "creative.png"
        if "." not in fname:
            fname = fname + ".png"
        mime = "image/jpeg" if fname.lower().endswith((".jpg", ".jpeg")) else "image/png"
        files = {"filename": (fname, raw, mime)}
        return _post_multipart(api_url, {"access_token": token}, files)
    if not url_or_bytes:
        return {"error": {"message": "Provide url (public image URL) or image_base64"}}
    if not (url_or_bytes.startswith("http://") or url_or_bytes.startswith("https://")):
        return {"error": {"message": "url_or_bytes must be an image URL (http/https), or pass image_base64 for upload."}}
    # Meta's /act_X/adimages endpoint does NOT accept a `url` form field — it only
    # accepts multipart bytes. Earlier we passed the URL through and Meta rejected
    # with `(#3) Application does not have the capability to make this API call.`
    # Fix: fetch the bytes ourselves, then route through the existing multipart path.
    # Fetch through the shared SSRF-safe fetcher: public destinations only,
    # pinned connection, every redirect hop re-validated, hard byte/time caps,
    # image/* Content-Type enforced before the body is read.
    try:
        from django.conf import settings as _settings
        _max = int(getattr(_settings, "FACEBOOK_MEDIA_MAX_BYTES", 30 * 1024 * 1024))
        _tmo = float(getattr(_settings, "FACEBOOK_MEDIA_FETCH_TIMEOUT", 20))
    except Exception:  # noqa: BLE001
        _max, _tmo = 30 * 1024 * 1024, 20.0
    try:
        from app.tenant_http import fetch_public_bytes, UnsafeURLError
        raw, _ctype, _final = fetch_public_bytes(
            url_or_bytes, max_bytes=_max, timeout=_tmo, content_types=("image/",),
        )
    except UnsafeURLError as exc:
        return {"error": {"message": f"Image URL rejected: {exc}",
                          "type": "image_fetch_error", "url": url_or_bytes}}
    except ValueError as exc:
        msg = str(exc)
        if "Content-Type" in msg:
            return {"error": {
                "message": (
                    f"URL did not return an image ({msg}). Meta accepts JPEG, PNG, GIF, BMP, "
                    "or WebP. If this URL is a workspace/share page, open it in a browser, "
                    "right-click the image, and use 'Copy image address' to get a direct image URL."
                ),
                "type": "image_validation_error", "url": url_or_bytes,
            }}
        return {"error": {"message": f"Could not fetch image URL: {msg}",
                          "type": "image_fetch_error", "url": url_or_bytes}}
    except Exception as exc:  # noqa: BLE001
        return {"error": {"message": f"Could not fetch image URL: {exc}",
                          "type": "image_fetch_error", "url": url_or_bytes}}
    if not raw:
        return {"error": {"message": "Fetched image URL returned empty body",
                          "type": "image_fetch_error", "url": url_or_bytes}}
    if len(raw) > 30 * 1024 * 1024:
        return {"error": {"message": "Fetched image exceeds 30MB limit",
                          "type": "image_fetch_error", "url": url_or_bytes}}
    bad = _validate_image_bytes(raw, source_label=f"image fetched from {url_or_bytes}")
    if bad:
        return bad
    fname = filename or url_or_bytes.rsplit("/", 1)[-1].split("?", 1)[0] or "upload.png"
    if "." not in fname:
        fname = fname + ".png"
    mime = _ctype or "image/jpeg"
    files = {"filename": (fname, raw, mime)}
    return _post_multipart(api_url, {"access_token": token}, files)


def check_image_hash_in_account(account_id, token, image_hash):
    """Check whether `image_hash` exists in `account_id`'s adimages library.

    Image hashes are account-scoped on Meta — a hash uploaded to act_A is
    invisible to act_B even when both accounts belong to the same user. This
    pre-flight guard catches the cross-account mismatch before we create a
    creative that would otherwise fail to deliver with "Image not found" in
    Ads Manager.

    Returns a dict: {"found": bool, "known": bool, "raw": <api response>}.
    `known` is False when Meta returns an error (e.g. token lacks ads_read on
    this account); callers should treat unknown as "proceed, do not block."
    """
    act = _account_id(account_id)
    if not act:
        return {"found": False, "known": False,
                "raw": {"error": {"message": "account_id is required"}}}
    if not image_hash or not str(image_hash).strip():
        return {"found": False, "known": False,
                "raw": {"error": {"message": "image_hash is required"}}}
    h = str(image_hash).strip()
    api_url = f"{BASE_URL}/{act}/adimages"
    params = {
        "access_token": token,
        "hashes": json.dumps([h]),
        "fields": "hash",
    }
    resp = _get(api_url, params)
    if isinstance(resp, dict) and "error" in resp:
        return {"found": False, "known": False, "raw": resp}
    data = resp.get("data") if isinstance(resp, dict) else None
    if not isinstance(data, list):
        return {"found": False, "known": False, "raw": resp}
    found = any(
        isinstance(it, dict) and str(it.get("hash") or "").strip() == h
        for it in data
    )
    return {"found": found, "known": True, "raw": resp}


def check_page_in_account(account_id, token, page_id):
    """Check whether `page_id` is bound to `account_id` for ad creation.

    Pages are account-scoped on Meta in the same way image hashes are: a page
    connected to ad account A cannot be used as `promoted_object.page_id` on
    ad account B, even when both belong to the same user. The adset POST
    fails with "(#100) Param promoted_object[page_id] must be a valid page ID"
    after we've already created the campaign, forcing a rollback. This guard
    catches the mismatch before any state is created.

    Detection is tiered - the Meta endpoints don't all see the same set of
    pages, especially in partner / agency setups where one Business shares
    pages with another:

      Tier 1: GET /{act}/promote_pages   - owner-assigned pages (fast happy path).
      Tier 2: GET /{act}/client_pages    - partner-shared pages (multi-business setups).
      Tier 3: GET /{page_id}?fields=ad_accounts{id} - cross-check from the page side.
      Tier 4: any Graph error => known=False (let caller proceed; Meta is final arbiter).

    Returns {"found": bool, "known": bool, "available_pages": list,
             "via": str|None, "tier_errors": dict, "raw": ...}.

    ``via`` is the tier that matched (``"promote_pages"`` / ``"client_pages"`` /
    ``"page_assigned"``) or None when not found in any tier. ``tier_errors``
    maps tier name to the Graph error message for diagnostics.

    ``known`` is False when ANY tier errored (e.g. token lacks
    `pages_show_list` / `business_management` on this account); callers should
    treat unknown as "proceed, do not block." When all three tiers complete
    cleanly with no match, ``known=True`` and the caller may surface it as a
    soft-warn (default) or hard-block (with ``options.strict_page_account_check``).
    """
    act = _account_id(account_id)
    if not act:
        return {"found": False, "known": False, "available_pages": [],
                "via": None, "tier_errors": {},
                "raw": {"error": {"message": "account_id is required"}}}
    if not page_id or not str(page_id).strip():
        return {"found": False, "known": False, "available_pages": [],
                "via": None, "tier_errors": {},
                "raw": {"error": {"message": "page_id is required"}}}
    pid = str(page_id).strip()

    tier_errors: Dict[str, str] = {}
    merged: List[Dict[str, str]] = []
    merged_ids: set = set()

    def _merge_rows(rows):
        if not isinstance(rows, list):
            return []
        out = []
        for it in rows:
            if not isinstance(it, dict):
                continue
            this_id = str(it.get("id") or "").strip()
            if not this_id:
                continue
            entry = {"id": this_id, "name": str(it.get("name") or "").strip()}
            out.append(entry)
            if this_id not in merged_ids:
                merged_ids.add(this_id)
                merged.append(entry)
        return out

    def _err_message(resp) -> str:
        try:
            return str((resp or {}).get("error", {}).get("message") or "error")
        except Exception:
            return "error"

    raw_dump: Dict[str, Any] = {}

    # Tier 1 - owner-assigned pages.
    api_url = f"{BASE_URL}/{act}/promote_pages"
    params = {"access_token": token, "fields": "id,name", "limit": 200}
    resp1 = _get(api_url, params)
    raw_dump["promote_pages"] = resp1
    if isinstance(resp1, dict) and "error" in resp1:
        tier_errors["promote_pages"] = _err_message(resp1)
    else:
        rows1 = resp1.get("data") if isinstance(resp1, dict) else None
        page_list1 = _merge_rows(rows1)
        if any(p["id"] == pid for p in page_list1):
            return {
                "found": True, "known": True,
                "available_pages": merged, "via": "promote_pages",
                "tier_errors": tier_errors, "raw": raw_dump,
            }

    # Tier 2 - partner-shared / client pages.
    api_url2 = f"{BASE_URL}/{act}/client_pages"
    resp2 = _get(api_url2, {"access_token": token, "fields": "id,name", "limit": 200})
    raw_dump["client_pages"] = resp2
    if isinstance(resp2, dict) and "error" in resp2:
        tier_errors["client_pages"] = _err_message(resp2)
    else:
        rows2 = resp2.get("data") if isinstance(resp2, dict) else None
        page_list2 = _merge_rows(rows2)
        if any(p["id"] == pid for p in page_list2):
            return {
                "found": True, "known": True,
                "available_pages": merged, "via": "client_pages",
                "tier_errors": tier_errors, "raw": raw_dump,
            }

    # Tier 3 - page-side reverse lookup. GET /{pid}?fields=ad_accounts{id}
    # asks the Page which ad accounts have been assigned to it. Tolerant of
    # error (this endpoint is documented but can be flaky / requires extra
    # scopes on some setups).
    api_url3 = f"{BASE_URL}/{pid}"
    resp3 = _get(api_url3, {
        "access_token": token,
        "fields": "ad_accounts.limit(200){id,account_id}",
    })
    raw_dump["page_assigned"] = resp3
    page_assigned_accounts: List[str] = []
    if isinstance(resp3, dict) and "error" in resp3:
        tier_errors["page_assigned"] = _err_message(resp3)
    else:
        aa = (resp3 or {}).get("ad_accounts") if isinstance(resp3, dict) else None
        rows3 = aa.get("data") if isinstance(aa, dict) else None
        if isinstance(rows3, list):
            for it in rows3:
                if not isinstance(it, dict):
                    continue
                # The endpoint returns ids as "act_XXX" or numeric account_id.
                aid_raw = str(it.get("id") or "").strip()
                aid_num = str(it.get("account_id") or "").strip()
                if aid_raw:
                    page_assigned_accounts.append(aid_raw)
                    if aid_raw.startswith("act_"):
                        page_assigned_accounts.append(aid_raw[4:])
                    else:
                        page_assigned_accounts.append(f"act_{aid_raw}")
                if aid_num:
                    page_assigned_accounts.append(aid_num)
                    page_assigned_accounts.append(f"act_{aid_num}")
        # Normalize the input account_id for comparison.
        act_norm = act
        act_bare = act[4:] if act.startswith("act_") else act
        check_set = {act_norm, act_bare, f"act_{act_bare}"}
        if any(x in check_set for x in page_assigned_accounts):
            return {
                "found": True, "known": True,
                "available_pages": merged, "via": "page_assigned",
                "tier_errors": tier_errors, "raw": raw_dump,
            }

    # No tier matched. Known only if all three succeeded.
    all_succeeded = (
        "promote_pages" not in tier_errors
        and "client_pages" not in tier_errors
        and "page_assigned" not in tier_errors
    )
    return {
        "found": False,
        "known": bool(all_succeeded),
        "available_pages": merged,
        "via": None,
        "tier_errors": tier_errors,
        "raw": raw_dump,
    }


def resolve_account_for_page(page_id, token):
    """Given a page_id, find the first active ad account in the user's
    portfolios where this page is bound (using the same 3-tier check as
    ``check_page_in_account``).

    Returns either::

        {
          "ok": True,
          "page_id": str,
          "account_id": "act_...",
          "account_name": str,
          "via": "promote_pages" | "client_pages" | "page_assigned",
          "tried_accounts": [{"id": str, "name": str, "checked": bool, "found": bool}],
        }

    or::

        {
          "ok": False,
          "error": str,
          "tried_accounts": [...],
        }

    Stops at the first match (no wasted API calls on the remaining accounts).
    """
    pid = str(page_id or "").strip()
    if not pid:
        return {"ok": False, "error": "page_id is required", "tried_accounts": []}
    if not token:
        return {"ok": False, "error": "token is required", "tried_accounts": []}

    accounts_resp = get_ad_accounts(token, limit=500)
    if isinstance(accounts_resp, dict) and accounts_resp.get("error"):
        return {
            "ok": False,
            "error": f"Could not list ad accounts: {(accounts_resp.get('error') or {}).get('message') or 'error'}",
            "tried_accounts": [],
        }
    rows = accounts_resp.get("data") if isinstance(accounts_resp, dict) else None
    if not isinstance(rows, list):
        rows = []
    # Status 1 == ACTIVE per Meta's account_status enum. Accept rows without
    # the field (some Business Manager flavours omit it).
    active = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        st = r.get("account_status")
        try:
            st_i = int(st) if st is not None else None
        except (TypeError, ValueError):
            st_i = None
        if st_i is None or st_i == 1:
            active.append(r)
    tried: List[Dict[str, Any]] = []
    for acc in active:
        aid = str(acc.get("id") or "").strip()
        aname = str(acc.get("name") or "").strip()
        if not aid:
            continue
        entry: Dict[str, Any] = {"id": aid, "name": aname, "checked": True, "found": False}
        chk = check_page_in_account(aid, token, pid)
        if chk.get("found"):
            entry["found"] = True
            entry["via"] = chk.get("via")
            tried.append(entry)
            return {
                "ok": True,
                "page_id": pid,
                "account_id": aid,
                "account_name": aname,
                "via": chk.get("via"),
                "tried_accounts": tried,
            }
        tried.append(entry)
    return {
        "ok": False,
        "error": (
            f"Page {pid} not bound to any active ad account in your portfolios. "
            "Connect the page to an ad account in Meta Business Manager (Business Settings > "
            "Pages > Add to ad account), or check that the user token has business_management scope."
        ),
        "tried_accounts": tried,
    }


def upload_video(account_id, token, video_url=None, file_url=None):
    """Upload a video to the ad account. Real API: POST /act_{id}/advideos. Pass video_url (public URL to video)."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    api_url = f"{BASE_URL}/{act}/advideos"
    data = {"access_token": token}
    url = video_url or file_url
    if not url:
        return {"error": {"message": "video_url (or file_url) is required"}}
    data["file_url"] = url
    return _post(api_url, data)


# --- Facebook Page posts (photo / video / feed) ---
def post_page_photo(page_id, token, url=None, caption=None, image_base64=None, filename=None):
    """Post a photo to a Facebook Page. Real API: POST /{page-id}/photos.

    Either ``url`` (public image URL) or ``image_base64`` (raw bytes) must be
    supplied. Bytes path uses multipart ``source`` — required for local-dev
    setups where the chat service's public URL isn't actually reachable from
    Facebook's crawler. Requires pages_manage_posts.
    """
    if not page_id or not str(page_id).strip():
        return {"error": {"message": "page_id is required"}}
    api_url = f"{BASE_URL}/{str(page_id).strip()}/photos"
    # Facebook rejects user-token writes to a page's /photos|/feed|/videos with a
    # misleading "(#200) publish_actions deprecated" error. The fix is to swap
    # the supplied user token for the page's own Page Access Token. Falls back
    # to the original token if the lookup fails (e.g. user is not a manager).
    page_tok = get_page_access_token(page_id, token) or token

    b64 = (image_base64 or "").strip()
    url_clean = (url or "").strip()

    if b64:
        # Multipart bytes path — works even when no public URL is reachable.
        import base64 as _b64, binascii
        try:
            payload = _b64.b64decode(b64, validate=False)
        except (binascii.Error, ValueError) as exc:
            return {"error": {"message": f"image_base64 is not valid base64: {exc}"}}
        if not payload:
            return {"error": {"message": "image_base64 decoded to empty bytes"}}
        data = {"access_token": page_tok}
        if caption:
            data["caption"] = str(caption).strip()
        fname = (filename or "image.jpg").strip() or "image.jpg"
        files = {"source": (fname, payload, "application/octet-stream")}
        return _post_multipart(api_url, data, files)

    if not url_clean or not url_clean.startswith(("http://", "https://")):
        return {"error": {"message": "url must be a public image URL (http or https), or pass image_base64"}}
    data = {"access_token": page_tok, "url": url_clean}
    if caption:
        data["caption"] = str(caption).strip()
    return _post(api_url, data)


def post_page_feed(page_id, token, message, link=None, picture=None, name=None, description=None):
    """Post to Page feed (text + optional link preview + optional image). Real API: POST /{page-id}/feed. Requires pages_manage_posts."""
    if not page_id or not str(page_id).strip():
        return {"error": {"message": "page_id is required"}}
    if not message or not str(message).strip():
        return {"error": {"message": "message is required for feed post"}}
    api_url = f"{BASE_URL}/{str(page_id).strip()}/feed"
    page_tok = get_page_access_token(page_id, token) or token
    data = {"access_token": page_tok, "message": message.strip()}
    if link and str(link).strip().startswith(("http://", "https://")):
        data["link"] = link.strip()
    if picture and str(picture).strip().startswith(("http://", "https://")):
        data["picture"] = picture.strip()
    if name:
        data["name"] = str(name).strip()
    if description:
        data["description"] = str(description).strip()
    return _post(api_url, data)


def post_page_video(page_id, token, video_url, description=None):
    """Post a video to a Facebook Page. Real API: POST /{page-id}/videos. Pass video_url (public URL). Requires pages_manage_posts."""
    if not page_id or not str(page_id).strip():
        return {"error": {"message": "page_id is required"}}
    api_url = f"{BASE_URL}/{str(page_id).strip()}/videos"
    page_tok = get_page_access_token(page_id, token) or token
    data = {"access_token": page_tok}
    if not video_url or not str(video_url).strip().startswith(("http://", "https://")):
        return {"error": {"message": "video_url must be a public video URL (http or https)"}}
    data["file_url"] = video_url.strip()
    if description:
        data["description"] = str(description).strip()
    return _post(api_url, data)


# --- Instagram Content Publishing (IG Graph API two-step container flow) ---
# Endpoints: POST /{ig-user-id}/media -> creation_id; poll GET /{creation_id};
# POST /{ig-user-id}/media_publish?creation_id=... Requires instagram_basic +
# instagram_content_publish + IG Business/Creator linked to a Page.

def _resolve_ig_user_id(page_id, instagram_user_id, token):
    """Resolve the IG-user-id (Instagram Business Account id). Returns (ig_user_id, error_or_None)."""
    if instagram_user_id and str(instagram_user_id).strip():
        return str(instagram_user_id).strip(), None
    if not page_id or not str(page_id).strip():
        return None, {"error": {"message": "instagram_user_id or page_id is required"}}
    res = get_instagram_accounts(str(page_id).strip(), token)
    if isinstance(res, dict) and res.get("error"):
        return None, res
    iba = (res or {}).get("instagram_business_account") if isinstance(res, dict) else None
    if isinstance(iba, dict) and iba.get("id"):
        return str(iba["id"]), None
    accounts = (res or {}).get("instagram_accounts") if isinstance(res, dict) else None
    if isinstance(accounts, dict):
        data = accounts.get("data") or []
        if isinstance(data, list) and data and isinstance(data[0], dict) and data[0].get("id"):
            return str(data[0]["id"]), None
    return None, {"error": {"message": "No Instagram Business Account linked to this Page"}}


_IG_MEDIA_HARD_CAP = 10000


def find_page_for_ig_user(token, ig_user_id, limit=100):
    """Reverse lookup: find the FB Page linked to an IG Business Account.

    Briefs often name a different Page (e.g. the advertiser's main Page) than
    the one the media's IG account actually cross-posts to, so cross-post
    resolution must start from the Page linked to the media owner — not the
    brief's page_id. Scans /me/accounts and matches
    instagram_business_account.id. Returns {"page_id", "page_name"} or {}.
    """
    igu = str(ig_user_id or "").strip()
    if not igu:
        return {}
    resp = _get(
        f"{BASE_URL}/me/accounts",
        {"access_token": token, "fields": "id,name,instagram_business_account", "limit": limit},
    )
    if not isinstance(resp, dict) or resp.get("error"):
        return {}
    for p in resp.get("data") or []:
        iba = (p.get("instagram_business_account") or {}).get("id")
        if str(iba or "") == igu:
            return {"page_id": str(p.get("id") or ""), "page_name": p.get("name") or ""}
    return {}


def resolve_ig_permalink_to_fb_xpost(
    token,
    instagram_permalink_url,
    page_id,
    *,
    instagram_user_id=None,
    caption_window_chars=50,
    timestamp_window_seconds=3600,
    posts_to_scan=50,
):
    """Translate an IG post permalink to its FB Page cross-post object_story_id.

    Used to bridge the Marketing API's hard restriction that political ads
    must run via a FB-Page-linked creative (subcode 2708016: "Ads about
    social issues, elections or politics must be connected to a Facebook
    Page"). When the user supplies an IG permalink for a political ad, we
    transparently find the cross-posted FB version and feed the resulting
    ``object_story_id`` to ``create_creative``.

    Matching strategy (in order, all must pass to claim a match):
      1. Fetch the IG media (caption + timestamp) via
         ``/{ig-user-id}/media`` paginated until the shortcode in the
         permalink matches.
      2. Fetch up to ``posts_to_scan`` FB Page posts via
         ``/{page_id}/published_posts`` using the page-scoped token.
      3. A FB post matches the IG media iff:
         - caption first ``caption_window_chars`` characters match
           (case-insensitive substring, both directions), AND
         - created_time within ``timestamp_window_seconds`` of IG
           timestamp (auto cross-posts land ~30s later, but manual
           dual-publishing is often minutes apart — hence a 1h default), AND
         - first attachment media_type is video / album / photo (i.e.
           a real content attachment, not a link share).

    Returns a dict with keys:
      - ``object_story_id`` (str): the FB cross-post id, or empty if no match.
      - ``ig_media`` (dict): the matched IG media object (id, caption,
        media_type, media_product_type, permalink, timestamp).
      - ``fb_post`` (dict): the matched FB post (id, created_time,
        is_eligible_for_promotion, attachments).
      - ``reason`` (str): when no match, human-readable explanation.

    Or on error: ``{"error": {...}}``.
    """
    import re as _re

    if not instagram_permalink_url or not str(instagram_permalink_url).strip():
        return {"error": {"message": "instagram_permalink_url is required"}}
    if not page_id or not str(page_id).strip():
        return {"error": {"message": "page_id is required to find the FB cross-post"}}

    perma = str(instagram_permalink_url).strip()
    # Extract shortcode: works for /p/<code>/ (feed) and /reel/<code>/ (reel)
    sc_match = _re.search(r"/(?:p|reel)/([^/?]+)", perma)
    if not sc_match:
        return {"error": {
            "message": f"Could not extract shortcode from permalink {perma!r}. "
                       "Expected /p/<code>/ or /reel/<code>/."
        }}
    shortcode = sc_match.group(1)

    # Resolve IG media — paginate until we find the shortcode
    ig_user_id, err = _resolve_ig_user_id(page_id, instagram_user_id, token)
    if err:
        return err

    ig_match = None
    pages_scanned = 0
    items_scanned = 0
    url = f"{BASE_URL}/{ig_user_id}/media"
    params = {
        "access_token": token,
        "fields": "id,caption,media_type,media_product_type,permalink,timestamp",
        "limit": 100,
    }
    current = _get(url, params)
    while True:
        pages_scanned += 1
        if not isinstance(current, dict) or current.get("error"):
            return current if isinstance(current, dict) else {"error": {"message": "IG media fetch failed"}}
        for m in current.get("data") or []:
            items_scanned += 1
            if shortcode in (m.get("permalink") or ""):
                ig_match = m
                break
        if ig_match:
            break
        if items_scanned >= _IG_MEDIA_HARD_CAP:
            break
        next_url = ((current.get("paging") or {}).get("next") or "")
        if not next_url:
            break
        current = _get(next_url, None)

    if not ig_match:
        return {
            "object_story_id": "",
            "ig_media": None,
            "fb_post": None,
            "reason": (
                f"Could not find IG media for shortcode {shortcode!r} in "
                f"{items_scanned} items across {pages_scanned} pages. "
                "Permalink may be for a media owned by a different IG account."
            ),
        }

    # Now find the FB cross-post. Use page-scoped token so cross-business
    # pages with restricted user-token grants still work.
    ig_caption = (ig_match.get("caption") or "")[:caption_window_chars].strip()
    ig_ts = ig_match.get("timestamp") or ""
    # Parse ISO timestamp → unix seconds
    ig_unix = 0
    try:
        # Format: 2026-05-28T06:03:43+0000
        from datetime import datetime as _dt
        ig_unix = int(_dt.strptime(ig_ts.replace("+0000", "+0000"),
                                   "%Y-%m-%dT%H:%M:%S%z").timestamp())
    except Exception:  # noqa: BLE001
        pass

    page_tok = get_page_access_token(str(page_id).strip(), token) or token

    fb_url = f"{BASE_URL}/{str(page_id).strip()}/published_posts"
    fb_params = {
        "access_token": page_tok,
        "fields": "id,message,created_time,is_eligible_for_promotion,"
                  "attachments{media_type,target{url}}",
        "limit": posts_to_scan,
    }
    # Pages with a large post history make Meta answer code 1 "Please reduce
    # the amount of data" for this query at limit=50 (live: BOL7 Page
    # 1876788849233669, 2026-09-08). That used to abort the whole lookup as
    # "FB Page posts unreachable" even though the cross-post existed 118s
    # from the IG reel. Now: shrink the page size on that error and walk
    # paging until ``posts_to_scan`` posts are seen OR the feed is already
    # older than the IG timestamp minus the window (newest-first feed, so
    # anything further back cannot match).
    fb_res = _get_shrinking(fb_url, fb_params)
    if not isinstance(fb_res, dict) or fb_res.get("error"):
        return {
            "object_story_id": "",
            "ig_media": ig_match,
            "fb_post": None,
            "reason": "FB Page posts unreachable: "
                      + str((fb_res or {}).get("error") if isinstance(fb_res, dict) else fb_res),
        }

    def _fb_unix(fp):
        try:
            from datetime import datetime as _dt
            return int(_dt.strptime(
                (fp.get("created_time") or "").replace("+0000", "+0000"),
                "%Y-%m-%dT%H:%M:%S%z",
            ).timestamp())
        except Exception:  # noqa: BLE001
            return 0

    fb_posts: list = []
    current = fb_res
    while True:
        batch = [fp for fp in (current.get("data") or []) if isinstance(fp, dict)]
        fb_posts.extend(batch)
        if len(fb_posts) >= posts_to_scan or not batch:
            break
        oldest = min((_fb_unix(fp) for fp in batch), default=0)
        if ig_unix and oldest and oldest < ig_unix - timestamp_window_seconds:
            break  # feed already older than any possible match
        cursors = ((current.get("paging") or {}).get("cursors") or {})
        after = cursors.get("after") if isinstance(cursors, dict) else None
        if not after:
            break
        nxt_params = dict(fb_params)
        nxt_params["limit"] = max(len(batch), 1)  # size Meta just accepted
        nxt_params["after"] = after
        nxt = _get_shrinking(fb_url, nxt_params)
        if not isinstance(nxt, dict) or nxt.get("error"):
            break  # keep what we have; partial scan is still useful
        current = nxt

    fb_match = None
    for fp in fb_posts:
        # caption match
        fb_msg = (fp.get("message") or "")[:caption_window_chars].strip()
        if ig_caption and fb_msg and (
            ig_caption.lower() in fb_msg.lower()
            or fb_msg.lower() in ig_caption.lower()
        ):
            # timestamp window
            ok_time = True
            if ig_unix:
                try:
                    from datetime import datetime as _dt
                    fb_unix = int(_dt.strptime(
                        (fp.get("created_time") or "").replace("+0000", "+0000"),
                        "%Y-%m-%dT%H:%M:%S%z",
                    ).timestamp())
                    if abs(fb_unix - ig_unix) > timestamp_window_seconds:
                        ok_time = False
                except Exception:  # noqa: BLE001
                    pass
            if not ok_time:
                continue
            # attachment must be media (video/album/photo), not a plain link
            atts = (fp.get("attachments") or {}).get("data") or []
            mtype = ((atts[0] or {}).get("media_type") or "").lower() if atts else ""
            if mtype not in ("video", "album", "photo"):
                continue
            fb_match = fp
            break

    if not fb_match:
        return {
            "object_story_id": "",
            "ig_media": ig_match,
            "fb_post": None,
            "reason": (
                f"IG media {ig_match.get('id')} matched, but no corresponding "
                f"FB cross-post found in the {len(fb_posts)} "
                "most-recent Page posts. The IG post may not have been shared "
                "to Facebook (toggle 'Share to Facebook' was off during posting)."
            ),
        }

    return {
        "object_story_id": fb_match.get("id"),
        "ig_media": ig_match,
        "fb_post": fb_match,
        "reason": "matched",
    }


def get_instagram_media(token, page_id=None, instagram_user_id=None,
                        fields=None, caption_query=None):
    """List ALL media (posts/reels/stories) on an IG Business Account.
    Real API: GET /{ig-user-id}/media.

    Use the returned ``id`` as ``source_instagram_media_id`` when creating an
    ad creative from an existing IG post (see create_creative).

    Either ``page_id`` (Page linked to the IG Business account) or
    ``instagram_user_id`` must be supplied. Auto-resolves via
    ``_resolve_ig_user_id`` when only ``page_id`` is given.

    Always walks every paging.next until exhausted (hard-capped at
    _IG_MEDIA_HARD_CAP items as a runaway safety). When ``caption_query``
    is provided, returns matching media and a ``search`` summary.

    ``caption_query`` matching is shortcode-aware: if the query looks like an
    IG SHORTCODE (e.g. "DY3yAOBDTV9" from instagram.com/reel/<shortcode>/), it
    is matched against each media's ``permalink`` (search.lookup =
    ``shortcode_permalink``) and the crawl stops at the first hit — because a
    shortcode never appears in caption text. Otherwise it is a case-insensitive
    substring match on ``caption`` (search.lookup = ``caption_text``).
    """
    ig_user_id, err = _resolve_ig_user_id(page_id, instagram_user_id, token)
    if err:
        return err
    url = f"{BASE_URL}/{ig_user_id}/media"
    if fields is None:
        fields = "id,caption,media_type,media_url,permalink,timestamp,thumbnail_url"
    # Meta /{ig-user-id}/media caps per-request size at 100 (same as /page/posts).
    # We still fetch ALL media via paging.next + cursor fallback — this only
    # changes how many round-trips it takes, not the total returned to caller.
    params = {"access_token": token, "fields": fields, "limit": 100}
    res = _get(url, params)
    if not isinstance(res, dict) or res.get("error"):
        return res

    query_raw = str(caption_query).strip() if caption_query else ""
    query_low = query_raw.lower()
    # A caption_query that looks like an IG SHORTCODE (e.g. "DY3yAOBDTV9" from
    # instagram.com/reel/<shortcode>/ or /p/<shortcode>/) will NEVER appear in
    # the caption text — matching it against `caption` always misses. Detect a
    # shortcode-shaped query (no spaces; mix of letters/digits/_/-, 5-30 chars)
    # and match it against the `permalink` (which contains the shortcode)
    # instead. Falls through to caption matching for normal text queries.
    # "shortcode-like": looks like an IG shortcode (full or partial) — letters
    # + digits, no spaces, 4-30 chars, not a normal word/sentence. We match
    # such a query against the PERMALINK (where the shortcode lives). We ALSO
    # always try a permalink match for any no-space query, so a partial code
    # like "DY3y" (4 chars, sent by an agent) still resolves even though it's
    # below the strict shortcode length.
    _is_shortcode = bool(query_raw) and bool(re.fullmatch(r"[A-Za-z0-9_-]{4,30}", query_raw)) \
        and not query_raw.isdigit() and " " not in query_raw
    all_media: list = []
    matches: list = []
    items_scanned = 0
    pages_scanned = 0
    current = res
    last_paging = current.get("paging") or {}

    while True:
        pages_scanned += 1
        data = current.get("data") or []
        items_scanned += len(data)
        for m in data:
            if not isinstance(m, dict):
                continue
            all_media.append(m)
            if query_low:
                permalink = (m.get("permalink") or "")
                # permalink match first (handles full + partial shortcodes),
                # then caption substring as the general text path.
                if _is_shortcode and query_raw in permalink:
                    matches.append(m)
                elif query_low in (m.get("caption") or "").lower():
                    matches.append(m)
        if matches and _is_shortcode:
            # Shortcode is unique — stop as soon as we find it (no need to crawl
            # the whole media history, which is what made this slow/miss before).
            break
        if items_scanned >= _IG_MEDIA_HARD_CAP:
            break
        paging_obj = current.get("paging") or {}
        next_url = paging_obj.get("next")
        next_res = None
        if next_url:
            next_res = _get(next_url, None)
        else:
            # IG's /media endpoint sometimes returns paging.cursors.after
            # without a paging.next URL. Build the next-page request
            # manually using the after cursor so we don't stop short.
            after_cursor = ((paging_obj.get("cursors") or {}).get("after")
                            if isinstance(paging_obj.get("cursors"), dict) else None)
            if not after_cursor:
                break
            cursor_params = dict(params)
            cursor_params["after"] = after_cursor
            next_res = _get(url, cursor_params)
        if not isinstance(next_res, dict) or next_res.get("error"):
            break
        # Empty data page means we've reached the end even if a cursor was returned.
        if not (next_res.get("data") or []):
            last_paging = next_res.get("paging") or last_paging
            break
        current = next_res
        last_paging = current.get("paging") or last_paging

    if query_low:
        return {
            "data": matches,
            "paging": last_paging,
            "search": {
                "query": str(caption_query).strip(),
                "lookup": "shortcode_permalink" if _is_shortcode else "caption_text",
                "pages_scanned": pages_scanned,
                "items_scanned": items_scanned,
                "matched": bool(matches),
            },
        }
    return {"data": all_media, "paging": last_paging}


_POST_INSIGHTS_DEFAULT_METRICS = {
    # Minimal seed defaults only — NOT an allow-list. Callers can pass any
    # metrics; on rejection the server's own "#100 must be one of the
    # following values:" list is parsed and the request adapts dynamically.
    # total_views is the number the IG app DISPLAYS on a post (includes
    # replays, FB cross-post views and paid/boosted views); plain views is
    # IG-surface only and can be 100x smaller on boosted posts — "posts
    # under X views" questions must use total_views to match what the
    # account owner sees.
    "instagram": "total_views,views,reach",
    # v25 removed the whole post_impressions* family for Page posts (rejected
    # with a list-less #100) — these three are what still answers "how did
    # this post do": video views, clicks, likes.
    "facebook": "post_video_views,post_clicks,post_reactions_like_total",
}

_POST_INSIGHTS_DEFAULT_FIELDS = {
    "instagram": "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count",
    "facebook": "id,message,created_time,permalink_url",
}


def _valid_metrics_from_error(err):
    """Parse Meta's '#100 metric[0] must be one of the following values: a, b, ...'
    error into a list of valid metric names. Returns [] if the message doesn't
    carry a value list (then the caller can't auto-adapt)."""
    msg = (err or {}).get("message") or "" if isinstance(err, dict) else str(err or "")
    m = re.search(r"must be one of the following values:\s*(.+)", msg)
    if not m:
        return []
    return [v.strip() for v in m.group(1).split(",") if v.strip()]


def _flatten_insights_item(item):
    """Lift insights.data[].values[0].value up to top-level {metric: value} keys.
    Items where Meta omitted a metric (unsupported for that media type) simply
    lack that key — callers must not assume presence."""
    row = {k: v for k, v in item.items() if k != "insights"}
    ins = item.get("insights")
    data = ins.get("data") if isinstance(ins, dict) else None
    for d in data or []:
        name = d.get("name") if isinstance(d, dict) else None
        vals = (d.get("values") or []) if isinstance(d, dict) else []
        if name and vals and isinstance(vals[0], dict):
            row[name] = vals[0].get("value")
    return row


_WHERE_OPS = {
    "lt": lambda a, b: a < b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "gte": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
}


def get_post_insights(token, page_id=None, instagram_user_id=None,
                      platform="instagram", post_ids=None, metrics=None,
                      fields=None, where=None, sort=None, limit=None):
    """Per-post view/engagement metrics for IG media OR FB Page posts in ONE call.

    Real API: GET /{container}/media|published_posts?fields=...,insights.metric(...)
    (field expansion → ~100 posts per HTTP round-trip, no per-post calls).

    platform='instagram' (default) walks the IG Business Account media edge
    (resolved from page_id via the linked account, or given instagram_user_id);
    platform='facebook' walks /{page_id}/published_posts (with the same
    user-token → page-token swap on error 190 as get_page_posts).
    post_ids (list) fetches just those objects instead of crawling.

    metrics is free-form (comma string or list). If Meta rejects a metric
    (error #100), the server's own valid-values list is parsed and the request
    retries with the intersection — so the tool adapts as Meta deprecates
    metrics (e.g. 'plays' → 'views') without any hardcoded allow-list. If
    nothing survives the intersection, the error is returned with
    'valid_metrics' so the caller can pick.

    where filters server-side: {"metric": "views", "lt": 5000} (ops: lt, lte,
    gt, gte, eq) — "posts under 5000 views" is one call, one response.
    sort: metric name, '-' prefix for descending (rows missing the metric sort
    last). limit: max rows returned AFTER filter+sort.
    """
    platform = (str(platform or "instagram").strip().lower())
    platform = "facebook" if platform in ("facebook", "fb", "page") else "instagram"

    if isinstance(metrics, (list, tuple)):
        metric_list = [str(m).strip() for m in metrics if str(m).strip()]
    else:
        metric_list = [m.strip() for m in str(
            metrics or _POST_INSIGHTS_DEFAULT_METRICS[platform]).split(",") if m.strip()]
    if fields is None:
        fields = _POST_INSIGHTS_DEFAULT_FIELDS[platform]

    def _fields_param(mlist):
        return f"{fields},insights.metric({','.join(mlist)})"

    # --- collect raw items (retrying once with server-validated metrics) ---
    metrics_used = list(metric_list)
    retried_with = None

    def _metric_retry(err, sample_id=None):
        """On #100 metric rejection, intersect requested metrics with the
        server's valid list. Some rejections (e.g. Page post metrics on v25)
        carry NO value list — then probe each metric against sample_id via
        /{id}/insights and keep the survivors. Returns the new list or None."""
        nonlocal metrics_used, retried_with
        if not isinstance(err, dict) or err.get("code") != 100:
            return None
        valid = _valid_metrics_from_error(err)
        if valid:
            keep = [m for m in metrics_used if m in valid]
        elif sample_id:
            keep = []
            for m in metrics_used:
                probe = _get(f"{BASE_URL}/{sample_id}/insights",
                             {"access_token": token, "metric": m})
                if isinstance(probe, dict) and not probe.get("error"):
                    keep.append(m)
        else:
            keep = []
        if not keep or keep == metrics_used:
            return None
        metrics_used = keep
        retried_with = keep
        return keep

    items = []
    scanned = 0

    if post_ids:
        ids = [str(i).strip() for i in (post_ids if isinstance(post_ids, (list, tuple)) else str(post_ids).split(",")) if str(i).strip()]
        for oid in ids:
            params = {"access_token": token, "fields": _fields_param(metrics_used)}
            res = _get(f"{BASE_URL}/{oid}", params)
            err = res.get("error") if isinstance(res, dict) else None
            if err:
                keep = _metric_retry(err, sample_id=oid)
                if keep:
                    params["fields"] = _fields_param(keep)
                    res = _get(f"{BASE_URL}/{oid}", params)
                    err = res.get("error") if isinstance(res, dict) else None
            if err:
                items.append({"id": oid, "error": err})
                continue
            if isinstance(res, dict):
                items.append(res)
        scanned = len(ids)
    else:
        if platform == "instagram":
            container, err = _resolve_ig_user_id(page_id, instagram_user_id, token)
            if err:
                return err
            url = f"{BASE_URL}/{container}/media"
            hard_cap = _IG_MEDIA_HARD_CAP
        else:
            if not page_id:
                return {"error": {"message": "page_id is required for platform=facebook"}}
            url = f"{BASE_URL}/{str(page_id).strip()}/published_posts"
            hard_cap = _PAGE_POSTS_HARD_CAP

        params = {"access_token": token, "fields": _fields_param(metrics_used), "limit": 100}
        res = _get(url, params)
        err = res.get("error") if isinstance(res, dict) else None
        # /published_posts rejects user tokens with #210 ("page access token
        # required"); /posts uses #190 subcode 2069032 for the same situation.
        if err and isinstance(err, dict) and err.get("code") in (190, 210) and platform == "facebook":
            page_tok = get_page_access_token(page_id, token)
            if page_tok and page_tok != token:
                token = page_tok
                params["access_token"] = page_tok
                res = _get(url, params)
                err = res.get("error") if isinstance(res, dict) else None
        if err:
            sample_id = None
            if isinstance(err, dict) and err.get("code") == 100 \
                    and not _valid_metrics_from_error(err):
                # List-less metric rejection — need a real object id to probe
                # metrics against. One id from the same edge is enough.
                sample = _get(url, {"access_token": token, "fields": "id", "limit": 1})
                sdata = (sample.get("data") or []) if isinstance(sample, dict) else []
                if sdata and isinstance(sdata[0], dict):
                    sample_id = sdata[0].get("id")
            keep = _metric_retry(err, sample_id=sample_id)
            if keep:
                params["fields"] = _fields_param(keep)
                res = _get(url, params)
                err = res.get("error") if isinstance(res, dict) else None
        if err or not isinstance(res, dict):
            if isinstance(err, dict) and err.get("code") == 100:
                valid = _valid_metrics_from_error(err)
                if valid:
                    return {"error": err, "valid_metrics": valid}
            return res

        current = res
        seen_ids = set()
        while True:
            data = current.get("data") or []
            scanned += len(data)
            for m in data:
                # Meta pages can overlap (published_posts returns the same post
                # on consecutive pages) — dedupe by id, keep first occurrence.
                if not isinstance(m, dict):
                    continue
                mid = m.get("id")
                if mid is not None and mid in seen_ids:
                    continue
                if mid is not None:
                    seen_ids.add(mid)
                items.append(m)
            if scanned >= hard_cap:
                break
            paging_obj = current.get("paging") or {}
            next_url = paging_obj.get("next")
            if next_url:
                next_res = _get(next_url, None)
            else:
                after_cursor = ((paging_obj.get("cursors") or {}).get("after")
                                if isinstance(paging_obj.get("cursors"), dict) else None)
                if not after_cursor:
                    break
                cursor_params = dict(params)
                cursor_params["after"] = after_cursor
                next_res = _get(url, cursor_params)
            if not isinstance(next_res, dict) or next_res.get("error"):
                break
            if not (next_res.get("data") or []):
                break
            current = next_res

    rows = [_flatten_insights_item(m) for m in items]

    # --- server-side filter ---
    no_metric = 0
    where_desc = None
    if isinstance(where, str):
        try:
            where = json.loads(where)
        except (TypeError, ValueError):
            return {"error": {"message": f"where must be a JSON object, got: {where!r}"}}
    if isinstance(where, dict) and where.get("metric"):
        metric = str(where["metric"]).strip()
        op_name = next((op for op in _WHERE_OPS if op in where), None)
        if not op_name:
            return {"error": {"message": "where needs one of lt/lte/gt/gte/eq, e.g. {\"metric\": \"views\", \"lt\": 5000}"}}
        threshold = where[op_name]
        kept = []
        for r in rows:
            val = r.get(metric)
            if not isinstance(val, (int, float)):
                no_metric += 1
                continue
            if _WHERE_OPS[op_name](val, threshold):
                kept.append(r)
        rows = kept
        where_desc = {"metric": metric, op_name: threshold}

    # --- sort + limit ---
    if sort:
        key = str(sort).strip()
        desc = key.startswith("-")
        key = key.lstrip("-")
        rows.sort(key=lambda r: (not isinstance(r.get(key), (int, float)),
                                 -(r.get(key) or 0) if desc else (r.get(key) or 0)))
    matched = len(rows)
    if isinstance(limit, int) and limit > 0:
        rows = rows[:limit]

    summary = {
        "platform": platform,
        "items_scanned": scanned,
        "matched": matched,
        "returned": len(rows),
        "metrics_used": metrics_used,
    }
    if retried_with:
        summary["metrics_auto_adjusted"] = retried_with
    if where_desc:
        summary["where"] = where_desc
        summary["skipped_no_metric"] = no_metric
    return {"data": rows, "summary": summary}


def _create_ig_container(ig_user_id, token, payload):
    """POST /{ig_user_id}/media. Returns Graph response (contains 'id' on success)."""
    api_url = f"{BASE_URL}/{str(ig_user_id).strip()}/media"
    data = {k: v for k, v in (payload or {}).items() if v is not None and v != ""}
    data["access_token"] = token
    return _post(api_url, data)


def _wait_ig_container(container_id, token, max_wait_sec=120, poll_sec=3):
    """Poll GET /{container_id}?fields=status_code,status until FINISHED.
    Returns {"ok": bool, "status_code": str, ...raw...}. On ERROR/EXPIRED/timeout, ok=False."""
    import time as _time
    api_url = f"{BASE_URL}/{str(container_id).strip()}"
    params = {"access_token": token, "fields": "status_code,status"}
    waited = 0
    last = {}
    while waited <= max_wait_sec:
        last = _get(api_url, params) or {}
        if isinstance(last, dict) and last.get("error"):
            return {"ok": False, **last}
        sc = (last.get("status_code") or "").upper() if isinstance(last, dict) else ""
        if sc == "FINISHED":
            return {"ok": True, **last}
        if sc in ("ERROR", "EXPIRED"):
            return {"ok": False, **last}
        _time.sleep(poll_sec)
        waited += poll_sec
    return {"ok": False, "error": {"message": f"Container {container_id} not FINISHED within {max_wait_sec}s", "last": last}}


def _publish_ig_container(ig_user_id, container_id, token):
    """POST /{ig_user_id}/media_publish?creation_id=container_id."""
    api_url = f"{BASE_URL}/{str(ig_user_id).strip()}/media_publish"
    return _post(api_url, {"access_token": token, "creation_id": str(container_id).strip()})


def _ig_create_wait_publish(ig_user_id, token, payload, wait=True, max_wait_sec=120):
    """Helper: create container -> (optionally wait) -> publish. Returns publish response or error dict."""
    container = _create_ig_container(ig_user_id, token, payload)
    if not isinstance(container, dict) or container.get("error") or not container.get("id"):
        return container if isinstance(container, dict) else {"error": {"message": "container creation failed"}}
    cid = container["id"]
    if wait:
        status = _wait_ig_container(cid, token, max_wait_sec=max_wait_sec)
        if not status.get("ok"):
            return {"error": {"message": "container not ready for publish", "container_id": cid, "status": status}}
    pub = _publish_ig_container(ig_user_id, cid, token)
    if isinstance(pub, dict) and not pub.get("error"):
        pub["_container_id"] = cid
    return pub


def post_instagram_photo(ig_user_id, token, image_url, caption=None):
    """Publish a single photo to an IG feed. Requires instagram_content_publish."""
    if not ig_user_id or not str(ig_user_id).strip():
        return {"error": {"message": "ig_user_id is required"}}
    if not image_url or not str(image_url).strip().startswith(("http://", "https://")):
        return {"error": {"message": "image_url must be a public URL (http or https)"}}
    payload = {"image_url": str(image_url).strip(), "caption": (str(caption).strip() if caption else None)}
    return _ig_create_wait_publish(ig_user_id, token, payload, wait=True, max_wait_sec=30)


def post_instagram_video(ig_user_id, token, video_url, caption=None, thumb_offset=None):
    """Publish a single video to an IG feed. Async — polls container status before publish."""
    if not ig_user_id or not str(ig_user_id).strip():
        return {"error": {"message": "ig_user_id is required"}}
    if not video_url or not str(video_url).strip().startswith(("http://", "https://")):
        return {"error": {"message": "video_url must be a public URL (http or https)"}}
    payload = {
        "media_type": "VIDEO",
        "video_url": str(video_url).strip(),
        "caption": (str(caption).strip() if caption else None),
        "thumb_offset": thumb_offset,
    }
    return _ig_create_wait_publish(ig_user_id, token, payload, wait=True, max_wait_sec=300)


def post_instagram_reel(ig_user_id, token, video_url, caption=None, share_to_feed=True, cover_url=None, audio_name=None):
    """Publish a Reel. Async — polls container status before publish."""
    if not ig_user_id or not str(ig_user_id).strip():
        return {"error": {"message": "ig_user_id is required"}}
    if not video_url or not str(video_url).strip().startswith(("http://", "https://")):
        return {"error": {"message": "video_url must be a public URL (http or https)"}}
    payload = {
        "media_type": "REELS",
        "video_url": str(video_url).strip(),
        "caption": (str(caption).strip() if caption else None),
        "share_to_feed": "true" if share_to_feed else "false",
        "cover_url": (str(cover_url).strip() if cover_url else None),
        "audio_name": (str(audio_name).strip() if audio_name else None),
    }
    return _ig_create_wait_publish(ig_user_id, token, payload, wait=True, max_wait_sec=300)


def post_instagram_story(ig_user_id, token, image_url=None, video_url=None):
    """Publish a Story (photo OR video). Pass exactly one of image_url / video_url."""
    if not ig_user_id or not str(ig_user_id).strip():
        return {"error": {"message": "ig_user_id is required"}}
    img = str(image_url).strip() if image_url else ""
    vid = str(video_url).strip() if video_url else ""
    if bool(img) == bool(vid):
        return {"error": {"message": "pass exactly one of image_url or video_url"}}
    if img and not img.startswith(("http://", "https://")):
        return {"error": {"message": "image_url must be a public URL (http or https)"}}
    if vid and not vid.startswith(("http://", "https://")):
        return {"error": {"message": "video_url must be a public URL (http or https)"}}
    payload = {"media_type": "STORIES"}
    if img:
        payload["image_url"] = img
        return _ig_create_wait_publish(ig_user_id, token, payload, wait=True, max_wait_sec=30)
    payload["video_url"] = vid
    return _ig_create_wait_publish(ig_user_id, token, payload, wait=True, max_wait_sec=300)


def post_instagram_carousel(ig_user_id, token, items, caption=None):
    """Publish a 2-10 item carousel.

    items: list of dicts, each {image_url|video_url, media_type?}. media_type
    auto-inferred to IMAGE if image_url, VIDEO if video_url. Per Meta API,
    every child container needs is_carousel_item=true; videos must reach
    FINISHED before the parent CAROUSEL container is created.
    """
    if not ig_user_id or not str(ig_user_id).strip():
        return {"error": {"message": "ig_user_id is required"}}
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (TypeError, ValueError):
            return {"error": {"message": "items must be a JSON list of {image_url|video_url}"}}
    if not isinstance(items, list) or not (2 <= len(items) <= 10):
        return {"error": {"message": "items must be a list of 2-10 entries"}}
    child_ids = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return {"error": {"message": f"items[{idx}] must be a dict"}}
        img = str(item.get("image_url") or "").strip()
        vid = str(item.get("video_url") or "").strip()
        if bool(img) == bool(vid):
            return {"error": {"message": f"items[{idx}] needs exactly one of image_url or video_url"}}
        payload = {"is_carousel_item": "true"}
        if img:
            if not img.startswith(("http://", "https://")):
                return {"error": {"message": f"items[{idx}].image_url must be public URL"}}
            payload["image_url"] = img
        else:
            if not vid.startswith(("http://", "https://")):
                return {"error": {"message": f"items[{idx}].video_url must be public URL"}}
            payload["media_type"] = "VIDEO"
            payload["video_url"] = vid
        container = _create_ig_container(ig_user_id, token, payload)
        if not isinstance(container, dict) or container.get("error") or not container.get("id"):
            return {"error": {"message": f"items[{idx}] container creation failed", "detail": container, "created_so_far": child_ids}}
        cid = container["id"]
        if vid:
            status = _wait_ig_container(cid, token, max_wait_sec=300)
            if not status.get("ok"):
                return {"error": {"message": f"items[{idx}] not ready", "container_id": cid, "status": status, "created_so_far": child_ids}}
        child_ids.append(cid)
    parent_payload = {
        "media_type": "CAROUSEL",
        "children": ",".join(child_ids),
        "caption": (str(caption).strip() if caption else None),
    }
    return _ig_create_wait_publish(ig_user_id, token, parent_payload, wait=True, max_wait_sec=120)


def _build_carousel_child_attachments(carousel_cards: Any, default_link: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Normalize a list of carousel-card dicts into Meta's ``child_attachments``
    payload shape.

    Meta's carousel format: every card needs ``link`` + ``image_hash`` (or
    ``video_id``); ``name`` (headline), ``description`` (subheading) and a
    per-card ``call_to_action`` are optional. The whole array goes inside
    ``link_data.child_attachments``.

    Accepts either a JSON string or a list of dicts. Returns the cleaned
    list, or None if the input is empty/invalid (so the caller can fall back
    to single-image link_data).
    """
    if not carousel_cards:
        return None
    if isinstance(carousel_cards, str):
        try:
            carousel_cards = json.loads(carousel_cards)
        except (TypeError, ValueError):
            return None
    if not isinstance(carousel_cards, list) or not carousel_cards:
        return None
    out: List[Dict[str, Any]] = []
    for card in carousel_cards:
        if not isinstance(card, dict):
            continue
        link = str(card.get("link") or default_link or "").strip()
        image_hash = str(card.get("image_hash") or "").strip()
        video_id = str(card.get("video_id") or "").strip()
        if not link or (not image_hash and not video_id):
            continue
        attachment: Dict[str, Any] = {"link": link}
        if image_hash:
            attachment["image_hash"] = image_hash
        if video_id:
            attachment["video_id"] = video_id
        if card.get("name"):
            attachment["name"] = str(card["name"]).strip()
        if card.get("description"):
            attachment["description"] = str(card["description"]).strip()
        cta = card.get("call_to_action")
        if isinstance(cta, dict) and cta.get("type"):
            attachment["call_to_action"] = cta
        out.append(attachment)
    # Meta requires 2-10 cards
    if len(out) < 2:
        return None
    return out[:10]


def _normalize_destination_spec(value):
    """Coerce a creative ``destination_spec`` (dict / JSON string / bare
    destination_type string) into the JSON string Meta expects, or "" when
    absent/invalid. Unknown destination types are passed through untouched —
    Meta owns that enum; we only upper-case it."""
    if value is None:
        return ""
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return ""
        if v.startswith("{"):
            try:
                value = json.loads(v)
            except (TypeError, ValueError):
                return ""
        else:
            value = {"destination_type": v}
    if not isinstance(value, dict):
        return ""
    out = dict(value)
    dt = str(out.get("destination_type") or "").strip().upper()
    if dt:
        out["destination_type"] = dt
    return json.dumps(out) if out else ""


def create_creative(account_id, token, name, object_story_spec=None, page_id=None, link_url=None, message=None, image_hash=None, object_story_id=None, instagram_actor_id=None, instagram_user_id=None, authorization_category=None, source_instagram_media_id=None, instagram_permalink_url=None, headline=None, description=None, carousel_cards=None, instant_experience_id=None, branded_content_sponsor_page_id=None, asset_feed_spec=None, bylines=None, destination_spec=None):
    """Create an ad creative. Real API: POST /act_{id}/adcreatives.
    Use object_story_id for existing Facebook Page post (page_id_post_id).
    Use source_instagram_media_id for existing Instagram posts INCLUDING Reels and
    videos — live-verified accepted by Meta (2026-08-13) for reel media; do not
    pre-emptively reroute reels to a FB cross-post. Carousels are the exception:
    Meta rejects media-id for CAROUSEL_ALBUM — use instagram_permalink_url there,
    paired with instagram_user_id so Meta can resolve the owning IG Business Account.
    Else object_story_spec (JSON) or page_id+link_url+message+image_hash.

    Carousel ads: pass ``carousel_cards`` as a JSON list of 2-10 cards. Each
    card needs ``link`` + ``image_hash`` (or ``video_id``); ``name`` (headline),
    ``description``, and per-card ``call_to_action`` are optional. Example:
    ``[{"link": "https://...", "image_hash": "abc", "name": "Card 1",
    "description": "...", "call_to_action": {"type": "SHOP_NOW", "value":
    {"link": "https://..."}}}, ...]``. Requires ``page_id`` + a top-level
    ``message`` (the carousel's primary text). Carousel takes priority over
    single-image when both are provided.

    headline + description fill link_data.name and link_data.description on the
    Meta side — both render as separate elements in the ad preview.
    authorization_category: e.g. POLITICAL for creatives in ISSUES_ELECTIONS_POLITICS campaigns (required by Meta for existing-post creatives).
    Pass instagram_actor_id (v19) or instagram_user_id (v22+) so the ad can run on Instagram. Get valid ID via get_instagram_accounts or get_page_backed_instagram_accounts for the Page."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    if not name:
        return {"error": {"message": "name is required"}}

    # Normalize bylines once — "Paid for by" disclaimer required for
    # ISSUES_ELECTIONS_POLITICS / FINANCIAL_PRODUCTS_SERVICES / etc. category
    # ads. Meta rejects with code 100 / subcode 2708016 when this is missing
    # on a political ad creative.
    _bylines_val = str(bylines or "").strip() if bylines else ""

    # Graph v26: creatives with a website destination are auto-routed to
    # WEBSITE_AND_SHOP when the advertiser has a Meta Shop. ``destination_spec``
    # (dict or JSON string, e.g. {"destination_type": "WEBSITE_AND_SHOP_OPT_OUT"})
    # is forwarded verbatim on EVERY creative shape so a brief can opt out.
    _dest_spec_val = _normalize_destination_spec(destination_spec)

    def _send(url, payload):
        if _dest_spec_val:
            payload["destination_spec"] = _dest_spec_val
        return _post(url, payload)

    # --- Instagram existing post (source_instagram_media_id) ---
    # When using an existing Instagram post, only pass source_instagram_media_id.
    # Do NOT pass instagram_actor_id alongside it — Meta rejects that combination.
    if source_instagram_media_id:
        api_url = f"{BASE_URL}/{act}/adcreatives"
        data = {"access_token": token, "name": name, "source_instagram_media_id": str(source_instagram_media_id).strip()}
        if authorization_category and str(authorization_category).strip():
            data["authorization_category"] = str(authorization_category).strip().upper()
        if _bylines_val:
            data["bylines"] = _bylines_val
        return _send(api_url, data)

    # --- Instagram existing post via permalink (covers feed posts, carousels,
    # reels — the cases where source_instagram_media_id is rejected by Meta).
    # Per spec §11 footnote: "Boosting IG carousel/feed posts via API requires
    # the FB cross-post ID (object_story_id format). Direct IG-only feed-post
    # boost via API needs instagram_permalink_url." We expose that field here.
    # Requires instagram_user_id so Meta can resolve the owning IG account.
    if instagram_permalink_url and str(instagram_permalink_url).strip():
        api_url = f"{BASE_URL}/{act}/adcreatives"
        data = {
            "access_token": token,
            "name": name,
            "instagram_permalink_url": str(instagram_permalink_url).strip(),
        }
        iu = str(instagram_user_id or "").strip()
        ia = str(instagram_actor_id or "").strip()
        if iu:
            data["instagram_user_id"] = iu
        elif ia:
            # v19 fallback when caller only has instagram_actor_id.
            data["instagram_actor_id"] = ia
        else:
            return {"error": {"message": "instagram_permalink_url requires instagram_user_id (or instagram_actor_id) so Meta can resolve the owning IG Business Account."}}
        # Do NOT wrap page_id in object_story_spec here — that triggers Meta
        # to expect link_data.link and rejects with subcode 2061015. Meta
        # auto-derives the FB Page identity from the IG account's linked Page
        # for permalink-mode creatives.
        if authorization_category and str(authorization_category).strip():
            data["authorization_category"] = str(authorization_category).strip().upper()
        if _bylines_val:
            data["bylines"] = _bylines_val
        return _send(api_url, data)

    # All adcreative writes go via BASE_URL (centralized v22+).
    api_url = f"{BASE_URL}/{act}/adcreatives"
    data = {"access_token": token, "name": name}
    if authorization_category and str(authorization_category).strip():
        data["authorization_category"] = str(authorization_category).strip().upper()
    if _bylines_val:
        data["bylines"] = _bylines_val
    if instagram_actor_id:
        data["instagram_actor_id"] = str(instagram_actor_id).strip()
    if instagram_user_id:
        data["instagram_user_id"] = str(instagram_user_id).strip()
    # Branded Content / Paid Partnership: top-level field. Required when this
    # creative declares a paid partnership with another Page (the sponsor).
    if branded_content_sponsor_page_id and str(branded_content_sponsor_page_id).strip():
        data["branded_content_sponsor_page_id"] = str(branded_content_sponsor_page_id).strip()
    # Dynamic Creative Optimization: when an asset_feed_spec is provided, Meta
    # combines bodies/titles/descriptions/images/videos/ctas into per-impression
    # variants and picks the winner. This branch is mutually exclusive with
    # carousel/single-image; the LLM chooses one. Ad set must also have
    # is_dynamic_creative=true (see create_adset).
    if asset_feed_spec is not None:
        spec_str = asset_feed_spec if isinstance(asset_feed_spec, str) else json.dumps(asset_feed_spec)
        if spec_str and spec_str.strip() and spec_str.strip() != "{}":
            if not page_id:
                return {"error": {"message": "asset_feed_spec requires page_id (Meta needs the Page identity for DCO)"}}
            data["asset_feed_spec"] = spec_str
            data["object_story_spec"] = json.dumps({"page_id": str(page_id).strip()})
            return _send(api_url, data)
    if object_story_id:
        data["object_story_id"] = str(object_story_id).strip()
        return _send(api_url, data)
    if object_story_spec:
        spec = object_story_spec
        parsed = None
        if isinstance(spec, str):
            try:
                parsed = json.loads(spec)
            except (TypeError, ValueError):
                pass
        elif isinstance(spec, dict):
            parsed = spec
        if parsed is not None and isinstance(parsed, dict):
            spec = dict(parsed)
            iuid = (instagram_user_id or "").strip()
            iaid = (instagram_actor_id or "").strip()
            # Ads Manager's "Identity" panel reads instagram_actor_id from
            # inside object_story_spec, not the top-level creative field.
            # Meta v22 accepts both as top-level kwargs (the API call
            # succeeds either way), but without the embedded actor_id the
            # UI shows "Use Facebook Page" + the "must be associated with
            # an Instagram account" red error. Embed both inside the spec
            # so the UI Identity row resolves correctly. iaid wins if both
            # are set; otherwise iuid is mirrored as actor_id since they
            # refer to the same IG identity in v22+.
            if iaid:
                spec["instagram_actor_id"] = iaid
                if iuid:
                    spec["instagram_user_id"] = iuid
            elif iuid:
                spec["instagram_user_id"] = iuid
                spec["instagram_actor_id"] = iuid
            # Merge link_url, message, headline, description into the spec when
            # the caller passed them as separate kwargs (e.g. direct callers
            # using the inline form). Existing spec values win — we only fill
            # blanks so a fully-formed object_story_spec from the caller is
            # never stomped.
            if link_url or message or headline or description:
                link_data = spec.get("link_data") or {}
                if not isinstance(link_data, dict):
                    link_data = {}
                if link_url:
                    link_data["link"] = link_url
                if message is not None and str(message).strip():
                    link_data["message"] = str(message).strip()
                if headline and str(headline).strip() and not link_data.get("name"):
                    link_data["name"] = str(headline).strip()
                if description and str(description).strip() and not link_data.get("description"):
                    link_data["description"] = str(description).strip()
                if link_data:
                    spec["link_data"] = link_data
            data["object_story_spec"] = json.dumps(spec)
        else:
            # Unparseable or non-dict: send as-is (original string or json.dumps of other)
            data["object_story_spec"] = object_story_spec if isinstance(object_story_spec, str) else json.dumps(object_story_spec)
    elif page_id and (carousel_cards is not None) and message:
        # Carousel: 2-10 cards, each with link + image_hash (or video_id).
        children = _build_carousel_child_attachments(carousel_cards, default_link=link_url)
        if not children:
            return {"error": {"message": "carousel_cards must be a list of 2-10 cards, each with at least 'link' and 'image_hash' (or 'video_id'). Got insufficient/invalid cards."}}
        link_data = {
            "link": str(link_url or children[0]["link"]).strip(),
            "message": str(message).strip(),
            "child_attachments": children,
            # `multi_share_optimized: true` lets Meta pick the best card order;
            # `multi_share_end_card: false` removes the auto "see more" card so
            # the brief's exact 2-10 cards are what users see.
            "multi_share_optimized": True,
            "multi_share_end_card": False,
        }
        if instant_experience_id and str(instant_experience_id).strip():
            # Carousel + Instant Experience = Collection ad with carousel cover.
            link_data["instant_experience_id"] = str(instant_experience_id).strip()
        if headline and str(headline).strip():
            link_data["name"] = str(headline).strip()
        if description and str(description).strip():
            link_data["description"] = str(description).strip()
        oss = {
            "page_id": str(page_id),
            "link_data": link_data,
        }
        iuid = (instagram_user_id or "").strip()
        iaid = (instagram_actor_id or "").strip()
        if iaid:
            oss["instagram_actor_id"] = iaid
            if iuid:
                oss["instagram_user_id"] = iuid
        elif iuid:
            oss["instagram_user_id"] = iuid
            oss["instagram_actor_id"] = iuid
        data["object_story_spec"] = json.dumps(oss)
    elif page_id and link_url and message and image_hash:
        link_data = {"link": link_url, "message": message, "image_hash": image_hash}
        if instant_experience_id and str(instant_experience_id).strip():
            # Single-image + Instant Experience = Collection ad with single cover.
            link_data["instant_experience_id"] = str(instant_experience_id).strip()
        if headline and str(headline).strip():
            link_data["name"] = str(headline).strip()
        if description and str(description).strip():
            link_data["description"] = str(description).strip()
        oss = {
            "page_id": str(page_id),
            "link_data": link_data,
        }
        iuid = (instagram_user_id or "").strip()
        iaid = (instagram_actor_id or "").strip()
        # Mirror the embedded-IG behaviour from the object_story_spec branch:
        # Ads Manager's Identity panel reads instagram_actor_id from inside
        # the spec, not the top-level kwarg.
        if iaid:
            oss["instagram_actor_id"] = iaid
            if iuid:
                oss["instagram_user_id"] = iuid
        elif iuid:
            oss["instagram_user_id"] = iuid
            oss["instagram_actor_id"] = iuid
        data["object_story_spec"] = json.dumps(oss)
    else:
        return {"error": {"message": "Provide object_story_spec (JSON) or page_id, link_url, message, and image_hash"}}
    return _send(api_url, data)


def _page_id_from_object_story_id(object_story_id):
    s = str(object_story_id or "").strip()
    if "_" in s:
        return s.split("_", 1)[0].strip()
    return ""


def validate_creative_spec(account_id, token, *, source_instagram_media_id=None,
                           object_story_id=None, instagram_user_id=None, page_id=None,
                           adset_id=None, name="__validate_only__"):
    """READ-ONLY creative probe: POST with ``execution_options=["validate_only"]``.

    Meta runs its full server-side validation (media eligibility, identity,
    objective fit) and COMMITS NOTHING — no creative, no ad, nothing appears
    in Ads Manager. Used to get Meta's real verdict on "can this IG media /
    Page post be used as a creative" without a write (e.g. subcode 1815279
    "Instagram video must be uploaded to Facebook" on IG reels).

    Two probe levels:
      - creative-level: POST /act_X/adcreatives (default).
      - ad-level (when ``adset_id`` is given): POST /act_X/ads with the
        creative spec inline — additionally validates objective/optimization
        fit against that existing ad set. The ad set is not modified.

    Returns {"verdict": "ACCEPTED"|"REJECTED", "level": ..., "request": {...},
    "meta_response": <raw>} — request echoes the exact fields sent minus token.
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    if not (source_instagram_media_id or object_story_id):
        return {"error": {"message": "source_instagram_media_id or object_story_id is required"}}
    spec = {"name": name}
    if source_instagram_media_id:
        spec["source_instagram_media_id"] = str(source_instagram_media_id).strip()
    if object_story_id:
        spec["object_story_id"] = str(object_story_id).strip()
    if instagram_user_id:
        spec["instagram_user_id"] = str(instagram_user_id).strip()
    # NOTE: do NOT wrap page_id in object_story_spec next to object_story_id —
    # Meta then expects link_data.link and rejects with 2061015. The Page is
    # derived from the post id itself (page_id_post_id).
    if adset_id:
        level = "ad"
        url = f"{BASE_URL}/{act}/ads"
        data = {
            "access_token": token,
            "name": name,
            "adset_id": str(adset_id).strip(),
            "status": "PAUSED",
            "creative": json.dumps(spec),
            "execution_options": json.dumps(["validate_only"]),
        }
    else:
        level = "creative"
        url = f"{BASE_URL}/{act}/adcreatives"
        data = {"access_token": token, **spec, "execution_options": json.dumps(["validate_only"])}
    resp = _post(url, data)
    shown = {k: v for k, v in data.items() if k != "access_token"}
    ok = isinstance(resp, dict) and not resp.get("error")
    return {
        "verdict": "ACCEPTED" if ok else "REJECTED",
        "level": level,
        "committed": False,
        "request": shown,
        "meta_response": resp,
    }


def create_creative_with_instagram_fallback(
    account_id,
    token,
    name,
    object_story_spec=None,
    page_id=None,
    link_url=None,
    message=None,
    image_hash=None,
    object_story_id=None,
    instagram_actor_id=None,
    instagram_user_id=None,
    authorization_category=None,
    source_instagram_media_id=None,
    instagram_permalink_url=None,
    headline=None,
    description=None,
    carousel_cards=None,
    instant_experience_id=None,
    branded_content_sponsor_page_id=None,
    asset_feed_spec=None,
    bylines=None,
    destination_spec=None,
):
    """
    Try create_creative with Instagram identity variants Meta accepts across API versions:
    user_id only, actor_id only, page-linked IG, page-backed IG, then Facebook-only (no IG fields).
    Stops on first non-Instagram-related error.
    If source_instagram_media_id or instagram_permalink_url is provided, delegates directly (no fallback needed).
    """
    # source_instagram_media_id is a direct path — no Instagram identity fallback needed
    if source_instagram_media_id:
        return create_creative(
            account_id, token, name,
            source_instagram_media_id=source_instagram_media_id,
            authorization_category=authorization_category,
            bylines=bylines,
            destination_spec=destination_spec,
        )

    # instagram_permalink_url is a direct path for IG feed/carousel/reel posts
    # where source_instagram_media_id is rejected. Needs an IG account id.
    # Also forward page_id so political ads can bind the FB Page identity
    # (Meta requires this for ISSUES_ELECTIONS_POLITICS — subcode 2708016).
    if instagram_permalink_url and str(instagram_permalink_url).strip():
        return create_creative(
            account_id, token, name,
            instagram_permalink_url=instagram_permalink_url,
            instagram_user_id=instagram_user_id,
            instagram_actor_id=instagram_actor_id,
            page_id=page_id,
            authorization_category=authorization_category,
            bylines=bylines,
            destination_spec=destination_spec,
        )

    iu = str(instagram_user_id or "").strip() if instagram_user_id else ""
    ia = str(instagram_actor_id or "").strip() if instagram_actor_id else ""
    pid = str(page_id or "").strip() if page_id else ""
    if not pid and object_story_id:
        pid = _page_id_from_object_story_id(object_story_id)

    # v22: instagram_actor_id is deprecated for /adcreatives. Migrate any
    # caller-supplied actor_id into instagram_user_id (Meta treats them as
    # the same identity in v22+). Keep one final actor_id-only attempt at
    # the bottom of the chain as a defence-in-depth for accounts on legacy
    # Meta versions; that branch will be removed when v23 ships.
    attempts: List[Dict[str, Any]] = []
    if iu:
        attempts.append({"instagram_user_id": iu, "instagram_actor_id": None})
    if ia and ia != iu:
        # Treat caller's actor_id as a user_id candidate first.
        attempts.append({"instagram_user_id": ia, "instagram_actor_id": None})

    if pid:
        pg = get_instagram_accounts(pid, token)
        if isinstance(pg, dict) and not pg.get("error"):
            ib = pg.get("instagram_business_account")
            if isinstance(ib, dict):
                iid = str(ib.get("id") or "").strip()
                if iid and not any((a.get("instagram_user_id") or "") == iid for a in attempts):
                    attempts.append({"instagram_user_id": iid, "instagram_actor_id": None})
        pb = get_page_backed_instagram_accounts(pid, token)
        rows = pb.get("data") or [] if isinstance(pb, dict) else []
        for row in rows:
            if isinstance(row, dict):
                bid = str(row.get("id") or "").strip()
                if bid and not any((a.get("instagram_user_id") or "") == bid for a in attempts):
                    attempts.append({"instagram_user_id": bid, "instagram_actor_id": None})

    # Legacy actor_id-only attempt — last resort before going FB-only.
    # Meta v22 deprecated this field; remove this branch when bumping to v23.
    if ia and not any(
        (a.get("instagram_actor_id") or "") == ia for a in attempts
    ):
        attempts.append({"instagram_user_id": None, "instagram_actor_id": ia})

    attempts.append({"instagram_user_id": None, "instagram_actor_id": None})

    last: Any = None
    seen_pairs = set()
    for extra in attempts:
        key = (extra.get("instagram_user_id"), extra.get("instagram_actor_id"))
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        r = create_creative(
            account_id,
            token,
            name,
            object_story_spec=object_story_spec,
            page_id=page_id,
            link_url=link_url,
            message=message,
            image_hash=image_hash,
            object_story_id=object_story_id,
            instagram_actor_id=extra.get("instagram_actor_id"),
            instagram_user_id=extra.get("instagram_user_id"),
            authorization_category=authorization_category,
            headline=headline,
            description=description,
            carousel_cards=carousel_cards,
            instant_experience_id=instant_experience_id,
            branded_content_sponsor_page_id=branded_content_sponsor_page_id,
            asset_feed_spec=asset_feed_spec,
            bylines=bylines,
            destination_spec=destination_spec,
        )
        last = r
        if "error" not in r:
            used = {k: v for k, v in extra.items() if v}
            if used:
                r["_instagram_fallback_used"] = used
            return r
        err = r.get("error") or {}
        em = str(err.get("message") or "").lower()
        if "instagram" not in em and "actor" not in em and "ig account" not in em:
            return r
    return last


def get_creatives(account_id, token, fields=None, limit=100):
    """List creatives for an ad account. Alias for get_ad_creatives. Real API: GET /act_{id}/adcreatives."""
    return get_ad_creatives(account_id, token, fields=fields, limit=limit)


def delete_creative(creative_id, token):
    """Delete an ad creative. Real API: DELETE /{creative_id}. Requires ads_management."""
    if not creative_id or not str(creative_id).strip():
        return {"error": {"message": "creative_id is required"}}
    url = f"{BASE_URL}/{str(creative_id).strip()}"
    return _delete(url, {"access_token": token})


# --- Comparison & analysis ---
def compare_campaigns(account_id, token, campaign_ids=None, date_preset="last_30d", time_start=None, time_end=None, limit=50):
    """Compare campaigns by performance. Returns side-by-side metrics; optionally filter by campaign_ids (list)."""
    data = get_campaign_performance(account_id, token, date_preset=date_preset, time_start=time_start, time_end=time_end, limit=limit)
    if "error" in data:
        return data
    rows = data.get("data") or []
    if campaign_ids:
        ids_set = {str(c).strip() for c in (campaign_ids if isinstance(campaign_ids, list) else [campaign_ids])}
        rows = [r for r in rows if r.get("campaign_id") in ids_set]
    comparison = []
    for r in rows:
        spend = float(r.get("spend") or 0)
        impressions = int(r.get("impressions") or 0)
        clicks = int(r.get("clicks") or 0)
        ctr = (clicks / impressions * 100) if impressions else 0
        comparison.append({
            "campaign_id": r.get("campaign_id"),
            "campaign_name": r.get("campaign_name"),
            "impressions": impressions,
            "clicks": clicks,
            "spend": round(spend, 2),
            "cpc": r.get("cpc"),
            "cpm": r.get("cpm"),
            "ctr": round(ctr, 2),
        })
    # Summary: best by CTR, by spend
    by_ctr = sorted([c for c in comparison if c["impressions"] > 0], key=lambda x: -x["ctr"])[:3]
    by_spend = sorted(comparison, key=lambda x: -x["spend"])[:3]
    return {"comparison": comparison, "summary": {"top_by_ctr": by_ctr, "top_by_spend": by_spend}, "date_preset": date_preset, "time_range": {"since": time_start, "until": time_end} if time_start and time_end else None}


def compare_adsets(account_id, token, adset_ids=None, date_preset="last_30d", time_start=None, time_end=None, limit=50):
    """Compare ad sets by performance. Optionally filter by adset_ids (list)."""
    data = get_adset_performance(account_id, token, date_preset=date_preset, time_start=time_start, time_end=time_end, limit=limit)
    if "error" in data:
        return data
    rows = data.get("data") or []
    if adset_ids:
        ids_set = {str(a).strip() for a in (adset_ids if isinstance(adset_ids, list) else [adset_ids])}
        rows = [r for r in rows if (r.get("adset_id")) in ids_set]
    comparison = []
    for r in rows:
        spend = float(r.get("spend") or 0)
        impressions = int(r.get("impressions") or 0)
        clicks = int(r.get("clicks") or 0)
        ctr = (clicks / impressions * 100) if impressions else 0
        comparison.append({
            "adset_id": r.get("adset_id"),
            "adset_name": r.get("adset_name"),
            "impressions": impressions,
            "clicks": clicks,
            "spend": round(spend, 2),
            "cpc": r.get("cpc"),
            "cpm": r.get("cpm"),
            "ctr": round(ctr, 2),
        })
    by_ctr = sorted([c for c in comparison if c["impressions"] > 0], key=lambda x: -x["ctr"])[:3]
    by_spend = sorted(comparison, key=lambda x: -x["spend"])[:3]
    return {"comparison": comparison, "summary": {"top_by_ctr": by_ctr, "top_by_spend": by_spend}, "date_preset": date_preset, "time_range": {"since": time_start, "until": time_end} if time_start and time_end else None}


def compare_ads(account_id, token, ad_ids=None, date_preset="last_30d", time_start=None, time_end=None, limit=50):
    """Compare ads by performance. Optionally filter by ad_ids (list)."""
    data = get_ad_performance(account_id, token, date_preset=date_preset, time_start=time_start, time_end=time_end, limit=limit)
    if "error" in data:
        return data
    rows = data.get("data") or []
    if ad_ids:
        ids_set = {str(a).strip() for a in (ad_ids if isinstance(ad_ids, list) else [ad_ids])}
        rows = [r for r in rows if (r.get("ad_id")) in ids_set]
    comparison = []
    for r in rows:
        spend = float(r.get("spend") or 0)
        impressions = int(r.get("impressions") or 0)
        clicks = int(r.get("clicks") or 0)
        ctr = (clicks / impressions * 100) if impressions else 0
        comparison.append({
            "ad_id": r.get("ad_id"),
            "ad_name": r.get("ad_name"),
            "impressions": impressions,
            "clicks": clicks,
            "spend": round(spend, 2),
            "cpc": r.get("cpc"),
            "cpm": r.get("cpm"),
            "ctr": round(ctr, 2),
        })
    by_ctr = sorted([c for c in comparison if c["impressions"] > 0], key=lambda x: -x["ctr"])[:3]
    by_spend = sorted(comparison, key=lambda x: -x["spend"])[:3]
    return {"comparison": comparison, "summary": {"top_by_ctr": by_ctr, "top_by_spend": by_spend}, "date_preset": date_preset, "time_range": {"since": time_start, "until": time_end} if time_start and time_end else None}


def _parse_action_value(action_values):
    """Sum value from action_values (e.g. purchase value). action_values is list of {action_type, value}."""
    total = 0
    if not action_values:
        return total
    for item in (action_values if isinstance(action_values, list) else []):
        if isinstance(item, dict) and "value" in item:
            try:
                total += float(item["value"])
            except (TypeError, ValueError):
                pass
        elif isinstance(item, str):
            try:
                total += float(item)
            except (TypeError, ValueError):
                pass
    return total


def roas_analysis(account_id, token, level="campaign", date_preset="last_30d", time_start=None, time_end=None, limit=50):
    """ROAS (Return on Ad Spend) analysis. level: campaign, adset, or ad. Uses actions and action_values from insights."""
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}
    url = f"{BASE_URL}/{act}/insights"
    level = (level or "campaign").lower()
    if level not in ("campaign", "adset", "ad"):
        level = "campaign"
    params = {
        "access_token": token,
        "level": level,
        "fields": "spend,impressions,clicks,actions,action_values",
        "limit": min(max(1, int(limit)), MAX_LIMIT),
    }
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset or "last_30d"
    data = _get(url, params)
    if "error" in data:
        return data
    rows = data.get("data") or []
    id_key = "campaign_id" if level == "campaign" else ("adset_id" if level == "adset" else "ad_id")
    name_key = "campaign_name" if level == "campaign" else ("adset_name" if level == "adset" else "ad_name")
    result = []
    for r in rows:
        spend = float(r.get("spend") or 0)
        value = _parse_action_value(r.get("action_values"))
        roas = (value / spend) if spend > 0 else 0
        result.append({
            id_key: r.get(id_key),
            name_key: r.get(name_key),
            "spend": round(spend, 2),
            "action_value": round(value, 2),
            "roas": round(roas, 2),
            "impressions": int(r.get("impressions") or 0),
            "clicks": int(r.get("clicks") or 0),
        })
    result.sort(key=lambda x: -x["roas"])
    return {"level": level, "roas_analysis": result, "date_preset": date_preset, "time_range": {"since": time_start, "until": time_end} if time_start and time_end else None}


def budget_recommendation(account_id, token, level="campaign", date_preset="last_30d", time_start=None, time_end=None, limit=30):
    """Budget recommendations based on performance: suggest scaling top performers and reviewing low performers."""
    if level == "campaign":
        data = get_campaign_performance(account_id, token, date_preset=date_preset, time_start=time_start, time_end=time_end, limit=limit)
        id_key, name_key = "campaign_id", "campaign_name"
    elif level == "adset":
        data = get_adset_performance(account_id, token, date_preset=date_preset, time_start=time_start, time_end=time_end, limit=limit)
        id_key, name_key = "adset_id", "adset_name"
    else:
        data = get_ad_performance(account_id, token, date_preset=date_preset, time_start=time_start, time_end=time_end, limit=limit)
        id_key, name_key = "ad_id", "ad_name"
    if "error" in data:
        return data
    rows = data.get("data") or []
    # Build list with spend and ctr
    items = []
    for r in rows:
        spend = float(r.get("spend") or 0)
        impressions = int(r.get("impressions") or 0)
        clicks = int(r.get("clicks") or 0)
        ctr = (clicks / impressions * 100) if impressions else 0
        items.append({"id": r.get(id_key), "name": r.get(name_key), "spend": spend, "ctr": ctr, "impressions": impressions})
    # Top by CTR (with meaningful spend) = scale; low CTR with spend = review
    with_spend = [i for i in items if i["spend"] > 0]
    by_ctr = sorted(with_spend, key=lambda x: -x["ctr"])
    scale_candidates = by_ctr[:5]
    review_candidates = sorted(with_spend, key=lambda x: x["ctr"])[:5]
    recommendations = []
    for i in scale_candidates:
        recommendations.append({"action": "consider_scale", "id": i["id"], "name": i["name"], "reason": f"High CTR {i['ctr']:.2f}%, spend ${i['spend']:.2f}"})
    for i in review_candidates:
        if i["ctr"] < 1.0 and i["spend"] > 1:
            recommendations.append({"action": "consider_review_or_pause", "id": i["id"], "name": i["name"], "reason": f"Low CTR {i['ctr']:.2f}%, spend ${i['spend']:.2f}"})
    return {"level": level, "recommendations": recommendations, "scale_candidates": [{"id": i["id"], "name": i["name"], "ctr": round(i["ctr"], 2), "spend": round(i["spend"], 2)} for i in scale_candidates], "review_candidates": [{"id": i["id"], "name": i["name"], "ctr": round(i["ctr"], 2), "spend": round(i["spend"], 2)} for i in review_candidates], "date_preset": date_preset, "time_range": {"since": time_start, "until": time_end} if time_start and time_end else None}


# --- Conversions API ---
def send_conversion_event(pixel_id, token, event_name, event_time, action_source, user_data=None, custom_data=None, event_id=None, event_source_url=None):
    """Send a server-side event to Conversions API. POST /{pixel_id}/events. Requires token with ads_management or pixel ownership.
    event_time: Unix timestamp (seconds). action_source: website, app, phone_call, chat, physical_store, system_generated, other.
    user_data and custom_data can be dict or JSON string."""
    if not pixel_id or not str(pixel_id).strip():
        return {"error": {"message": "pixel_id is required"}}
    if not event_name or not event_time or not action_source:
        return {"error": {"message": "event_name, event_time (unix), and action_source are required"}}
    try:
        et = int(event_time)
    except (TypeError, ValueError):
        return {"error": {"message": "event_time must be a Unix timestamp (seconds)"}}
    url = f"{BASE_URL}/{str(pixel_id).strip()}/events"
    payload = {
        "event_name": str(event_name).strip(),
        "event_time": et,
        "action_source": str(action_source).strip(),
    }
    if user_data is not None:
        payload["user_data"] = user_data if isinstance(user_data, dict) else json.loads(user_data) if isinstance(user_data, str) else {}
    if custom_data is not None:
        payload["custom_data"] = custom_data if isinstance(custom_data, dict) else json.loads(custom_data) if isinstance(custom_data, str) else {}
    if event_id:
        payload["event_id"] = str(event_id).strip()
    if event_source_url:
        payload["event_source_url"] = str(event_source_url).strip()
    body = {"data": [payload]}
    return _post_json(url, body, params={"access_token": token})


# --- Catalog API ---
def get_product_catalogs(token, business_id=None, limit=100):
    """List product catalogs. GET /me/product_catalogs or GET /{business_id}/owned_product_catalogs. Requires catalog_management or business_management."""
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    if business_id and str(business_id).strip():
        url = f"{BASE_URL}/{str(business_id).strip()}/owned_product_catalogs"
    else:
        url = f"{BASE_URL}/me/product_catalogs"
    params = {"access_token": token, "fields": "id,name,product_count,vertical", "limit": limit}
    return _get(url, params)


def get_catalog(catalog_id, token, fields=None):
    """Get a single product catalog. GET /{catalog_id}. Requires catalog_management."""
    if not catalog_id or not str(catalog_id).strip():
        return {"error": {"message": "catalog_id is required"}}
    url = f"{BASE_URL}/{str(catalog_id).strip()}"
    params = {"access_token": token, "fields": fields or "id,name,product_count,vertical,created_time"}
    return _get(url, params)


def get_catalog_products(catalog_id, token, limit=100, filter_spec=None, fields=None):
    """List products in a catalog. GET /{catalog_id}/products. Requires catalog_management. Do not rely on pagination for full catalog."""
    if not catalog_id or not str(catalog_id).strip():
        return {"error": {"message": "catalog_id is required"}}
    url = f"{BASE_URL}/{str(catalog_id).strip()}/products"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "limit": limit, "fields": fields or "id,name,category,description,price"}
    if filter_spec:
        params["filter"] = filter_spec if isinstance(filter_spec, str) else json.dumps(filter_spec)
    return _get(url, params)


# --- Business Management API ---
def get_business_owned_ad_accounts(business_id, token, limit=100):
    """List ad accounts owned by a business. GET /{business_id}/owned_ad_accounts. Requires business_management."""
    if not business_id or not str(business_id).strip():
        return {"error": {"message": "business_id is required"}}
    url = f"{BASE_URL}/{str(business_id).strip()}/owned_ad_accounts"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": "id,name,account_id,account_status", "limit": limit}
    return _get(url, params)


def get_business_owned_pages(business_id, token, limit=100):
    """List Facebook Pages owned by a business. GET /{business_id}/owned_pages. Requires business_management."""
    if not business_id or not str(business_id).strip():
        return {"error": {"message": "business_id is required"}}
    url = f"{BASE_URL}/{str(business_id).strip()}/owned_pages"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": "id,name,category,access_token", "limit": limit}
    return _get(url, params)


def get_business_client_ad_accounts(business_id, token, limit=100):
    """List client ad accounts (agency access). GET /{business_id}/client_ad_accounts. Requires business_management."""
    if not business_id or not str(business_id).strip():
        return {"error": {"message": "business_id is required"}}
    url = f"{BASE_URL}/{str(business_id).strip()}/client_ad_accounts"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": "id,name,account_id", "limit": limit}
    return _get(url, params)


# --- Meta Business Extension (Page connected assets) ---
def get_page_extension_info(page_id, token, fields=None):
    """Get Page info with connected Instagram/extension fields. GET /{page_id}. Fields: instagram_business_account. Requires pages_show_list or pages_read_engagement."""
    if not page_id or not str(page_id).strip():
        return {"error": {"message": "page_id is required"}}
    url = f"{BASE_URL}/{str(page_id).strip()}"
    params = {"access_token": token, "fields": fields or "id,name,category,instagram_business_account"}
    return _get(url, params)


# --- Commerce Platform (product sets, feeds) ---
def get_catalog_product_sets(catalog_id, token, limit=100):
    """List product sets in a catalog. GET /{catalog_id}/product_sets. Commerce Platform. Requires catalog_management."""
    if not catalog_id or not str(catalog_id).strip():
        return {"error": {"message": "catalog_id is required"}}
    url = f"{BASE_URL}/{str(catalog_id).strip()}/product_sets"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": "id,name,product_count", "limit": limit}
    return _get(url, params)


def get_catalog_product_feeds(catalog_id, token, limit=100):
    """List product feeds for a catalog. GET /{catalog_id}/product_feeds. Commerce Platform. Requires catalog_management."""
    if not catalog_id or not str(catalog_id).strip():
        return {"error": {"message": "catalog_id is required"}}
    url = f"{BASE_URL}/{str(catalog_id).strip()}/product_feeds"
    limit = min(max(1, int(limit) if limit else 100), MAX_LIMIT)
    params = {"access_token": token, "fields": "id,name,schedule,encoding", "limit": limit}
    return _get(url, params)


# ---------- Performance Summary----------


def get_performance_summary(account_id, token, date_preset="last_7d", time_start=None, time_end=None):
    """
    Aggregated performance dashboard: all active campaigns with totals for CTR, CPC, spend, conversions, ROAS.
    Returns { campaigns: [...], totals: { spend, impressions, clicks, ctr, cpc, conversions, roas } }.
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}

    url = f"{BASE_URL}/{act}/insights"
    fields = "campaign_id,campaign_name,impressions,clicks,spend,cpc,cpm,ctr,actions,action_values"
    params = {
        "access_token": token,
        "level": "campaign",
        "fields": fields,
        "limit": MAX_LIMIT,
    }
    if time_start and time_end:
        params["time_range"] = json.dumps({"since": time_start, "until": time_end})
    else:
        params["date_preset"] = date_preset

    data = _get(url, params)
    if isinstance(data, dict) and "error" in data:
        return data

    campaigns = []
    total_spend = 0.0
    total_impressions = 0
    total_clicks = 0
    total_conversions = 0
    total_purchase_value = 0.0

    for row in data.get("data", []):
        impressions = int(row.get("impressions") or 0)
        clicks = int(row.get("clicks") or 0)
        spend = float(row.get("spend") or 0)

        # Extract conversions from actions
        conversions = 0
        for action in (row.get("actions") or []):
            if action.get("action_type") in ("offsite_conversion.fb_pixel_purchase", "purchase", "lead", "complete_registration"):
                conversions += int(action.get("value") or 0)

        # Extract ROAS from action_values
        purchase_value = 0.0
        for av in (row.get("action_values") or []):
            if av.get("action_type") in ("offsite_conversion.fb_pixel_purchase", "purchase"):
                purchase_value += float(av.get("value") or 0)

        roas = round(purchase_value / spend, 2) if spend > 0 else 0.0

        campaigns.append({
            "campaign_id": row.get("campaign_id"),
            "campaign_name": row.get("campaign_name"),
            "impressions": impressions,
            "clicks": clicks,
            "spend": round(spend, 2),
            "ctr": round(float(row.get("ctr") or 0), 4),
            "cpc": round(float(row.get("cpc") or 0), 2),
            "conversions": conversions,
            "purchase_value": round(purchase_value, 2),
            "roas": roas,
        })

        total_spend += spend
        total_impressions += impressions
        total_clicks += clicks
        total_conversions += conversions
        total_purchase_value += purchase_value

    total_ctr = round((total_clicks / total_impressions * 100) if total_impressions > 0 else 0, 4)
    total_cpc = round((total_spend / total_clicks) if total_clicks > 0 else 0, 2)
    total_roas = round((total_purchase_value / total_spend) if total_spend > 0 else 0, 2)

    return {
        "account_id": account_id,
        "date_preset": date_preset,
        "campaigns": campaigns,
        "totals": {
            "spend": round(total_spend, 2),
            "impressions": total_impressions,
            "clicks": total_clicks,
            "ctr": total_ctr,
            "cpc": total_cpc,
            "conversions": total_conversions,
            "purchase_value": round(total_purchase_value, 2),
            "roas": total_roas,
        },
        "campaign_count": len(campaigns),
    }


# ---------- Auto-Optimization ----------


def auto_optimize_account(account_id, token, min_impressions=1000, min_spend=5.0, dry_run=False):
    """
    Analyze all active campaigns/adsets/ads and apply optimization rules.
    Rules:
    - Pause ads with CTR < 0.5% and spend > min_spend (after min_impressions)
    - Increase budget by 20% for adsets with CTR > 2% and ROAS > 2x
    - Decrease budget by 20% for adsets with ROAS < 0.5x
    - Pause adsets with 0 conversions after spend > 2x target CPA

    Returns list of actions taken (or would-be-taken if dry_run=True).
    """
    act = _account_id(account_id)
    if not act:
        return {"error": {"message": "account_id is required"}}

    actions = []

    # Fetch ad-level performance (last 7 days)
    ad_data = get_ad_performance(account_id, token, date_preset="last_7d", limit=200)
    if isinstance(ad_data, dict) and "error" in ad_data:
        return {"error": ad_data["error"], "actions": []}

    for row in ad_data.get("data", []):
        ad_id = row.get("ad_id")
        impressions = int(row.get("impressions") or 0)
        clicks = int(row.get("clicks") or 0)
        spend = float(row.get("spend") or 0)
        ctr = float(row.get("ctr") or 0)

        if impressions >= min_impressions and spend >= min_spend and ctr < 0.5:
            action = {
                "type": "pause_ad",
                "ad_id": ad_id,
                "ad_name": row.get("ad_name", ""),
                "reason": f"Low CTR ({ctr:.2f}%) with {impressions} impressions and ${spend:.2f} spend",
                "metrics": {"ctr": ctr, "impressions": impressions, "spend": spend},
            }
            if not dry_run:
                result = pause_ad(ad_id, token)
                action["result"] = result
                action["applied"] = "error" not in (result if isinstance(result, dict) else {})
            else:
                action["applied"] = False
                action["dry_run"] = True
            actions.append(action)

    # Fetch adset-level performance for budget optimization
    adset_data = get_adset_performance(account_id, token, date_preset="last_7d", limit=200)
    if isinstance(adset_data, dict) and "error" not in adset_data:
        # Also fetch adset details for current budgets
        adset_details = get_ad_sets(account_id, token, limit=200)
        budget_map = {}
        if isinstance(adset_details, dict) and "data" in adset_details:
            for adset in adset_details["data"]:
                adset_id = adset.get("id")
                if adset_id:
                    budget_map[adset_id] = {
                        "daily_budget": int(adset.get("daily_budget") or 0),
                        "lifetime_budget": int(adset.get("lifetime_budget") or 0),
                    }

        for row in adset_data.get("data", []):
            adset_id = row.get("adset_id")
            impressions = int(row.get("impressions") or 0)
            clicks = int(row.get("clicks") or 0)
            spend = float(row.get("spend") or 0)
            ctr = float(row.get("ctr") or 0)

            if impressions < min_impressions:
                continue

            # Get current budget
            current = budget_map.get(adset_id, {})
            current_daily = current.get("daily_budget", 0)

            # High performer: CTR > 2% -> increase budget by 20%
            if ctr > 2.0 and current_daily > 0:
                new_budget = int(current_daily * 1.2)
                action = {
                    "type": "increase_budget",
                    "adset_id": adset_id,
                    "adset_name": row.get("adset_name", ""),
                    "reason": f"High CTR ({ctr:.2f}%) - scaling budget +20%",
                    "current_daily_budget": current_daily,
                    "new_daily_budget": new_budget,
                    "metrics": {"ctr": ctr, "impressions": impressions, "spend": spend},
                }
                if not dry_run:
                    result = update_budget(adset_id, token, daily_budget=new_budget)
                    action["result"] = result
                    action["applied"] = "error" not in (result if isinstance(result, dict) else {})
                else:
                    action["applied"] = False
                    action["dry_run"] = True
                actions.append(action)

            # Low performer: CTR < 0.5% and significant spend -> decrease budget by 20%
            elif ctr < 0.5 and spend >= min_spend * 2 and current_daily > 0:
                new_budget = max(int(current_daily * 0.8), 100)  # minimum 100 cents = $1
                action = {
                    "type": "decrease_budget",
                    "adset_id": adset_id,
                    "adset_name": row.get("adset_name", ""),
                    "reason": f"Low CTR ({ctr:.2f}%) with ${spend:.2f} spend - reducing budget -20%",
                    "current_daily_budget": current_daily,
                    "new_daily_budget": new_budget,
                    "metrics": {"ctr": ctr, "impressions": impressions, "spend": spend},
                }
                if not dry_run:
                    result = update_budget(adset_id, token, daily_budget=new_budget)
                    action["result"] = result
                    action["applied"] = "error" not in (result if isinstance(result, dict) else {})
                else:
                    action["applied"] = False
                    action["dry_run"] = True
                actions.append(action)

    return {
        "account_id": account_id,
        "dry_run": dry_run,
        "actions_count": len(actions),
        "actions": actions,
        "rules_applied": [
            "Pause ads with CTR < 0.5% (min {} impressions, min ${} spend)".format(min_impressions, min_spend),
            "Increase budget +20% for adsets with CTR > 2%",
            "Decrease budget -20% for adsets with CTR < 0.5% and high spend",
        ],
    }


# ---------------------------------------------------------------------------
# Promotable-post verification (2026-09-22)
#
# Incident: an agent supplied object_story_id values that did not exist on the
# page (it fabricated them instead of reading /{page}/posts). Meta answered
# with 100/1487790 "promoted object is invalid" and 100/2446187 "this post
# can't be boosted", which the agent then reported to the user as "your posts
# are not eligible" — the exact opposite of the truth (all 6 posts WERE
# eligible; the ids pointed at nothing).
#
# EXISTENCE and ELIGIBILITY are different failures and must not be conflated:
#   - exists=False  -> the id is wrong. Nothing to boost. Never overridable.
#   - eligible=False-> the post is real but Meta declines it. The caller may
#                      still proceed (video soft-path / allow_ineligible_post),
#                      which is why this helper REPORTS and does not decide.
# ---------------------------------------------------------------------------

# Meta's "object does not exist / cannot be loaded" family. Subcode 33 and the
# GraphMethodException both mean "this id is not reachable by this token".
_NONEXISTENT_ERROR_SUBCODES = {33}
_NONEXISTENT_ERROR_TYPES = {"GraphMethodException"}


def verify_promotable_post(object_story_id, token):
    """Read-only probe of a ``{page_id}_{post_id}`` story id.

    Returns ``{exists, eligible, message, page_id, video_buying_eligibility,
    is_video, error}``. Never raises and never mutates anything: a probe
    failure degrades to ``exists=None`` (unknown) so callers can choose to
    proceed rather than block on a transient read error.
    """
    out = {
        "object_story_id": str(object_story_id or "").strip(),
        "exists": None,
        "eligible": None,
        "message": "",
        "page_id": "",
        "video_buying_eligibility": None,
        "is_video": False,
        "error": None,
    }
    oid = out["object_story_id"]
    if not oid:
        out["exists"] = False
        out["error"] = "object_story_id is empty"
        return out
    if "_" in oid:
        out["page_id"] = oid.split("_", 1)[0].strip()
    # Page-scoped token: is_eligible_for_promotion / message are not readable
    # with a bare user token on many pages (pages_read_engagement).
    page_tok = None
    if out["page_id"]:
        try:
            page_tok = get_page_access_token(out["page_id"], token)
        except Exception:  # noqa: BLE001 — fall back to the user token
            page_tok = None
    try:
        probe = _get(
            f"{BASE_URL}/{oid}",
            {
                "access_token": page_tok or token,
                "fields": "id,message,is_eligible_for_promotion,video_buying_eligibility,"
                          "attachments{media_type}",
            },
        )
    except Exception as exc:  # noqa: BLE001 — probe is best-effort
        out["error"] = str(exc)
        return out
    if not isinstance(probe, dict):
        out["error"] = "unreadable probe response"
        return out
    err = probe.get("error")
    if err:
        subcode = err.get("error_subcode")
        etype = str(err.get("type") or "")
        msg = str(err.get("message") or "")
        # Only a genuine "does not exist" is treated as non-existence; a
        # permission/rate error must NOT be reported as a wrong id.
        if subcode in _NONEXISTENT_ERROR_SUBCODES or etype in _NONEXISTENT_ERROR_TYPES \
                or "does not exist" in msg.lower():
            out["exists"] = False
        out["error"] = msg or "probe failed"
        return out
    out["exists"] = True
    out["message"] = str(probe.get("message") or "")
    out["eligible"] = probe.get("is_eligible_for_promotion")
    out["video_buying_eligibility"] = probe.get("video_buying_eligibility")
    atts = (probe.get("attachments") or {}).get("data") or []
    if atts:
        out["is_video"] = str((atts[0] or {}).get("media_type") or "").lower() == "video"
    return out
