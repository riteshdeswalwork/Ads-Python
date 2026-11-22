"""
Production-style document-driven Facebook campaign sync: validation, targeting resolution,
placement sanitization, creative modes, pixel attach, verify.

Each successful sync creates **one** new Meta campaign, ad set, creative, and ad using **only** the supplied
document (no reuse of prior Facebook entities from doc_id mapping or name matching). If any step after
campaign creation fails, the campaign is **deleted** by default so failed retries do not pile up duplicate
campaigns; set ``options.keep_campaign_on_sync_failure=true`` to keep the campaign for debugging.
``doc_id`` is stored after success for audit/reference only, not to reattach old ad sets or ads.

Strict mode documents cannot force Meta-side attribution defaults, placement expansion (e.g. Advantage+), or
learning-phase behavior; see STRICT_META_PLATFORM_LIMITATIONS on strict preview/sync responses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as _dt_tz
from typing import Any, Dict, List, Optional, Tuple

from . import api_errors
from . import meta_validation
from . import services

logger = logging.getLogger("facebook_ads_mcp")

# Dynamic, override-able config for Meta-API auto-fix rules. Override at
# runtime by setting META_API_RULES_JSON in the env, e.g.
#   META_API_RULES_JSON='{"schedule_grace_seconds": 600}'
# Empty / unset => bundled defaults below.
_META_RULES_DEFAULTS: Dict[str, Any] = {
    "schedule_grace_seconds": 300,
    "min_adset_duration_seconds": 86400,
    "position_remaps": {
        "reels:facebook_positions": "facebook_reels:facebook_positions",
        "reels:instagram_positions": "reels:instagram_positions",
    },
    "fallback_existing_post_without_id_to_link_ad": True,
    "default_cta_type": "LEARN_MORE",
    "default_daily_budget_minor_units": 5000,
    # Interest names containing any of these substrings (case-insensitive) are
    # filtered out of flexible_spec before sending to Meta. Catches Meta-side
    # deprecated "admins" interests that still appear in batch_interest_search
    # but get rejected at adset creation with subcode 1487079.
    "blocked_interest_name_patterns": [
        "admin",                  # "Instagram business profile admins", "Business Page admins"
        "page admin",
        "business profile admin",
        "organisation",           # "Marketing services and organisations (marketing)" — entity-targeting
        "organization",           # US spelling
    ],
    # Hint message appended to auto_fixes when Meta returns a rate-limit error
    # (code 80004 or message contains "too many calls"). Override via
    # META_API_RULES_JSON to localize / shorten / change the wording.
    "rate_limit_hint": (
        "Meta rate-limited this ad account (code 80004). Wait 10-15 minutes "
        "before retrying — the per-account create rate cap was hit. The brief "
        "itself is fine; this is purely Meta throttling."
    ),
    # Read-only discovery tools whose results are safe to cache cross-run for
    # the same user. 5-minute TTL. Lets follow-up turns reuse account/page IDs
    # without re-fetching from Meta. Override via META_API_RULES_JSON to add
    # connectors or change the list. Empty list => no cross-run cache.
    "cross_run_cacheable_tools": [
        "facebook_get_accounts",
        "facebook_get_pages",
        "facebook_get_pixels",
        "facebook_get_instagram_accounts",
        "facebook_get_businesses",
        "facebook_get_all_ad_accounts_from_facebook",
    ],
    "cross_run_cache_ttl_seconds": 300,
}


def _load_meta_rules() -> Dict[str, Any]:
    rules = json.loads(json.dumps(_META_RULES_DEFAULTS))
    raw = os.environ.get("META_API_RULES_JSON", "").strip()
    if raw:
        try:
            override = json.loads(raw)
            if isinstance(override, dict):
                rules.update(override)
        except (ValueError, TypeError) as exc:
            logger.warning("META_API_RULES_JSON parse failed; using defaults: %s", exc)
    return rules


META_RULES = _load_meta_rules()

DEFAULT_DAILY_BUDGET_CENTS = int(META_RULES.get("default_daily_budget_minor_units") or 5000)
DEFAULT_CTA = str(META_RULES.get("default_cta_type") or "LEARN_MORE").upper()

# Call-optimized goals route to phone numbers, NOT websites. Setting
# destination_type=WEBSITE for these defeats optimization and triggers Meta
# error 1885364. Adsets with these goals also need promoted_object.page_id.
CALL_OPTIMIZATION_GOALS = frozenset({"QUALITY_CALL", "CALL", "CALLS", "PHONE_CALL"})

# Strict-mode campaign objectives: Marketing API outcome_* only (no legacy ENGAGEMENT / AWARENESS labels).
STRICT_VALID_CAMPAIGN_OBJECTIVES = frozenset(
    {
        "OUTCOME_ENGAGEMENT",
        "OUTCOME_AWARENESS",
        "OUTCOME_LEADS",
        "OUTCOME_SALES",
        "OUTCOME_TRAFFIC",
        "OUTCOME_APP_PROMOTION",
    }
)
STRICT_LEGACY_OBJECTIVE_HINTS = {
    "ENGAGEMENT": "OUTCOME_ENGAGEMENT",
    "AWARENESS": "OUTCOME_AWARENESS",
    "CONVERSIONS": "OUTCOME_SALES",
    "LINK_CLICKS": "OUTCOME_TRAFFIC",
    "LEAD_GENERATION": "OUTCOME_LEADS",
    "APP_INSTALLS": "OUTCOME_APP_PROMOTION",
}

# Non-strict mode: normalize legacy AND shorthand objectives to Marketing API OUTCOME_* values.
# Superset of STRICT_LEGACY_OBJECTIVE_HINTS plus common shorthand aliases.
LEGACY_OBJECTIVE_MAP = {
    **STRICT_LEGACY_OBJECTIVE_HINTS,
    "TRAFFIC": "OUTCOME_TRAFFIC",
    "LEADS": "OUTCOME_LEADS",
    "SALES": "OUTCOME_SALES",
}

# Objective-aware defaults for non-strict mode adset fields.
OBJECTIVE_ADSET_DEFAULTS = {
    "OUTCOME_ENGAGEMENT": {"optimization_goal": "POST_ENGAGEMENT", "billing_event": "IMPRESSIONS"},
    "OUTCOME_AWARENESS": {"optimization_goal": "REACH", "billing_event": "IMPRESSIONS"},
    "OUTCOME_TRAFFIC": {"optimization_goal": "LINK_CLICKS", "billing_event": "IMPRESSIONS", "destination_type": "WEBSITE"},
    "OUTCOME_SALES": {"optimization_goal": "OFFSITE_CONVERSIONS", "billing_event": "IMPRESSIONS", "destination_type": "WEBSITE"},
    "OUTCOME_LEADS": {"optimization_goal": "LEAD_GENERATION", "billing_event": "IMPRESSIONS", "destination_type": "WEBSITE"},
    "OUTCOME_APP_PROMOTION": {"optimization_goal": "APP_INSTALLS", "billing_event": "IMPRESSIONS", "destination_type": "APP"},
}

VALID_ADSET_DESTINATION_TYPES = frozenset({
    "WEBSITE", "APP", "MESSENGER", "APPLINKS_AUTOMATIC", "WHATSAPP",
    "INSTAGRAM_DIRECT", "FACEBOOK",
    "MESSAGING_MESSENGER_WHATSAPP",
    "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
    "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP",
    "MESSAGING_INSTAGRAM_DIRECT_WHATSAPP",
    "SHOP_AUTOMATIC",
    "ON_AD", "ON_POST", "ON_EVENT", "ON_VIDEO", "ON_PAGE",
    "INSTAGRAM_PROFILE", "FACEBOOK_PAGE",
    "INSTAGRAM_PROFILE_AND_FACEBOOK_PAGE",
    "INSTAGRAM_LIVE", "FACEBOOK_LIVE", "IMAGINE",
    "PHONE_CALL",
})

DESTINATION_TYPE_ALIASES = {
    "ON_FORM": "ON_AD",
    "INSTANT_FORM": "ON_AD",
    "LEAD_FORM": "ON_AD",
    "LEADGEN_FORM": "ON_AD",
    "FORM": "ON_AD",
}

OPTIMIZATION_GOAL_ALIASES = {
    "LEADS": "LEAD_GENERATION",
    "LEAD": "LEAD_GENERATION",
    "INSTANT_FORM": "LEAD_GENERATION",
    "INSTANT_FORMS": "LEAD_GENERATION",
    "LEADGEN": "LEAD_GENERATION",
    "MAXIMIZE_LEADS": "LEAD_GENERATION",
    "QUALITY_LEADS": "QUALITY_LEAD",
    "ENGAGEMENT": "POST_ENGAGEMENT",
    "POST_ENGAGEMENTS": "POST_ENGAGEMENT",
    "REACHES": "REACH",
    "IMPRESSION": "IMPRESSIONS",
    "TRAFFIC": "LINK_CLICKS",
    "CLICKS": "LINK_CLICKS",
    "LANDING_PAGE_VIEW": "LANDING_PAGE_VIEWS",
    "CONVERSION": "OFFSITE_CONVERSIONS",
    "CONVERSIONS": "OFFSITE_CONVERSIONS",
    "SALES": "OFFSITE_CONVERSIONS",
    "APP_INSTALL": "APP_INSTALLS",
    # VIDEO_VIEWS is a REAL, distinct Meta optimization goal (the official
    # Marketing API lists VIDEO_VIEWS and THRUPLAY separately). Do NOT collapse
    # VIDEO_VIEWS -> THRUPLAY: that broke the UI-equivalent path (Meta's
    # "Get more video views" = OUTCOME_ENGAGEMENT + VIDEO_VIEWS). Only the
    # singular typo alias is normalized to the canonical VIDEO_VIEWS.
    "VIDEO_VIEW": "VIDEO_VIEWS",
    "MESSAGE": "CONVERSATIONS",
    "MESSAGES": "CONVERSATIONS",
    "CALL": "QUALITY_CALL",
    "CALLS": "QUALITY_CALL",
    "PHONE_CALL": "QUALITY_CALL",
    "PHONE_CALLS": "QUALITY_CALL",
}

STRICT_VALID_CREATIVE_MODES = frozenset({"LINK_AD", "EXISTING_POST", "WHATSAPP"})
STRICT_PREVIEW_CACHE_TTL = 3600

# Bump when strict_validate rules or allowed enums change (clients may send options.strict_validator_version to assert alignment).
STRICT_VALIDATOR_VERSION = 1

# Meta-side behavior not fully controllable via our payloads (document for strict-mode operators).
STRICT_META_PLATFORM_LIMITATIONS = (
    "Attribution windows and defaults may be applied or adjusted by Meta beyond what you send.",
    "Placements can still expand (e.g. Advantage+, network suggestions) depending on account/campaign settings.",
    "Learning phase and delivery optimization behavior are controlled by Meta, not this MCP server.",
)


def _targeting_mode_from_document(doc: Dict[str, Any]) -> str:
    """RAW = flexible_spec / IDs only (no targeting search). RESOLVE = interest_queries / behavior_queries / education search.

    In strict_mode, default is RAW (deterministic production targeting). Explicit targeting_mode on document root,
    options, or targeting overrides. RESOLVE in strict requires options.allow_targeting_resolve (dev / non-deterministic).
    """
    opts = doc.get("options") if isinstance(doc.get("options"), dict) else {}
    t = doc.get("targeting") if isinstance(doc.get("targeting"), dict) else {}
    strict = strict_mode_from_document(doc)
    explicit = False
    for key in (doc.get("targeting_mode"), opts.get("targeting_mode"), t.get("targeting_mode")):
        if key is not None and str(key).strip():
            explicit = True
            u = str(key).strip().upper()
            if u in ("RAW", "RESOLVE"):
                return u
            raise ValueError(
                f"Invalid combination: targeting_mode={key!r} is not allowed. Use: \"RAW\" (direct IDs / flexible_spec only) "
                f"or \"RESOLVE\" (run interest/behavior/education search APIs; non-deterministic)."
            )
    if strict:
        return "RAW"
    return "RESOLVE"


def _strict_preview_cache_key(account_id: str, doc_hash: str) -> str:
    aid = str(account_id or "").replace("act_", "").strip()
    return f"facebook_ads_mcp:strict_preview_ok:{aid}:{doc_hash}"


def _mark_strict_preview_ok(account_id: str, doc_hash: str) -> None:
    try:
        from django.core.cache import cache
        cache.set(_strict_preview_cache_key(account_id, doc_hash), 1, timeout=STRICT_PREVIEW_CACHE_TTL)
    except Exception as exc:
        logger.warning("strict preview cache set failed (sync gate may fail): %s", exc)


def _strict_preview_ok_in_cache(account_id: str, doc_hash: str) -> bool:
    try:
        from django.core.cache import cache
        return bool(cache.get(_strict_preview_cache_key(account_id, doc_hash)))
    except Exception as exc:
        logger.warning("strict preview cache get failed: %s", exc)
        return False


def _clear_strict_preview_ok(account_id: str, doc_hash: str) -> None:
    try:
        from django.core.cache import cache
        cache.delete(_strict_preview_cache_key(account_id, doc_hash))
    except Exception as exc:
        logger.warning("strict preview cache delete failed: %s", exc)


def strict_validate(doc: Dict[str, Any]) -> None:
    """
    Pre-flight validation for strict_mode before Meta API calls.
    Raises ValueError with actionable messages (invalid combination / missing field / enum).
    """
    opts = doc.get("options") if isinstance(doc.get("options"), dict) else {}
    camp = doc.get("campaign") if isinstance(doc.get("campaign"), dict) else {}
    ads = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    cr = doc.get("creative") if isinstance(doc.get("creative"), dict) else {}
    ad = doc.get("ad") if isinstance(doc.get("ad"), dict) else {}
    tracking = doc.get("tracking") if isinstance(doc.get("tracking"), dict) else {}
    tdoc = doc.get("targeting")
    if isinstance(tdoc, str):
        tdoc = _parse_json_maybe(tdoc)
    if not isinstance(tdoc, dict):
        tdoc = {}

    obj = str(camp.get("objective") or "").strip()
    if not obj:
        raise ValueError(
            "Invalid input: missing required field campaign.objective.\n"
            f"Use: one of {', '.join(sorted(STRICT_VALID_CAMPAIGN_OBJECTIVES))} (Marketing API outcome objectives)."
        )
    obj_u = obj.upper()
    if obj_u not in STRICT_VALID_CAMPAIGN_OBJECTIVES:
        hint = STRICT_LEGACY_OBJECTIVE_HINTS.get(obj_u)
        allowed = ", ".join(sorted(STRICT_VALID_CAMPAIGN_OBJECTIVES))
        if hint:
            raise ValueError(
                f"Invalid objective: {obj!r} is not allowed in strict_mode.\n"
                f"Use: {hint!r}\n"
                f"(Full allowed set: {allowed}.)"
            )
        raise ValueError(
            f"Invalid objective: {obj!r} is not allowed in strict_mode.\n"
            f"Use: an OUTCOME_* objective from the Marketing API.\n"
            f"Allowed: {allowed}."
        )

    og = str(ads.get("optimization_goal") or "").strip()
    if not og:
        raise ValueError(
            "Invalid input: missing required field adset.optimization_goal.\n"
            "Use: set optimization_goal to the Meta value for your objective (e.g. POST_ENGAGEMENT for engagement, "
            "OFFSITE_CONVERSIONS when optimizing for pixel events)."
        )

    mode = str(cr.get("mode") or "").strip().upper()
    if not mode:
        raise ValueError(
            "Invalid input: missing required field creative.mode.\n"
            f"Use: one of {', '.join(sorted(STRICT_VALID_CREATIVE_MODES))}."
        )
    if mode not in STRICT_VALID_CREATIVE_MODES:
        raise ValueError(
            f"Invalid combination: creative.mode={cr.get('mode')!r} is not allowed in strict_mode.\n"
            f"Use: one of {', '.join(sorted(STRICT_VALID_CREATIVE_MODES))}."
        )

    ad_name = str(ad.get("name") or "").strip()
    if not ad_name:
        raise ValueError(
            "Invalid input: missing required field ad.name.\n"
            "Use: set ad.name explicitly (strict_mode does not invent an ad name)."
        )

    tm = _targeting_mode_from_document(doc)
    if tm == "RESOLVE" and not _truthy_doc_option(opts.get("allow_targeting_resolve")):
        raise ValueError(
            "Invalid combination: targeting_mode RESOLVE is not allowed in strict_mode unless you opt in.\n"
            "Why: Meta targeting search can return different IDs for the same query (non-deterministic).\n"
            "Use: targeting_mode RAW with targeting.flexible_spec built from facebook_targeting_search / "
            "facebook_batch_interest_search (exact IDs).\n"
            "Dev only: set options.allow_targeting_resolve to true together with targeting_mode RESOLVE."
        )

    has_iq = isinstance(tdoc.get("interest_queries"), list) and any(str(x).strip() for x in tdoc.get("interest_queries") or [])
    has_bq = isinstance(tdoc.get("behavior_queries"), list) and any(str(x).strip() for x in tdoc.get("behavior_queries") or [])
    has_eq = isinstance(tdoc.get("education_status_queries"), list) and any(str(x).strip() for x in tdoc.get("education_status_queries") or [])
    if tm == "RAW" and (has_iq or has_bq or has_eq):
        raise ValueError(
            "Invalid combination: targeting_mode is RAW but targeting still has interest_queries, behavior_queries, "
            "or education_status_queries.\n"
            "Use: remove those query lists and put resolved IDs in targeting.flexible_spec, "
            "or set targeting_mode to RESOLVE with options.allow_targeting_resolve=true (dev / non-deterministic only)."
        )

    page_id_creative = str(cr.get("page_id") or ads.get("page_id") or "").strip()
    if mode == "EXISTING_POST":
        has_post_id = any(
            str(cr.get(k) or "").strip()
            for k in ("object_story_id", "source_instagram_media_id", "instagram_post_id", "instagram_permalink_url")
        )
        if not has_post_id:
            raise ValueError(
                "Invalid combination: creative.mode is EXISTING_POST but no post id is supplied. "
                "Set ONE of: object_story_id (FB page post: page_id_post_id), source_instagram_media_id (IG post/Reel/video), "
                "or instagram_permalink_url (IG feed/carousel/reel)."
            )
    elif mode == "LINK_AD":
        if not page_id_creative:
            raise ValueError(
                "Invalid combination: creative.mode is LINK_AD but page_id is missing. "
                "Set creative.page_id or adset.page_id."
            )
        if not str(cr.get("link_url") or "").strip():
            raise ValueError(
                "Invalid combination: creative.mode is LINK_AD but creative.link_url is missing."
            )
        if not str(cr.get("cta_type") or "").strip():
            raise ValueError(
                "Invalid combination: creative.mode is LINK_AD but creative.cta_type is missing. "
                "Set cta_type to a valid Meta CTA enum (e.g. LEARN_MORE, WHATSAPP_MESSAGE)."
            )
        if tracking.get("pixel_id") and (tracking.get("custom_event_type") is None or not str(tracking.get("custom_event_type")).strip()):
            raise ValueError(
                "Invalid combination: tracking.pixel_id is set for LINK_AD but tracking.custom_event_type is missing. "
                "Set custom_event_type to a Meta-accepted value (e.g. CONTENT_VIEW, PURCHASE)."
            )
    elif mode == "WHATSAPP":
        wa = str(cr.get("whatsapp_phone_e164") or cr.get("whatsapp_number") or "").strip()
        if not page_id_creative or not wa:
            raise ValueError(
                "Invalid combination: creative.mode is WHATSAPP but page_id or whatsapp_phone_e164/whatsapp_number is missing."
            )
# Common valid placement tokens (Meta changes over time; unknowns are stripped).
ALLOWED_FB_POSITIONS = frozenset(
    {
        "feed",
        "right_hand_column",
        "instant_article",
        "instream_video",
        "marketplace",
        "video_feeds",
        "story",
        "search",
        "facebook_reels",
        "facebook_reels_overlay",
        "profile_feed",
    }
)
# Graph v26 removed the Instagram Explore feed placement ("explore") and
# Messenger Stories ("story" in messenger_positions). Both apply to EVERY API
# version from 2026-10-27, so they are no longer allowed here: non-strict
# briefs get them stripped with an auto_fix note, strict briefs get an error.
ALLOWED_IG_POSITIONS = frozenset(
    {"stream", "story", "reels", "profile_feed", "ig_search"}
)
# Manual Messenger placements (use with publisher_platforms including "messenger").
ALLOWED_MESSENGER_POSITIONS = frozenset({"messenger_home", "sponsored_messages"})
# Placements Meta retired in v26 — kept only so the strip message can say WHY.
_RETIRED_PLACEMENTS_V26 = {
    ("instagram_positions", "explore"): "Instagram Explore feed placement was removed in Graph API v26 (all versions from 2026-10-27)",
    ("messenger_positions", "story"): "Messenger Stories placement was removed in Graph API v26 (all versions from 2026-10-27)",
}
# Audience Network / Threads manual placements (Meta enum subset; invalid tokens stripped in non-strict).
ALLOWED_AUDIENCE_NETWORK_POSITIONS = frozenset(
    {"classic", "instream_video", "rewarded_video", "banner", "interstitial", "native"}
)
ALLOWED_THREADS_POSITIONS = frozenset({"threads_stream", "threads_profile_feed"})


def _parse_json_maybe(val: Any) -> Any:
    if isinstance(val, str) and val.strip().startswith(("{", "[")):
        try:
            return json.loads(val)
        except (TypeError, ValueError):
            return val
    return val


def _deep_merge(a: Dict, b: Dict) -> Dict:
    out = dict(a)
    for k, v in (b or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _truthy_doc_option(val: Any) -> bool:
    """JSON/MCP may send booleans or strings; avoid bool('false') == True."""
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


def strict_mode_from_document(doc: Dict[str, Any]) -> bool:
    """True if document or options.strict_mode requests exact passthrough (no normalize, no idempotency cache)."""
    if not isinstance(doc, dict):
        return False
    opts = doc.get("options") if isinstance(doc.get("options"), dict) else {}
    return _truthy_doc_option(doc.get("strict_mode", opts.get("strict_mode")))


def _infer_creative_mode(doc: Dict[str, Any]) -> Optional[str]:
    """Universal creative-mode inference from brief signals.

    Single source of truth for "what creative MODE does this brief imply"
    — used by both the strict and non-strict validation paths so the
    auto-detection is identical across every ad type.

    Priority (highest → lowest):

    1. ``creative.mode`` explicitly set by the brief → return that (no
       overriding the user's intent).
    2. ``creative.object_story_id`` present → ``EXISTING_POST`` (the brief
       is boosting an existing Page post).
    3. ``creative.source_instagram_media_id`` present → ``EXISTING_POST``
       (boosting an existing Instagram post).
    4. ``options.use_existing_post`` is truthy → ``EXISTING_POST`` (the
       brief explicitly asked for boost mode, even if the agent forgot to
       set creative.mode).
    5. ``adset.destination_type=WHATSAPP`` or ``creative.cta_type=WHATSAPP_MESSAGE``
       → ``WHATSAPP``.
    6. ``creative.asset_feed_spec`` present → ``DCO`` (Dynamic Creative).
    7. ``creative.carousel_cards`` present → ``CAROUSEL``.
    8. ``creative.instant_experience_id`` present → ``COLLECTION``.
    9. ``creative.lead_gen_form_id`` (or ``leadgen_form_id``) present
       AND ``adset.destination_type=ON_AD`` → ``LEAD_FORM``.
    10. ``creative.branded_content_sponsor_page_id`` present →
        ``BRANDED_CONTENT``.
    11. Fallback: None (caller decides — defaults to LINK_AD downstream).

    Returns the inferred mode string, or None if no signal exists.
    """
    cr = doc.get("creative") if isinstance(doc.get("creative"), dict) else {}
    ads = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    opts = doc.get("options") if isinstance(doc.get("options"), dict) else {}

    existing_mode = str(cr.get("mode") or "").strip().upper()
    if existing_mode:
        return existing_mode

    if str(cr.get("object_story_id") or "").strip():
        return "EXISTING_POST"
    if str(cr.get("source_instagram_media_id") or "").strip():
        return "EXISTING_POST"
    if str(cr.get("instagram_permalink_url") or "").strip():
        return "EXISTING_POST"
    if _truthy_doc_option(opts.get("use_existing_post")):
        return "EXISTING_POST"

    dt = str(ads.get("destination_type") or "").strip().upper()
    cta_doc = cr.get("call_to_action")
    cta_type = ""
    if isinstance(cta_doc, dict):
        cta_type = str(cta_doc.get("type") or "").strip().upper()
    if not cta_type:
        cta_type = str(cr.get("cta_type") or "").strip().upper()
    if dt == "WHATSAPP" or cta_type == "WHATSAPP_MESSAGE":
        return "WHATSAPP"

    if cr.get("asset_feed_spec"):
        return "DCO"
    if cr.get("carousel_cards"):
        return "CAROUSEL"
    if str(cr.get("instant_experience_id") or "").strip():
        return "COLLECTION"
    if (str(cr.get("lead_gen_form_id") or "").strip()
            or str(cr.get("leadgen_form_id") or "").strip()):
        if dt == "ON_AD":
            return "LEAD_FORM"
    if str(cr.get("branded_content_sponsor_page_id") or "").strip():
        return "BRANDED_CONTENT"

    return None


def validate_document_strict_passthrough(doc: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Deep copy only; required keys. No mode/CTA/budget/objective normalization."""
    out = json.loads(json.dumps(doc)) if isinstance(doc, dict) else {}
    opts_chk = out.get("options") if isinstance(out.get("options"), dict) else {}
    sv_raw = opts_chk.get("strict_validator_version")
    if sv_raw is not None and str(sv_raw).strip() != "":
        try:
            sv = int(sv_raw)
        except (TypeError, ValueError):
            raise ValueError(
                f"Invalid input: options.strict_validator_version must be an integer (server implements {STRICT_VALIDATOR_VERSION})."
            ) from None
        if sv != STRICT_VALIDATOR_VERSION:
            raise ValueError(
                f"strict_validator_version mismatch: document declares {sv} but this server implements {STRICT_VALIDATOR_VERSION}.\n"
                "Use: update your client to the current strict rules, or omit options.strict_validator_version until aligned."
            ) from None
    camp = out.get("campaign")
    if not isinstance(camp, dict) or not str(camp.get("name") or "").strip():
        raise ValueError("Invalid input: missing required field campaign.name (strict_mode).")
    if not str(camp.get("objective") or "").strip():
        raise ValueError("Invalid input: missing required field campaign.objective (strict_mode).")
    ads = out.get("adset")
    if not isinstance(ads, dict) or not str(ads.get("name") or "").strip():
        raise ValueError("Invalid input: missing required field adset.name (strict_mode).")
    cr = out.get("creative")
    if not isinstance(cr, dict):
        raise ValueError("Invalid input: document.creative must be an object (strict_mode).")
    if not str(cr.get("name") or "").strip():
        raise ValueError("Invalid input: missing required field creative.name (strict_mode).")
    ad = out.get("ad")
    if not isinstance(ad, dict) or not str(ad.get("name") or "").strip():
        raise ValueError(
            "Invalid input: missing required field ad.name. strict_mode requires document.ad.name (no default ad name)."
        )
    # Universal mode auto-inference (runs in strict path too — without
    # this, strict_validate rejects a perfectly-formed brief that omits
    # creative.mode when the brief gave us signals like
    # `creative.object_story_id`, `creative.source_instagram_media_id`,
    # `options.use_existing_post=true`, or destination_type=WHATSAPP).
    # This applies to ANY ad type — the inference rules are connector-
    # wide, not specific to a particular campaign brief.
    inferred = _infer_creative_mode(out)
    if inferred and not str(cr.get("mode") or "").strip():
        cr["mode"] = inferred
        out["creative"] = cr
    strict_validate(out)
    return out, []


def suggest_relaxed_fixes_for_strict_error(message: str) -> List[str]:
    """When strict validation fails, suggest non-strict or opt-in flags (optional operator hints)."""
    m = message.lower()
    hints: List[str] = []
    if any(
        x in m
        for x in (
            "campaign.objective",
            "invalid objective",
            "objective=",
            "outcome_*",
            "outcome_",
        )
    ):
        hints.append(
            "Relaxed: set strict_mode / options.strict_mode to false so legacy objectives (e.g. ENGAGEMENT) can normalize to OUTCOME_*."
        )
    if "targeting_mode" in m or "allow_targeting_resolve" in m or "flexible_spec" in m:
        hints.append(
            "Relaxed: disable strict_mode to use interest_queries resolution without RAW-only rules, "
            "or keep strict and set options.allow_targeting_resolve=true with explicit targeting_mode RESOLVE (non-deterministic)."
        )
    if "strict_preview" in m or "strict preview" in m or "strict_preview_document_hash" in m or "preview proof" in m:
        hints.append(
            "Fix: call facebook_preview_document_campaign with the same document, then set options.strict_preview_document_hash "
            "to the preview's document_hash_sha256 before sync."
        )
    if "ad.name" in m or "creative.name" in m or "optimization_goal" in m:
        hints.append(
            "Relaxed: non-strict mode may default some names; or add the missing fields named in the error while keeping strict_mode."
        )
    if "placement" in m or "positions" in m:
        hints.append(
            "Relaxed: non-strict mode can drop invalid placement tokens instead of failing; or replace values with allowed Meta tokens."
        )
    if "creative.mode" in m or "link_url" in m or "cta_type" in m or "pixel_id" in m:
        hints.append(
            "Relaxed: disable strict_mode for LINK_AD pixel/CTA auto-adjustments, or supply every field the error lists under strict_mode."
        )
    seen: set[str] = set()
    out: List[str] = []
    for h in hints:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:6]


def _api_error_message(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    err = resp.get("error") or {}
    return str(err.get("message") or err.get("error_user_msg") or "")


def _has_api_error(resp: Any) -> bool:
    return isinstance(resp, dict) and "error" in resp


def _reel_boost_ineligible_message(
    ig_media_id: str,
    err: Any,
    *,
    media_product_type: str = "",
    media_type: str = "",
    permalink: str = "",
    pages: Any = None,
    eligible_to_boost: Optional[bool] = None,
    lookup_notes: str = "",
) -> str:
    """Build the user-facing message for a REAL Meta 1815279 rejection of an
    existing IG reel/video creative.

    ``eligible_to_boost`` is Meta's own verdict from the IG media field
    ``boost_eligibility_info.eligible_to_boost`` (present on v25). When it is
    True the reel is NOT music/sticker/rights-blocked — live case 2026-09-08
    (bol7technologies reel Dc-4BTwgY0Z, boosted hours after publish) — so the
    message must not blame the content; the usual cause is a freshly published
    reel whose ad-asset processing has not finished. ``lookup_notes`` carries
    why the FB cross-post rescue could not run (e.g. Meta refused the Page
    posts query) so the user sees the real blocker instead of a guess.

    Cross-posting is NOT a Meta requirement: Meta removed the "re-upload the IG
    video to Facebook first" rule on 2023-06-27 (Marketing API changelog);
    source_instagram_media_id references the reel directly. So a 1815279 means
    THIS specific reel is boost-INELIGIBLE (dominant cause: licensed/third-party
    music, a filter, or interactive stickers; also rights/collab/region) — it is
    NOT a blanket "reels can't be boosted via API" rule.

    Eligibility probe: the IG media node exposes no boost-eligibility field, so
    the richest real signal is Meta's own error payload (error_user_title /
    error_user_msg — the authoritative human reason, often names the cause). We
    surface that plus the probed reel identity/permalink. Kept a pure function
    so the phrasing is unit-testable without the heavy sync pipeline.
    """
    err = err if isinstance(err, dict) else {}
    reason = " ".join(
        p for p in (
            str(err.get("error_user_title") or "").strip(),
            str(err.get("error_user_msg") or err.get("error_user_message") or "").strip(),
        ) if p
    ).strip()
    reel_id = (
        f"{(media_product_type or media_type or 'IG media').strip() or 'IG media'} {ig_media_id}"
        + (f" ({permalink})" if permalink else "")
    )
    notes = f" Cross-post rescue: {lookup_notes.strip()}" if lookup_notes and lookup_notes.strip() else ""
    if eligible_to_boost is True:
        return (
            f"Meta rejected {reel_id} with error 1815279"
            + (f" — Meta says: \"{reason}\"." if reason else ".")
            + " HOWEVER Meta's own eligibility check on this media reports "
            "eligible_to_boost=true, so this is NOT a licensed-music / filter / "
            "sticker / rights block: the reel is fine for in-app Boost, but Meta "
            "will not use THIS Instagram media id as the ad's video source via the "
            "API. Live-verified 2026-09-09 (validate_only, nothing created): reels "
            "that were published to Facebook and Instagram together (Business Suite "
            "multi-platform publish, FB copy within ~2 min) are rejected this way "
            "persistently, while reels published on Instagram first are accepted. "
            "It is NOT a timing issue — retrying later does not help. OPTIONS: "
            "(a) use the reel's Facebook copy as the creative (same video; the ad "
            "still runs on Instagram placements, but it is Meta's copy, not the "
            "original reel with its likes/comments) — requires the user's OK when "
            "the brief says Instagram-only; (b) publish future reels from the "
            "Instagram app first and share to Facebook afterwards, then boost via "
            "the IG media id. Do NOT pick another reel on your own."
            + notes
        )
    return (
        f"Meta rejected {reel_id} with error 1815279"
        + (f" — Meta says: \"{reason}\"." if reason else ".")
        + (" Meta's eligibility check reports eligible_to_boost=false." if eligible_to_boost is False else "")
        + notes
        + " NOTE: Meta does NOT require cross-posting to run an IG reel as an ad "
        "(that rule was removed 2023-06-27; source_instagram_media_id references "
        "the reel directly). This specific reel is boost-INELIGIBLE — almost always "
        "because it contains licensed/third-party music, a filter, or interactive "
        "stickers (it can also be a rights/collab/region restriction). Open the reel "
        "in the app → ⋯ → 'Boost' to see Instagram's own ineligibility reason. "
        "FIX: repost the reel without licensed audio/effects, then boost it (works "
        "via source_instagram_media_id, no cross-post needed). WORKAROUND: cross-post "
        f"this reel to the Page ({pages if pages else '[]'}) in Business Suite so the "
        "video lands in Facebook's library, or promote an eligible IG photo/carousel "
        "instead."
    )


def _sync_step_error(auto_fixes: List[str], err: Any, step: str, **extra: Any) -> Dict[str, Any]:
    """Standard failure payload with human-readable why_failed.

    If the underlying error dict carries a ``user_options`` field (set by
    meta_validation when a forbidden combo has structured alternatives), it is
    surfaced at the top level of the response so callers — typically the agent
    — can show them to the user instead of silent fixes.
    """
    e = err if isinstance(err, dict) else {"message": str(err)}
    out: Dict[str, Any] = {"ok": False, "error": e, "step": step, "auto_fixes": list(auto_fixes), **extra}
    out["why_failed"] = api_errors.explain_graph_error(e)
    # Surface user_options to the top of the response when present.
    # Keeps the existing 'error.user_options' nested copy intact for callers
    # that already read the error dict directly.
    opts = e.get("user_options") if isinstance(e, dict) else None
    if isinstance(opts, list) and opts:
        out["user_options"] = opts
    return out


def _truthy_sync_option(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


def _apply_campaign_rollback_on_sync_failure(
    token: str,
    campaign_id: Optional[str],
    options: Dict[str, Any],
    auto_fixes: List[str],
    failed_step: str,
) -> Dict[str, Any]:
    """
    After a failed document sync, delete the campaign so failed runs do not leave extra campaigns.
    Opt out with options.keep_campaign_on_sync_failure=true.
    """
    extra: Dict[str, Any] = {}
    cid = str(campaign_id or "").strip()
    if not cid:
        return extra
    if _truthy_sync_option(options.get("keep_campaign_on_sync_failure")):
        auto_fixes.append(
            f"Sync failed at {failed_step}; campaign {cid} kept (options.keep_campaign_on_sync_failure=true)."
        )
        extra["campaign_kept_on_failure"] = True
        return extra
    dr = services.delete_campaign(cid, token)
    if _has_api_error(dr):
        auto_fixes.append(
            f"Sync failed at {failed_step}; rollback delete_campaign failed: {_api_error_message(dr)}"
        )
        extra["rollback_ok"] = False
        extra["rollback_error"] = dr.get("error")
    else:
        auto_fixes.append(
            f"Rolled back campaign {cid} after failure at {failed_step} (avoids orphan duplicate campaigns)."
        )
        extra["rollback_ok"] = True
        extra["rolled_back_campaign_id"] = cid
        extra["campaign_id"] = None
        extra["adset_id"] = None
        extra["creative_id"] = None
    return extra


_KNOWN_BAD_INTERESTS_CACHE_KEY = "fb_ads:known_bad_interest_ids:v1"
_KNOWN_BAD_INTERESTS_TTL_DEFAULT = 7 * 24 * 60 * 60   # 7 days

# Global static blocklist of interest/behavior IDs that Meta has confirmed
# deprecated (subcode 1487079 / 1487694 / 2446395) across multiple accounts.
# Promoted from per-account learn-cache to a global seed because these IDs
# are deprecated globally, not per-account — every new account hits the same
# wall on its first run.
#
# Override at runtime with FB_ADS_GLOBAL_BAD_INTEREST_IDS=id1,id2,... in env.
_GLOBAL_BAD_INTEREST_IDS = frozenset({
    # From learn-cache observations across BOL7 accounts:
    "6297846662583",  # "Instagram business profile admins"
    "6020530281783",  # "Business Page admins"
    "6086568164383",  # "Marketing API developers (last 90 days)" — internal-only
    "6853952393067",  # "Marketing services and organisations" (deprecated alias)
    "6003340066119",  # "Young Entrepreneurs" (deprecated alias)
    "6004382299972",  # "Facebook access (mobile): all mobile devices" (deprecated)
    "849873341726582",   # "Founder" (deprecated demographic)
    "103189639738794",   # "MD/CEO" (deprecated demographic)
    "109288035760640",   # "Business Owner" (deprecated demographic)
    "6003374632277",     # "Freelancer (careers)" (deprecated)
    "6002714898572",     # "Small business owners" (deprecated/restricted)
})


def _global_bad_interest_ids() -> set[str]:
    """Return the static + env-supplied global blocklist."""
    out: set[str] = set(_GLOBAL_BAD_INTEREST_IDS)
    extra = (os.environ.get("FB_ADS_GLOBAL_BAD_INTEREST_IDS") or "").strip()
    if extra:
        for x in extra.split(","):
            x = x.strip()
            if x:
                out.add(x)
    return out


def _learned_bad_interest_ids() -> set[str]:
    """Return the set of interest IDs Meta has previously rejected with 1487079.
    Cleared automatically by the cache TTL; admins can also clear manually via
    Django cache or by setting ``FB_ADS_KNOWN_BAD_INTERESTS_TTL=0``.

    Always merges the global static blocklist (see ``_GLOBAL_BAD_INTEREST_IDS``)
    so brand-new accounts don't have to learn-by-failure on well-known
    deprecated IDs.
    """
    base = _global_bad_interest_ids()
    try:
        ttl = int(os.environ.get("FB_ADS_KNOWN_BAD_INTERESTS_TTL", _KNOWN_BAD_INTERESTS_TTL_DEFAULT))
    except (TypeError, ValueError):
        ttl = _KNOWN_BAD_INTERESTS_TTL_DEFAULT
    if ttl <= 0:
        return base
    try:
        from django.core.cache import cache
        v = cache.get(_KNOWN_BAD_INTERESTS_CACHE_KEY) or set()
        learned = set(v) if isinstance(v, (set, list, tuple)) else set()
        return base | learned
    except Exception:
        return base


def _record_bad_interest_ids(ids: List[str]) -> None:
    """Persist newly-rejected interest IDs so future sync calls can drop them
    pre-flight. No-op if Django cache isn't available or TTL=0."""
    if not ids:
        return
    try:
        ttl = int(os.environ.get("FB_ADS_KNOWN_BAD_INTERESTS_TTL", _KNOWN_BAD_INTERESTS_TTL_DEFAULT))
    except (TypeError, ValueError):
        ttl = _KNOWN_BAD_INTERESTS_TTL_DEFAULT
    if ttl <= 0:
        return
    try:
        from django.core.cache import cache
        existing = cache.get(_KNOWN_BAD_INTERESTS_CACHE_KEY) or set()
        if not isinstance(existing, set):
            existing = set(existing)
        merged = existing | {str(x) for x in ids if x}
        cache.set(_KNOWN_BAD_INTERESTS_CACHE_KEY, merged, timeout=ttl)
    except Exception as exc:
        logger.debug("Could not persist known-bad interest ids: %s", exc)


def _validate_targeting_ids_with_meta(
    account_id: str, token: str,
    items: List[Tuple[str, str]],
    timeout: int = 8,
) -> set[str]:
    """Generic Meta /targetingvalidation probe across multiple axes.

    Args:
        items: list of (type, id) tuples, e.g. [("interests", "6003..."), ("behaviors", "6002...")]
               Meta accepts: interests, behaviors, demographics, work_employers,
               work_positions, education_majors, education_schools.

    Returns set of IDs Meta marked valid=False. Returns empty set on any error.
    """
    if not items:
        return set()
    if not token or not account_id:
        return set()
    try:
        import requests
        act = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
        targeting_list = [{"type": str(t), "id": str(i)} for (t, i) in items if t and i]
        if not targeting_list:
            return set()
        params = {
            "access_token": token,
            "targeting_list": json.dumps(targeting_list),
        }
        url = f"{services.BASE_URL}/{act}/targetingvalidation"
        r = requests.get(url, params=params, timeout=timeout)
        body = r.json() if r.content else {}
        if not isinstance(body, dict):
            return set()
        rows = body.get("data") or []
        invalid: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "")
            if rid and row.get("valid") is False:
                invalid.add(rid)
        return invalid
    except Exception as exc:
        logger.debug("targetingvalidation probe failed: %s", exc)
        return set()


def _validate_interests_with_meta(
    account_id: str, token: str, ids: List[str], timeout: int = 8,
) -> set[str]:
    """Backwards-compatible single-axis probe (interests only)."""
    return _validate_targeting_ids_with_meta(
        account_id, token,
        [("interests", str(i)) for i in (ids or []) if i],
        timeout=timeout,
    )


# ----------------------------------------------------------------------
# Universal pre-flight (dry-run) validator
# ----------------------------------------------------------------------
# Meta's Marketing API supports execution_options=["validate_only"] on POST
# endpoints — it returns the same error response a real create would, without
# actually creating anything. We use it to surface ALL errors before the real
# create_campaign / create_adset / create_creative round-trips, so the caller
# can fix the document up front instead of hitting reactive errors mid-flow.
#
# The dry-run focuses on the ad set endpoint because that is where 90% of
# brief-rejection errors live: targeting (interests, geo, age, gender, special
# ad category restrictions), bid strategy, optimization goal compatibility,
# placement validity, promoted_object requirements, destination_type whitelists,
# schedule windows. Campaign and creative dry-runs can be added if needed.
#
# Disable with FB_ADS_DRY_RUN_PREFLIGHT=0.

# Subcodes that we know how to auto-resolve OR that the existing reactive
# pipeline already handles. We log them as auto_fixes but do not fail the sync.
_KNOWN_AUTOFIXABLE_SUBCODES = {
    1487079,   # invalid targeting (deprecated interest)
    1487694,   # category no longer available
    2446395,   # internal/dev-only category
    1815869,   # image dimensions
    2490487,   # bid amount mismatch
    1815857,   # bid amount required for bid_strategy
    1885649,   # ad set budget is missing (CBO→ABO fallback)
}

# Subcodes where the dry-run is wrong because Meta is returning a contextual
# error tied to the placeholder campaign_id (which doesn't exist in dry-run).
# We swallow these — they will not appear in real create_adset.
_DRYRUN_FALSE_POSITIVE_SUBCODES = {
    100,       # generic "param invalid" — usually about campaign_id placeholder
}


def _dry_run_adset_validate(
    account_id: str,
    token: str,
    *,
    name: str,
    optimization_goal: Optional[str],
    billing_event: Optional[str],
    daily_budget: Optional[int],
    lifetime_budget: Optional[int],
    targeting: Optional[Dict[str, Any]],
    bid_strategy: Optional[str],
    promoted_object: Optional[Dict[str, Any]],
    destination_type: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    timeout: int = 12,
    bid_amount: Optional[int] = None,
) -> Dict[str, Any]:
    """Send a validate_only POST to /act_X/adsets and return Meta's verdict.

    Returns a dict shape:
      { ok: bool, error: { code, subcode, message, blame_field_specs } | None }

    Never raises — any transport/parse failure returns ``{"ok": True}`` so we
    don't block sync on a flaky pre-flight probe.
    """
    if not (account_id and token):
        return {"ok": True}
    try:
        import requests
        act = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
        url = f"{services.BASE_URL}/{act}/adsets"
        data: Dict[str, Any] = {
            "access_token": token,
            "name": name or "preflight_validate",
            "status": "PAUSED",
            "billing_event": billing_event or "IMPRESSIONS",
            # validate_only tells Meta: run all your validators, return errors,
            # but do NOT persist anything. campaign_id is required syntactically
            # so we use 0 as a placeholder; Meta only complains about targeting/
            # bid/budget shape, which is exactly what we want to catch.
            "campaign_id": "0",
            "execution_options": json.dumps(["validate_only"]),
        }
        if optimization_goal:
            data["optimization_goal"] = optimization_goal
        if daily_budget is not None:
            data["daily_budget"] = str(int(daily_budget))
        if lifetime_budget is not None:
            data["lifetime_budget"] = str(int(lifetime_budget))
        if targeting:
            data["targeting"] = json.dumps(targeting) if not isinstance(targeting, str) else targeting
        if bid_strategy:
            data["bid_strategy"] = str(bid_strategy).strip()
        if bid_amount is not None:
            try:
                data["bid_amount"] = str(int(bid_amount))
            except (TypeError, ValueError):
                pass
        if promoted_object:
            data["promoted_object"] = json.dumps(promoted_object) if not isinstance(promoted_object, str) else promoted_object
        if destination_type:
            data["destination_type"] = str(destination_type).strip()
        if start_time:
            data["start_time"] = str(start_time).strip()
        if end_time:
            data["end_time"] = str(end_time).strip()
        r = requests.post(url, data=data, timeout=timeout)
        body = r.json() if r.content else {}
        # Meta returns 200 with {"success": true} when validate_only passes.
        if r.status_code < 400:
            return {"ok": True}
        err = body.get("error") if isinstance(body, dict) else None
        if not isinstance(err, dict):
            return {"ok": True}
        code = err.get("code")
        subcode = err.get("error_subcode")
        # Suppress dry-run-only false positives (caused by placeholder campaign_id).
        if subcode is None and code in _DRYRUN_FALSE_POSITIVE_SUBCODES:
            msg = str(err.get("message") or "").lower()
            if "campaign" in msg and ("id" in msg or "exist" in msg):
                return {"ok": True}
        edata = err.get("error_data") if isinstance(err.get("error_data"), dict) else {}
        return {
            "ok": False,
            "error": {
                "code": code,
                "subcode": subcode,
                "message": err.get("message"),
                "user_msg": err.get("error_user_msg"),
                "blame_field_specs": edata.get("blame_field_specs"),
            },
        }
    except Exception as exc:
        logger.debug("dry-run adset validate failed: %s", exc)
        return {"ok": True}


def _dry_run_preflight(
    account_id: str,
    token: str,
    *,
    targeting: Optional[Dict[str, Any]],
    optimization_goal: Optional[str],
    billing_event: Optional[str],
    daily_budget: Optional[int],
    lifetime_budget: Optional[int],
    bid_strategy: Optional[str],
    promoted_object: Optional[Dict[str, Any]],
    destination_type: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    adset_name: str,
    auto_fixes: List[str],
    bid_amount: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Run the universal pre-flight and translate Meta errors into auto_fixes /
    blocking errors.

    Returns:
      - ``None`` if pre-flight passed or surfaced only known-autofixable issues.
      - ``{"blocking_error": {...}}`` if Meta returned an error that the existing
        reactive pipeline cannot fix, so the caller should bail BEFORE creating
        a campaign. This way nothing has to be rolled back.
    """
    flag = (os.environ.get("FB_ADS_DRY_RUN_PREFLIGHT") or "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return None

    verdict = _dry_run_adset_validate(
        account_id,
        token,
        name=adset_name,
        optimization_goal=optimization_goal,
        billing_event=billing_event,
        daily_budget=daily_budget,
        lifetime_budget=lifetime_budget,
        targeting=targeting,
        bid_strategy=bid_strategy,
        promoted_object=promoted_object,
        destination_type=destination_type,
        start_time=start_time,
        end_time=end_time,
        bid_amount=bid_amount,
    )

    if verdict.get("ok"):
        auto_fixes.append("Pre-flight dry-run: Meta validated the ad set spec — no errors detected.")
        return None

    err = verdict.get("error") or {}
    code = err.get("code")
    subcode = err.get("error_subcode")
    msg = str(err.get("message") or "").strip()
    user_msg = str(err.get("user_msg") or "").strip()
    blame = err.get("blame_field_specs")
    summary = user_msg or msg or f"Meta error code={code} subcode={subcode}"

    # Log which fields Meta blamed so the caller (and any downstream agent)
    # can see exactly where to look.
    if blame:
        try:
            blame_str = json.dumps(blame)
        except Exception:
            blame_str = str(blame)
        auto_fixes.append(
            f"Pre-flight dry-run: Meta flagged fields {blame_str} (code={code} subcode={subcode}): {summary[:200]}"
        )
    else:
        auto_fixes.append(
            f"Pre-flight dry-run: Meta returned code={code} subcode={subcode}: {summary[:200]}"
        )

    # If the subcode is one our reactive pipeline already handles, do not block.
    # The existing _create_adset_with_retry + learn-cache + filters will resolve
    # it on the real call.
    if subcode in _KNOWN_AUTOFIXABLE_SUBCODES:
        auto_fixes.append(
            f"Pre-flight: subcode {subcode} is auto-fixable downstream; continuing to real create."
        )
        return None

    # Unknown / unfixable error — bail BEFORE creating a campaign.
    return {
        "blocking_error": {
            "message": f"Pre-flight dry-run rejected the ad set spec: {summary}",
            "code": code,
            "error_subcode": subcode,
            "blame_field_specs": blame,
            "preflight_user_msg": user_msg,
        }
    }


def _maybe_emit_meta_hints(auto_fixes: List[str], err: Any, step: str) -> None:
    """If the captured error is a Meta rate limit (code 80004 or 'too many calls'),
    OR an interest-rejection at create_adset (subcode 1487079), append a
    human-readable hint to ``auto_fixes`` AND learn the bad interest IDs so
    subsequent syncs auto-drop them. Driven by META_RULES['rate_limit_hint']."""
    if not isinstance(err, dict):
        return
    inner = err.get("error") if isinstance(err.get("error"), dict) else err
    code = inner.get("code") if isinstance(inner, dict) else None
    subcode = inner.get("error_subcode") if isinstance(inner, dict) else None
    msg = str((inner or {}).get("message") or "").lower()

    # --- Rate-limit hint (Add 2) ---
    if code == 80004 or "too many calls" in msg or subcode == 2446079:
        hint = META_RULES.get("rate_limit_hint")
        if hint and hint not in auto_fixes:
            auto_fixes.append(str(hint))

    # --- Known-bad interest learning (Add 3) ---
    # Meta subcodes that signal interest/behavior IDs which should be
    # auto-dropped in future runs:
    #   1487079 — "Invalid targeting spec" (often "admins" / organisation IDs).
    #   1487694 — "The category you've selected is no longer available."
    #   2446395 — Deprecated/internal targeting category (e.g. "Marketing API developers").
    DEPRECATED_INTEREST_SUBCODES = {1487079, 1487694, 2446395}
    if step == "create_adset" and subcode in DEPRECATED_INTEREST_SUBCODES and isinstance(inner, dict):
        edata = inner.get("error_data") if isinstance(inner.get("error_data"), dict) else {}
        blame = edata.get("blame_field_specs")
        captured: List[str] = []
        if isinstance(blame, list):
            for entry in blame:
                if isinstance(entry, list):
                    for v in entry:
                        if isinstance(v, (str, int)) and str(v).isdigit():
                            captured.append(str(v))
        if captured:
            _record_bad_interest_ids(captured)
            auto_fixes.append(
                f"Recorded {len(captured)} interest/behavior ID(s) as known-bad after "
                f"subcode {subcode} rejection — future sync calls will auto-drop them."
            )


def _sync_fail_with_rollback(
    auto_fixes: List[str],
    err: Any,
    step: str,
    token: str,
    options: Dict[str, Any],
    campaign_id: Optional[str] = None,
    **extra: Any,
) -> Dict[str, Any]:
    _maybe_emit_meta_hints(auto_fixes, err, step)
    rb = _apply_campaign_rollback_on_sync_failure(token, campaign_id, options, auto_fixes, step)
    base = _sync_step_error(auto_fixes, err, step, **extra)
    base.update(rb)
    if rb.get("rollback_ok"):
        base["campaign_id"] = None
        base["adset_id"] = None
        base["creative_id"] = None
    return base


def sanitize_placements_in_targeting(
    targeting: Dict[str, Any], strict: bool = False,
) -> Tuple[Dict[str, Any], List[str]]:
    """Remove invalid placement tokens; if nothing valid left, drop placement keys (Advantage+ style).
    If strict=True, invalid tokens raise instead of being dropped."""
    fixes: List[str] = []
    if not isinstance(targeting, dict):
        return {}, fixes
    t = dict(targeting)
    for key, allowed in (
        ("facebook_positions", ALLOWED_FB_POSITIONS),
        ("instagram_positions", ALLOWED_IG_POSITIONS),
        ("messenger_positions", ALLOWED_MESSENGER_POSITIONS),
    ):
        raw = t.get(key)
        if not raw:
            continue
        if not isinstance(raw, list):
            continue
        cleaned = [str(x).strip().lower() for x in raw if str(x).strip().lower() in allowed]
        dropped = [x for x in raw if str(x).strip().lower() not in allowed]
        if dropped:
            retired = [
                _RETIRED_PLACEMENTS_V26[(key, str(x).strip().lower())]
                for x in dropped
                if (key, str(x).strip().lower()) in _RETIRED_PLACEMENTS_V26
            ]
            why = (" " + "; ".join(retired) + ".") if retired else ""
            if strict:
                raise ValueError(
                    f"Invalid placement: {key!r} contains values Meta rejects in strict_mode: {dropped!r}.{why}\n"
                    f"Use: only allowed lowercase tokens for this field (see server placement allowlists), or omit manual placements."
                )
            fixes.append(f"Removed invalid {key}: {dropped}.{why}")
        if cleaned:
            t[key] = cleaned
        else:
            if strict and raw:
                raise ValueError(
                    f"Invalid placement: {key!r} has no valid tokens left after validation.\n"
                    f"Use: pass at least one allowed token for {key!r}, or remove the key for automatic placements."
                )
            t.pop(key, None)
            fixes.append(f"Cleared {key} (no valid tokens); using automatic placements for that platform subset")
    # If publisher_platforms set but all position lists gone, keep platforms (Meta may auto-place)
    for key, allowed in (
        ("audience_network_positions", ALLOWED_AUDIENCE_NETWORK_POSITIONS),
        ("threads_positions", ALLOWED_THREADS_POSITIONS),
    ):
        raw = t.get(key)
        if not raw:
            continue
        if not isinstance(raw, list):
            continue
        cleaned = [str(x).strip().lower() for x in raw if str(x).strip().lower() in allowed]
        dropped = [x for x in raw if str(x).strip().lower() not in allowed]
        if dropped:
            if strict:
                raise ValueError(
                    f"Invalid placement: {key!r} contains values Meta may reject in strict_mode: {dropped!r}.\n"
                    f"Use: allowed tokens for this field (see server placement allowlists), or omit manual placements."
                )
            fixes.append(f"Removed invalid {key}: {dropped}")
        if cleaned:
            t[key] = cleaned
        else:
            if strict and raw:
                raise ValueError(
                    f"Invalid placement: {key!r} has no valid tokens left after validation.\n"
                    f"Use: pass at least one allowed token for {key!r}, or remove the key for automatic placements."
                )
            t.pop(key, None)
            fixes.append(f"Cleared {key} (no valid tokens); using automatic placements for that platform subset")

    ppub = t.get("publisher_platforms")
    if isinstance(ppub, list):
        t["publisher_platforms"] = [str(p).strip().lower() for p in ppub if str(p).strip()]
    return t, fixes


def apply_automatic_placements(targeting: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """Strip manual placement fields so Meta uses Advantage+ / automatic placements."""
    fixes = ["Dropped publisher_platforms and position lists for Advantage+ (automatic) placements fallback"]
    t = dict(targeting) if isinstance(targeting, dict) else {}
    for k in (
        "publisher_platforms",
        "device_platforms",
        "facebook_positions",
        "instagram_positions",
        "messenger_positions",
        "audience_network_positions",
        "threads_positions",
    ):
        t.pop(k, None)
    return t, fixes


def _normalize_objective_for_compare(obj: Any) -> str:
    return str(obj or "").strip().upper()


def _objectives_match(api_objective: Any, doc_objective: str) -> bool:
    return _normalize_objective_for_compare(api_objective) == _normalize_objective_for_compare(doc_objective)


def find_campaign_by_name(account_id: str, token: str, name: str) -> Optional[str]:
    """First campaign with exact name (any objective)."""
    if not name or not str(name).strip():
        return None
    filt = json.dumps([{"field": "name", "operator": "EQUAL", "value": str(name).strip()}])
    data = services.get_campaigns(account_id, token, limit=50, filtering=filt)
    if _has_api_error(data):
        return None
    rows = data.get("data") or []
    if rows:
        return str(rows[0].get("id") or "").strip() or None
    return None


def find_campaign_by_name_and_objective(
    account_id: str, token: str, name: str, expected_objective: str
) -> Optional[str]:
    """Return campaign id for exact name where API objective matches expected_objective (utility; sync no longer uses this)."""
    if not name or not str(name).strip() or not str(expected_objective or "").strip():
        return None
    filt = json.dumps([{"field": "name", "operator": "EQUAL", "value": str(name).strip()}])
    data = services.get_campaigns(account_id, token, limit=50, filtering=filt)
    if _has_api_error(data):
        return None
    for row in data.get("data") or []:
        cid = str(row.get("id") or "").strip()
        if not cid:
            continue
        if _objectives_match(row.get("objective"), expected_objective):
            return cid
    return None


def resolve_interest_queries(
    account_id: str, token: str, queries: List[str], fixes: List[str], strict: bool = False,
) -> List[Dict[str, Any]]:
    interests: List[Dict[str, Any]] = []
    for q in queries or []:
        qq = str(q).strip()
        if not qq:
            continue
        r = services.targeting_search_with_fallback(account_id, token, "adinterest", q=qq, limit=10)
        if _has_api_error(r):
            if strict:
                raise ValueError(f"strict_mode: invalid targeting — interest search failed for {qq!r}: {_api_error_message(r)}")
            fixes.append(f"Interest search failed for {qq!r}: {_api_error_message(r)}")
            continue
        row = (r.get("data") or [{}])[0]
        tid = row.get("id")
        if tid:
            interests.append({"id": str(tid), "name": row.get("name", qq)})
        else:
            if strict:
                raise ValueError(f"strict_mode: invalid targeting — no adinterest match for {qq!r}")
            fixes.append(f"No adinterest match for {qq!r}; skipped")
    return interests


def build_targeting_from_document(
    account_id: str, token: str, doc: Dict[str, Any], fixes: List[str], strict: bool = False,
) -> Dict[str, Any]:
    tdoc = doc.get("targeting")
    if isinstance(tdoc, str):
        tdoc = _parse_json_maybe(tdoc)
    if not isinstance(tdoc, dict):
        tdoc = {}
    # Compatibility: accept BOTH the Meta-API nested form and the MCP's flat form.
    # Meta's own docs (and the agent's rule_body) describe targeting as
    #   {"geo_locations": {"countries": [...], "cities": [...], "regions": [...]}}
    # but downstream services.build_targeting_spec expects FLAT keys
    #   {"geo_countries": [...], "geo_cities": [...], "geo_regions": [...]}
    # Without this adapter the nested form silently produces empty geo and
    # Meta rejects with subcode 1885364 ("audience targeting is incomplete").
    geoloc = tdoc.get("geo_locations")
    if isinstance(geoloc, dict):
        tdoc = dict(tdoc)  # don't mutate the caller's doc
        nested_to_flat = {
            "countries":            "geo_countries",
            "regions":              "geo_regions",
            "cities":               "geo_cities",
            "custom_locations":     "geo_custom_locations",
            "country_groups":       "geo_country_groups",
            "location_types":       "geo_location_types",
            "zips":                 "geo_zips",
        }
        for nested_key, flat_key in nested_to_flat.items():
            if geoloc.get(nested_key) is not None and tdoc.get(flat_key) is None:
                tdoc[flat_key] = geoloc[nested_key]
        # Pop the nested form so downstream callers don't see both shapes.
        tdoc.pop("geo_locations", None)

    # Conflict guard: Meta does not accept BOTH cities and countries in the
    # same geo_locations spec — it returns subcode 1487756 ("Locations can't
    # be used"). If both are present, prefer cities (more specific) and drop
    # countries with an auto_fix note.
    if tdoc.get("geo_cities") and tdoc.get("geo_countries"):
        dropped = tdoc.pop("geo_countries", None)
        fixes.append(
            f"Dropped geo_locations.countries={dropped} because cities were also "
            f"set (Meta rejects both with subcode 1487756). Cities take precedence."
        )

    # Universal city-key validator. The agent often pastes hardcoded city keys
    # from memory ("1023040" etc.) without running facebook_targeting_search.
    # Stale/wrong keys resolve to garbage cities ("Chanpura Pain", "Khamar")
    # which Meta accepts silently. Probe each key against Meta's
    # /search?type=adgeolocationmeta&keys=... and reject the sync if ANY key
    # resolves to something other than a real city or returns no data.
    cities_for_validation = tdoc.get("geo_cities")
    if isinstance(cities_for_validation, list) and cities_for_validation and account_id and token:
        keys_to_check: List[str] = []
        for c in cities_for_validation:
            if isinstance(c, dict):
                k = str(c.get("key") or "").strip()
            else:
                k = str(c or "").strip()
            if k and k.isdigit():
                keys_to_check.append(k)
        if keys_to_check:
            try:
                import requests as _rq
                act = account_id if str(account_id).startswith("act_") else f"act_{account_id}"
                url = f"{services.BASE_URL}/search"
                params = {
                    "access_token": token,
                    "type": "adgeolocationmeta",
                    "keys": json.dumps(keys_to_check),
                }
                vr = _rq.get(url, params=params, timeout=10)
                vbody = vr.json() if vr.content else {}
            except Exception:
                vbody = {}
            data_rows = vbody.get("data") if isinstance(vbody, dict) else None
            if isinstance(data_rows, list) and data_rows:
                # Build a key -> {name, type, country_code} map.
                resolved = {}
                for row in data_rows:
                    if not isinstance(row, dict):
                        continue
                    rk = str(row.get("key") or "").strip()
                    if rk:
                        resolved[rk] = row
                bad_keys: List[str] = []
                for k in keys_to_check:
                    row = resolved.get(k)
                    if not row:
                        bad_keys.append(f"{k} (Meta returned no match)")
                        continue
                    rtype = str(row.get("type") or "").lower()
                    if rtype != "city":
                        bad_keys.append(
                            f"{k} resolves to '{row.get('name')}' (type={rtype}, "
                            f"country={row.get('country_code')})"
                        )
                if bad_keys:
                    msg = (
                        "FABRICATED OR STALE CITY KEYS DETECTED — refusing to create "
                        "campaign. Bad keys: " + "; ".join(bad_keys) + ". "
                        "Run facebook_targeting_search with search_type=adgeolocation, "
                        "country_code=IN, location_types=[\"city\"] for each city, "
                        "and copy the EXACT 'key' from the returned data. Do NOT "
                        "reuse city keys from memory."
                    )
                    fixes.append(msg)
                    raise ValueError(msg)
    targeting_mode = _targeting_mode_from_document(doc)
    # Pre-built flexible_spec / geo in document
    flexible_from_queries: List[Dict[str, Any]] = []
    if targeting_mode == "RESOLVE":
        iq = tdoc.get("interest_queries")
        if isinstance(iq, list) and iq:
            resolved = resolve_interest_queries(account_id, token, iq, fixes, strict=strict)
            if resolved:
                flexible_from_queries.append({"interests": resolved})
    bq = tdoc.get("behavior_queries")
    if targeting_mode == "RESOLVE" and isinstance(bq, list) and bq:
        for q in bq:
            qq = str(q).strip()
            if not qq:
                continue
            r = services.targeting_search_with_fallback(account_id, token, "adTargetingCategory", q=qq, limit=5)
            if _has_api_error(r):
                if strict:
                    raise ValueError(f"strict_mode: invalid targeting — behavior search failed for {qq!r}: {_api_error_message(r)}")
                fixes.append(f"Behavior search failed for {qq!r}: {_api_error_message(r)}")
                continue
            row = (r.get("data") or [{}])[0]
            if row.get("id"):
                flexible_from_queries.append({"behaviors": [{"id": str(row["id"]), "name": row.get("name", qq)}]})
            else:
                if strict:
                    raise ValueError(f"strict_mode: invalid targeting — no behavior match for {qq!r}")
                fixes.append(f"No behavior match for {qq!r}; skipped")
    edu = tdoc.get("education_status_queries")
    if targeting_mode == "RESOLVE" and isinstance(edu, list) and edu:
        for q in edu:
            qq = str(q).strip()
            if not qq:
                continue
            r = services.targeting_search_with_fallback(account_id, token, "adTargetingCategory", q=qq, limit=5)
            if _has_api_error(r):
                if strict:
                    raise ValueError(f"strict_mode: invalid targeting — education search failed for {qq!r}: {_api_error_message(r)}")
                fixes.append(f"Education search failed for {qq!r}: {_api_error_message(r)}")
                continue
            row = (r.get("data") or [{}])[0]
            if row.get("id"):
                flexible_from_queries.append(
                    {"education_statuses": [int(row["id"])]} if str(row["id"]).isdigit() else {"education_majors": [{"id": str(row["id"])}]}
                )
            else:
                if strict:
                    raise ValueError(f"strict_mode: invalid targeting — no education match for {qq!r}")
                fixes.append(f"No education match for {qq!r}; skipped")

    raw_flex = tdoc.get("flexible_spec")
    if isinstance(raw_flex, str):
        raw_flex = _parse_json_maybe(raw_flex)
    merged_flex: List[Any] = list(raw_flex) if isinstance(raw_flex, list) else []
    merged_flex.extend(flexible_from_queries)
    # Rename `demographics` -> `behaviors` in flexible_spec slices. Meta's API
    # has NO `demographics` axis (the Ads Manager UI shows "Demographics" as a
    # category but the API uses `behaviors` for items like "Entrepreneurs",
    # "Small business owners", etc.). Sending `demographics` triggers code 1.
    if merged_flex:
        renamed_count = 0
        for slice_ in merged_flex:
            if not isinstance(slice_, dict):
                continue
            demos = slice_.get("demographics")
            if isinstance(demos, list) and demos:
                existing_behaviors = slice_.get("behaviors") or []
                if not isinstance(existing_behaviors, list):
                    existing_behaviors = []
                slice_["behaviors"] = existing_behaviors + demos
                slice_.pop("demographics", None)
                renamed_count += len(demos)
        if renamed_count:
            fixes.append(
                f"Renamed {renamed_count} 'demographics' entrie(s) to 'behaviors' "
                "(Meta's API has no 'demographics' axis in flexible_spec; "
                "those IDs are 'behaviors' in the API)."
            )

    # Split mixed-axis flexible_spec slices. Meta rejects (code 1, no subcode)
    # when a single slice contains multiple axes — each axis must be its own
    # slice. Auto-split so the agent can keep using the more natural
    # "{interests:[...], behaviors:[...]}" shorthand.
    if merged_flex:
        split_flex: List[Any] = []
        for slice_ in merged_flex:
            if not isinstance(slice_, dict):
                split_flex.append(slice_)
                continue
            present_axes = [k for k in ("interests", "behaviors",
                                         "education_statuses", "education_majors",
                                         "income", "industries", "family_statuses",
                                         "life_events", "work_employers", "work_positions",
                                         "relationship_statuses")
                            if isinstance(slice_.get(k), list) and slice_.get(k)]
            if len(present_axes) > 1:
                # Split into one slice per axis. Preserve any non-axis keys
                # (e.g. "name") on each emitted slice for parity with input.
                non_axis = {k: v for k, v in slice_.items() if k not in present_axes}
                for ax in present_axes:
                    one_slice = dict(non_axis)
                    one_slice[ax] = slice_[ax]
                    split_flex.append(one_slice)
                fixes.append(
                    f"Split flexible_spec slice with multiple axes ({', '.join(present_axes)}) "
                    "into separate slices (Meta rejects mixed-axis slices with code 1)."
                )
            else:
                split_flex.append(slice_)
        merged_flex = split_flex
    # Drop interest IDs Meta has rejected before (learned from past 1487079).
    learned_bad = _learned_bad_interest_ids()
    if learned_bad and merged_flex:
        cleaned: List[Any] = []
        for slice_ in merged_flex:
            if not isinstance(slice_, dict):
                cleaned.append(slice_)
                continue
            new_slice = dict(slice_)
            for axis_key in ("interests", "behaviors", "demographics"):
                items = new_slice.get(axis_key)
                if not isinstance(items, list):
                    continue
                kept = []
                for it in items:
                    iid = str(it.get("id") or "") if isinstance(it, dict) else ""
                    if iid and iid in learned_bad:
                        fixes.append(
                            f"Dropped {axis_key[:-1]} {iid} (known-bad ID from prior 1487079 rejection)"
                        )
                        continue
                    kept.append(it)
                if kept:
                    new_slice[axis_key] = kept
                else:
                    new_slice.pop(axis_key, None)
            # Keep the slice if it still carries any targeting axis at all —
            # not just interests/behaviors/demographics. Narrow checks here used
            # to silently drop slices targeting work_employers, income, etc.
            if _slice_has_any_targeting(new_slice):
                cleaned.append(new_slice)
        merged_flex = cleaned
    # Filter Meta-deprecated interests (e.g. "Instagram business profile admins",
    # "Business Page admins") that still appear in batch_interest_search but
    # cause subcode 1487079 at adset creation. Driven by META_RULES.
    blocked_patterns = [
        str(p).lower().strip()
        for p in (META_RULES.get("blocked_interest_name_patterns") or [])
        if str(p).strip()
    ]
    if blocked_patterns and merged_flex:
        cleaned_flex: List[Any] = []
        for slice_ in merged_flex:
            if not isinstance(slice_, dict):
                cleaned_flex.append(slice_)
                continue
            new_slice = dict(slice_)
            for axis_key in ("interests", "behaviors", "demographics"):
                items = new_slice.get(axis_key)
                if not isinstance(items, list):
                    continue
                kept: List[Any] = []
                for it in items:
                    name = ""
                    if isinstance(it, dict):
                        name = str(it.get("name") or "").lower()
                    if name and any(p in name for p in blocked_patterns):
                        fixes.append(
                            f"Filtered deprecated {axis_key[:-1]} '{it.get('name')}' "
                            f"(matches blocked pattern; Meta rejects with subcode 1487079)"
                        )
                        continue
                    kept.append(it)
                if kept:
                    new_slice[axis_key] = kept
                else:
                    new_slice.pop(axis_key, None)
            # Drop empty slices entirely. Includes ALL targeting axes so a
            # slice that retains valid work_employers / income / etc. survives
            # even when its interests/behaviors got blocked by a pattern match.
            if _slice_has_any_targeting(new_slice):
                cleaned_flex.append(new_slice)
        merged_flex = cleaned_flex
    # Reject fabricated targeting IDs LOUDLY. The agent sometimes invents
    # round-number or sequential interest IDs (e.g. 6003100000000) instead of
    # using the IDs returned by facebook_batch_interest_search. Meta rejects
    # these with subcode 1487079 only at create_adset, AFTER the campaign exists,
    # which produces orphan campaigns. Catch them up front and fail with a
    # clear ValueError so the agent sees the exact bad IDs and can correct.
    # Real Meta interest IDs are random 13-15 digit numbers.
    if merged_flex:
        offenders: List[str] = []
        for slice_ in merged_flex:
            if not isinstance(slice_, dict):
                continue
            for axis_key in ("interests", "behaviors", "demographics"):
                items = slice_.get(axis_key)
                if not isinstance(items, list):
                    continue
                for it in items:
                    iid = str(it.get("id") or "") if isinstance(it, dict) else ""
                    if not iid:
                        continue
                    # Heuristics: non-digit, too short, or many trailing zeros.
                    looks_fake = (
                        not iid.isdigit()
                        or len(iid) < 11
                        or (iid.endswith("00000") and iid.count("0") >= 6)
                    )
                    if looks_fake:
                        nm = (it.get("name") if isinstance(it, dict) else "") or "unnamed"
                        offenders.append(f"{axis_key}:{iid} ({nm})")
        if offenders:
            msg = (
                "FABRICATED TARGETING IDs DETECTED — refusing to create campaign. "
                f"Suspect IDs: {', '.join(offenders)}. "
                "Real Meta interest/behavior IDs are random 13-15 digit numbers. "
                "Run facebook_batch_interest_search with the desired interest names, "
                "then copy the EXACT 'id' values from the 'found' array into "
                "targeting.flexible_spec. Do NOT invent IDs."
            )
            # Surface in fixes too so agents that swallow the exception still see why.
            fixes.append(msg)
            raise ValueError(msg)
    # Pre-flight Meta-side validity probe. The batch_interest_search endpoint
    # returns IDs that look fine but are stale/deprecated under the hood, which
    # surface as subcode 1487079 only at create_adset (after the campaign exists).
    # Asking Meta's /targetingvalidation up front lets us drop them before we
    # touch the campaign, eliminating the reactive rollback round-trip.
    # Disable with FB_ADS_PREFLIGHT_VALIDATE_INTERESTS=0.
    preflight_on = (os.environ.get("FB_ADS_PREFLIGHT_VALIDATE_INTERESTS") or "1").strip().lower() not in ("0", "false", "no", "off")
    if preflight_on and merged_flex:
        # Collect (axis, id) pairs across every Meta-validatable targeting
        # axis, not just interests/behaviors/demographics. Probing all in
        # one round-trip catches deprecation across every axis (subcodes
        # 1487079 / 1487694 / 2446395) before we touch a campaign and
        # protects axes briefs increasingly use (work_employers, life_events,
        # education_majors, etc.).
        _AXIS_TO_VALIDATION_TYPE = {
            "interests":           "interests",
            "behaviors":           "behaviors",
            "demographics":        "demographics",
            "work_employers":      "work_employers",
            "work_positions":      "work_positions",
            "education_majors":    "education_majors",
            "education_schools":   "education_schools",
            "education_statuses":  "education_statuses",
            "industries":          "industries",
            "income":              "income",
            "family_statuses":     "family_statuses",
            "life_events":         "life_events",
            "relationship_statuses": "relationship_statuses",
        }
        candidate_pairs: List[Tuple[str, str]] = []
        for slice_ in merged_flex:
            if not isinstance(slice_, dict):
                continue
            for axis_key, vtype in _AXIS_TO_VALIDATION_TYPE.items():
                items = slice_.get(axis_key)
                if not isinstance(items, list):
                    continue
                for it in items:
                    if isinstance(it, dict) and it.get("id"):
                        candidate_pairs.append((vtype, str(it["id"])))
        if candidate_pairs:
            invalid_ids = _validate_targeting_ids_with_meta(account_id, token, candidate_pairs)
            if invalid_ids:
                _record_bad_interest_ids(list(invalid_ids))
                pruned_flex: List[Any] = []
                for slice_ in merged_flex:
                    if not isinstance(slice_, dict):
                        pruned_flex.append(slice_)
                        continue
                    new_slice = dict(slice_)
                    for axis_key in _AXIS_TO_VALIDATION_TYPE:
                        items = new_slice.get(axis_key)
                        if not isinstance(items, list):
                            continue
                        kept = []
                        for it in items:
                            iid = str(it.get("id") or "") if isinstance(it, dict) else ""
                            if iid and iid in invalid_ids:
                                fixes.append(
                                    f"Pre-flight pruned {axis_key[:-1] if axis_key.endswith('s') else axis_key} {iid} "
                                    f"({(it.get('name') if isinstance(it, dict) else '') or 'unnamed'}) — "
                                    f"Meta /targetingvalidation reports valid=false"
                                )
                                continue
                            kept.append(it)
                        if kept:
                            new_slice[axis_key] = kept
                        else:
                            new_slice.pop(axis_key, None)
                    # Keep the slice if any targeting axis remains — preserves
                    # work_employers / income / etc. when the preflight probe
                    # only invalidated the interests on this slice.
                    if _slice_has_any_targeting(new_slice):
                        pruned_flex.append(new_slice)
                merged_flex = pruned_flex
    merged_arg = merged_flex if merged_flex else None

    spec = services.build_targeting_spec(
        geo_countries=tdoc.get("geo_countries"),
        geo_regions=tdoc.get("geo_regions"),
        geo_cities=tdoc.get("geo_cities"),
        geo_custom_locations=tdoc.get("geo_custom_locations"),
        geo_country_groups=tdoc.get("geo_country_groups"),
        geo_location_types=tdoc.get("geo_location_types"),
        geo_radius_km=tdoc.get("geo_radius_km"),
        age_min=tdoc.get("age_min"),
        age_max=tdoc.get("age_max"),
        genders=tdoc.get("genders"),
        flexible_spec=json.dumps(merged_arg) if merged_arg is not None else None,
        custom_audiences=tdoc.get("custom_audiences"),
        excluded_geo_locations=tdoc.get("excluded_geo_locations"),
        excluded_custom_audiences=tdoc.get("excluded_custom_audiences"),
        targeting_override=tdoc.get("targeting_json") or tdoc.get("raw"),
        publisher_platforms=tdoc.get("publisher_platforms"),
        device_platforms=tdoc.get("device_platforms"),
        facebook_positions=tdoc.get("facebook_positions"),
        instagram_positions=tdoc.get("instagram_positions"),
        messenger_positions=tdoc.get("messenger_positions"),
    )
    placements_doc = doc.get("placements") or {}
    if isinstance(placements_doc, dict):
        if placements_doc.get("use_automatic_placements") or placements_doc.get("advantage_plus"):
            spec, pfixes = apply_automatic_placements(spec)
            fixes.extend(pfixes)
        else:
            spec = _deep_merge(
                spec,
                {
                    k: placements_doc[k]
                    for k in (
                        "publisher_platforms",
                        "device_platforms",
                        "facebook_positions",
                        "instagram_positions",
                        "messenger_positions",
                        "audience_network_positions",
                        "threads_positions",
                    )
                    if placements_doc.get(k) is not None
                },
            )
    # Pass-through fields that build_targeting_spec doesn't take as kwargs but
    # Meta requires for valid targeting. Without these the API rejects the
    # adset (e.g. targeting_automation missing => subcode 1870227).
    for passthrough_key in ("targeting_automation", "exclusions", "narrow", "user_os", "user_device", "wireless_carrier"):
        if tdoc.get(passthrough_key) is not None:
            spec[passthrough_key] = tdoc[passthrough_key]
    # Universal placement alias map (Meta v25 enums). Runs in BOTH strict and
    # non-strict paths because strict's validate_document_strict_passthrough
    # skips validate_and_normalize_document where the alias call also happens.
    _alias_placement_tokens(spec, fixes)
    spec, pfixes = sanitize_placements_in_targeting(spec, strict=strict)
    fixes.extend(pfixes)
    # Strip 'search' from facebook_positions when optimization is call-based.
    # Meta sometimes accepts at validation but rejects at delivery for call ads.
    adset_doc = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    opt_goal = str(adset_doc.get("optimization_goal", "")).upper()
    if opt_goal in CALL_OPTIMIZATION_GOALS:
        fb_positions = spec.get("facebook_positions") or []
        if isinstance(fb_positions, list) and "search" in fb_positions:
            spec["facebook_positions"] = [p for p in fb_positions if p != "search"]
            fixes.append(
                f"Removed 'search' from facebook_positions for {opt_goal} "
                "(Meta rejects search placement for call-optimization ads)"
            )
    return spec


# Meta's current Marketing API enum for special_ad_categories. Any value not in
# this set is silently dropped (with an auto_fix log line) — Meta returns
# (#100) Invalid parameter or (#100) parameter is required for unknown enums,
# and the LLM has historically tried to invent stand-in values like "NONE"
# (causing 1885097 Type Mismatch on the next call).
_SPECIAL_AD_CATEGORIES_ENUM = frozenset({
    "HOUSING",
    "EMPLOYMENT",
    "CREDIT",
    "ISSUES_ELECTIONS_POLITICS",
    "ONLINE_GAMBLING_AND_GAMING",
    "FINANCIAL_PRODUCTS_SERVICES",
})

# Values the LLM has produced as a "no special category" sentinel — all
# rewritten to the empty-list literal Meta actually expects.
_SPECIAL_AD_CATEGORIES_NONE_TOKENS = frozenset({
    "NONE", "NULL", "NA", "N/A", "NIL", "EMPTY", "DEFAULT", "STANDARD",
})


def _normalize_special_ad_categories_on_campaign(camp: Dict[str, Any], fixes: List[str]) -> None:
    """Coerce ``campaign.special_ad_categories`` into a Meta-valid list.

    Meta requires the field present with a list value; an empty list ``[]`` is
    the correct shape when no special category applies. The LLM has produced
    several wrong shapes — this function fixes them all and emits an auto_fix
    line per change so the caller can surface them.

    Rules:
      - ``None`` / missing → ``[]`` (Meta still requires the field present)
      - ``[]`` → ``[]`` (passthrough — and we now ALWAYS write the key back so
        downstream callers never drop the empty list before POST)
      - ``"foo"`` (string) → JSON-parse if possible, else ``[]``
      - ``["NONE"]`` / ``["none"]`` / ``[null]`` / ``[""]`` → ``[]`` + auto_fix
      - Unknown enum strings (anything outside ``_SPECIAL_AD_CATEGORIES_ENUM``)
        → dropped with auto_fix
      - Legacy ``ISSUES_ELECTIONS_OR_POLITICS`` → ``ISSUES_ELECTIONS_POLITICS``
    """
    raw = camp.get("special_ad_categories")
    # Coerce to list shape.
    if raw is None:
        camp["special_ad_categories"] = []
        return
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = [raw] if str(raw).strip() else []
    if not isinstance(raw, list):
        camp["special_ad_categories"] = []
        return
    out: List[str] = []
    dropped: List[str] = []
    for x in raw:
        if x is None:
            dropped.append("null")
            continue
        u = str(x).strip().upper()
        if not u:
            dropped.append("empty string")
            continue
        if u in _SPECIAL_AD_CATEGORIES_NONE_TOKENS:
            dropped.append(u)
            continue
        if u == "ISSUES_ELECTIONS_OR_POLITICS":
            fixes.append(
                "Normalized special_ad_categories ISSUES_ELECTIONS_OR_POLITICS -> ISSUES_ELECTIONS_POLITICS (current Marketing API enum)"
            )
            u = "ISSUES_ELECTIONS_POLITICS"
        if u not in _SPECIAL_AD_CATEGORIES_ENUM:
            dropped.append(u)
            continue
        out.append(u)
    if dropped:
        fixes.append(
            "special_ad_categories: dropped invalid value(s) "
            f"{dropped} → resolved list is {out}. Meta accepts only "
            f"{sorted(_SPECIAL_AD_CATEGORIES_ENUM)} or [] for none. Send `[]` "
            "exactly (empty array literal) when no special category applies — "
            "do NOT send ['NONE'], 'null', or omit the field; Meta returns #100."
        )
    camp["special_ad_categories"] = out


def _parse_meta_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    s_norm = s.replace("Z", "+00:00")
    if "+" in s_norm[10:] and s_norm[-5] != ":":
        s_norm = s_norm[:-2] + ":" + s_norm[-2:]
    try:
        dt = datetime.fromisoformat(s_norm)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt_tz.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _format_meta_time(dt: datetime) -> str:
    return dt.astimezone(_dt_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _normalize_schedule(ads: Dict[str, Any], fixes: List[str]) -> None:
    """Auto-bump past start_time and ensure end_time >= start_time + min_duration.

    Meta runtime rejects ad sets with daily_budget when the schedule window is
    <24h+buffer (subcode 1487793 "Campaign schedule is too short"). A naive
    exact-24h push fails because by the time Meta validates the spec a few
    seconds have elapsed since we computed the times. Add a safety buffer
    (default 1h) on top of the documented minimum so spec computation +
    Meta validation latency don't shrink the window below 24h.
    """
    grace = int(META_RULES.get("schedule_grace_seconds") or 300)
    base_min_dur = int(META_RULES.get("min_adset_duration_seconds") or 86400)
    # Safety buffer above Meta's documented minimum. Meta validates against
    # the time of API receipt, not the time we constructed the spec — by then
    # a few seconds to minutes have passed, so end_time = start_time + 24h
    # exactly can land at 23h59m on Meta's clock. 1h is generous but cheap.
    safety_buffer = int(META_RULES.get("min_adset_duration_buffer_seconds") or 3600)
    min_dur = base_min_dur + safety_buffer
    now_utc = datetime.now(tz=_dt_tz.utc)

    raw_start = ads.get("start_time")
    raw_end = ads.get("end_time")
    start_dt = _parse_meta_time(raw_start)
    end_dt = _parse_meta_time(raw_end)
    original_start_dt = start_dt
    start_was_bumped = False

    if start_dt is not None and start_dt < now_utc:
        new_start = now_utc + timedelta(seconds=grace)
        ads["start_time"] = _format_meta_time(new_start)
        fixes.append(
            f"Bumped past start_time {raw_start} -> {ads['start_time']} (Meta rejects past start times)"
        )
        start_dt = new_start
        start_was_bumped = True

    if end_dt is not None:
        ref_start = start_dt or now_utc
        if end_dt < ref_start + timedelta(seconds=min_dur):
            # If the original end_time was meant to give a longer window than
            # the minimum (e.g. user wanted a 7-day campaign), preserve that
            # window relative to the new start_time. Otherwise apply the
            # min_dur floor.
            preserved_window = None
            if start_was_bumped and original_start_dt is not None and end_dt > original_start_dt:
                desired = end_dt - original_start_dt
                if desired > timedelta(seconds=min_dur):
                    preserved_window = desired
            if preserved_window is not None:
                new_end = ref_start + preserved_window
                reason = (
                    f"to preserve the original {preserved_window.total_seconds()/86400:.1f}-day "
                    f"window after start_time was bumped"
                )
            else:
                new_end = ref_start + timedelta(seconds=min_dur)
                reason = (
                    f"to ensure >= start_time + {min_dur//3600}h "
                    f"(Meta's 24h minimum + {safety_buffer//3600}h safety buffer for daily_budget)"
                )
            ads["end_time"] = _format_meta_time(new_end)
            fixes.append(
                f"Pushed end_time {raw_end} -> {ads['end_time']} {reason}"
            )


# Universal placement-alias maps. Meta v25 renamed several common user-input
# tokens; apply BEFORE the strict allowlist check so canonical tokens land in
# ALLOWED_FB_POSITIONS / ALLOWED_IG_POSITIONS. Same map applies to any ad type.
_FB_POSITION_ALIASES: Dict[str, str] = {
    "reels": "facebook_reels",
    "reel": "facebook_reels",
    "story": "story",
    "stories": "story",
    "feed": "feed",
    "search": "search",
    "marketplace": "marketplace",
    "video_feed": "video_feeds",
    "video_feeds": "video_feeds",
    "right_hand_column": "right_hand_column",
    "instant_article": "instant_article",
    "instream_video": "instream_video",
    "facebook_reels": "facebook_reels",
    "facebook_reels_overlay": "facebook_reels_overlay",
    "profile_feed": "profile_feed",
}

# Instagram positions: bare "reels" / "story" ARE canonical on the IG side
# (different from facebook_positions). Map common user-input typos.
_IG_POSITION_ALIASES: Dict[str, str] = {
    "reels": "reels",
    "reel": "reels",
    "stories": "story",
    "story": "story",
    "feed": "stream",
    "stream": "stream",
    "profile_feed": "profile_feed",
    "ig_search": "ig_search",
    "search": "ig_search",
}


def _alias_placement_tokens(holder: Dict[str, Any], fixes: List[str]) -> None:
    """Map common user-input placement tokens to Meta's canonical v25 enums.

    Operates on whichever dict holds the lists (``placements`` or
    ``targeting``). Applies BEFORE the strict allowlist check so legitimate
    tokens like ``"reels"`` (facebook_positions) survive normalization as
    ``"facebook_reels"``. Idempotent - already-canonical tokens stay put.
    """
    if not isinstance(holder, dict):
        return
    for field, table in (
        ("facebook_positions", _FB_POSITION_ALIASES),
        ("instagram_positions", _IG_POSITION_ALIASES),
    ):
        lst = holder.get(field)
        if not isinstance(lst, list) or not lst:
            continue
        new_list: List[str] = []
        for raw in lst:
            tok = str(raw or "").strip().lower()
            if not tok:
                continue
            canonical = table.get(tok, tok)
            if canonical != tok:
                fixes.append(
                    f"Aliased {field}: '{raw}' -> '{canonical}' (Meta v25 token)."
                )
            if canonical not in new_list:
                new_list.append(canonical)
        holder[field] = new_list


# Map special_ad_categories enum -> required creative.authorization_category.
# Only politics needs the creative flag; housing/employment/credit have their
# own compliance (targeting restrictions) and Meta does not require the creative
# field to match. Driven by Meta error subcode 2446466.
_SPECIAL_TO_AUTH: Dict[str, str] = {
    "ISSUES_ELECTIONS_POLITICS": "POLITICAL",
}


def _cascade_special_ad_category_to_creative(
    camp: Dict[str, Any], creative: Dict[str, Any], fixes: List[str],
) -> None:
    """When campaign.special_ad_categories implies an authorization_category
    on the creative, auto-set it. Does NOT overwrite an existing value the
    brief supplied.
    """
    if not isinstance(camp, dict) or not isinstance(creative, dict):
        return
    cats_raw = camp.get("special_ad_categories")
    if not isinstance(cats_raw, list):
        return
    cats = [str(c).strip().upper() for c in cats_raw if c]
    for cat in cats:
        auth = _SPECIAL_TO_AUTH.get(cat)
        if not auth:
            continue
        existing = str(creative.get("authorization_category") or "").strip()
        if existing:
            continue
        creative["authorization_category"] = auth
        fixes.append(
            f"Auto-set creative.authorization_category={auth} because "
            f"campaign.special_ad_categories includes {cat} (Meta requires "
            f"both to match - subcode 2446466 otherwise)."
        )


def _strip_invalid_destination_type(
    camp: Dict[str, Any], ads: Dict[str, Any], fixes: List[str],
) -> None:
    """Drop adset.destination_type when invalid for the chosen objective.

    Universal across ad types - uses ``meta_validation._VALID_OBJECTIVE_DESTINATIONS``
    (sourced from Meta's docs). Without this, the strict_mode preview validator
    catches the bad combo but the auto-fix path does NOT remove the value before
    sync, leading to a create_ad-time Meta error after campaign+adset already exist.
    """
    if not isinstance(camp, dict) or not isinstance(ads, dict):
        return
    obj = str(camp.get("objective") or "").strip().upper()
    dt = str(ads.get("destination_type") or "").strip().upper()
    if not obj or not dt:
        return
    try:
        valid = meta_validation._VALID_OBJECTIVE_DESTINATIONS.get(obj)
    except AttributeError:
        return
    if valid is None:
        return  # unknown objective - let Meta surface the real error
    if dt in valid:
        return
    ads.pop("destination_type", None)
    fixes.append(
        f"Dropped destination_type={dt} - invalid for objective {obj}. "
        f"Valid: {sorted(valid) or 'omit (Meta defaults to UNDEFINED)'}."
    )


def _remap_misplaced_positions(targeting: Dict[str, Any], fixes: List[str]) -> None:
    """Remap or rename placement tokens that are valid in spirit but in the
    wrong list / wrong name. Driven by META_RULES['position_remaps']."""
    if not isinstance(targeting, dict):
        return
    remaps = META_RULES.get("position_remaps") or {}
    if not isinstance(remaps, dict):
        return
    for src_key, dst_key in remaps.items():
        if not isinstance(src_key, str) or not isinstance(dst_key, str):
            continue
        try:
            src_token, src_field = src_key.split(":", 1)
            dst_token, dst_field = dst_key.split(":", 1)
        except ValueError:
            continue
        src_list = targeting.get(src_field)
        if not isinstance(src_list, list):
            continue
        src_token_lc = src_token.strip().lower()
        if not any(str(x).strip().lower() == src_token_lc for x in src_list):
            continue
        new_src = [x for x in src_list if str(x).strip().lower() != src_token_lc]
        if new_src:
            targeting[src_field] = new_src
        else:
            targeting.pop(src_field, None)
        dst_list = targeting.get(dst_field)
        if not isinstance(dst_list, list):
            dst_list = []
        dst_token_lc = dst_token.strip().lower()
        if not any(str(x).strip().lower() == dst_token_lc for x in dst_list):
            dst_list.append(dst_token_lc)
            targeting[dst_field] = dst_list
        if src_key != dst_key:
            fixes.append(
                f"Remapped placement '{src_token}' from {src_field} to {dst_field}"
                + (f" (renamed to '{dst_token}')" if src_token_lc != dst_token_lc else "")
            )


def _fallback_existing_post_without_id(creative: Dict[str, Any], fixes: List[str]) -> None:
    """If creative.mode=EXISTING_POST but no post id, fall back to LINK_AD."""
    if not META_RULES.get("fallback_existing_post_without_id_to_link_ad", True):
        return
    if not isinstance(creative, dict):
        return
    mode = str(creative.get("mode") or "").upper()
    if mode != "EXISTING_POST":
        return
    has_post_id = any(
        str(creative.get(k) or "").strip()
        for k in ("object_story_id", "instagram_post_id", "source_instagram_media_id", "instagram_permalink_url")
    )
    if has_post_id:
        return
    creative["mode"] = "LINK_AD"
    fixes.append(
        "Fell back creative.mode EXISTING_POST -> LINK_AD (no object_story_id / "
        "instagram_post_id / source_instagram_media_id / instagram_permalink_url supplied)"
    )


def validate_and_normalize_document(
    doc: Dict[str, Any],
    account_id: Optional[str] = None,
    token: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    fixes: List[str] = []
    out = json.loads(json.dumps(doc)) if isinstance(doc, dict) else {}

    # Pre-flight, dynamic-config-driven fixes (auto-fix common Meta-API
    # failure modes BEFORE the existing normalization logic runs).
    creative_pre = out.get("creative") if isinstance(out.get("creative"), dict) else {}
    if creative_pre:
        _fallback_existing_post_without_id(creative_pre, fixes)
        out["creative"] = creative_pre
    targeting_pre = out.get("targeting") if isinstance(out.get("targeting"), dict) else None
    if isinstance(targeting_pre, dict):
        _remap_misplaced_positions(targeting_pre, fixes)
        out["targeting"] = targeting_pre
    placements_pre = out.get("placements") if isinstance(out.get("placements"), dict) else None
    if isinstance(placements_pre, dict):
        _remap_misplaced_positions(placements_pre, fixes)
        _alias_placement_tokens(placements_pre, fixes)
        out["placements"] = placements_pre
    if isinstance(targeting_pre, dict):
        _alias_placement_tokens(targeting_pre, fixes)
        out["targeting"] = targeting_pre
    adset_pre = out.get("adset") if isinstance(out.get("adset"), dict) else None
    if isinstance(adset_pre, dict):
        _normalize_schedule(adset_pre, fixes)
        out["adset"] = adset_pre

    # Auto-cascade campaign.special_ad_categories -> creative.authorization_category.
    # Meta error subcode 2446466 fires at create_ad if campaign declares a
    # politics category but the creative lacks the corresponding flag. We do
    # NOT overwrite an authorization_category that the brief already set.
    _camp_pre = out.get("campaign") if isinstance(out.get("campaign"), dict) else None
    _creative_pre = out.get("creative") if isinstance(out.get("creative"), dict) else None
    if isinstance(_camp_pre, dict) and isinstance(_creative_pre, dict):
        _cascade_special_ad_category_to_creative(_camp_pre, _creative_pre, fixes)
        out["creative"] = _creative_pre

    # Strip invalid destination_type for the chosen objective BEFORE downstream
    # validators run. Uses Meta's destination_type compatibility matrix.
    _ads_pre = out.get("adset") if isinstance(out.get("adset"), dict) else None
    if isinstance(_camp_pre, dict) and isinstance(_ads_pre, dict):
        _strip_invalid_destination_type(_camp_pre, _ads_pre, fixes)
        out["adset"] = _ads_pre

    creative = out.get("creative") if isinstance(out.get("creative"), dict) else {}
    # Universal creative-mode inference. Single source of truth — keeps
    # strict and non-strict paths consistent and handles ALL ad types
    # (EXISTING_POST / WHATSAPP / DCO / CAROUSEL / COLLECTION / LEAD_FORM /
    # BRANDED_CONTENT) the same way, not just LINK_AD vs EXISTING_POST.
    inferred_mode = _infer_creative_mode(out)
    prior_mode = str(creative.get("mode") or "").upper()
    if inferred_mode and inferred_mode != prior_mode:
        creative["mode"] = inferred_mode
        if not prior_mode:
            fixes.append(
                f"Auto-set creative.mode={inferred_mode} from document signals "
                "(object_story_id / source_instagram_media_id / options.use_existing_post / "
                "destination_type / asset_feed_spec / carousel_cards / instant_experience_id)."
            )
    elif not prior_mode and not inferred_mode:
        # No signals at all — fall back to LINK_AD (the legacy default).
        creative["mode"] = "LINK_AD"
    elif prior_mode and not inferred_mode:
        # User explicitly set a mode; keep it as-is (normalized to upper).
        creative["mode"] = prior_mode

    # Side effect carried over from the previous logic: EXISTING_POST
    # creatives are incompatible with destination_type=WEBSITE.
    if str(creative.get("mode") or "").upper() == "EXISTING_POST":
        if str((out.get("adset") or {}).get("destination_type", "")).upper() == "WEBSITE":
            out.setdefault("adset", {})["destination_type"] = None
            fixes.append("Removed destination_type WEBSITE (incompatible with existing-post creative)")
    out["creative"] = creative

    cta = str(creative.get("cta_type") or "").strip().upper() or DEFAULT_CTA
    creative["cta_type"] = cta if cta else DEFAULT_CTA

    camp = out.setdefault("campaign", {})
    ads = out.setdefault("adset", {})
    if not camp.get("name"):
        raise ValueError("document.campaign.name is required")
    if not camp.get("objective"):
        raise ValueError("document.campaign.objective is required")
    _normalize_special_ad_categories_on_campaign(camp, fixes)

    obj_raw = str(camp.get("objective") or "").strip().upper()
    mapped_objective = LEGACY_OBJECTIVE_MAP.get(obj_raw)
    if mapped_objective:
        camp["objective"] = mapped_objective
        fixes.append(f"Normalized campaign objective {obj_raw} -> {mapped_objective} (Marketing API)")

    # Do not override campaign objective, optimization_goal, bid_strategy, or creative.authorization_category
    # for political/social special ads — use exactly what the document provides. Meta may reject some combos;
    # surface API errors to the caller instead of rewriting user intent.

    # --- Objective-aware adset defaults (non-strict only) ---
    final_objective = str(camp.get("objective") or "").strip().upper()
    obj_defaults = OBJECTIVE_ADSET_DEFAULTS.get(final_objective, {})
    raw_og = ads.get("optimization_goal")
    if raw_og is not None:
        og_norm = str(raw_og).strip().upper()
        if og_norm in ("", "NONE"):
            ads["optimization_goal"] = None
        elif og_norm in OPTIMIZATION_GOAL_ALIASES:
            mapped = OPTIMIZATION_GOAL_ALIASES[og_norm]
            ads["optimization_goal"] = mapped
            fixes.append(f"Normalized optimization_goal {og_norm} -> {mapped} (Meta does not accept {og_norm})")
        else:
            ads["optimization_goal"] = og_norm
    if not ads.get("optimization_goal") and obj_defaults.get("optimization_goal"):
        ads["optimization_goal"] = obj_defaults["optimization_goal"]
        fixes.append(f"Applied default optimization_goal={obj_defaults['optimization_goal']} for {final_objective}")
    if not ads.get("billing_event") and obj_defaults.get("billing_event"):
        ads["billing_event"] = obj_defaults["billing_event"]
    creative_mode = str(creative.get("mode") or "").upper()
    raw_dt = ads.get("destination_type")
    if raw_dt is not None:
        dt_norm = str(raw_dt).strip().upper()
        if dt_norm in ("", "NONE"):
            ads["destination_type"] = None
        elif dt_norm in DESTINATION_TYPE_ALIASES:
            mapped = DESTINATION_TYPE_ALIASES[dt_norm]
            ads["destination_type"] = mapped
            fixes.append(f"Normalized destination_type {dt_norm} -> {mapped} (Meta does not accept {dt_norm})")
        elif dt_norm not in VALID_ADSET_DESTINATION_TYPES:
            ads["destination_type"] = None
            fixes.append(f"Stripped invalid destination_type={dt_norm} (not in Meta whitelist)")
        else:
            ads["destination_type"] = dt_norm
    if final_objective == "OUTCOME_LEADS" and not ads.get("destination_type"):
        ads["destination_type"] = "ON_AD"
        fixes.append("Applied default destination_type=ON_AD for OUTCOME_LEADS (Instant Form)")
    elif (
        creative_mode != "EXISTING_POST"
        and not ads.get("destination_type")
        and obj_defaults.get("destination_type")
        and str(ads.get("optimization_goal", "")).upper() not in CALL_OPTIMIZATION_GOALS
    ):
        ads["destination_type"] = obj_defaults["destination_type"]
        fixes.append(f"Applied default destination_type={obj_defaults['destination_type']} for {final_objective}")

    # Universal goal-driven destination_type normalization (covers ANY ad
    # type whose goal optimizes against the ad itself). When an
    # optimization_goal is in `_GOALS_PREFER_UNDEFINED_DESTINATION` AND the
    # supplied destination_type is not in Meta's valid set for the
    # objective, clear destination_type — Meta treats absence as UNDEFINED
    # which is universally accepted. Sourced from Meta's own
    # destination_type compatibility matrix.
    try:
        from .meta_validation import (
            _GOALS_PREFER_UNDEFINED_DESTINATION,
            _VALID_OBJECTIVE_DESTINATIONS,
        )
        og_final = str(ads.get("optimization_goal") or "").strip().upper()
        dt_final = str(ads.get("destination_type") or "").strip().upper()
        if og_final in _GOALS_PREFER_UNDEFINED_DESTINATION and dt_final:
            valid_destinations = _VALID_OBJECTIVE_DESTINATIONS.get(final_objective) or frozenset()
            if dt_final not in valid_destinations:
                # Existing-post boosts optimize engagement against the post
                # itself, so Meta REQUIRES the post-level destination and
                # REJECTS UNDEFINED here ("Performance goal isn't available",
                # subcode 2490408). Remap the bad destination to ON_POST
                # instead of clearing it. Matrix-driven (ON_POST must be a
                # valid destination for the objective) — no goal hardcoding.
                if creative_mode == "EXISTING_POST" and "ON_POST" in valid_destinations:
                    ads["destination_type"] = "ON_POST"
                    fixes.append(
                        f"Remapped destination_type={dt_final} -> ON_POST for goal={og_final} under "
                        f"{final_objective} (existing-post boost; Meta requires ON_POST and rejects UNDEFINED)."
                    )
                elif "UNDEFINED" in valid_destinations:
                    ads["destination_type"] = None
                    fixes.append(
                        f"Cleared destination_type={dt_final} for goal={og_final} under {final_objective} "
                        f"(Meta rejects this combo; omitting destination_type maps to UNDEFINED, accepted universally)."
                    )
    except Exception:
        # Best-effort normalization; never let import or attribute errors
        # break the doc flow — downstream validate_document_for_sync will
        # still catch the bad combo.
        pass

    # Existing-post boosts with an on-content goal but NO destination supplied:
    # the objective-default block above is intentionally skipped for
    # EXISTING_POST, yet Meta still REQUIRES the post-level destination
    # (rejects an absent one with subcode 2490408). Set ON_POST from the
    # granular (objective, goal) whitelist so any post-engagement-style goal
    # works — no hardcoded goal enum.
    if creative_mode == "EXISTING_POST" and not ads.get("destination_type"):
        try:
            from .meta_validation import _OBJECTIVE_GOAL_DESTINATIONS as _OGD
            _og = str(ads.get("optimization_goal") or "").strip().upper()
            if "ON_POST" in (_OGD.get((final_objective, _og)) or frozenset()):
                ads["destination_type"] = "ON_POST"
                fixes.append(
                    f"Set destination_type=ON_POST for goal={_og} under {final_objective} "
                    f"(existing-post boost requires the post-level destination; Meta rejects an absent one)."
                )
        except Exception:
            pass

    # Goals whose granular (objective, goal) whitelist names exactly ONE
    # destination and does NOT allow UNDEFINED (e.g. THRUPLAY -> ON_VIDEO
    # under OUTCOME_ENGAGEMENT): Meta hard-rejects an absent destination_type
    # with subcode 2490408, so inject the required value. Runs AFTER the
    # clear-to-UNDEFINED normalization above, so a bad supplied destination
    # (dropped there) still ends up as the required one. Matrix-driven — no
    # goal hardcoding. Call goals excluded, matching the objective-default
    # guard above.
    if not ads.get("destination_type"):
        try:
            from .meta_validation import required_destination_for
            _og = str(ads.get("optimization_goal") or "").strip().upper()
            if _og not in CALL_OPTIMIZATION_GOALS:
                _req_dt = required_destination_for(final_objective, _og)
                if _req_dt:
                    ads["destination_type"] = _req_dt
                    fixes.append(
                        f"Set destination_type={_req_dt} for goal={_og} under {final_objective} "
                        f"(Meta requires this destination and rejects an absent one - subcode 2490408)."
                    )
        except Exception:
            pass

    # QUALITY_CALL needs promoted_object.page_id so Meta knows which page's
    # phone number to optimize for. Pull from creative.page_id if not set.
    opt_goal_norm = str(ads.get("optimization_goal", "")).upper()
    if opt_goal_norm in CALL_OPTIMIZATION_GOALS and not ads.get("promoted_object"):
        page_id_for_call = (creative or {}).get("page_id")
        if page_id_for_call:
            ads["promoted_object"] = {"page_id": str(page_id_for_call)}
            fixes.append(f"Auto-set promoted_object.page_id={page_id_for_call} for {opt_goal_norm}")

    # Lead-form id placement. The Marketing API does NOT accept any form-id key
    # inside promoted_object on the ad set — Meta's adsets endpoint rejects with
    # code=100 ("Invalid keys ... in promoted_object"). The form id belongs on
    # the AD CREATIVE, specifically at:
    #   creative.object_story_spec.link_data.call_to_action.value.lead_gen_form_id
    # If a brief / agent put any *_form_id variant in promoted_object, lift it
    # over to the creative and strip from promoted_object so Meta accepts the
    # ad set. Aliases supported: lead_form_id, leadgen_form_id, leadform_id,
    # lead_form, form_id, leadgen_form.
    _LEAD_FORM_ID_ALIASES = (
        "lead_form_id", "leadgen_form_id", "leadform_id",
        "lead_form", "form_id", "leadgen_form",
    )
    if isinstance(ads.get("promoted_object"), dict):
        po = dict(ads["promoted_object"])
        lifted_form_id = None
        for alias in _LEAD_FORM_ID_ALIASES:
            if alias in po:
                lifted_form_id = str(po.pop(alias)).strip()
                if lifted_form_id:
                    break
        if lifted_form_id:
            cr = creative if isinstance(creative, dict) else {}
            if not cr.get("lead_gen_form_id") and not cr.get("leadgen_form_id"):
                cr["lead_gen_form_id"] = lifted_form_id
                fixes.append(
                    f"Moved adset.promoted_object lead-form id -> creative.lead_gen_form_id={lifted_form_id} "
                    "(Meta accepts lead-form ids on the creative, not the ad set)."
                )
                if isinstance(creative, dict):
                    creative.update(cr)
                else:
                    doc["creative"] = cr  # type: ignore[index]
        # also normalize app_id alias
        if "app" in po and "application_id" not in po:
            po["application_id"] = po.pop("app")
        ads["promoted_object"] = po

    # LEAD_GENERATION optimization requires promoted_object.page_id only.
    # Pull from creative if missing; surface a clear note if no lead form id
    # is anywhere yet.
    if opt_goal_norm == "LEAD_GENERATION":
        po = ads.get("promoted_object") if isinstance(ads.get("promoted_object"), dict) else {}
        po = dict(po)
        if not po.get("page_id"):
            page_id_for_lead = (creative or {}).get("page_id")
            if page_id_for_lead:
                po["page_id"] = str(page_id_for_lead)
                fixes.append(f"Auto-set promoted_object.page_id={page_id_for_lead} for LEAD_GENERATION")
        cr = creative if isinstance(creative, dict) else {}
        if not (cr.get("lead_gen_form_id") or cr.get("leadgen_form_id")):
            fixes.append(
                "WARNING: LEAD_GENERATION needs creative.lead_gen_form_id. "
                "Create a Lead Form via facebook_create_lead_form, then put its id "
                "into creative.lead_gen_form_id (NOT in adset.promoted_object)."
            )
        ads["promoted_object"] = po

    if not ads.get("name"):
        raise ValueError("document.adset.name is required")

    cbo = bool(camp.get("is_adset_budget_sharing_enabled")) or camp.get("daily_budget") or camp.get("lifetime_budget")
    if cbo and (ads.get("daily_budget") or ads.get("lifetime_budget")):
        ads.pop("daily_budget", None)
        ads.pop("lifetime_budget", None)
        fixes.append("Removed ad set budget fields (campaign uses CBO / campaign-level budget)")
    if not cbo and not ads.get("daily_budget") and not ads.get("lifetime_budget"):
        ads["daily_budget"] = int(out.get("options", {}).get("default_daily_budget_cents") or DEFAULT_DAILY_BUDGET_CENTS)
        fixes.append(f"Applied default ad set daily_budget={ads['daily_budget']} cents (document had no budget)")

    # Fix 1 — COST_CAP / bid-cap strategies require bid_amount on the AD SET,
    # not on the campaign. If the brief put bid_amount on the campaign (or
    # only on one side), copy/lift it to the adset so create_adset doesn't
    # fail with subcode 1815857 ("bid_amount required").
    _BID_CAP_STRATEGIES = {"COST_CAP", "LOWEST_COST_WITH_BID_CAP", "TARGET_COST"}
    _bs = str(camp.get("bid_strategy") or ads.get("bid_strategy") or "").strip().upper()
    if _bs in _BID_CAP_STRATEGIES and ads.get("bid_amount") in (None, "", 0):
        camp_bid = camp.get("bid_amount")
        if camp_bid not in (None, "", 0):
            try:
                ads["bid_amount"] = int(camp_bid)
                fixes.append(
                    f"Copied campaign.bid_amount={camp_bid} -> adset.bid_amount "
                    f"(required for bid_strategy={_bs} on the ad set)"
                )
            except (TypeError, ValueError):
                pass
        else:
            # No bid_amount anywhere, but a bid-cap strategy is set. Meta rejects
            # this with subcode 1815857 ("Bid amount required for bid strategy").
            # The safe, common-case fix is to drop to LOWEST_COST_WITHOUT_CAP
            # (automatic bidding — needs no bid_amount). This also resolves the
            # political-category case where Meta swaps the implicit strategy to a
            # bid-cap variant and then demands bid_amount. The advertiser can set
            # an explicit bid_amount later if they want a cap.
            for _obj in (camp, ads):
                if str(_obj.get("bid_strategy") or "").strip().upper() in _BID_CAP_STRATEGIES:
                    _obj["bid_strategy"] = "LOWEST_COST_WITHOUT_CAP"
            fixes.append(
                f"bid_strategy={_bs} requires a bid_amount which was not supplied "
                "(would fail with subcode 1815857). Switched to LOWEST_COST_WITHOUT_CAP "
                "(automatic bidding, no bid cap). Set adset.bid_amount explicitly to keep a cap."
            )
            _bs = "LOWEST_COST_WITHOUT_CAP"

    # Fix 2 — MESSAGE_PAGE CTA needs Messenger destination wiring.
    # Click-to-Messenger ads require destination_type=MESSENGER and
    # 'messenger' in publisher_platforms; otherwise create_ad fails.
    # Prefer nested call_to_action.type (the brief's source of truth), then
    # fall back to the flat cta_type (which the doc-normalizer may have set
    # to DEFAULT_CTA when the brief used the nested shape only).
    _cta_norm = ""
    _cta_nested = (creative or {}).get("call_to_action")
    if isinstance(_cta_nested, dict):
        _cta_norm = str(_cta_nested.get("type") or "").strip().upper()
    if not _cta_norm:
        _cta_norm = str((creative or {}).get("cta_type") or "").strip().upper()
    if _cta_norm == "MESSAGE_PAGE":
        # Lift it onto cta_type so downstream creative builders see it too.
        if str((creative or {}).get("cta_type") or "").strip().upper() != "MESSAGE_PAGE":
            creative["cta_type"] = "MESSAGE_PAGE"
            fixes.append("Lifted creative.call_to_action.type=MESSAGE_PAGE -> creative.cta_type")
        _cur_dt = str(ads.get("destination_type") or "").strip().upper()
        if _cur_dt != "MESSENGER":
            ads["destination_type"] = "MESSENGER"
            fixes.append(
                f"Set adset.destination_type=MESSENGER for MESSAGE_PAGE CTA"
                f"{f' (was {_cur_dt})' if _cur_dt else ''}"
            )
        plc = out.get("placements") if isinstance(out.get("placements"), dict) else None
        tgt = out.get("targeting") if isinstance(out.get("targeting"), dict) else None
        for _holder in (plc, tgt):
            if not isinstance(_holder, dict):
                continue
            pp = _holder.get("publisher_platforms")
            if isinstance(pp, list):
                pp_norm = [str(p).strip().lower() for p in pp if str(p).strip()]
                if "messenger" not in pp_norm:
                    pp_norm.append("messenger")
                    _holder["publisher_platforms"] = pp_norm
                    fixes.append("Added 'messenger' to publisher_platforms for MESSAGE_PAGE CTA")
            if not _holder.get("messenger_positions"):
                _holder["messenger_positions"] = ["messenger_home"]
                fixes.append("Defaulted messenger_positions=['messenger_home'] for MESSAGE_PAGE CTA")

    # Fix 3 — Audience-Network-only placements fail with subcode 2446384
    # ("Audience Network needs Feed"). AN inventory cannot render without a
    # Feed-eligible parent placement. If publisher_platforms is exactly
    # ['audience_network'], prepend 'facebook' + ensure facebook_positions
    # includes 'feed'.
    for _holder in (
        out.get("placements") if isinstance(out.get("placements"), dict) else None,
        out.get("targeting") if isinstance(out.get("targeting"), dict) else None,
    ):
        if not isinstance(_holder, dict):
            continue
        pp = _holder.get("publisher_platforms")
        if not isinstance(pp, list):
            continue
        pp_norm = [str(p).strip().lower() for p in pp if str(p).strip()]
        if pp_norm == ["audience_network"]:
            _holder["publisher_platforms"] = ["facebook", "audience_network"]
            fb_pos = _holder.get("facebook_positions")
            if not isinstance(fb_pos, list) or "feed" not in [str(x).strip().lower() for x in fb_pos]:
                _holder["facebook_positions"] = ["feed"]
            fixes.append(
                "Added 'facebook'+feed to AN-only placements (Audience Network requires a Feed parent)"
            )

    # Universal image resolution. The agent often passes creative.image_url (or
    # image_base64) without uploading it first — and then the creative ships
    # without an image_hash, so Meta renders a stock fallback image. Upload it
    # ourselves here so the creative builder downstream gets a real hash.
    cr_for_img = out.get("creative") if isinstance(out.get("creative"), dict) else None
    if isinstance(cr_for_img, dict):
        img_hash = str(cr_for_img.get("image_hash") or "").strip()
        img_url = str(cr_for_img.get("image_url") or "").strip()
        img_b64 = cr_for_img.get("image_base64") or cr_for_img.get("image_b64")
        creative_mode = str(cr_for_img.get("mode") or "").upper()
        if not img_hash and (img_url or img_b64) and account_id and token:
            try:
                up = services.upload_image(
                    account_id, token,
                    url_or_bytes=img_url or None,
                    image_base64=img_b64 if not img_url else None,
                    filename=cr_for_img.get("image_filename") or cr_for_img.get("filename"),
                )
            except Exception as exc:
                up = {"error": {"message": f"image upload threw: {exc}"}}
            if isinstance(up, dict) and not up.get("error"):
                new_hash = str(up.get("hash") or up.get("image_hash") or "").strip()
                if new_hash:
                    cr_for_img["image_hash"] = new_hash
                    img_hash = new_hash
                    fixes.append(
                        f"Auto-uploaded creative.{'image_url' if img_url else 'image_base64'} "
                        f"-> image_hash={new_hash} (MCP fetched bytes and uploaded to /act_X/adimages)"
                    )
            else:
                err_msg = (up.get("error") or {}).get("message") if isinstance(up, dict) else str(up)
                fixes.append(f"Auto-upload of creative image failed: {err_msg}")

        # Hard-fail if the brief intended an image but we still have no hash.
        # Without this, Meta would silently render its stock fallback — exactly
        # the surprise users have hit. Existing-post ads legitimately have no
        # image_hash and are exempted.
        if (not img_hash and (img_url or img_b64)
                and creative_mode != "EXISTING_POST"):
            raise ValueError(
                "creative.image_hash could not be resolved. The MCP attempted to "
                "upload creative.image_url / image_base64 but the upload failed "
                "(see auto_fixes). Refusing to ship the ad — Meta would otherwise "
                "render a stock fallback image instead of the user's creative."
            )

    return out, fixes


def pause_other_adsets_in_campaign(account_id: str, token: str, campaign_id: str, keep_adset_id: str, fixes: List[str]) -> None:
    data = services.get_ad_sets(
        account_id,
        token,
        limit=200,
        filtering=json.dumps([{"field": "campaign_id", "operator": "EQUAL", "value": str(campaign_id)}]),
    )
    if _has_api_error(data):
        fixes.append(f"Could not list ad sets to pause siblings: {_api_error_message(data)}")
        return
    for row in data.get("data") or []:
        aid = str(row.get("id") or "")
        if not aid or aid == keep_adset_id:
            continue
        if str(row.get("status") or "").upper() == "ACTIVE":
            r = services.pause_adset(aid, token)
            if _has_api_error(r):
                fixes.append(f"Failed to pause sibling ad set {aid}: {_api_error_message(r)}")
            else:
                fixes.append(f"Paused sibling ad set {aid} (safe deploy)")


_RERESOLVE_INTEREST_AXES = ("interests",)
_MAX_RERESOLVE_ATTEMPTS = 2


# All Meta targeting axes a flexible_spec slice can carry. Used by the
# "keep slice if it has anything left" check in the three filter passes
# below — earlier versions only checked interests/behaviors/demographics,
# which silently dropped slices that had only work_employers, income,
# industries, life_events, etc. after their interests got pruned.
_TARGETING_AXES_FOR_KEEP = (
    "interests", "behaviors", "demographics",
    "work_employers", "work_positions",
    "education_majors", "education_schools", "education_statuses",
    "industries", "income", "family_statuses", "life_events",
    "relationship_statuses", "user_device", "user_os",
    "connections", "friends_of_connections",
)


def _slice_has_any_targeting(s: Dict[str, Any]) -> bool:
    """True if a flexible_spec slice still carries any non-empty targeting axis."""
    if not isinstance(s, dict):
        return False
    return any(s.get(k) for k in _TARGETING_AXES_FOR_KEEP)


# Goal → default Meta custom_event_type used by the pixel auto-resolver. The
# table is small enough to hardcode; admin override comes from the brief
# (``tracking.custom_event_type``) which always wins.
_DEFAULT_CET_BY_GOAL: Dict[str, str] = {
    "LANDING_PAGE_VIEWS": "CONTENT_VIEW",
    "OFFSITE_CONVERSIONS": "PURCHASE",
    "VALUE": "PURCHASE",
    "LEAD_GENERATION": "LEAD",
    "CONVERSATIONS": "CONTACT",
}


# Goal → ad-level tracking_specs ``action.type`` list. Built into the ad's
# tracking_specs so funnel reports fire the right event regardless of the
# ad set's optimization goal (some users want to optimize for clicks but
# attribute conversions on landing-page views, etc.).
_TRACKING_SPEC_ACTION_TYPE_BY_GOAL: Dict[str, List[str]] = {
    "LANDING_PAGE_VIEWS": ["landing_page_view"],
    "LINK_CLICKS": ["link_click"],
    "OFFSITE_CONVERSIONS": ["offsite_conversion"],
    "VALUE": ["offsite_conversion"],
    "LEAD_GENERATION": ["offsite_conversion"],
    "CONVERSATIONS": ["onsite_conversion.messaging_first_reply"],
}


def _resolve_video_data_from_post(oid: str, page_id: str, page_tok: str, user_tok: str) -> Optional[Dict[str, Any]]:
    """For an EXISTING_POST that is a VIDEO/Reel whose object_story_id cannot be
    promoted directly (Meta subcode 1487472 "Page post can't be used"), build a
    ``video_data`` object_story_spec instead — a fresh page-backed video creative
    from the post's underlying ``video_id`` + thumbnail.

    This mirrors what the Meta "Boost reel" UI does internally: it does not
    promote the post id, it builds a new video creative from the same video.
    VERIFIED working (validate_only) where the object_story_id path failed.

    Returns the ``video_data`` dict (without page_id — caller adds it) or None
    if the post has no resolvable video_id. Best-effort; never raises.

    NOTE: the resulting creative does NOT carry the original post's social proof
    (likes/comments) — it is a new creative showing the same video. This is the
    same trade-off the UI makes for non-promotable video posts.
    """
    from facebook_ads_mcp import services as _svc
    tok = page_tok or user_tok
    try:
        post = _svc._get(
            f"{_svc.BASE_URL}/{oid}",
            {"access_token": tok,
             "fields": "message,attachments{media_type,media{image{src}},target{id}}"},
        )
        if not isinstance(post, dict) or post.get("error"):
            return None
        att = (post.get("attachments") or {}).get("data") or []
        if not att:
            return None
        a0 = att[0] or {}
        if str(a0.get("media_type") or "").lower() != "video":
            return None
        video_id = ((a0.get("target") or {}).get("id") or "").strip()
        if not video_id:
            return None
        thumb = (((a0.get("media") or {}).get("image") or {}).get("src") or "").strip()
        if not thumb:
            # Meta requires a thumbnail for video_data; fetch one from the video.
            v = _svc._get(f"{_svc.BASE_URL}/{video_id}", {"access_token": tok, "fields": "picture"})
            thumb = (v.get("picture") or "").strip() if isinstance(v, dict) else ""
        if not thumb:
            return None
        vd: Dict[str, Any] = {"video_id": video_id, "image_url": thumb}
        msg = str(post.get("message") or "").strip()
        if msg:
            vd["message"] = msg
        return vd
    except Exception:  # noqa: BLE001 — best-effort fallback
        return None


def _create_ig_direct_creative(account_id, token, *, name, ig_media_id, page_id,
                               instagram_user_id, link, authorization_category, bylines):
    """Build an ad creative DIRECTLY from an Instagram media, with the Facebook
    Page attached for identity/disclaimer — the documented "Use Posts as
    Instagram Ads" path. Works for POLITICAL ads (the Page carries the
    "Paid for by" disclaimer) WITHOUT needing the IG post to exist as a FB
    Page cross-post.

    VERIFIED (validate_only, 2026-05-30) that this shape is accepted for a
    political ad where object_story_id cross-posts were ineligible:
        source_instagram_media_id + object_id(page) + instagram_user_id + link
    (all TOP-LEVEL; putting link inside object_story_spec fails with 2061015).

    Returns the services.create_creative-style dict (has 'id' on success, or
    {'error': ...}). page_id, ig_media_id, instagram_user_id are required.
    """
    from facebook_ads_mcp import services as _svc
    data = {
        "access_token": token,
        "name": name,
        "source_instagram_media_id": str(ig_media_id).strip(),
        "object_id": str(page_id).strip(),
        "instagram_user_id": str(instagram_user_id).strip(),
        # A link is required (2061015 otherwise). Default to the Page if the
        # brief gave none — the ad still promotes the IG media.
        "link": str(link or f"https://www.facebook.com/{str(page_id).strip()}").strip(),
    }
    if authorization_category:
        data["authorization_category"] = str(authorization_category).strip()
    if bylines:
        data["bylines"] = str(bylines).strip()
    # account_id may arrive as "123" or "act_123"; the edge requires act_ prefix.
    acct = _svc._account_id(account_id)
    return _svc._post(f"{_svc.BASE_URL}/{acct}/adcreatives", data)


def _build_ad_tracking_specs(pixel_id: str, optimization_goal: str) -> Optional[List[Dict[str, Any]]]:
    """Return Meta-shaped ``tracking_specs`` for ad creation, or None to skip.

    Format: ``[{"action.type": [<event>], "fb_pixel": [<pixel_id>]}]`` —
    one entry per pixel/event the ad should attribute. Empty pixel or
    unmapped goal → None (caller skips the field, ad set's promoted_object
    pixel still drives optimization).
    """
    pid = (pixel_id or "").strip()
    if not pid:
        return None
    goal = (optimization_goal or "").strip().upper()
    actions = _TRACKING_SPEC_ACTION_TYPE_BY_GOAL.get(goal)
    if not actions:
        return None
    return [{"action.type": actions, "fb_pixel": [pid]}]


def _autoresolve_page(
    doc: Dict[str, Any],
    token: str,
    auto_fixes: List[str],
) -> None:
    """Resolve creative.page_id from a user-supplied page name, or pick the
    first available Page when neither name nor id is set.

    Strategy (universal — covers any user input shape):
    1. If creative.page_id or adset.page_id is set → no-op (user provided id).
    2. If creative.page_name (or adset.page_name, or doc.options.page_name) is
       set → fetch get_pages, match by case-insensitive substring. If no match,
       raise ValueError so the sync flow surfaces a clear error instead of
       silently picking a wrong page. If multiple match, prefer exact (case-
       insensitive) match; otherwise raise ValueError listing candidates.
    3. If no name is given but exactly ONE page is accessible → use it and
       log auto_fix.
    4. If no name is given and MULTIPLE pages are accessible → raise
       ValueError listing pages, since silently picking the first is the
       exact failure mode this fix targets.
    """
    creative = doc.get("creative") if isinstance(doc.get("creative"), dict) else {}
    adset = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    opts = doc.get("options") if isinstance(doc.get("options"), dict) else {}
    if (creative.get("page_id") or "").strip():
        return
    if (adset.get("page_id") or "").strip():
        return

    requested_name = ""
    for src in (creative.get("page_name"), adset.get("page_name"), opts.get("page_name")):
        if isinstance(src, str) and src.strip():
            requested_name = src.strip()
            break

    try:
        pages = services.get_pages(token)
    except Exception as exc:
        auto_fixes.append(f"Auto-resolve page: get_pages failed ({exc}); brief must specify page_id.")
        return
    rows = pages.get("data") if isinstance(pages, dict) else None
    rows = [r for r in (rows or []) if isinstance(r, dict) and (r.get("id") or "")]
    if not rows:
        auto_fixes.append("Auto-resolve page: no Pages accessible to this user; brief must specify page_id.")
        return

    if requested_name:
        req_l = requested_name.lower()
        exact = [r for r in rows if str(r.get("name") or "").strip().lower() == req_l]
        partial = [r for r in rows if req_l in str(r.get("name") or "").lower()]
        chosen = None
        if len(exact) == 1:
            chosen = exact[0]
        elif len(exact) > 1:
            names = [f"{r.get('name')!r} (id={r.get('id')})" for r in exact]
            raise ValueError(
                f"Page name {requested_name!r} matches multiple accessible Pages exactly: "
                f"{', '.join(names)}. Specify page_id directly to disambiguate."
            )
        elif len(partial) == 1:
            chosen = partial[0]
            auto_fixes.append(
                f"Resolved page_name {requested_name!r} -> {chosen.get('name')!r} ({chosen.get('id')}) via case-insensitive substring match."
            )
        elif len(partial) > 1:
            names = [f"{r.get('name')!r} (id={r.get('id')})" for r in partial]
            raise ValueError(
                f"Page name {requested_name!r} matches multiple Pages: {', '.join(names)}. "
                "Specify page_id directly or use the exact Page name."
            )
        else:
            available = [f"{r.get('name')!r} (id={r.get('id')})" for r in rows]
            raise ValueError(
                f"Page name {requested_name!r} is not accessible to this user. "
                f"Accessible Pages: {', '.join(available)}. "
                "Either link the requested Page to the user / Business in Meta Business Manager, "
                "or set creative.page_id (or creative.page_name) to one of the listed Pages."
            )
        pid = str(chosen.get("id") or "").strip()
        if pid:
            creative["page_id"] = pid
            doc["creative"] = creative
        return

    # No requested name. Only proceed silently if exactly ONE page exists —
    # otherwise raise so the user must pick explicitly (no more silent first-page).
    if len(rows) == 1:
        first = rows[0]
        pid = str(first.get("id") or "").strip()
        if not pid:
            return
        creative.setdefault("page_id", pid)
        doc["creative"] = creative
        auto_fixes.append(
            f"Auto-picked the only accessible page \"{first.get('name') or pid}\" ({pid}) — brief did not specify page_id."
        )
        return

    available = [f"{r.get('name')!r} (id={r.get('id')})" for r in rows]
    raise ValueError(
        "Brief did not specify creative.page_id or creative.page_name, and multiple Pages are accessible: "
        f"{', '.join(available)}. Specify page_name or page_id explicitly to avoid the wrong Page being used."
    )


_TRACKING_GOALS_FOR_AUTORESOLVE = {
    "LANDING_PAGE_VIEWS", "OFFSITE_CONVERSIONS", "VALUE",
    "LEAD_GENERATION", "CONVERSATIONS",
}

# Call-optimization goals don't use website pixel attribution. Without an
# explicit note in auto_fixes the user sees an empty Tracking section and
# assumes a bug; this set drives the transparency message in
# ``_autoresolve_pixel``.
_CALL_OPTIMIZATION_GOALS_FOR_NOTE = {"QUALITY_CALL", "CALL_VOLUME"}


def _parse_pixel_last_fired(ts: Any) -> float:
    """Parse Meta's ``last_fired_time`` ISO-8601 → epoch seconds. Missing = 0."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        # Meta returns e.g. "2026-05-02T09:14:00+0000"; tolerate "Z" too.
        s = str(ts).replace("Z", "+0000")
        # +HHMM doesn't roundtrip via fromisoformat on some Pythons; normalise.
        if len(s) >= 5 and s[-5] in "+-" and s[-3] != ":":
            s = s[:-2] + ":" + s[-2:]
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def _autoresolve_pixel(
    doc: Dict[str, Any],
    account_id: str,
    token: str,
    auto_fixes: List[str],
) -> None:
    """Pick the most-recently-fired pixel when the goal implies tracking and brief omitted it."""
    adset = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    goal = str(adset.get("optimization_goal") or "").strip().upper()
    if goal not in _TRACKING_GOALS_FOR_AUTORESOLVE:
        # Surface what tracking IS wired for goals that don't use a website
        # pixel (QUALITY_CALL uses promoted_object.page_id; REACH/IMPRESSIONS
        # don't need attribution at all). Without this note the user sees an
        # empty Tracking section and assumes something is broken.
        if goal in _CALL_OPTIMIZATION_GOALS_FOR_NOTE:
            auto_fixes.append(
                f"{goal} goal: website pixel not applicable — Meta uses "
                "promoted_object.page_id for call attribution. The brief's "
                "creative.call_to_action.value.phone_number is injected as "
                "link_data.call_to_action.value.link='tel:...' so the CTA "
                "dials the right number."
            )
        return
    tracking = doc.get("tracking") if isinstance(doc.get("tracking"), dict) else {}
    if (tracking.get("pixel_id") or "").strip():
        return
    try:
        pixels = services.get_pixels(account_id, token)
    except Exception as exc:
        auto_fixes.append(f"Auto-resolve pixel: get_pixels failed ({exc}); ad set will ship without pixel attribution.")
        return
    rows = pixels.get("data") if isinstance(pixels, dict) else None
    if not rows:
        auto_fixes.append(
            "Auto-resolve pixel: no Meta Pixels found on this account; "
            "ad set created without conversion attribution. Create one in Events Manager and re-sync."
        )
        return
    ranked = sorted(
        (r for r in rows if isinstance(r, dict)),
        key=lambda r: _parse_pixel_last_fired(r.get("last_fired_time")),
        reverse=True,
    )
    if not ranked:
        return
    chosen = ranked[0]
    chosen_id = str(chosen.get("id") or "").strip()
    if not chosen_id:
        return
    cet = _DEFAULT_CET_BY_GOAL.get(goal, "CONTENT_VIEW")
    tracking["pixel_id"] = chosen_id
    tracking.setdefault("custom_event_type", cet)
    doc["tracking"] = tracking
    note = (
        f"Auto-picked pixel \"{chosen.get('name') or chosen_id}\" ({chosen_id}) "
        f"+ custom_event_type={cet} for goal {goal}"
    )
    if chosen.get("last_fired_time"):
        note += f" — most recently fired {chosen.get('last_fired_time')}"
    others = [str(r.get("id") or "") for r in ranked[1:4] if str(r.get("id") or "")]
    if others:
        note += f" (other pixels available: {', '.join(others)})"
    auto_fixes.append(note)


def _autoresolve_interests(
    doc: Dict[str, Any],
    account_id: str,
    token: str,
    auto_fixes: List[str],
) -> None:
    """Replace name-only or fake-ID interest entries with fresh IDs from batch_interest_search.

    Run BEFORE preflight so the LLM never has to think about interest IDs;
    auto-pruning the known-bad cache catches IDs Meta has previously
    deprecated even if search still surfaces them.
    """
    targeting = doc.get("targeting") if isinstance(doc.get("targeting"), dict) else {}
    flex = targeting.get("flexible_spec") if isinstance(targeting.get("flexible_spec"), list) else None
    if not flex:
        return
    bad_ids = _learned_bad_interest_ids()
    rescued: List[str] = []
    for slice_ in flex:
        if not isinstance(slice_, dict):
            continue
        items = slice_.get("interests")
        if not isinstance(items, list) or not items:
            continue
        # Names that need resolving: items missing id, or with id that's
        # missing/non-numeric/in known-bad cache (catches LLM hallucinations
        # before the create call).
        to_resolve: List[str] = []
        keep_unchanged: List[Dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            iid = str(entry.get("id") or "").strip()
            nm = str(entry.get("name") or entry.get("query") or "").strip()
            if iid and iid.isdigit() and iid not in bad_ids:
                keep_unchanged.append(entry)  # already valid-looking
                continue
            if nm:
                to_resolve.append(nm)
        if not to_resolve:
            continue
        try:
            search = services.batch_interest_search(account_id, token, to_resolve)
        except Exception as exc:
            auto_fixes.append(f"Auto-resolve interests: batch_interest_search failed ({exc}); leaving slice as-is for the retry loop to handle.")
            continue
        if not isinstance(search, dict):
            continue
        new_items = list(keep_unchanged)
        for found in search.get("found") or []:
            if not isinstance(found, dict):
                continue
            fid = str(found.get("id") or "").strip()
            if not fid or not fid.isdigit() or fid in bad_ids:
                continue
            fname = str(found.get("name") or "").strip()
            fquery = str(found.get("query") or "").strip()
            # Strict same-name guard: refuse fuzzy near-match canonicals.
            if fquery and fname and not _names_match_for_swap(fquery, fname):
                auto_fixes.append(
                    f"Auto-resolve interests: '{fquery}' search returned different "
                    f"concept '{fname}' (id {fid}); dropped to avoid brief corruption."
                )
                continue
            # Strip Meta's parenthetical taxonomy qualifier from the outgoing
            # name (e.g. "Digital marketing (marketing)" → "Digital marketing").
            # Meta keys interests by ID server-side — the name is just a label
            # for Ads Manager. Cleaner names also help the next round of
            # `_names_match_for_swap` if the user re-runs with the previous doc.
            display_name = re.sub(r"\s*\([^)]*\)\s*$", "", fname or fquery).strip() or (fname or fquery)
            new_items.append({"id": fid, "name": display_name})
            rescued.append(f"{fquery or fname} → {fid}")
        for missing in search.get("missing") or []:
            auto_fixes.append(f"Auto-resolve interests: no Meta match for \"{missing}\" — dropped.")
        if new_items:
            slice_["interests"] = new_items
        else:
            slice_.pop("interests", None)
    if rescued:
        auto_fixes.append("Resolved interests via batch_interest_search: " + ", ".join(rescued[:8]) +
                          ("..." if len(rescued) > 8 else ""))


def _autoresolve_behaviors_and_demographics(
    doc: Dict[str, Any],
    account_id: str,
    token: str,
    auto_fixes: List[str],
) -> None:
    """Resolve flexible_spec behaviors + demographics via adTargetingCategory search.

    Sister to ``_autoresolve_interests`` but covers the two axes the LLM
    cannot reliably emit IDs for (Meta's ``/search?type=adinterest``
    doesn't return behavior/demographic taxonomy items). Walks each slice's
    ``behaviors`` and ``demographics`` lists, collects names that need
    resolving (no id, fake-pattern id, or id in the learned-bad cache),
    batch-searches via ``services.batch_targeting_category_search``, and
    routes each match into the ``behaviors`` axis (Meta's API has no
    ``demographics`` axis — the rename pass at production_flow.py:1370
    folds any leftover demographics into behaviors anyway, but writing
    directly to behaviors here keeps the data shape consistent through
    preflight).

    Drops names Meta doesn't have with a per-name auto_fixes line so the
    user sees exactly which targeting items the brief lost.
    """
    targeting = doc.get("targeting") if isinstance(doc.get("targeting"), dict) else {}
    flex = targeting.get("flexible_spec") if isinstance(targeting.get("flexible_spec"), list) else None
    if not flex:
        return
    bad_ids = _learned_bad_interest_ids()
    rescued_by_axis: Dict[str, List[str]] = {"behaviors": [], "demographics": []}

    for slice_ in flex:
        if not isinstance(slice_, dict):
            continue

        # Combine names to resolve from both axes in a single batch call —
        # Meta returns the path on each row so we can route them back to
        # the right axis afterwards. Track which names came from which
        # input axis only for messaging purposes.
        to_resolve: List[str] = []
        keep_unchanged: Dict[str, List[Dict[str, Any]]] = {"behaviors": [], "demographics": []}
        sources: Dict[str, str] = {}  # name → "behaviors" or "demographics" (input axis)

        for axis_in in ("behaviors", "demographics"):
            items = slice_.get(axis_in)
            if not isinstance(items, list) or not items:
                continue
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                iid = str(entry.get("id") or "").strip()
                nm = str(entry.get("name") or entry.get("query") or "").strip()
                if iid and iid.isdigit() and (11 <= len(iid) <= 15) and iid not in bad_ids:
                    keep_unchanged[axis_in].append(entry)
                    continue
                if nm:
                    to_resolve.append(nm)
                    sources.setdefault(nm, axis_in)

        if not to_resolve and not (keep_unchanged["behaviors"] or keep_unchanged["demographics"]):
            continue

        # Behaviors that already passed validation stay where they are.
        # Demographics that passed validation get folded into behaviors
        # immediately so the rest of the orchestrator sees a single axis.
        merged_behaviors: List[Dict[str, Any]] = list(keep_unchanged["behaviors"]) + list(keep_unchanged["demographics"])

        if to_resolve:
            try:
                search = services.batch_targeting_category_search(account_id, token, to_resolve)
            except Exception as exc:
                auto_fixes.append(
                    f"Auto-resolve behaviors/demographics: batch_targeting_category_search failed ({exc}); "
                    "leaving slice as-is for the retry loop to handle."
                )
                search = {"found": [], "missing": to_resolve}
            if not isinstance(search, dict):
                search = {"found": [], "missing": to_resolve}

            for found in search.get("found") or []:
                if not isinstance(found, dict):
                    continue
                fid = str(found.get("id") or "").strip()
                if not fid or not fid.isdigit() or fid in bad_ids:
                    continue
                fname = str(found.get("name") or "").strip()
                fquery = str(found.get("query") or "").strip()
                # Strict same-name guard: refuse fuzzy near-match canonicals
                # so a brief's "Frequent Buyers" never gets corrupted into
                # an unrelated behavior with a similar word.
                if fquery and fname and not _names_match_for_swap(fquery, fname):
                    src_axis = sources.get(fquery, "behaviors")
                    singular = "demographic" if src_axis == "demographics" else "behavior"
                    auto_fixes.append(
                        f"Auto-resolve {singular}: '{fquery}' search returned different "
                        f"concept '{fname}' (id {fid}); dropped to avoid brief corruption."
                    )
                    continue
                path = found.get("path") if isinstance(found.get("path"), list) else []
                first_seg = str(path[0]).strip().lower() if path else ""
                merged_behaviors.append({"id": fid, "name": fname or fquery})
                # Report under the input axis the brief originally used.
                src_axis = sources.get(fquery, first_seg or "behaviors")
                key = src_axis if src_axis in ("behaviors", "demographics") else "behaviors"
                rescued_by_axis[key].append(f"{fquery or fname} → {fid}")
            for missing in search.get("missing") or []:
                src_axis = sources.get(missing, "behaviors")
                singular = "demographic" if src_axis == "demographics" else "behavior"
                auto_fixes.append(
                    f"Auto-resolve {singular} '{missing}': no Meta adTargetingCategory match — dropped."
                )

        # Write the merged behaviors back; remove demographics entirely so
        # downstream code (and Meta) sees a clean shape.
        if merged_behaviors:
            slice_["behaviors"] = merged_behaviors
        else:
            slice_.pop("behaviors", None)
        slice_.pop("demographics", None)

    summary_parts: List[str] = []
    if rescued_by_axis["behaviors"]:
        summary_parts.append("behaviors: " + ", ".join(rescued_by_axis["behaviors"][:8]) +
                             ("..." if len(rescued_by_axis["behaviors"]) > 8 else ""))
    if rescued_by_axis["demographics"]:
        summary_parts.append("demographics: " + ", ".join(rescued_by_axis["demographics"][:8]) +
                             ("..." if len(rescued_by_axis["demographics"]) > 8 else ""))
    if summary_parts:
        auto_fixes.append("Resolved behaviors/demographics via batch_targeting_category_search: " +
                          " | ".join(summary_parts))


def _autoresolve_cities(
    doc: Dict[str, Any],
    account_id: str,
    token: str,
    auto_fixes: List[str],
) -> None:
    """Resolve geo_cities entries via targeting_search; reject fabricated keys.

    Validates BOTH input shapes the LLM uses:
    - ``targeting.geo_cities`` (brief-shorthand, processed by build_targeting_spec).
    - ``targeting.geo_locations.cities`` (Meta API shape; what build_targeting_from_document
      eventually produces). Production runs show the LLM emitting directly to
      ``geo_locations.cities`` to skip the build step, bypassing the validator
      that only looked at ``geo_cities``.

    Three input shapes per entry:

    1. String form: ``"Mumbai"`` — search by name, accept top-rated city match.
    2. Dict with ``name``: ``{"name": "Mumbai", ...}`` — search by name; if the
       entry already has a ``key`` AND that key matches Meta's canonical key
       for the searched name, accept; otherwise replace key with the
       canonical (the LLM emitted a fake key — production case: keys 1040000,
       1026692 resolve to "Purasavakkam", "Harinpur" not "Hyderabad", "Chennai").
    3. Dict with ``key`` only (no name): drop — there's nothing to validate
       against and the key likely resolves to the wrong city.
    """
    targeting = doc.get("targeting") if isinstance(doc.get("targeting"), dict) else {}

    # Find which shape the LLM used. Validate both; only one should typically
    # be present per run.
    cities_shorthand = targeting.get("geo_cities")
    geo_locations = targeting.get("geo_locations") if isinstance(targeting.get("geo_locations"), dict) else None
    cities_meta_shape = geo_locations.get("cities") if geo_locations else None

    has_shorthand = isinstance(cities_shorthand, list) and cities_shorthand
    has_meta_shape = isinstance(cities_meta_shape, list) and cities_meta_shape
    if not (has_shorthand or has_meta_shape):
        return

    # Validate one source-of-truth list. If both are set (rare), shorthand wins
    # and we'll write into both for downstream safety.
    cities = cities_shorthand if has_shorthand else cities_meta_shape

    new_cities: List[Dict[str, Any]] = []
    rescued: List[str] = []
    swapped: List[str] = []
    dropped_no_name: List[str] = []
    # Auto-route: items that don't match "city" but DO match a sibling Meta
    # location type (neighborhood, region, zip, subcity, country) — collect
    # here and merge into geo_locations.<key> after the loop. This avoids
    # silent drops for users who put "Connaught Place" (a neighborhood) in
    # the cities array.
    rerouted_by_type: Dict[str, List[Dict[str, Any]]] = {}
    rerouted_messages: List[str] = []

    # Step 1: parse every entry into (entry_key, entry_name) up front so the
    # network step has only the inputs that need a Meta call. This also lets
    # us hoist the "key without name" rejection out of the network loop.
    parsed: List[Tuple[Any, str, str]] = []  # (original_entry, entry_key, entry_name)
    for entry in cities:
        if isinstance(entry, dict):
            entry_key = (entry.get("key") or "")
            entry_key = str(entry_key).strip() if entry_key else ""
            entry_name = str(
                entry.get("name") or entry.get("city") or entry.get("query") or ""
            ).strip()
        else:
            entry_key = ""
            entry_name = str(entry or "").strip()

        # Case 3: key with no name — refuse. The LLM violated the rule_body
        # (every targeting item must carry a name). Without a name we can't
        # validate the key against Meta's canonical record.
        if entry_key and not entry_name:
            dropped_no_name.append(entry_key)
            continue

        # Need a name to search.
        if not entry_name:
            continue

        parsed.append((entry, entry_key, entry_name))

    # Step 2: fan out targeting_search calls in parallel. Meta's targetingsearch
    # endpoint serializes per-process when called sequentially (each city ~1.3s
    # round-trip from this region) — for a 10-city brief that's 13s wasted on
    # network wait. ThreadPoolExecutor with a small worker pool keeps total
    # wall-time near the slowest single call. We DO NOT skip revalidation when
    # `key` is already present: production has seen the LLM emit fake keys
    # (e.g. 1040000 → "Purasavakkam" not "Hyderabad"), so every entry must
    # still be checked against Meta's canonical record.
    def _fetch(name: str):
        try:
            return services.targeting_search_with_fallback(
                account_id, token, "adgeolocation", q=name, limit=5,
            ), None
        except Exception as exc:  # noqa: BLE001 — propagated as a per-entry failure below.
            return None, exc

    if parsed:
        max_workers = min(8, len(parsed))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            search_results = list(pool.map(lambda p: _fetch(p[2]), parsed))
    else:
        search_results = []

    # Map Meta search-result `type` → the corresponding geo_locations key.
    # `subcity` is Meta's term for sub-city units in some countries; treat it
    # like a neighborhood for routing purposes (Meta uses geo_locations.subcities).
    _TYPE_TO_GEO_KEY = {
        "city": None,  # already handled below as the primary path
        "neighborhood": "neighborhoods",
        "region": "regions",
        "subcity": "subcities",
        "zip": "zips",
        "country": "countries",
        "country_group": "country_groups",
        "geo_market": "geo_markets",
        "electoral_district": "electoral_districts",
    }
    # Ranked preference order when no exact-name match exists — prefer the
    # most specific geographic unit first.
    _FALLBACK_TYPE_ORDER = ("neighborhood", "subcity", "zip", "region", "country")

    # Step 3: process sequentially to preserve input order in `new_cities`
    # and keep auto_fixes/swapped/rescued message order deterministic.
    for (entry, entry_key, entry_name), (res, exc) in zip(parsed, search_results):
        if exc is not None:
            auto_fixes.append(f"Auto-resolve city \"{entry_name}\": targeting_search failed ({exc}); dropped.")
            continue
        rows = res.get("data") if isinstance(res, dict) else None
        rows_list = [r for r in (rows or []) if isinstance(r, dict)]
        match = next(
            (r for r in rows_list if str(r.get("type") or "").lower() == "city"),
            None,
        )
        # If no city match, try other location types in preference order so
        # entries like "Connaught Place" (neighborhood) are routed to
        # geo_locations.neighborhoods instead of silently dropped.
        if not match or not (match.get("key") or "").strip():
            alt_match = None
            for alt_type in _FALLBACK_TYPE_ORDER:
                alt_match = next(
                    (r for r in rows_list if str(r.get("type") or "").lower() == alt_type
                     and (r.get("key") or "")),
                    None,
                )
                if alt_match:
                    break
            if alt_match:
                alt_type_l = str(alt_match.get("type") or "").lower()
                alt_geo_key = _TYPE_TO_GEO_KEY.get(alt_type_l)
                if alt_geo_key:
                    alt_entry: Dict[str, Any] = {
                        "key": str(alt_match.get("key")),
                        "name": str(alt_match.get("name") or entry_name),
                    }
                    if alt_match.get("country_code"):
                        alt_entry["country_code"] = alt_match.get("country_code")
                    # Cities/subcities/neighborhoods accept radius; regions/zips/
                    # countries do not. Apply radius only where Meta accepts it.
                    if alt_type_l in ("subcity", "neighborhood"):
                        radius_alt = None
                        if isinstance(entry, dict):
                            radius_alt = entry.get("radius")
                        try:
                            radius_int = int(radius_alt) if radius_alt is not None else 0
                        except (TypeError, ValueError):
                            radius_int = 0
                        if 1 <= radius_int <= 80:
                            alt_entry["radius"] = radius_int
                            alt_entry["distance_unit"] = "kilometer"
                    rerouted_by_type.setdefault(alt_geo_key, []).append(alt_entry)
                    rerouted_messages.append(
                        f"\"{entry_name}\" routed to geo_locations.{alt_geo_key} "
                        f"(Meta type={alt_type_l}, key={alt_match.get('key')})"
                    )
                    continue
            auto_fixes.append(
                f"Auto-resolve \"{entry_name}\": no Meta match found in cities, "
                "neighborhoods, regions, zips, or countries — dropped. "
                "Check spelling or pick a higher-level location."
            )
            continue

        canonical_key = str(match.get("key"))
        canonical_name = str(match.get("name") or entry_name)

        # Case 2: entry has a key — verify it matches the canonical for this name.
        if entry_key and entry_key != canonical_key:
            swapped.append(f"{entry_name}: {entry_key}→{canonical_key} ({canonical_name})")

        # City-state upgrade: when the matched CITY is the namesake of its own
        # region (Delhi city inside Delhi region, Chandigarh, etc. — detected
        # purely from Meta's own search result, no hardcoded names) and the
        # brief did NOT ask for a radius, target the REGION instead. City
        # entries always carry a radius (default 25 km below), which renders
        # as "X (+25 km)" in Ads Manager and spills into neighbouring states —
        # the exact opposite of an "only X" brief. The region key covers the
        # precise administrative boundary with no radius. An explicit radius
        # means the caller wanted the circle, so it is honoured unchanged.
        _explicit_radius = entry.get("radius") if isinstance(entry, dict) else None
        _match_region = str(match.get("region") or "").strip()
        _match_region_id = str(match.get("region_id") or "").strip()
        if (
            _explicit_radius in (None, "")
            and _match_region_id
            and _match_region
            and _match_region.casefold() == canonical_name.casefold()
        ):
            rerouted_by_type.setdefault("regions", []).append(
                {"key": _match_region_id, "name": _match_region}
            )
            auto_fixes.append(
                f"\"{canonical_name}\" is a city-state (city key {canonical_key} is the "
                f"namesake of its own region {_match_region_id}) and no radius was "
                f"requested — targeted geo_locations.regions[{_match_region_id}] for the "
                "exact boundary instead of city+default-25km (which would include "
                "neighbouring areas outside it)."
            )
            continue

        city_dict: Dict[str, Any] = {
            "key": canonical_key,
            "name": canonical_name,
        }
        if match.get("country_code"):
            city_dict["country_code"] = match.get("country_code")
        # Default radius: Meta requires 17-80km for cities; pick 25 as a sane mid.
        radius = None
        if isinstance(entry, dict):
            radius = entry.get("radius")
        _radius_was_explicit = isinstance(radius, (int, float)) and 17 <= int(radius) <= 80
        city_dict["radius"] = int(radius) if _radius_was_explicit else 25
        city_dict["distance_unit"] = "kilometer"
        if not _radius_was_explicit:
            auto_fixes.append(
                f"City \"{canonical_name}\" targeted with DEFAULT radius 25 km — Ads "
                f"Manager will show '{canonical_name} (+25 km)' and delivery includes "
                "surrounding areas. Pass targeting.geo_radius_km / a per-city radius, "
                "or use the region key from targeting_search for exact boundaries."
            )
        new_cities.append(city_dict)
        rescued.append(f"{entry_name} → key:{canonical_key}")

    # Write the validated list back to whichever shape the LLM used. When the
    # LLM emitted Meta-shape (geo_locations.cities), the validated list goes
    # there so the rest of the orchestrator picks it up unchanged. When it
    # used shorthand, build_targeting_spec downstream will move it into
    # geo_locations.cities itself.
    # Ensure geo_locations exists if we need to merge rerouted items into it.
    if rerouted_by_type and geo_locations is None:
        geo_locations = {}
        targeting["geo_locations"] = geo_locations
    if has_meta_shape:
        if new_cities:
            geo_locations["cities"] = new_cities
        else:
            geo_locations.pop("cities", None)
    # Merge any rerouted entries (neighborhoods, regions, zips, etc.) into
    # the appropriate geo_locations sub-key. Preserve any existing values.
    if rerouted_by_type and geo_locations is not None:
        for geo_key, items in rerouted_by_type.items():
            existing = geo_locations.get(geo_key)
            if isinstance(existing, list):
                # Dedupe by canonical key.
                seen_keys = {str(it.get("key")) for it in existing if isinstance(it, dict) and it.get("key")}
                for it in items:
                    if str(it.get("key")) not in seen_keys:
                        existing.append(it)
                        seen_keys.add(str(it.get("key")))
                geo_locations[geo_key] = existing
            else:
                geo_locations[geo_key] = items
    if has_meta_shape or rerouted_by_type:
        if not geo_locations:
            targeting.pop("geo_locations", None)
        else:
            targeting["geo_locations"] = geo_locations
    if has_shorthand:
        if new_cities:
            targeting["geo_cities"] = new_cities
        else:
            targeting.pop("geo_cities", None)
    doc["targeting"] = targeting
    if rerouted_messages:
        auto_fixes.append(
            "Routed non-city locations to correct geo_locations subkey: "
            + "; ".join(rerouted_messages[:8])
            + ("..." if len(rerouted_messages) > 8 else "")
        )
    if rescued:
        auto_fixes.append("Resolved cities via targeting_search: " + ", ".join(rescued[:8]) +
                          ("..." if len(rescued) > 8 else ""))
    if swapped:
        auto_fixes.append(
            "Corrected fake city keys: " + "; ".join(swapped[:5]) +
            ("..." if len(swapped) > 5 else "") +
            " (LLM emitted wrong key; canonical key from /search applied)."
        )
    if dropped_no_name:
        auto_fixes.append(
            f"Dropped {len(dropped_no_name)} geo_cities entry(ies) with no name: "
            f"keys {dropped_no_name[:5]}"
            + ("..." if len(dropped_no_name) > 5 else "")
            + " — Meta would accept these keys but they may resolve to wrong cities. "
            "Re-emit with both 'name' and 'key' (or just 'name' for auto-resolve)."
        )


def _autoresolve_locales(
    doc: Dict[str, Any],
    account_id: str,
    token: str,
    auto_fixes: List[str],
) -> None:
    """Translate brief.targeting.languages (strings) → Meta targeting.locales (int IDs)."""
    targeting = doc.get("targeting") if isinstance(doc.get("targeting"), dict) else {}
    langs = targeting.get("languages") or targeting.get("locales_names")
    if not isinstance(langs, list) or not langs:
        return
    locale_ids: List[int] = []
    rescued: List[str] = []
    for entry in langs:
        if isinstance(entry, int):
            locale_ids.append(entry)
            continue
        if isinstance(entry, dict) and entry.get("key"):
            try:
                locale_ids.append(int(entry["key"]))
                continue
            except (TypeError, ValueError):
                pass
        name = str(entry or "").strip()
        if not name or name.isdigit():
            try:
                locale_ids.append(int(name))
                continue
            except (TypeError, ValueError):
                continue
        try:
            res = services.targeting_search_with_fallback(
                account_id, token, "adlocale", q=name, limit=5,
            )
        except Exception as exc:
            auto_fixes.append(f"Auto-resolve locale \"{name}\": targeting_search failed ({exc}); dropped.")
            continue
        rows = res.get("data") if isinstance(res, dict) else None
        match = next(
            (r for r in (rows or []) if isinstance(r, dict) and (r.get("key") or r.get("id"))),
            None,
        )
        if not match:
            auto_fixes.append(f"Auto-resolve locale \"{name}\": no Meta locale match — dropped.")
            continue
        try:
            locale_ids.append(int(match.get("key") or match.get("id")))
        except (TypeError, ValueError):
            continue
        rescued.append(f"{name} → {match.get('key') or match.get('id')}")
    if locale_ids:
        targeting["locales"] = locale_ids
    targeting.pop("languages", None)
    targeting.pop("locales_names", None)
    doc["targeting"] = targeting
    if rescued:
        auto_fixes.append("Resolved locales via targeting_search: " + ", ".join(rescued))


def _autoresolve_instagram_identity(
    doc: Dict[str, Any],
    token: str,
    auto_fixes: List[str],
) -> None:
    """Fill ``creative.instagram_user_id`` from the page when the LLM omitted it.

    Briefs commonly say "Use connected Instagram account" — the LLM either
    hardcodes a stale ID or omits the field. This helper looks at the
    creative's page_id (or ad set's) and resolves the linked
    ``instagram_business_account.id`` via Graph. The IG resolution tool itself
    auto-swaps to a page-scoped token on Meta error #190 (services.py change
    from Round 1) so a single call always succeeds.

    No-op when ``instagram_user_id`` or ``instagram_actor_id`` is already set —
    explicit overrides always win.
    """
    creative = doc.get("creative") if isinstance(doc.get("creative"), dict) else None
    if not creative:
        return
    if creative.get("instagram_user_id") or creative.get("instagram_actor_id"):
        return
    ads = doc.get("ad") if isinstance(doc.get("ad"), dict) else {}
    page_id = str(creative.get("page_id") or ads.get("page_id") or "").strip()
    if not page_id:
        return
    try:
        ig = services.get_instagram_accounts(page_id, token)
    except Exception as exc:  # noqa: BLE001
        auto_fixes.append(f"Auto-resolve instagram_user_id: lookup failed ({exc}); leaving creative without IG identity (Facebook-only delivery).")
        return
    if not isinstance(ig, dict) or ig.get("error"):
        # Don't surface the error verbatim — the LLM doesn't need to know that
        # IG isn't linked, and partial-page-token gating is already handled
        # inside services.get_instagram_accounts.
        return
    iba = ig.get("instagram_business_account") or {}
    iba_id = str(iba.get("id") or "").strip()
    if iba_id:
        creative["instagram_user_id"] = iba_id
        auto_fixes.append(f"Auto-resolved creative.instagram_user_id={iba_id} from page_id={page_id}.")
        return
    # Fallback: instagram_accounts (legacy, not always present).
    legacy = ig.get("instagram_accounts") or {}
    legacy_data = legacy.get("data") if isinstance(legacy, dict) else None
    if isinstance(legacy_data, list) and legacy_data:
        first = legacy_data[0] if isinstance(legacy_data[0], dict) else {}
        legacy_id = str(first.get("id") or "").strip()
        if legacy_id:
            creative["instagram_user_id"] = legacy_id
            auto_fixes.append(f"Auto-resolved creative.instagram_user_id={legacy_id} from page_id={page_id} (legacy instagram_accounts edge).")


def _autoresolve_phase(
    doc: Dict[str, Any],
    account_id: str,
    token: str,
    auto_fixes: List[str],
) -> None:
    """Single auto-resolution pass: page → pixel → interests → cities → locales.

    Mutates ``doc`` in place. Each sub-helper is a no-op when the brief
    already supplied the field (so explicit overrides always win). Reports
    every auto-pick to ``auto_fixes`` so the agent can surface them.

    Order matters: page first (pixel auto-resolution can use its connected
    pages later if needed), then pixel, then targeting axes which are
    independent of each other.
    """
    before = len(auto_fixes)
    _autoresolve_page(doc, token, auto_fixes)
    _autoresolve_pixel(doc, account_id, token, auto_fixes)
    _autoresolve_interests(doc, account_id, token, auto_fixes)
    _autoresolve_behaviors_and_demographics(doc, account_id, token, auto_fixes)
    _autoresolve_cities(doc, account_id, token, auto_fixes)
    _autoresolve_locales(doc, account_id, token, auto_fixes)
    _autoresolve_instagram_identity(doc, token, auto_fixes)
    if len(auto_fixes) > before:
        auto_fixes.insert(before, f"--- Auto-resolution phase: filled {len(auto_fixes) - before} field(s) ---")


# Axes whose IDs Meta returns from /search?type=adinterest (the deterministic
# layer). Other axes (work_employers, education_*, income, etc.) stay on the
# /targetingvalidation preflight because /search doesn't cover them.
_REVALIDATE_AXES = ("interests", "behaviors")


# Universal: optimization goals where a website pixel either gets
# hard-rejected by Meta OR adds no delivery value. Pre-emptively strip
# pixel_id to avoid a wasted Meta round-trip and possible orphan campaigns
# (when keep_campaign_on_sync_failure=true). Covers ALL ad types whose
# goal falls in one of these categories — not specific to any particular
# campaign brief.
#
# Categories (Meta-documented across the API):
# - CALL goals: pixel + destination_type=PHONE_CALL is hard-rejected with
#   subcode 1815704 ("Invalid destination type for promoting pixel").
# - Video-view goals: Meta has no VIDEO_VIEW enum for
#   `promoted_object[custom_event_type]`; pixel + custom_event_type='VIDEO_VIEW'
#   fails with error code 100 ("must be one of the following values").
# - Awareness goals: optimize against Meta's reach/recall models, not
#   website events. Pixel attaches but adds no delivery value.
# - Engagement-on-Meta-surface goals: the engagement happens on Meta
#   (post likes, page likes, event RSVPs) — a website pixel never fires
#   for these.
_NO_PIXEL_ATTACH_GOALS = {
    # CALL goals
    "QUALITY_CALL", "CALL_VOLUME",
    # Video-view goals
    "THRUPLAY", "VIDEO_VIEWS", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS",
    # Awareness goals
    "REACH", "IMPRESSIONS", "AD_RECALL_LIFT",
    # Engagement-on-Meta goals
    "POST_ENGAGEMENT", "PAGE_LIKES", "EVENT_RESPONSES",
}


# Video-family optimization goals. The creative MUST carry a video asset
# (video_id from facebook_upload_video, OR an EXISTING_POST/INSTAGRAM_EXISTING
# whose underlying post is a video, OR an asset_feed_spec with videos[]).
# Image-only creatives are hard-rejected by Meta at adset/ad create with
# error_subcode 1815503 ("Non-video post is not valid for Video Views
# optimisation goal"). Universal across every ad type that targets video
# delivery — not specific to any single brief.
_VIDEO_REQUIRED_GOALS = frozenset({
    "THRUPLAY",
    "VIDEO_VIEWS",
    "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS",
})


# Below this count of total flexible_spec entries, calls campaigns deliver
# poorly because Meta's call optimizer needs a broad enough audience to find
# call-intent users. Auto-enable Advantage+ Audience to widen reach when the
# brief's resolved targeting is too thin.
_MIN_FLEX_ENTRIES_BEFORE_ADVANTAGE_FALLBACK = 5


def _strip_pixel_for_call_goals(
    doc: Dict[str, Any],
    auto_fixes: List[str],
) -> None:
    """Remove ``tracking.pixel_id`` when the optimization goal does not use
    a website pixel for attribution / delivery.

    Universal across ALL ad types — covers (see ``_NO_PIXEL_ATTACH_GOALS``
    for the full list):

    - CALL goals (QUALITY_CALL, CALL_VOLUME): Meta rejects pixel +
      destination_type=PHONE_CALL with subcode 1815704.
    - Video-view goals (THRUPLAY, VIDEO_VIEWS, TWO_SECOND_CONTINUOUS_VIDEO_VIEWS):
      Meta has no VIDEO_VIEW enum for ``promoted_object[custom_event_type]``
      and rejects with error 100.
    - Awareness goals (REACH, IMPRESSIONS, AD_RECALL_LIFT): optimization
      runs against Meta's reach/recall models, not website events.
    - Engagement-on-Meta goals (POST_ENGAGEMENT, PAGE_LIKES, EVENT_RESPONSES):
      engagement happens on Meta surfaces; the pixel never fires.

    Function name kept as ``_strip_pixel_for_call_goals`` for backwards
    compatibility with existing call sites — the function now applies to
    all non-pixel-attach goals, not just CALL goals.
    """
    adset = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    goal = str(adset.get("optimization_goal") or "").strip().upper()
    if goal not in _NO_PIXEL_ATTACH_GOALS:
        return
    tracking = doc.get("tracking") if isinstance(doc.get("tracking"), dict) else None
    if not tracking:
        return
    if not tracking.get("pixel_id"):
        return
    pid = tracking.pop("pixel_id", None)
    cet = tracking.pop("custom_event_type", None)  # paired field; meaningless without pixel
    doc["tracking"] = tracking
    # Tailored message per category so the user understands WHY it was stripped.
    # Universal logic — applies to any ad type whose goal falls in these buckets.
    if goal in ("QUALITY_CALL", "CALL_VOLUME"):
        reason = (
            "Meta rejects pixel + destination_type=PHONE_CALL with subcode 1815704. "
            "Call attribution comes from promoted_object.page_id automatically."
        )
    elif goal in ("THRUPLAY", "VIDEO_VIEWS", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"):
        reason = (
            "Meta has no VIDEO_VIEW custom_event_type for promoted_object — "
            "video-views goals don't attach a website pixel. Delivery is driven "
            "by Meta's own video engagement models."
        )
    elif goal in ("REACH", "IMPRESSIONS", "AD_RECALL_LIFT"):
        reason = (
            "Awareness goals optimize against Meta's reach/recall models, not "
            "website events — the pixel adds no delivery value here."
        )
    else:  # POST_ENGAGEMENT, PAGE_LIKES, EVENT_RESPONSES
        reason = (
            "Engagement-on-Meta goals optimize for on-Meta interactions; a "
            "website pixel never fires for these and would only cause noise."
        )
    cet_part = f" + custom_event_type={cet}" if cet else ""
    auto_fixes.append(
        f"Stripped tracking.pixel_id={pid}{cet_part} from document for {goal} goal — {reason}"
    )


def _validate_creative_required_fields(
    creative_cfg: Dict[str, Any],
    sync_mode: str,
    *,
    optimization_goal: Optional[str] = None,
    ad_account_has_media: bool = False,
) -> Optional[Dict[str, str]]:
    """Per-creative-mode required-field validator.

    Runs upfront in the sync flow so we fail BEFORE any Meta object is
    created. Each creative MODE has different mandatory fields; missing
    them later (in services.create_creative) leaves orphan campaigns +
    ad sets behind that the user has to clean up manually.

    When ``optimization_goal`` is one of the video-family goals
    (THRUPLAY / VIDEO_VIEWS / TWO_SECOND_CONTINUOUS_VIDEO_VIEWS), the
    creative MUST also carry a video asset, otherwise Meta hard-rejects
    at adset/ad create with subcode 1815503 ("Non-video post is not
    valid for Video Views optimisation goal"). This check is universal
    across every ad type that targets video delivery.

    Returns:
        None if the creative passes validation for this mode + goal.
        A blocking-error dict {"message": "..."} if any required field is missing.

    Universal across ad types — this is the single source of truth for
    "what does mode X need on the creative."
    """
    if not isinstance(creative_cfg, dict):
        return {
            "message": "Document.creative is missing or not an object."
        }
    mode = (sync_mode or "").upper()
    goal = (optimization_goal or "").strip().upper()
    page_id = str(creative_cfg.get("page_id") or "").strip()
    link_url = str(creative_cfg.get("link_url") or "").strip()
    object_story_id = str(creative_cfg.get("object_story_id") or "").strip()
    source_ig_media = str(creative_cfg.get("source_instagram_media_id") or "").strip()
    ig_permalink = str(creative_cfg.get("instagram_permalink_url") or "").strip()
    image_hash = str(creative_cfg.get("image_hash") or "").strip()
    video_id = str(creative_cfg.get("video_id") or "").strip()
    instant_experience_id = str(creative_cfg.get("instant_experience_id") or "").strip()
    asset_feed_spec = creative_cfg.get("asset_feed_spec")
    carousel_cards = creative_cfg.get("carousel_cards")
    branded_sponsor = str(creative_cfg.get("branded_content_sponsor_page_id") or "").strip()

    # Universal goal-aware asset check. Applies before mode-specific checks
    # below so the error message tells the user the REAL problem (video
    # missing for video goal) rather than a generic mode-field complaint.
    # We can't introspect EXISTING_POST / INSTAGRAM_EXISTING / DCO assets
    # without fetching from Meta — leave those to Meta itself (which gives
    # the same 1815503 error if the post is image-only). For LINK_AD,
    # CAROUSEL, and direct-asset modes we can catch this upfront.
    if goal in _VIDEO_REQUIRED_GOALS:
        # Has a direct video signal? video_id (uploaded video) or
        # asset_feed_spec with a videos[] entry, or carousel_cards with at
        # least one card carrying video_id.
        has_video = False
        if video_id:
            has_video = True
        elif isinstance(asset_feed_spec, dict):
            vids = asset_feed_spec.get("videos") or []
            if isinstance(vids, (list, tuple)) and vids:
                has_video = True
        elif isinstance(carousel_cards, list):
            has_video = any(
                isinstance(c, dict) and str(c.get("video_id") or "").strip()
                for c in carousel_cards
            )
        # If only an image_hash is set and no video → fail loud, fail early.
        if not has_video and image_hash and mode in ("LINK_AD", "CAROUSEL", "DCO", ""):
            return {"message": (
                f"adset.optimization_goal={goal} requires a video creative, "
                f"but the document only carries an image (image_hash={image_hash}). "
                "Meta rejects image-only ads with this goal "
                "(subcode 1815503: 'Non-video post is not valid for Video Views "
                "optimisation goal'). Fixes: "
                "(a) upload a video via facebook_upload_video and set "
                "creative.video_id, OR "
                "(b) switch creative.mode to EXISTING_POST and set "
                "creative.source_instagram_media_id / object_story_id to an "
                "existing VIDEO post, OR "
                "(c) change adset.optimization_goal to an image-friendly goal "
                "(e.g. REACH, IMPRESSIONS, POST_ENGAGEMENT, LINK_CLICKS)."
            )}

    # Mode-specific requirements. Order: most-specific first, then fallback to LINK_AD.
    if mode == "EXISTING_POST":
        if not object_story_id and not source_ig_media and not ig_permalink:
            return {"message": (
                "EXISTING_POST mode requires ONE of: "
                "creative.object_story_id (FB page post, format page_id_post_id), "
                "creative.source_instagram_media_id (IG story media id), or "
                "creative.instagram_permalink_url (IG feed post / carousel / reel — "
                "the /p/<shortcode>/ or /reel/<shortcode>/ URL). "
                "Fetch a real post id via facebook_get_page_posts / "
                "facebook_get_instagram_accounts / facebook_get_instagram_media."
            )}
        return None

    if mode == "INSTAGRAM_EXISTING":
        if not source_ig_media:
            return {"message": (
                "INSTAGRAM_EXISTING mode requires creative.source_instagram_media_id. "
                "Do NOT pass instagram_actor_id with it — Meta rejects that combination."
            )}
        return None

    if mode == "WHATSAPP":
        if not page_id:
            return {"message": (
                "WHATSAPP mode requires creative.page_id. Pass it directly or "
                "let the connector resolve it via creative.page_name."
            )}
        return None

    if mode == "CAROUSEL":
        if not page_id:
            return {"message": "CAROUSEL mode requires creative.page_id."}
        if not isinstance(carousel_cards, (list, str)) or not carousel_cards:
            return {"message": (
                "CAROUSEL mode requires creative.carousel_cards (JSON list of 2-10 cards, "
                "each with link + image_hash or video_id; optional name/description/call_to_action)."
            )}
        return None

    if mode == "COLLECTION":
        if not page_id:
            return {"message": "COLLECTION mode requires creative.page_id."}
        if not instant_experience_id:
            return {"message": (
                "COLLECTION mode requires creative.instant_experience_id "
                "(pre-create the Canvas/Instant Experience in Ads Manager first)."
            )}
        return None

    if mode == "DCO":
        if not page_id:
            return {"message": "DCO mode requires creative.page_id."}
        if not asset_feed_spec:
            return {"message": (
                "DCO (Dynamic Creative) mode requires creative.asset_feed_spec — "
                "a JSON dict with arrays for bodies, titles, descriptions, images, "
                "videos, ad_formats, call_to_action_types."
            )}
        return None

    if mode == "DPA":
        if not page_id:
            return {"message": "DPA mode requires creative.page_id."}
        # product_set_id is on the adset's promoted_object — checked elsewhere.
        return None

    if mode == "LEAD_FORM":
        if not page_id:
            return {"message": "LEAD_FORM mode requires creative.page_id."}
        # lead_gen_form_id is set inside object_story_spec.link_data.call_to_action;
        # accept either an explicit creative.lead_gen_form_id or the nested form.
        if not creative_cfg.get("lead_gen_form_id") and not creative_cfg.get("leadgen_form_id"):
            return {"message": (
                "LEAD_FORM mode requires creative.lead_gen_form_id (or "
                "leadgen_form_id) — create the form via facebook_create_lead_form "
                "and pass the returned id."
            )}
        return None

    if mode == "BRANDED_CONTENT":
        if not page_id:
            return {"message": "BRANDED_CONTENT mode requires creative.page_id."}
        if not branded_sponsor:
            return {"message": (
                "BRANDED_CONTENT mode requires creative.branded_content_sponsor_page_id "
                "(both pages must have consented in Meta Business Suite)."
            )}
        return None

    # LINK_AD is the default fallback. Requires page_id + link_url + at least
    # one creative asset (image_hash, video_id, asset_feed_spec, or carousel).
    # NOTE: this is exactly the check that used to fire AFTER campaign create
    # at production_flow.py:~4910 — moved here so it fires upfront.
    if mode == "LINK_AD" or not mode:
        missing = []
        if not page_id:
            missing.append("page_id")
        if not link_url:
            missing.append("link_url")
        has_asset = bool(image_hash or video_id or asset_feed_spec or carousel_cards)
        if not has_asset and not ad_account_has_media:
            missing.append(
                "an asset (image_hash via facebook_upload_image, OR video_id via "
                "facebook_upload_video, OR carousel_cards, OR asset_feed_spec)"
            )
        if missing:
            return {"message": (
                f"LINK_AD mode requires: {', '.join(missing)}. "
                "If the brief intended an existing Instagram/Facebook post, set "
                "creative.mode='EXISTING_POST' and supply creative.object_story_id "
                "or creative.source_instagram_media_id instead."
            )}
        return None

    # Unknown mode — let downstream surface Meta's exact error.
    return None


def _enable_advantage_audience_when_sparse(
    doc: Dict[str, Any],
    auto_fixes: List[str],
) -> None:
    """For QUALITY_CALL / CALL_VOLUME goals: when post-resolve targeting is
    sparse (fewer than ``_MIN_FLEX_ENTRIES_BEFORE_ADVANTAGE_FALLBACK`` entries
    across all flexible_spec axes), enable Meta's Advantage+ Audience so
    delivery widens beyond the few survivors. No-op when targeting is
    already sufficient or when the brief explicitly opted out via
    ``targeting.targeting_automation.advantage_audience=0``.
    """
    targeting = doc.get("targeting") if isinstance(doc.get("targeting"), dict) else None
    if not targeting:
        return
    adset = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    goal = str(adset.get("optimization_goal") or "").strip().upper()
    if goal not in _NO_PIXEL_ATTACH_GOALS:
        return  # only call goals get this fallback today
    flex = targeting.get("flexible_spec") if isinstance(targeting.get("flexible_spec"), list) else None
    total_entries = 0
    if flex:
        for slice_ in flex:
            if not isinstance(slice_, dict):
                continue
            for axis in ("interests", "behaviors", "demographics"):
                items = slice_.get(axis)
                if isinstance(items, list):
                    total_entries += len(items)
    if total_entries >= _MIN_FLEX_ENTRIES_BEFORE_ADVANTAGE_FALLBACK:
        return
    ta = targeting.get("targeting_automation")
    if not isinstance(ta, dict):
        ta = {}
    # Respect explicit opt-out from the brief.
    if ta.get("advantage_audience") == 0:
        return
    if ta.get("advantage_audience") == 1:
        return  # already enabled, no-op
    ta["advantage_audience"] = 1
    targeting["targeting_automation"] = ta
    doc["targeting"] = targeting
    auto_fixes.append(
        f"Enabled Advantage+ Audience for {goal} goal because resolved "
        f"flexible_spec has only {total_entries} entries (< "
        f"{_MIN_FLEX_ENTRIES_BEFORE_ADVANTAGE_FALLBACK}). Meta will widen "
        "delivery beyond the brief's narrow targeting; set "
        "targeting.targeting_automation.advantage_audience=0 in the brief "
        "to opt out."
    )


def _names_match_for_swap(original: str, canonical: str) -> bool:
    """True iff ``canonical`` is the same concept as ``original``.

    Case-insensitive comparison with three layers of tolerance:

    1. Strip parenthetical qualifiers (``(business and finance)``).
    2. Normalize common word-form variants Meta uses interchangeably across
       its taxonomy: singular ↔ plural (``Owner`` ↔ ``Owners``, ``Shopper`` ↔
       ``Shoppers``), and the ``-tor`` ↔ ``-tion`` family
       (``Entrepreneur`` ↔ ``Entrepreneurship``). This captures Meta's
       ongoing taxonomy consolidation without opening the door to genuine
       fuzzy mismatches (``Business school`` vs ``Information technology
       consulting`` still falls through).
    3. Whitespace collapsed to single space.

    Meta's ``/search`` sometimes returns a fuzzy near-match (e.g.
    ``Business school`` → id for ``Information technology consulting``) as
    the top hit when no exact concept exists. The strict guard rejects
    those. When Meta's canonical IS just a word-form variation of the
    briefed name, accept the swap.
    """
    import re

    _SUFFIX_PAIRS = (
        ("ship", ""),       # Entrepreneurship → Entrepreneur
        ("ship", "s"),      # Entrepreneurship → Entrepreneurs
        ("ers", "er"),      # Owners → Owner
        ("ers", "ership"),  # Owners → Ownership
        ("er", "ers"),      # Owner → Owners
        ("er", "ership"),
        ("s", ""),          # Shoppers → Shopper (last so it doesn't gobble plurals already handled)
    )

    def _norm(s: str) -> str:
        s = re.sub(r"\s*\([^)]*\)\s*", "", str(s or "")).strip().lower()
        s = re.sub(r"[^\w\s]", " ", s)  # punctuation → space (so "small-business" → "small business")
        return re.sub(r"\s+", " ", s).strip()

    def _word_set(s: str) -> set:
        return {_word_root(w) for w in s.split()}

    def _word_root(w: str) -> str:
        # Reduce a single word to a normalized "stem" by stripping common
        # variant suffixes. Cheap; not a real stemmer.
        for suf in ("ership", "ship", "ers", "er", "es", "ies", "s"):
            if len(w) > len(suf) + 2 and w.endswith(suf):
                return w[: -len(suf)]
        return w

    a, b = _norm(original), _norm(canonical)
    if a == b:
        return True
    # Word-set comparison after stemming. Both phrases must reduce to the
    # same set of word roots — order-independent, plurals/forms tolerated.
    return _word_set(a) == _word_set(b)


def _revalidate_flex_via_search(
    doc: Dict[str, Any],
    account_id: str,
    token: str,
    auto_fixes: List[str],
) -> None:
    """Deterministic pre-flight: confirm each {id, name} in flexible_spec is
    still in Meta's live taxonomy; swap stale IDs for canonical ones; drop
    deprecated names with a clear note.

    Why this exists: Meta's ``/targetingvalidation`` endpoint is too lenient
    — it returns ``valid: True`` for IDs that ``/adsets`` later rejects with
    subcode 1487079. The only reliable validator is ``/search?type=adinterest``
    (used by ``services.batch_interest_search``): it returns *only* IDs that
    are currently in the active taxonomy. So for every entry that has a
    name we can search, we look it up; if the canonical ID differs from the
    LLM's emitted one we swap; if the name returns nothing it's deprecated
    and we drop. Filters returned canonical IDs against the learned-bad
    cache so a re-rejected ID never gets reintroduced.

    Skips entries that are name-only (the auto-resolve phase fills them in)
    and entries that have neither id nor name (no signal to act on).
    """
    targeting = doc.get("targeting") if isinstance(doc.get("targeting"), dict) else None
    if not targeting:
        return
    flex = targeting.get("flexible_spec") if isinstance(targeting.get("flexible_spec"), list) else None
    if not flex:
        return

    # Collect work per axis so each axis is searched against its correct
    # Meta taxonomy: interests via /search?type=adinterest (batch_interest_search),
    # behaviors via /search?type=adTargetingCategory (batch_targeting_category_search).
    # The previous single-batch approach searched ALL names against adinterest, so
    # behaviors got corrupted into closest interest matches and then dropped.
    work_by_axis: Dict[str, List[Tuple[int, int, str, str]]] = {ax: [] for ax in _REVALIDATE_AXES}
    for s_idx, slice_ in enumerate(flex):
        if not isinstance(slice_, dict):
            continue
        for axis_key in _REVALIDATE_AXES:
            items = slice_.get(axis_key)
            if not isinstance(items, list):
                continue
            for it_idx, entry in enumerate(items):
                if not isinstance(entry, dict):
                    continue
                nm = str(entry.get("name") or "").strip()
                iid = str(entry.get("id") or "").strip()
                if not nm or not iid:
                    continue  # name-only handled by auto-resolve; id-only is rejected by rule
                work_by_axis[axis_key].append((s_idx, it_idx, nm, iid))

    if not any(work_by_axis.values()):
        return

    bad_ids = _learned_bad_interest_ids()
    canonical_for_axis: Dict[str, Dict[str, str]] = {ax: {} for ax in _REVALIDATE_AXES}
    canonical_label_for_axis: Dict[str, Dict[str, str]] = {ax: {} for ax in _REVALIDATE_AXES}
    missing_for_axis: Dict[str, set[str]] = {ax: set() for ax in _REVALIDATE_AXES}

    def _ingest_search_result(axis_key: str, result: Any) -> None:
        """Populate canonical maps for ``axis_key`` from a search-helper result."""
        if not isinstance(result, dict):
            return
        for entry in result.get("found") or []:
            if not isinstance(entry, dict):
                continue
            q = str(entry.get("query") or entry.get("name") or "").strip()
            cid = str(entry.get("id") or "").strip()
            if not q or not cid or not cid.isdigit():
                continue
            # Defence in depth: refuse anything that doesn't look like a real
            # Meta ID (13-15 digits, no all-zero tail) regardless of what the
            # helper accepted.
            if len(cid) < 11 or len(cid) > 15:
                continue
            if cid.endswith("00000") and cid.count("0") >= 6:
                continue
            if cid in bad_ids:
                continue
            canonical_for_axis[axis_key][q] = cid
            canonical_label_for_axis[axis_key][q] = str(entry.get("name") or q)
        for m in result.get("missing") or []:
            if isinstance(m, str):
                missing_for_axis[axis_key].add(m)

    if work_by_axis["interests"]:
        names = list(dict.fromkeys(w[2] for w in work_by_axis["interests"]))
        try:
            _ingest_search_result("interests", services.batch_interest_search(account_id, token, names))
        except Exception as exc:
            auto_fixes.append(
                f"Revalidate interests via /search failed ({exc}); leaving "
                "interest entries to /targetingvalidation preflight."
            )

    if work_by_axis["behaviors"]:
        names = list(dict.fromkeys(w[2] for w in work_by_axis["behaviors"]))
        try:
            _ingest_search_result("behaviors", services.batch_targeting_category_search(account_id, token, names))
        except Exception as exc:
            auto_fixes.append(
                f"Revalidate behaviors via /search failed ({exc}); leaving "
                "behavior entries to /targetingvalidation preflight."
            )

    # Apply mutations slice-by-slice. We mark items for removal first so we
    # can do a single in-place rebuild per axis (preserves declaration order
    # for the survivors and avoids index drift).
    swap_count = 0
    drop_count = 0
    for s_idx, slice_ in enumerate(flex):
        if not isinstance(slice_, dict):
            continue
        for axis_key in _REVALIDATE_AXES:
            items = slice_.get(axis_key)
            if not isinstance(items, list):
                continue
            new_items: List[Dict[str, Any]] = []
            for entry in items:
                if not isinstance(entry, dict):
                    new_items.append(entry)
                    continue
                nm = str(entry.get("name") or "").strip()
                iid = str(entry.get("id") or "").strip()
                if not nm or not iid:
                    new_items.append(entry)
                    continue
                cid = canonical_for_axis[axis_key].get(nm)
                if cid is None:
                    taxonomy_name = "adinterest" if axis_key == "interests" else "adTargetingCategory"
                    if nm in missing_for_axis[axis_key]:
                        auto_fixes.append(
                            f"Dropped {axis_key[:-1]} '{nm}' (was id {iid}) — "
                            f"not in Meta's current {taxonomy_name} taxonomy."
                        )
                    else:
                        # Search returned rows but none survived the filters
                        # (cross-type match, fake-id pattern, or known-bad
                        # cache hit).
                        auto_fixes.append(
                            f"Dropped {axis_key[:-1]} '{nm}' (was id {iid}) — "
                            f"no usable Meta {taxonomy_name} match "
                            "(likely cross-type or deprecated)."
                        )
                    drop_count += 1
                    continue
                if cid != iid:
                    new_label = canonical_label_for_axis[axis_key].get(nm) or nm
                    if not _names_match_for_swap(nm, new_label):
                        # Meta returned a different-concept canonical (fuzzy
                        # neighbour). Drop instead of corrupting the brief.
                        auto_fixes.append(
                            f"Dropped {axis_key[:-1]} '{nm}' (was id {iid}) — "
                            f"Meta /search returned a different-concept canonical "
                            f"'{new_label}' (id {cid}); refusing to corrupt the brief."
                        )
                        drop_count += 1
                        continue
                    auto_fixes.append(
                        f"Swapped {axis_key[:-1]} '{nm}' id {iid} → {cid} "
                        f"(Meta /search canonical id; alias '{new_label}')."
                    )
                    swap_count += 1
                    new_items.append({"id": cid, "name": new_label})
                else:
                    new_items.append(entry)
            if new_items:
                slice_[axis_key] = new_items
            else:
                slice_.pop(axis_key, None)

    if swap_count or drop_count:
        auto_fixes.insert(
            0,
            f"--- /search revalidate: swapped {swap_count}, dropped {drop_count} flex entries ---"
        )


def _slugify_for_utm(s: str) -> str:
    """ASCII-only, lowercase, underscore-separated. Keeps UTM values clean."""
    import re
    if not s:
        return ""
    out = re.sub(r"[^a-zA-Z0-9]+", "_", str(s).strip()).strip("_").lower()
    return out or "ad"


def _apply_utm_params(
    doc: Dict[str, Any],
    auto_fixes: List[str],
) -> None:
    """Append utm_* params to creative.link_url for LINK_AD when they're absent.

    No-op for EXISTING_POST / WHATSAPP modes. Brief can override per-param via
    ``tracking.utm_overrides`` (dict). User-supplied utm_source on the URL is
    preserved — we never stomp explicit attribution.
    """
    creative = doc.get("creative") if isinstance(doc.get("creative"), dict) else {}
    mode = str(creative.get("mode") or "LINK_AD").upper()
    if mode != "LINK_AD":
        return
    link = str(creative.get("link_url") or "").strip()
    if not link:
        return
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    parsed = urlparse(link)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "utm_source" in existing:
        return  # respect user-set attribution
    campaign_name = str((doc.get("campaign") or {}).get("name") or "")
    ad_name = str((doc.get("ad") or {}).get("name") or "")
    defaults = {
        "utm_source": "facebook",
        "utm_medium": "paid",
        "utm_campaign": _slugify_for_utm(campaign_name),
        "utm_content": _slugify_for_utm(ad_name),
    }
    overrides = (doc.get("tracking") or {}).get("utm_overrides")
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if isinstance(v, str) and v.strip():
                defaults[k] = v.strip()
    merged = {**existing, **defaults}
    new_query = urlencode(merged)
    new_link = urlunparse(parsed._replace(query=new_query))
    creative["link_url"] = new_link
    doc["creative"] = creative
    auto_fixes.append(
        f"Auto-appended UTM params to creative.link_url: utm_campaign={defaults['utm_campaign']}, "
        f"utm_content={defaults['utm_content']}"
    )


def _reresolve_dropped_interests(
    account_id: str,
    token: str,
    popped_slice: Any,
) -> Optional[Dict[str, Any]]:
    """Try to rebuild a flexible_spec slice from the names of the IDs Meta rejected.

    The retry loop currently pops the whole failing slice and drops it. That
    silently loses interests the user explicitly briefed, just because Meta
    deprecated the IDs the LLM emitted. Before falling back to drop, we look at
    the *names* on the popped items, re-query ``batch_interest_search`` for
    fresh IDs (Meta's search endpoint only returns currently-valid IDs), and
    build a replacement slice. Behaviors / demographics / etc. don't have a
    name-search counterpart in this codebase so they keep the old drop
    behaviour.

    Returns the new slice dict to splice back in, or ``None`` when nothing
    could be rescued (caller drops as before).
    """
    if not isinstance(popped_slice, dict):
        return None
    rebuilt: Dict[str, Any] = {}
    for axis_key in _RERESOLVE_INTEREST_AXES:
        items = popped_slice.get(axis_key)
        if not isinstance(items, list) or not items:
            continue
        # Names come from the brief — IDs were the failing part, names should
        # still be human-readable.
        names = []
        for item in items:
            if isinstance(item, dict):
                nm = (item.get("name") or item.get("query") or "").strip()
                if nm:
                    names.append(nm)
        if not names:
            continue
        try:
            search = services.batch_interest_search(account_id, token, names)
        except Exception as exc:
            logger.warning("batch_interest_search re-resolve failed: %s", exc)
            return None
        if not isinstance(search, dict):
            return None
        bad = _learned_bad_interest_ids()
        fresh_items: List[Dict[str, Any]] = []
        for entry in search.get("found", []) or []:
            if not isinstance(entry, dict):
                continue
            iid = str(entry.get("id") or "").strip()
            if not iid or not iid.isdigit() or iid in bad:
                continue
            fresh_items.append({"id": iid, "name": entry.get("name") or entry.get("query") or ""})
        if fresh_items:
            rebuilt[axis_key] = fresh_items

    # Preserve any axes we don't re-resolve (behaviors, demographics) so the
    # slice retains shape if Meta accepts those alongside fresh interests.
    for axis_key, axis_val in popped_slice.items():
        if axis_key in rebuilt or axis_key in _RERESOLVE_INTEREST_AXES:
            continue
        if axis_val:
            rebuilt[axis_key] = axis_val

    return rebuilt or None


def create_adset_with_targeting_retries(
    account_id: str,
    token: str,
    campaign_id: str,
    name: str,
    status: str,
    billing_event: str,
    optimization_goal: Optional[str],
    targeting: Dict[str, Any],
    daily_budget: Optional[int],
    lifetime_budget: Optional[int],
    bid_strategy: Optional[str],
    promoted_object: Optional[Dict],
    page_id: Optional[str],
    destination_type: Optional[str],
    start_time: Optional[str],
    end_time: Optional[str],
    fixes: List[str],
    max_retries: int = 5,
    strict: bool = False,
    bid_amount: Optional[int] = None,
    campaign_daily_budget: Optional[int] = None,
    campaign_lifetime_budget: Optional[int] = None,
    bid_strategy_for_default: Optional[str] = None,
    frequency_control_specs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    t = dict(targeting) if isinstance(targeting, dict) else {}
    attempt = 0
    last_err = None
    last_resp: Any = None
    cap = 1 if strict else max(1, int(max_retries))
    reresolve_attempts_remaining = _MAX_RERESOLVE_ATTEMPTS
    cbo_strategy_pinned = False
    while attempt < cap:
        attempt += 1
        resp = services.create_adset(
            account_id,
            token,
            campaign_id,
            name,
            status=status,
            daily_budget=daily_budget,
            lifetime_budget=lifetime_budget,
            billing_event=billing_event or "IMPRESSIONS",
            optimization_goal=optimization_goal,
            targeting=t,
            bid_strategy=bid_strategy,
            bid_amount=bid_amount,
            promoted_object=promoted_object,
            page_id=page_id,
            destination_type=destination_type,
            start_time=start_time,
            end_time=end_time,
            frequency_control_specs=frequency_control_specs,
        )
        last_resp = resp
        if not _has_api_error(resp):
            if attempt > 1:
                fixes.append(f"Ad set created after {attempt} targeting retries")
            return resp
        last_err = _api_error_message(resp)
        if strict:
            break
        em = last_err.lower()
        # Pull the Meta subcode from the response so we can route on the
        # canonical signal, not on free-text keywords. Subcodes 1487079 /
        # 1487694 / 2446395 all mean "deprecated/unavailable targeting category"
        # — they must trigger the same flexible_spec drain regardless of message.
        err_obj = resp.get("error") if isinstance(resp, dict) else {}
        if not isinstance(err_obj, dict):
            err_obj = {}
        inner = err_obj.get("last_response", {}) if isinstance(err_obj.get("last_response"), dict) else err_obj
        if isinstance(inner, dict) and isinstance(inner.get("error"), dict):
            inner = inner["error"]
        subcode = None
        if isinstance(inner, dict):
            try:
                subcode = int(inner.get("error_subcode")) if inner.get("error_subcode") is not None else None
            except (TypeError, ValueError):
                subcode = None
        is_deprecated_category = subcode in (1487079, 1487694, 2446395)
        is_targeting_keyword = ("flexible" in em or "interest" in em or "targeting" in em
                                or "behavior" in em or "education" in em
                                or "category no longer" in em or "no longer available" in em)

        # Universal CBO→ABO fallback: subcode 1885649 ("Ad set budget is missing")
        # means Meta wants the budget on the ad set even though the campaign was
        # created with CBO. Push the campaign's budget down to the ad set and
        # retry. Single-adset shapes work fine this way.
        if (subcode == 1885649
                and (campaign_daily_budget or campaign_lifetime_budget)
                and not daily_budget and not lifetime_budget):
            if campaign_daily_budget:
                daily_budget = int(campaign_daily_budget)
                fixes.append(
                    f"CBO→ABO fallback: copied campaign.daily_budget={daily_budget} "
                    f"-> adset.daily_budget after Meta subcode 1885649 "
                    f"('Ad set budget is missing')"
                )
            elif campaign_lifetime_budget:
                lifetime_budget = int(campaign_lifetime_budget)
                fixes.append(
                    f"CBO→ABO fallback: copied campaign.lifetime_budget={lifetime_budget} "
                    f"-> adset.lifetime_budget after Meta subcode 1885649"
                )
            continue

        # Universal default bid_amount injection: subcodes 1815857 / 2490487 mean
        # "bid_amount required". When the user picked a bid-cap strategy without
        # supplying bid_amount, inject Meta's documented rule of thumb (5% of
        # daily_budget, clamped) and retry. Strategy is preserved.
        _BID_CAP = {"COST_CAP", "LOWEST_COST_WITH_BID_CAP", "TARGET_COST"}
        _bs_norm = str(bid_strategy or bid_strategy_for_default or "").strip().upper()
        if subcode in (1815857, 2490487) and not bid_amount and _bs_norm in _BID_CAP:
            db = int(daily_budget or campaign_daily_budget or 0)
            if db > 0:
                injected = max(100, min(db // 20, db // 2))
                bid_amount = injected
                fixes.append(
                    f"Injected default adset.bid_amount={injected} "
                    f"(5% of daily_budget={db}) after Meta subcode {subcode} "
                    f"required bid_amount for bid_strategy={_bs_norm}"
                )
                continue

        # CBO-default trap: the brief never chose a bid-cap strategy, yet Meta
        # demands bid_amount — the campaign (typically lifetime-budget CBO,
        # created without bid_strategy) got a Meta-assigned default that
        # requires a cap. Pin the campaign to LOWEST_COST_WITHOUT_CAP and
        # retry the ad set unchanged (under CBO the strategy lives on the
        # campaign, not the ad set).
        if (subcode in (1815857, 2490487) and not bid_amount
                and _bs_norm not in _BID_CAP and not cbo_strategy_pinned
                and not strict):
            cbo_strategy_pinned = True
            u = services.update_campaign(
                campaign_id, token, bid_strategy="LOWEST_COST_WITHOUT_CAP")
            if isinstance(u, dict) and not u.get("error"):
                fixes.append(
                    f"Meta subcode {subcode} (bid_amount required) with no bid-cap "
                    "strategy in the brief: pinned campaign bid_strategy="
                    "LOWEST_COST_WITHOUT_CAP and retried the ad set."
                )
                continue
            fixes.append(
                f"Meta subcode {subcode}: campaign bid_strategy pin failed "
                f"({(u.get('error') or {}).get('message') if isinstance(u, dict) else u}); not retrying this path."
            )

        # Strip flexible_spec partially. We learn the IDs in the popped slice
        # so future syncs (any account) skip them up front.
        flex = t.get("flexible_spec")
        if isinstance(flex, list) and flex and (is_deprecated_category or is_targeting_keyword):
            flex = list(flex)
            popped = flex.pop()
            # Learn every ID in the popped slice into the per-account learn-cache.
            learned_ids: List[str] = []
            if isinstance(popped, dict):
                for axis_key in ("interests", "behaviors", "demographics", "work_employers",
                                 "work_positions", "education_majors", "education_schools"):
                    axis_val = popped.get(axis_key)
                    if isinstance(axis_val, list):
                        for item in axis_val:
                            iid = None
                            if isinstance(item, dict):
                                iid = str(item.get("id") or "").strip()
                            elif item is not None:
                                iid = str(item).strip()
                            if iid and iid.isdigit():
                                learned_ids.append(iid)
            if learned_ids:
                _record_bad_interest_ids(learned_ids)
                fixes.append(
                    f"Learned {len(learned_ids)} bad targeting ID(s) from Meta rejection "
                    f"(subcode={subcode}); future syncs will skip them: {','.join(learned_ids[:5])}"
                    + ("..." if len(learned_ids) > 5 else "")
                )
            # Before silently dropping the popped slice, try to rescue any
            # named interests on it via batch_interest_search — the names are
            # what the user briefed, the IDs are what Meta deprecated. New
            # search results give us currently-valid IDs (and we filter them
            # against the known-bad cache). Capped per ad set so a Meta-side
            # outage during search can't loop forever.
            rebuilt_slice = None
            if reresolve_attempts_remaining > 0:
                reresolve_attempts_remaining -= 1
                rebuilt_slice = _reresolve_dropped_interests(account_id, token, popped)
            if rebuilt_slice:
                flex.append(rebuilt_slice)
                rescued_ids = [
                    str(x.get("id"))
                    for ax in _RERESOLVE_INTEREST_AXES
                    for x in (rebuilt_slice.get(ax) or [])
                    if isinstance(x, dict) and x.get("id")
                ]
                fixes.append(
                    f"Retry ad set: re-resolved {len(rescued_ids)} interest(s) via "
                    f"batch_interest_search instead of dropping (subcode={subcode}): "
                    f"{','.join(rescued_ids[:5])}"
                    + ("..." if len(rescued_ids) > 5 else "")
                )
                t["flexible_spec"] = flex
                continue
            t["flexible_spec"] = flex if flex else None
            if not flex:
                t.pop("flexible_spec", None)
            fixes.append(
                f"Retry ad set: dropped last flexible_spec slice "
                f"(subcode={subcode}, msg={last_err[:100]})"
            )
            continue

        if "placement" in em or "position" in em:
            t, pf = apply_automatic_placements(t)
            fixes.extend(pf)
            fixes.append(f"Retry ad set after placement fallback ({last_err[:120]})")
            continue
        break
    return {"error": {"message": last_err or "ad set creation failed", "last_response": last_resp}}


def build_link_object_story_spec(
    page_id: str,
    link: str,
    message: str,
    cta_type: Optional[str],
    image_hash: Optional[str],
    strict: bool = False,
    cta_value: Optional[Dict[str, Any]] = None,
    lead_gen_form_id: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Dict[str, Any]:
    spec: Dict[str, Any] = {"page_id": str(page_id).strip(), "link_data": {"link": link, "message": message or ""}}
    if image_hash:
        spec["link_data"]["image_hash"] = str(image_hash).strip()
    # Headline (Meta's link_data.name) and description (link_data.description)
    # render as separate elements in the ad preview. Without them the brief's
    # "headline" / "description" silently vanish — Meta accepts the post but
    # the ad shows just the body copy and image.
    if name and str(name).strip():
        spec["link_data"]["name"] = str(name).strip()
    if description and str(description).strip():
        spec["link_data"]["description"] = str(description).strip()
    if strict and not (cta_type and str(cta_type).strip()):
        raise ValueError("strict_mode: document.creative.cta_type is required for LINK_AD")
    ct = (str(cta_type).strip().upper() if cta_type and str(cta_type).strip() else DEFAULT_CTA)
    # Build CTA value. For lead-gen, Meta REQUIRES the form id inside
    # call_to_action.value.lead_gen_form_id (NOT in promoted_object). When the
    # caller passes lead_gen_form_id, merge it into the value dict and force
    # CTA type to SIGN_UP if no specific lead-friendly CTA was set.
    value: Dict[str, Any] = dict(cta_value) if isinstance(cta_value, dict) else {"link": link}

    # CALL_NOW normalization. Meta REJECTS call_to_action.value.phone_number
    # with code=100 ("Invalid keys 'phone_number'"). The accepted shape for
    # CALL_NOW on a link ad is value={"link": "tel:+91..."}. Rewrite the
    # intuitive {"phone_number": "..."} shape that briefs commonly use.
    if ct == "CALL_NOW":
        raw_phone = value.pop("phone_number", None) or value.pop("phone", None)
        if raw_phone:
            digits = re.sub(r"[^\d+]", "", str(raw_phone))
            if digits and not digits.startswith("+"):
                digits = "+" + digits
            value["link"] = f"tel:{digits}"
        elif "link" not in value:
            value["link"] = link

    if lead_gen_form_id and str(lead_gen_form_id).strip():
        value["lead_gen_form_id"] = str(lead_gen_form_id).strip()
        # Lead-friendly CTAs Meta accepts on lead ads
        _LEAD_CTAS = {"SIGN_UP", "GET_QUOTE", "SUBSCRIBE", "APPLY_NOW", "LEARN_MORE", "BOOK_TRAVEL", "DOWNLOAD"}
        if ct not in _LEAD_CTAS:
            ct = "SIGN_UP"
    spec["link_data"]["call_to_action"] = {"type": ct, "value": value}
    return spec


def _compute_document_hash(doc: Dict[str, Any]) -> str:
    """SHA-256 hash of the document for change detection.

    The fields the strict-mode gate writes into ``options`` are EXCLUDED from
    the canonical representation we hash. Otherwise the caller's correct
    sequence - call preview, get hash H, set ``options.strict_preview_document_hash=H``,
    call sync - would mutate the document body between preview and sync and
    cause an infinite mismatch loop.

    Excluded keys (under ``options``): ``strict_preview_document_hash``,
    ``strict_preview_hash`` (alias), ``strict_validator_version``. When
    stripping leaves the options dict empty AND the original doc did not
    carry an options key (or carried only excluded keys), the empty options
    dict is also stripped so a fresh document and the same document with
    only the hash field added produce identical hashes.
    """
    import copy as _copy
    cleaned = _copy.deepcopy(doc) if isinstance(doc, dict) else doc
    if isinstance(cleaned, dict):
        opts = cleaned.get("options")
        if isinstance(opts, dict):
            for k in ("strict_preview_document_hash",
                      "strict_preview_hash",
                      "strict_validator_version"):
                opts.pop(k, None)
            if not opts:
                cleaned.pop("options", None)
    raw = json.dumps(cleaned, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _save_doc_mapping(
    doc_id: str, user_id: str, account_id: str,
    campaign_id: str, adset_id: str, ad_id: str, creative_id: str,
    doc_hash: str, doc_json: str,
):
    """Create or update doc_id mapping."""
    if not doc_id:
        return
    from .models import FacebookAdsDocMapping
    account_id_clean = account_id.replace("act_", "")
    FacebookAdsDocMapping.objects.update_or_create(
        doc_id=doc_id, user_id=user_id or "", account_id=account_id_clean,
        defaults={
            "campaign_id": campaign_id or "",
            "adset_id": adset_id or "",
            "ad_id": ad_id or "",
            "creative_id": creative_id or "",
            "document_hash": doc_hash,
            "document_json": doc_json[:32000],
            "status": "active",
        },
    )


def _format_ad_plan_summary(doc: Dict[str, Any], targeting: Dict[str, Any]) -> str:
    """Human-readable plan for user approval (no secrets)."""
    lines: List[str] = []
    camp = doc.get("campaign") or {}
    ads = doc.get("adset") or {}
    cr = doc.get("creative") or {}
    trk = doc.get("tracking") if isinstance(doc.get("tracking"), dict) else {}
    mode = str(cr.get("mode") or "LINK_AD").upper()

    lines.append("## Campaign")
    lines.append(f"- Name: {camp.get('name')}")
    lines.append(f"- Objective: {camp.get('objective')}")
    lines.append(
        "- Sync: each run creates a new Meta campaign + ad set + ad from this document only (no reuse of old Facebook entities)."
    )
    lines.append(f"- Status: {camp.get('status') or 'PAUSED'}")
    if camp.get("special_ad_categories"):
        lines.append(f"- Special ad categories: {camp.get('special_ad_categories')}")
    if camp.get("special_ad_category_country"):
        lines.append(f"- Special ad countries: {camp.get('special_ad_category_country')}")
    if camp.get("is_adset_budget_sharing_enabled"):
        lines.append("- CBO / shared ad set budget: enabled")
    if camp.get("daily_budget") is not None:
        lines.append(f"- Campaign daily_budget (cents): {camp.get('daily_budget')}")
    if camp.get("lifetime_budget") is not None:
        lines.append(f"- Campaign lifetime_budget (cents): {camp.get('lifetime_budget')}")

    lines.append("")
    lines.append("## Ad set")
    lines.append(f"- Name: {ads.get('name')}")
    lines.append(f"- Optimization goal: {ads.get('optimization_goal')}")
    lines.append(f"- Billing: {ads.get('billing_event') or 'IMPRESSIONS'}")
    if ads.get("daily_budget") is not None:
        lines.append(f"- Ad set daily_budget (cents): {ads.get('daily_budget')}")
    if ads.get("lifetime_budget") is not None:
        lines.append(f"- Ad set lifetime_budget (cents): {ads.get('lifetime_budget')}")
    lines.append(f"- Destination type: {ads.get('destination_type') or '(none / API default)'}")
    if ads.get("start_time") or ads.get("end_time"):
        lines.append(f"- Schedule: {ads.get('start_time')} → {ads.get('end_time')}")

    lines.append("")
    lines.append("## Creative")
    lines.append(f"- Mode: {mode}")
    if cr.get("name"):
        lines.append(f"- Creative name: {cr.get('name')}")
    if mode == "EXISTING_POST":
        lines.append(f"- object_story_id: {cr.get('object_story_id')}")
        if cr.get("authorization_category"):
            lines.append(f"- authorization_category: {cr.get('authorization_category')}")
    elif mode == "WHATSAPP":
        lines.append(f"- WhatsApp: {cr.get('whatsapp_phone_e164') or cr.get('whatsapp_number')}")
    else:
        lines.append(f"- link_url: {cr.get('link_url')}")
        lines.append(f"- cta_type: {cr.get('cta_type') or DEFAULT_CTA}")
        msg = str(cr.get("message") or "")
        if msg:
            lines.append(f"- message (excerpt): {msg[:200]}{'…' if len(msg) > 200 else ''}")
    if cr.get("page_id"):
        lines.append(f"- page_id: {cr.get('page_id')}")

    if trk.get("pixel_id"):
        lines.append("")
        lines.append("## Tracking")
        lines.append(f"- pixel_id: {trk.get('pixel_id')}")
        lines.append(f"- custom_event_type: {trk.get('custom_event_type') or 'PAGE_VIEW'}")

    tgt_json = json.dumps(targeting or {}, indent=2, default=str)
    max_t = 8000
    lines.append("")
    lines.append("## Resolved targeting (as sent to Meta)")
    if len(tgt_json) > max_t:
        lines.append(tgt_json[:max_t] + "\n… [truncated]")
    else:
        lines.append(tgt_json)

    return "\n".join(lines)


def _preview_payload_from_doc(
    account_id: str,
    token: str,
    doc: Dict[str, Any],
    vfixes: List[str],
    user_id: str = "",
    strict_mode: bool = False,
    user_input_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    tfixes: List[str] = []
    preview_blocking_error: Optional[Dict[str, str]] = None
    # Run the same auto-resolution the sync path runs, so the preview shows
    # the user the actual document sync will use (page, pixel, interests,
    # cities, locales, UTMs all filled in). Skipped in strict mode — strict
    # preview must use exactly what the caller supplied.
    if not strict_mode:
        try:
            _autoresolve_phase(doc, account_id, token, tfixes)
        except ValueError as exc:
            # _autoresolve_page raises ValueError on page-name mismatch and
            # other unrecoverable ambiguities. Surface as a blocking-style
            # preview error so the user sees the problem before sync.
            preview_blocking_error = {"message": str(exc)}
        # Deterministic /search-based revalidation: catches deprecated IDs
        # /targetingvalidation lies about. Runs after auto-resolve so any
        # name-only entries already have IDs to compare against.
        _revalidate_flex_via_search(doc, account_id, token, tfixes)
        # Strip leftover pixel_id from briefs that carry one for CALL goals.
        # Meta rejects pixel + PHONE_CALL with subcode 1815704; pixel won't
        # actually attach for CALL anyway (see _NO_PIXEL_ATTACH_GOALS).
        _strip_pixel_for_call_goals(doc, tfixes)
        # When call campaigns end up with very thin flexible_spec after
        # validation drops, Meta's call-optimizer underdelivers — enable
        # Advantage+ Audience to widen reach.
        _enable_advantage_audience_when_sparse(doc, tfixes)
        crp_prev = doc.get("creative") if isinstance(doc.get("creative"), dict) else {}
        adp_prev = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
        sm_chk = str(crp_prev.get("mode") or "LINK_AD").upper()
        if crp_prev.get("object_story_id"):
            sm_chk = "EXISTING_POST"
        elif sm_chk == "WHATSAPP" or str(adp_prev.get("destination_type") or "").upper() == "WHATSAPP":
            sm_chk = "WHATSAPP"
        else:
            sm_chk = "LINK_AD"
        if sm_chk == "LINK_AD":
            _apply_utm_params(doc, tfixes)
    targeting = build_targeting_from_document(account_id, token, doc, tfixes, strict=strict_mode)
    saved_preview = str((doc.get("adset") or {}).get("saved_audience_id") or "").strip()
    if saved_preview:
        opts_pv = doc.get("options") if isinstance(doc.get("options"), dict) else {}
        aa_pv = (doc.get("adset") or {}).get("advantage_audience")
        if aa_pv is None:
            aa_pv = opts_pv.get("saved_audience_advantage_audience", 0)
        try:
            aa_pv_i = int(aa_pv) if aa_pv is not None else 0
        except (TypeError, ValueError):
            aa_pv_i = 0
        merged_pv = services.merge_saved_audience_targeting_with_placements(
            token, saved_preview, targeting, aa_pv_i
        )
        if not _has_api_error(merged_pv) and isinstance(merged_pv, dict):
            if merged_pv.get("geo_locations") or merged_pv.get("custom_audiences") or merged_pv.get("flexible_spec"):
                targeting = merged_pv
                tfixes.append(
                    f"Preview targeting includes pre-merged saved_audience_id={saved_preview} (same as first ad set create when sync runs)."
                )
        elif _has_api_error(merged_pv):
            tfixes.append(
                f"Preview: could not pre-merge saved audience ({_api_error_message(merged_pv)}); sync may use post-ad-set apply."
            )

    crp = doc.get("creative") or {}
    adp = doc.get("adset") or {}
    sm_prev = str(crp.get("mode") or "LINK_AD").upper()
    if crp.get("object_story_id"):
        sm_prev = "EXISTING_POST"
    elif sm_prev == "WHATSAPP" or str(adp.get("destination_type") or "").upper() == "WHATSAPP":
        sm_prev = "WHATSAPP"
    else:
        sm_prev = "LINK_AD"
    vr_pv = meta_validation.validate_document_for_sync(
        doc, strict=strict_mode, creative_mode=sm_prev, destination_type=adp.get("destination_type")
    )
    tfixes.extend(vr_pv.get("validation_notes") or [])

    auto_fixes = list(vfixes) + tfixes
    summary = _format_ad_plan_summary(doc, targeting)
    doc_hash = _compute_document_hash(doc)
    out: Dict[str, Any] = {
        "ok": True,
        "phase": "preview_only",
        "confirmation_required": True,
        "message": (
            "Nothing was created in Meta. Show summary_for_user to the user. After they explicitly approve, "
            "call facebook_sync_document_campaign again with the same document and user_confirmed=true, "
            "or set options.user_confirmed_execute to true in the document."
        ),
        "summary_for_user": summary,
        "normalized_document": doc,
        "resolved_targeting": targeting,
        "auto_fixes": auto_fixes,
        "document_hash_sha256": doc_hash,
    }
    # Promote page-resolution (or similar) ValueError to a blocking error
    # so users see it in preview before sync ever runs.
    effective_blocking = vr_pv.get("blocking_error") or preview_blocking_error
    if effective_blocking:
        out["sync_would_fail"] = True
        out["sync_blocking_error"] = effective_blocking
        out["why_failed"] = api_errors.explain_graph_error(effective_blocking)
        # If the blocking error carries structured user_options (e.g. from
        # validate_objective_and_optimization), surface them at the top so the
        # agent can show them to the user instead of trying silent fixes.
        if isinstance(effective_blocking, dict):
            opts = effective_blocking.get("user_options")
            if isinstance(opts, list) and opts:
                out["user_options"] = opts
        out["message"] = (
            "Preview only: this document would fail validation on sync (see sync_blocking_error / user_options). "
            "Fix the document — or pick one of user_options — before calling facebook_sync_document_campaign."
        )
    if user_id:
        out["user_id"] = user_id
    if strict_mode:
        _mark_strict_preview_ok(account_id, doc_hash)
        out["strict_mode"] = True
        out["strict_preview_token_cached"] = True
        out["strict_validator_version"] = STRICT_VALIDATOR_VERSION
        out["strict_meta_platform_limitations"] = list(STRICT_META_PLATFORM_LIMITATIONS)
        out["message"] = (
            "Nothing was created in Meta (strict preview). Show summary_for_user. On sync, set "
            "options.strict_preview_document_hash to document_hash_sha256 from this response (same document). "
            "Sync is rejected without a matching recent strict preview or if the document hash differs. "
            "Strict defaults targeting to RAW (deterministic); RESOLVE requires options.allow_targeting_resolve. "
            f"strict_validator_version={STRICT_VALIDATOR_VERSION} — set options.strict_validator_version to match when you want a hard contract check."
        )
        out["strict_debug"] = {
            "user_input": user_input_snapshot,
            "document_used": doc,
            "final_targeting": targeting,
        }
    return out


def preview_document_campaign(account_id: str, token: str, document: Any, user_id: str = "") -> Dict[str, Any]:
    """
    Validate document, resolve targeting (search APIs), return summary only — no campaign/ad/creative writes.
    Use for agent flows: show plan → user confirms → sync_document_campaign.
    """
    if isinstance(document, str):
        document = json.loads(document)
    if not isinstance(document, dict):
        raise ValueError("document must be a JSON object")
    user_snap = json.loads(json.dumps(document))
    strict = strict_mode_from_document(document)
    if strict:
        doc, vfixes = validate_document_strict_passthrough(document)
    else:
        doc, vfixes = validate_and_normalize_document(document, account_id=account_id, token=token)
    return _preview_payload_from_doc(
        account_id, token, doc, list(vfixes), user_id,
        strict_mode=strict, user_input_snapshot=user_snap if strict else None,
    )


def sync_document_campaign(
    account_id: str, token: str, document: Any, user_id: str = "", user_confirmed: bool = False,
) -> Dict[str, Any]:
    """
    Create one new campaign each run (document objective), new ad set, creative, ad; attach pixel; verify.
    Rolls back (deletes) the campaign if a later step fails unless options.keep_campaign_on_sync_failure is true.
    `document` may be a dict or JSON string.

    Does not reuse an existing campaign by doc_id mapping or by campaign name + objective (avoids stale objectives
    and Meta errors when the document objective differs from an old campaign).

    If `doc_id` is present, the mapping row is still updated after success with the new entity ids (tracking only).

    If `options.require_user_confirmation` is true, this call only creates in Meta when `user_confirmed` is true
    or `options.user_confirmed_execute` is true; otherwise returns the same preview payload as
    `preview_document_campaign` (phase preview_only).

    Deprecated no-op: options.always_create_new_campaign (behavior is always \"new campaign\" now).

    strict_mode (document.strict_mode or options.strict_mode): passthrough document (no normalize), no idempotency
    cache (see tool_dispatch), targeting/placement resolution errors raise, no ad set retry fallbacks, no verify/pixel
    auto-fixes, exact ad set name (no timestamp suffix). Optional strict_debug on success with user_input vs payloads.
    """
    auto_fixes: List[str] = []
    if isinstance(document, str):
        document = json.loads(document)
    if not isinstance(document, dict):
        raise ValueError("document must be a JSON object")

    user_input_snapshot = json.loads(json.dumps(document))
    strict = strict_mode_from_document(document)
    try:
        if strict:
            doc, vfixes = validate_document_strict_passthrough(document)
        else:
            doc, vfixes = validate_and_normalize_document(document, account_id=account_id, token=token)
    except ValueError as exc:
        # validate_and_normalize_document raises ValueError for hard-fails
        # like "image could not be resolved" — turn it into a structured
        # error response instead of an exception bubble.
        return _sync_step_error(auto_fixes, {"message": str(exc)}, "validate_document")
    options = doc.get("options") if isinstance(doc.get("options"), dict) else {}
    doc_hash = _compute_document_hash(doc)
    req_confirm = _truthy_doc_option(options.get("require_user_confirmation"))
    doc_execute = _truthy_doc_option(options.get("user_confirmed_execute"))
    if req_confirm and not (bool(user_confirmed) or doc_execute):
        return _preview_payload_from_doc(
            account_id, token, doc, list(vfixes), user_id,
            strict_mode=strict, user_input_snapshot=user_input_snapshot if strict else None,
        )

    auto_fixes.extend(vfixes)

    if strict:
        opt_h = str(
            options.get("strict_preview_document_hash") or options.get("strict_preview_hash") or ""
        ).strip()
        if not opt_h:
            _gmsg = (
                "Invalid combination: strict_mode sync without strict preview proof.\n"
                "Use: call facebook_preview_document_campaign with this document, then set "
                "options.strict_preview_document_hash to the preview response's document_hash_sha256."
            )
            return {
                "ok": False,
                "phase": "strict_preview_required",
                "auto_fixes": auto_fixes,
                "error": {"message": _gmsg},
                "why_failed": api_errors.explain_graph_error({"message": _gmsg}),
                "relaxed_fix_suggestions": suggest_relaxed_fixes_for_strict_error(_gmsg),
                "strict_validator_version": STRICT_VALIDATOR_VERSION,
                "step": "strict_preview_gate",
                "document_hash_sha256": doc_hash,
            }
        if opt_h.lower() != doc_hash.lower():
            _gmsg = (
                "Invalid combination: options.strict_preview_document_hash does not match this document's SHA-256.\n"
                f"Use: re-run facebook_preview_document_campaign after any document change; expected hash for this payload: {doc_hash!r}."
            )
            return {
                "ok": False,
                "phase": "strict_preview_hash_mismatch",
                "auto_fixes": auto_fixes,
                "error": {"message": _gmsg},
                "why_failed": api_errors.explain_graph_error({"message": _gmsg}),
                "relaxed_fix_suggestions": suggest_relaxed_fixes_for_strict_error(_gmsg),
                "strict_validator_version": STRICT_VALIDATOR_VERSION,
                "step": "strict_preview_gate",
                "document_hash_sha256": doc_hash,
            }
        if not _strict_preview_ok_in_cache(account_id, doc_hash):
            _gmsg = (
                "Invalid combination: strict preview is missing or expired for this account/document hash.\n"
                "Use: call facebook_preview_document_campaign again with the same document, then sync within the cache TTL."
            )
            return {
                "ok": False,
                "phase": "strict_preview_stale",
                "auto_fixes": auto_fixes,
                "error": {"message": _gmsg},
                "why_failed": api_errors.explain_graph_error({"message": _gmsg}),
                "relaxed_fix_suggestions": suggest_relaxed_fixes_for_strict_error(_gmsg),
                "strict_validator_version": STRICT_VALIDATOR_VERSION,
                "step": "strict_preview_gate",
                "document_hash_sha256": doc_hash,
            }

    # Auto-resolution phase: fill in everything the brief left implicit
    # (page, pixel, interest IDs, city keys, locale IDs) so the document
    # the rest of this function sees is "complete." Mutates ``doc`` in
    # place; reports every pick to ``auto_fixes``. Runs before validation
    # so the validators see resolved values, and before
    # ``build_targeting_from_document`` so its filters operate on real
    # Meta IDs. Skipped in strict mode — strict callers are expected to
    # supply a fully-formed document already (preview will surface gaps).
    if not strict:
        try:
            _autoresolve_phase(doc, account_id, token, auto_fixes)
        except ValueError as exc:
            # _autoresolve_page raises ValueError on unrecoverable page-name
            # ambiguity (mismatch / multiple matches / no name + multiple
            # pages). Convert to a structured sync error so the caller sees
            # a clear message instead of a 500.
            return _sync_step_error(auto_fixes, {"message": str(exc)}, "auto_resolution")
        # Deterministic /search-based revalidation: catches deprecated IDs
        # that /targetingvalidation lies about. Runs after auto-resolve so
        # any name-only entries already have IDs to compare against, and
        # before build_targeting_from_document so its filters never see
        # known-deprecated IDs.
        _revalidate_flex_via_search(doc, account_id, token, auto_fixes)
        # Strip leftover pixel_id from briefs that carry one for CALL goals.
        # Meta rejects pixel + PHONE_CALL with subcode 1815704; pixel won't
        # actually attach for CALL anyway (see _NO_PIXEL_ATTACH_GOALS).
        _strip_pixel_for_call_goals(doc, auto_fixes)
        # When call campaigns end up with very thin flexible_spec after
        # validation drops, Meta's call-optimizer underdelivers — enable
        # Advantage+ Audience to widen reach.
        _enable_advantage_audience_when_sparse(doc, auto_fixes)

    camp = doc["campaign"]
    ads_cfg = doc["adset"]
    creative_cfg = doc["creative"]
    tracking = doc.get("tracking") if isinstance(doc.get("tracking"), dict) else {}
    ad_cfg = doc.get("ad") if isinstance(doc.get("ad"), dict) else {}

    # Universal mode detection — same logic as the strict / non-strict
    # validators, so what the sync flow uses lines up with what the
    # validators expect. Covers all ad types, not just LINK_AD vs
    # EXISTING_POST vs WHATSAPP.
    sync_mode_v = _infer_creative_mode(doc) or str(creative_cfg.get("mode") or "").upper() or "LINK_AD"

    # Auto-build UTM params on creative.link_url (LINK_AD only, no-op when
    # already set). Done after auto-resolve so the campaign/ad names are
    # final, and after sync_mode_v is decided so we know it's LINK_AD.
    if not strict and sync_mode_v == "LINK_AD":
        _apply_utm_params(doc, auto_fixes)
        creative_cfg = doc["creative"]  # refresh after mutation

    vr_pre = meta_validation.validate_document_for_sync(
        doc, strict=strict, creative_mode=sync_mode_v, destination_type=ads_cfg.get("destination_type")
    )
    if vr_pre.get("blocking_error"):
        return _sync_step_error(auto_fixes, vr_pre["blocking_error"], "validate_document")
    auto_fixes.extend(vr_pre.get("validation_notes") or [])

    # Pre-flight creative validation: catch missing-required-field problems
    # BEFORE any Meta object is created. Previously, a missing link_url on
    # LINK_AD would only fail at the creative-create step, leaving an orphan
    # campaign + ad set behind. Validate required fields per mode upfront so
    # we fail fast with no side effects.
    creative_block = _validate_creative_required_fields(
        creative_cfg,
        sync_mode_v,
        optimization_goal=ads_cfg.get("optimization_goal"),
        ad_account_has_media=False,
    )
    if creative_block:
        return _sync_step_error(auto_fixes, creative_block, "validate_creative")

    # --- doc_id: persisted after success for audit only (never used to reuse Meta ad set / ad / campaign) ---
    doc_id = str(doc.get("doc_id") or options.get("doc_id") or "").strip() or None
    mapping_action = None

    campaign_name = str(camp["name"]).strip()
    is_cbo = bool(camp.get("is_adset_budget_sharing_enabled"))
    doc_objective = str(camp.get("objective") or "").strip()
    if not strict:
        auto_fixes.append(
            "Document sync: creates one Meta campaign + ad set + creative + ad. "
            "If a later step fails, the campaign is deleted by default (no orphan duplicate). "
            "Set options.keep_campaign_on_sync_failure=true to keep it for debugging."
        )

    try:
        targeting = build_targeting_from_document(account_id, token, doc, auto_fixes, strict=strict)
    except ValueError as exc:
        # Hard rejections (e.g. fabricated interest IDs) — bail BEFORE any
        # campaign is created so we never leave an orphan.
        return _sync_step_error(auto_fixes, {"message": str(exc)}, "targeting_validation")

    saved_aud_pre = str(ads_cfg.get("saved_audience_id") or "").strip()
    pre_merged_saved_audience = False
    if saved_aud_pre:
        opts_doc_sa = doc.get("options") if isinstance(doc.get("options"), dict) else {}
        aa_raw_sa = ads_cfg.get("advantage_audience")
        if aa_raw_sa is None:
            aa_raw_sa = opts_doc_sa.get("saved_audience_advantage_audience", 0)
        try:
            advantage_sa = int(aa_raw_sa) if aa_raw_sa is not None else 0
        except (TypeError, ValueError):
            advantage_sa = 0
        merged_sa = services.merge_saved_audience_targeting_with_placements(
            token, saved_aud_pre, targeting, advantage_sa
        )
        if not _has_api_error(merged_sa) and isinstance(merged_sa, dict):
            has_audience = bool(
                merged_sa.get("geo_locations")
                or merged_sa.get("custom_audiences")
                or merged_sa.get("flexible_spec")
            )
            if has_audience:
                targeting = merged_sa
                pre_merged_saved_audience = True
                auto_fixes.append(
                    f"Pre-merged saved_audience_id={saved_aud_pre} into initial ad set targeting (document placements preserved)."
                )
        else:
            em_sa = _api_error_message(merged_sa) if _has_api_error(merged_sa) else "incomplete audience"
            auto_fixes.append(
                f"Saved audience pre-merge unavailable ({em_sa}); will apply saved_audience_id after ad set create."
            )

    adset_name = str(ads_cfg["name"]).strip()

    daily_b = ads_cfg.get("daily_budget")
    lifetime_b = ads_cfg.get("lifetime_budget")
    if is_cbo or camp.get("daily_budget") or camp.get("lifetime_budget"):
        daily_b, lifetime_b = None, None

    promoted_object = ads_cfg.get("promoted_object")
    if isinstance(promoted_object, str):
        promoted_object = _parse_json_maybe(promoted_object)
    page_id_creative = str(creative_cfg.get("page_id") or ads_cfg.get("page_id") or "").strip() or None

    # --- IG-media page consistency guard (Meta issue 1885029 "Pages don't match") ---
    # When the creative promotes an existing IG post, the ad's Page MUST be the
    # Page linked to the media owner's IG Business Account. Briefs often name a
    # different Page (the advertiser's main one); Meta then CREATES the ad but
    # flags it WITH_ISSUES / HARD_ERROR 1885029 and it never delivers. Detect
    # the mismatch up front and swap to the linked Page (auto-fix, non-strict).
    _ig_media_for_page = str(creative_cfg.get("source_instagram_media_id") or "").strip()
    if not strict and _ig_media_for_page:
        try:
            _mprobe = services._get(
                f"{services.BASE_URL}/{_ig_media_for_page}",
                {"access_token": token, "fields": "owner"},
            )
            _owner = ""
            if isinstance(_mprobe, dict) and not _mprobe.get("error"):
                _owner = str((_mprobe.get("owner") or {}).get("id") or "").strip()
            _linked = services.find_page_for_ig_user(token, _owner) if _owner else {}
            _linked_pg = str(_linked.get("page_id") or "").strip()
            if _linked_pg and _linked_pg != (page_id_creative or ""):
                if page_id_creative:
                    auto_fixes.append(
                        f"Page consistency: creative promotes IG media {_ig_media_for_page} whose IG account "
                        f"is linked to Page {_linked_pg} ({_linked.get('page_name')!r}), not the brief's "
                        f"page_id {page_id_creative}. Swapped the ad's Page to the linked one — Meta "
                        "hard-errors 1885029 ('Pages don't match') otherwise."
                    )
                else:
                    auto_fixes.append(
                        f"Resolved creative Page from IG media owner: {_linked_pg} ({_linked.get('page_name')!r})."
                    )
                page_id_creative = _linked_pg
                creative_cfg["page_id"] = _linked_pg
        except Exception:  # noqa: BLE001 — consistency probe is best-effort
            pass

    destination_type = ads_cfg.get("destination_type")
    if str(destination_type or "").strip().upper() in ("", "NONE"):
        destination_type = None

    if strict:
        mode = str(creative_cfg.get("mode") or "").strip().upper() or "LINK_AD"
        dt_raw = ads_cfg.get("destination_type")
        dtu = str(dt_raw or "").strip().upper()
        destination_type = None if dtu in ("", "NONE") else str(dt_raw).strip()
        if mode == "EXISTING_POST":
            promoted_object = promoted_object if isinstance(promoted_object, dict) else {}
        po_pixel = None
        # See lenient-path comment below: lead-form ad sets reject pixel events
        # in promoted_object (Meta error 2446814). Skip in strict mode too.
        _og_for_pixel_strict = str(ads_cfg.get("optimization_goal") or "").strip().upper()
        _dt_for_pixel_strict = str(destination_type or "").strip().upper()
        _is_lead_form_destination_strict = (
            _og_for_pixel_strict == "LEAD_GENERATION" or _dt_for_pixel_strict == "ON_AD"
        )
        if tracking.get("pixel_id") and mode == "LINK_AD" and not _is_lead_form_destination_strict:
            cet = tracking.get("custom_event_type")
            if cet is None or (isinstance(cet, str) and not cet.strip()):
                return _sync_step_error(
                    auto_fixes,
                    {"message": "strict_mode: tracking.custom_event_type is required when pixel_id is set for LINK_AD"},
                    "validate_tracking",
                )
            po_pixel = {
                "pixel_id": str(tracking["pixel_id"]).strip(),
                "custom_event_type": str(cet).strip(),
            }
            if page_id_creative:
                po_pixel["page_id"] = page_id_creative
            if not promoted_object:
                promoted_object = dict(po_pixel)
            elif isinstance(promoted_object, dict):
                promoted_object = {**promoted_object, **po_pixel}
        elif tracking.get("pixel_id") and _is_lead_form_destination_strict:
            auto_fixes.append(
                "strict_mode: skipped promoted_object.pixel_id/custom_event_type for "
                "lead-form ad set (Meta error 2446814 — conversion event not applicable)."
            )
    else:
        mode = str(creative_cfg.get("mode") or "LINK_AD").upper()
        if mode == "EXISTING_POST":
            dt = str(destination_type or "").strip().upper()
            if dt in ("ON_POST", "ON_PAGE", "ON_VIDEO", "ON_EVENT", "ON_AD"):
                destination_type = dt
            else:
                destination_type = None
            promoted_object = promoted_object if isinstance(promoted_object, dict) else {}
            if page_id_creative:
                promoted_object = {**promoted_object, "page_id": page_id_creative}

        po_pixel = None
        # LEAD_GENERATION (Instant Form, ON_AD destination) DOES NOT use a pixel
        # in promoted_object — the form submission is the conversion. Setting
        # custom_event_type=CONTENT_VIEW (or any other event) for OUTCOME_LEADS
        # returns Meta error 2446814 "Conversion event unavailable". Keep the
        # pixel out of promoted_object for these flows; tracking_specs on the
        # ad still records the pixel for analytics.
        _og_for_pixel = str(ads_cfg.get("optimization_goal") or "").strip().upper()
        _dt_for_pixel = str(destination_type or "").strip().upper()
        _is_lead_form_destination = _og_for_pixel == "LEAD_GENERATION" or _dt_for_pixel == "ON_AD"
        if tracking.get("pixel_id") and mode == "LINK_AD" and not _is_lead_form_destination:
            po_pixel = {
                "pixel_id": str(tracking["pixel_id"]).strip(),
                "custom_event_type": services.PIXEL_EVENT_TYPE_MAP.get(
                    str(tracking.get("custom_event_type") or "PAGE_VIEW").strip(),
                    str(tracking.get("custom_event_type") or "CONTENT_VIEW").strip().upper(),
                ),
            }
            if page_id_creative:
                po_pixel["page_id"] = page_id_creative
            if not promoted_object:
                promoted_object = dict(po_pixel)
            elif isinstance(promoted_object, dict):
                promoted_object = {**promoted_object, **po_pixel}
        elif tracking.get("pixel_id") and _is_lead_form_destination:
            auto_fixes.append(
                "Skipped promoted_object.pixel_id/custom_event_type for "
                f"optimization_goal={_og_for_pixel or 'LEAD_GENERATION'} + "
                "destination_type=ON_AD (Instant Form). Meta rejects pixel "
                "events on lead-form ad sets (error 2446814 'Conversion event "
                "unavailable'). The form submission IS the conversion; "
                "tracking_specs on the ad keeps the pixel reachable for analytics."
            )

        # Surface a clear "no pixel attached" warning when the optimization
        # goal implies tracking but the brief omitted the pixel ID. The agent
        # rule_body tells the LLM to call facebook_get_pixels first; this
        # warning catches the case where it skipped that step. Non-fatal:
        # Meta still creates the ad set, just without conversion attribution.
        _opt_goal = str(ads_cfg.get("optimization_goal") or "").strip().upper()
        _TRACKING_GOALS = {
            "LANDING_PAGE_VIEWS", "OFFSITE_CONVERSIONS", "VALUE",
            "LEAD_GENERATION", "CONVERSATIONS",
        }
        if mode == "LINK_AD" and _opt_goal in _TRACKING_GOALS and not tracking.get("pixel_id"):
            auto_fixes.append(
                f"Ad set optimization_goal={_opt_goal} typically benefits from a Meta Pixel, "
                "but the brief did not supply tracking.pixel_id. Created without pixel attachment "
                "— recommend running facebook_get_pixels and re-syncing with tracking.pixel_id "
                "+ tracking.custom_event_type to enable conversion attribution."
            )

    # Pre-flight: pages are account-scoped on Meta. If promoted_object.page_id
    # (or the creative's page_id) is not connected to this ad account, the
    # adset POST fails with "(#100) Param promoted_object[page_id] must be a
    # valid page ID" *after* the campaign is created, forcing a rollback.
    # Verify the page is promotable from this account first. On any Graph
    # error (e.g. token lacks pages_show_list on this account), proceed —
    # only an explicit "not in promote_pages list" response blocks.
    _page_id_to_check = None
    if isinstance(promoted_object, dict) and promoted_object.get("page_id"):
        _page_id_to_check = str(promoted_object.get("page_id")).strip() or None
    if not _page_id_to_check and page_id_creative:
        _page_id_to_check = str(page_id_creative).strip() or None
    if _page_id_to_check:
        _page_check = services.check_page_in_account(
            account_id, token, _page_id_to_check,
        )
        if _page_check.get("known") and not _page_check.get("found"):
            _act_str = str(account_id).strip()
            if not _act_str.startswith("act_"):
                _act_str = f"act_{_act_str}"
            _avail = _page_check.get("available_pages") or []
            _avail_preview = ", ".join(
                f"{p.get('id')} ({p.get('name')})" for p in _avail[:5]
            ) or "none"
            _tier_errs = _page_check.get("tier_errors") or {}
            _checked_tiers = [
                t for t in ("promote_pages", "client_pages", "page_assigned")
                if t not in _tier_errs
            ]
            # Default: soft-warn and let Meta validate at create_creative —
            # the 3-tier check can still miss legitimately-bound pages in
            # partner / agency setups, and the connector already has rollback
            # safety if Meta truly rejects the binding. Opt-in to the old
            # hard-block via options.strict_page_account_check=true.
            _strict_page_check = _truthy_doc_option(options.get("strict_page_account_check"))
            _msg = (
                f"Page {_page_id_to_check} not found in any of "
                f"{_checked_tiers} for account {_act_str}. "
                f"Available pages on this account: {_avail_preview}"
                + ("..." if len(_avail) > 5 else "")
            )
            if _strict_page_check:
                _msg_hard = (
                    f"Page {_page_id_to_check} is not connected to ad account "
                    f"{_act_str}. Pages are account-scoped on Meta - re-connect "
                    f"the page to {_act_str} in Business Manager, or pick a page "
                    f"already on this account. Available on {_act_str}: {_avail_preview}"
                    + ("..." if len(_avail) > 5 else "")
                )
                auto_fixes.append(_msg_hard)
                return _sync_step_error(
                    auto_fixes,
                    {"message": _msg_hard, "type": "page_account_mismatch",
                     "page_id": _page_id_to_check, "account_id": _act_str,
                     "available_pages": _avail},
                    "validate_page",
                )
            # Soft-warn (new default).
            auto_fixes.append(
                _msg + " Proceeding anyway - Meta will reject at create_creative "
                "if binding is truly missing. Set options.strict_page_account_check=true "
                "to restore the old hard-block behavior."
            )

    # Universal pre-flight: ask Meta to validate the ad set spec WITHOUT creating
    # anything. Catches every targeting/budget/bid/objective issue in one round
    # trip, before we burn a campaign-create + rollback cycle. See
    # _dry_run_preflight docstring for what's caught.
    #
    # Runs in BOTH strict and non-strict modes — Meta's validate_only is
    # non-destructive and the second line of defense after offline validation.
    # Skippable via env var FB_ADS_DRY_RUN_PREFLIGHT=0 if desired.
    _budget_for_dryrun = ads_cfg.get("daily_budget") if not is_cbo else None
    _lifetime_for_dryrun = ads_cfg.get("lifetime_budget") if not is_cbo else None
    if is_cbo and not _budget_for_dryrun and not _lifetime_for_dryrun:
        _budget_for_dryrun = camp.get("daily_budget")
        _lifetime_for_dryrun = camp.get("lifetime_budget")
    _preflight = _dry_run_preflight(
        account_id,
        token,
        targeting=targeting,
        optimization_goal=ads_cfg.get("optimization_goal"),
        billing_event=ads_cfg.get("billing_event"),
        daily_budget=_budget_for_dryrun,
        lifetime_budget=_lifetime_for_dryrun,
        bid_strategy=camp.get("bid_strategy") or ads_cfg.get("bid_strategy"),
        promoted_object=promoted_object,
        destination_type=destination_type,
        start_time=ads_cfg.get("start_time"),
        end_time=ads_cfg.get("end_time"),
        adset_name=adset_name,
        auto_fixes=auto_fixes,
        bid_amount=(int(ads_cfg.get("bid_amount") or camp.get("bid_amount") or 0) or None),
    )
    if _preflight and _preflight.get("blocking_error"):
        return _sync_step_error(auto_fixes, _preflight["blocking_error"], "preflight_dry_run")

    # Pre-flight reach estimate (non-fatal). When the resolved targeting will
    # serve a tiny audience (< 10k people), append a warning so the caller can
    # surface it to the user before the ad runs and underdelivers. Wrapped in
    # try/except — a failed reach call MUST NOT block sync; Meta sometimes
    # returns 17 (rate limit) or returns no estimate for very narrow specs.
    try:
        if isinstance(targeting, dict) and targeting.get("geo_locations"):
            re_est = services.get_reach_estimate(
                account_id, token,
                targeting_spec=targeting,
                optimization_goal=ads_cfg.get("optimization_goal"),
            )
            if isinstance(re_est, dict) and not re_est.get("error"):
                est_data = re_est.get("data") or re_est
                if isinstance(est_data, list) and est_data:
                    est_data = est_data[0]
                lo = (est_data or {}).get("users_lower_bound") or (est_data or {}).get("estimate_mau_lower_bound")
                hi = (est_data or {}).get("users_upper_bound") or (est_data or {}).get("estimate_mau_upper_bound")
                try:
                    lo_i = int(lo) if lo is not None else None
                    hi_i = int(hi) if hi is not None else None
                except (TypeError, ValueError):
                    lo_i = hi_i = None
                if lo_i is not None and hi_i is not None:
                    if hi_i < 10000:
                        auto_fixes.append(
                            f"Reach estimate {lo_i:,}–{hi_i:,} is below 10,000 — Meta may "
                            "fail to deliver this ad. Consider broadening: enable "
                            "Advantage+ audience (requires age_max=65), expand cities, "
                            "drop strict 'Narrow audience' filters, or remove tiny "
                            "custom audiences."
                        )
                    else:
                        auto_fixes.append(f"Reach estimate: {lo_i:,}–{hi_i:,} eligible users.")
    except Exception as _re_exc:  # noqa: BLE001
        logger.info("Reach estimate pre-flight skipped: %s", _re_exc)

    # Custom-audience size check (non-fatal). Custom audiences below ~1,000
    # people are typically blocked from delivery by Meta. Surface the warning
    # so the caller knows the targeting will produce zero impressions even if
    # everything else is correct.
    try:
        if isinstance(targeting, dict):
            ca_list = targeting.get("custom_audiences") or []
            if isinstance(ca_list, list) and ca_list:
                ca_ids = [str(c.get("id") or "").strip() for c in ca_list if isinstance(c, dict)]
                ca_ids = [i for i in ca_ids if i.isdigit()]
                if ca_ids:
                    ca_resp = services.get_custom_audiences(account_id, token, limit=200)
                    if isinstance(ca_resp, dict) and not ca_resp.get("error"):
                        by_id = {
                            str(a.get("id")): a
                            for a in (ca_resp.get("data") or [])
                            if isinstance(a, dict) and a.get("id")
                        }
                        small = []
                        for cid in ca_ids:
                            audience = by_id.get(cid)
                            if not audience:
                                continue
                            lo = audience.get("approximate_count_lower_bound") or 0
                            try:
                                lo_i = int(lo)
                            except (TypeError, ValueError):
                                lo_i = 0
                            if 0 < lo_i < 1000:
                                small.append(f"{audience.get('name') or cid} (~{lo_i})")
                            elif lo_i == 0:
                                small.append(f"{audience.get('name') or cid} (size unknown / not yet populated)")
                        if small:
                            auto_fixes.append(
                                "Custom audience(s) too small for delivery: "
                                + ", ".join(small)
                                + ". Meta typically blocks delivery below ~1,000 people. "
                                "Combine these lists into a single larger audience or "
                                "remove them from targeting before launching."
                            )
    except Exception as _ca_exc:  # noqa: BLE001
        logger.info("Custom audience size check skipped: %s", _ca_exc)

    # --- Pre-flight: validate_only probe on the ad set spec ---
    # Catches per-account×objective×goal lockouts (subcode 2490408) BEFORE
    # any campaign/adset is written. Meta supports
    # execution_options=["validate_only"] which runs full server-side
    # validation but commits nothing. This neutralizes the "campaign
    # created, then adset rejected, then rollback" loop that wastes
    # account writes on goals Meta won't allow on this account+page.
    # Honors user memory feedback_no_hardcoding.md: zero hardcoded
    # fallback goals — Meta is the only source of truth, we just
    # surface its response cleanly to the user.
    _requested_goal = (ads_cfg.get("optimization_goal") or "").strip().upper()
    if _requested_goal:
        try:
            _probe_spec = {
                "name": "__validate_only_probe__",
                "campaign_id": "0",  # placeholder; validate_only ignores invalid id for goal check
                "status": "PAUSED",
                "billing_event": str(ads_cfg.get("billing_event") or "IMPRESSIONS"),
                "optimization_goal": _requested_goal,
                "daily_budget": str(int(daily_b)) if daily_b else "10000",
                "targeting": targeting if isinstance(targeting, (dict, list)) else (
                    json.loads(targeting) if isinstance(targeting, str) and targeting.strip().startswith("{") else {"geo_locations": {"countries": ["IN"]}}
                ),
            }
            if isinstance(promoted_object, dict) and promoted_object:
                _probe_spec["promoted_object"] = promoted_object
            elif page_id_creative:
                _probe_spec["promoted_object"] = {"page_id": str(page_id_creative).strip()}
            _val_resp = services.validate_adset_spec(account_id, token, _probe_spec)
        except Exception:  # noqa: BLE001 — probe is best-effort
            _val_resp = None

        if isinstance(_val_resp, dict) and _val_resp.get("error"):
            _err = _val_resp["error"] or {}
            _subcode = _err.get("error_subcode")
            _err_data_raw = _err.get("error_data") or ""
            _blame_is_goal = False
            try:
                _ed = json.loads(_err_data_raw) if isinstance(_err_data_raw, str) else _err_data_raw
                _bf = (_ed or {}).get("blame_field_specs") or []
                for _entry in _bf:
                    if isinstance(_entry, list) and "optimization_goal" in _entry:
                        _blame_is_goal = True
                        break
            except (TypeError, ValueError):
                pass
            if _subcode == 2490408 and _blame_is_goal:
                return _sync_step_error(
                    auto_fixes,
                    {
                        "message": (
                            f"Pre-flight: optimization_goal={_requested_goal!r} is "
                            f"NOT available on account {account_id} for objective "
                            f"{doc_objective!r} (with the current special_ad_categories "
                            "and Page-AdAccount grant). Meta returned subcode 2490408 "
                            "('Performance goal isn't available') in a validate-only "
                            "probe BEFORE any campaign was created. No rollback needed. "
                            "Fix: pick a different optimization_goal that Meta accepts "
                            "for this exact configuration. Common safe goals on "
                            "OUTCOME_AWARENESS political ads: REACH, IMPRESSIONS, "
                            "AD_RECALL_LIFT. Meta does not expose a global goal-list "
                            "endpoint; try the candidate by re-submitting the brief."
                        ),
                        "requested_optimization_goal": _requested_goal,
                        "campaign_objective": doc_objective,
                        "account_id": account_id,
                        "error_subcode": 2490408,
                        "meta_user_msg": _err.get("error_user_msg"),
                    },
                    "preflight_goal_availability",
                )

    cr = services.create_campaign(
        account_id,
        token,
        name=campaign_name,
        objective=doc_objective,
        status=str(camp.get("status") or "PAUSED").strip(),
        special_ad_categories=camp.get("special_ad_categories"),
        special_ad_category_country=camp.get("special_ad_category_country"),
        is_adset_budget_sharing_enabled=is_cbo,
        daily_budget=camp.get("daily_budget"),
        lifetime_budget=camp.get("lifetime_budget"),
        bid_strategy=camp.get("bid_strategy"),
        strict=strict,
    )
    if _has_api_error(cr):
        return _sync_step_error(auto_fixes, cr.get("error"), "create_campaign")
    campaign_id = str(cr.get("id") or "").strip()
    if isinstance(cr.get("_auto_fixes"), list):
        auto_fixes.extend(cr["_auto_fixes"])
    if doc_id:
        mapping_action = "created"

    adset_id = None

    if strict:
        new_adset_name = adset_name
    else:
        suffix = time.strftime("-%Y%m%d-%H%M%S")
        new_adset_name = f"{adset_name}{suffix}"
    as_resp = create_adset_with_targeting_retries(
        account_id,
        token,
        campaign_id,
        new_adset_name,
        str(ads_cfg.get("status") or "PAUSED"),
        str(ads_cfg.get("billing_event") or "IMPRESSIONS"),
        ads_cfg.get("optimization_goal"),
        targeting,
        int(daily_b) if daily_b is not None else None,
        int(lifetime_b) if lifetime_b is not None else None,
        ads_cfg.get("bid_strategy") or camp.get("bid_strategy"),
        promoted_object if isinstance(promoted_object, dict) and promoted_object else None,
        page_id_creative if not (isinstance(promoted_object, dict) and promoted_object.get("page_id")) else None,
        destination_type,
        ads_cfg.get("start_time"),
        ads_cfg.get("end_time"),
        auto_fixes,
        max_retries=int(options.get("max_targeting_retries") or 5),
        strict=strict,
        bid_amount=(int(ads_cfg.get("bid_amount") or camp.get("bid_amount") or 0) or None),
        campaign_daily_budget=(int(camp.get("daily_budget")) if camp.get("daily_budget") else None),
        campaign_lifetime_budget=(int(camp.get("lifetime_budget")) if camp.get("lifetime_budget") else None),
        bid_strategy_for_default=(camp.get("bid_strategy") or ads_cfg.get("bid_strategy")),
        frequency_control_specs=ads_cfg.get("frequency_control_specs"),
    )
    if _has_api_error(as_resp):
        return _sync_fail_with_rollback(
            auto_fixes, as_resp.get("error"), "create_adset", token, options, campaign_id=campaign_id,
        )
    adset_id = str(as_resp.get("id") or "").strip()

    saved_aud = str(ads_cfg.get("saved_audience_id") or "").strip()
    if saved_aud and not pre_merged_saved_audience:
        opts_doc = doc.get("options") if isinstance(doc.get("options"), dict) else {}
        aa_raw = ads_cfg.get("advantage_audience")
        if aa_raw is None:
            aa_raw = opts_doc.get("saved_audience_advantage_audience", 0)
        try:
            advantage_audience = int(aa_raw) if aa_raw is not None else 0
        except (TypeError, ValueError):
            advantage_audience = 0
        sav = services.sync_adset_targeting_from_saved_audience(
            adset_id,
            saved_aud,
            token,
            preserve_placements=True,
            optimization_goal=ads_cfg.get("optimization_goal"),
            bid_strategy=ads_cfg.get("bid_strategy"),
            destination_type=destination_type,
            billing_event=ads_cfg.get("billing_event"),
            promoted_object=promoted_object if isinstance(promoted_object, dict) and promoted_object else None,
            advantage_audience=advantage_audience,
        )
        if _has_api_error(sav):
            return _sync_fail_with_rollback(
                auto_fixes, sav.get("error"), "sync_saved_audience", token, options,
                campaign_id=campaign_id, adset_id=adset_id,
            )
        if not strict:
            auto_fixes.append(f"Applied saved_audience_id={saved_aud} to ad set (placements preserved from document)")

    _opt_goal_for_pixel = str(ads_cfg.get("optimization_goal") or "").strip().upper()
    if tracking.get("pixel_id") and _opt_goal_for_pixel in _NO_PIXEL_ATTACH_GOALS:
        # CALL optimization goals don't use website pixel attribution; the
        # ad set's promoted_object.page_id drives call attribution. Attempting
        # the attach is a no-op at best (Meta accepts but ignores) and at
        # worst contradicts the _autoresolve_pixel transparency note. Skip it
        # cleanly.
        auto_fixes.append(
            f"Skipped pixel attach for {_opt_goal_for_pixel} goal — Meta uses "
            "promoted_object.page_id for call attribution; pixel attach is a no-op."
        )
    elif tracking.get("pixel_id"):
        ap = services.attach_pixel_to_adset(
            adset_id,
            token,
            str(tracking["pixel_id"]).strip(),
            custom_event_type=tracking.get("custom_event_type"),
            page_id=tracking.get("page_id") or page_id_creative,
        )
        if _has_api_error(ap):
            if strict:
                return _sync_fail_with_rollback(
                    auto_fixes, ap.get("error"), "attach_pixel", token, options,
                    campaign_id=campaign_id, adset_id=adset_id,
                )
            auto_fixes.append(f"Pixel attach retry via update failed: {_api_error_message(ap)}")
        elif isinstance(ap, dict) and ap.get("_pixel_not_attached"):
            auto_fixes.append(
                str(ap.get("reason") or "Pixel not attached: not allowed for POST_ENGAGEMENT + ON_POST ad sets."),
            )
        else:
            if not strict:
                auto_fixes.append("Verified pixel attach via attach_pixel_to_adset")

    creative_id = None
    if strict:
        cname = str(creative_cfg.get("name") or "").strip()
        if not cname:
            return _sync_fail_with_rollback(
                auto_fixes,
                {"message": "strict_mode: document.creative.name is required"},
                "creative",
                token,
                options,
                campaign_id=campaign_id,
                adset_id=adset_id,
            )
    else:
        cname = str(creative_cfg.get("name") or f"creative-{adset_id}").strip()
    if mode == "EXISTING_POST":
        oid = str(creative_cfg.get("object_story_id") or "").strip()
        ig_media_id = str(creative_cfg.get("source_instagram_media_id") or "").strip()
        ig_permalink = str(creative_cfg.get("instagram_permalink_url") or "").strip()
        # Set by the eligibility pre-flight when a non-promotable video post is
        # switched to a video_data creative (UI-style). None = use object_story_id.
        _video_data_override: Optional[Dict[str, Any]] = None
        if not oid and not ig_media_id and not ig_permalink:
            return _sync_fail_with_rollback(
                auto_fixes,
                {"message": "EXISTING_POST requires one of: object_story_id (FB page post), source_instagram_media_id (IG story), or instagram_permalink_url (IG feed post / carousel / reel)."},
                "creative",
                token,
                options,
                campaign_id=campaign_id,
                adset_id=adset_id,
            )

        # --- Auto-translate: IG permalink + POLITICAL → FB cross-post ---
        # Meta hard-rejects political ads built from instagram_permalink_url
        # alone (subcode 2708016: "Ads about social issues, elections or
        # politics must be connected to a Facebook Page"). Verified against
        # /act_X/adcreatives with execution_options=["validate_only"] on
        # 2026-05-29. There is no API-side workaround that keeps the IG-
        # direct path. The accepted production path is object_story_id of
        # the FB cross-post of the same IG media.
        # If the user gave us an IG permalink AND political flag AND a page,
        # we transparently resolve the FB cross-post here so the user's
        # brief stays IG-shaped while the API call uses the working path.
        _auth_cat_check = str(creative_cfg.get("authorization_category") or "").strip().upper()
        _page_id_for_xpost = str(
            creative_cfg.get("page_id") or page_id_creative or ""
        ).strip()
        # The IG-DIRECT creative path (built later) is now the preferred way to
        # run political IG posts — it needs no FB cross-post and works for
        # IG-only posts. So skip this cross-post translation BY DEFAULT and let
        # IG-direct handle it. Set options.prefer_fb_crosspost=true to force the
        # old behaviour (e.g. to preserve the original post's likes/comments).
        _prefer_xpost = _truthy_doc_option(options.get("prefer_fb_crosspost"))
        if (
            _prefer_xpost
            and ig_permalink
            and not oid
            and not ig_media_id
            and _auth_cat_check == "POLITICAL"
            and _page_id_for_xpost
        ):
            try:
                from facebook_ads_mcp import services as _svc
                _xpost = _svc.resolve_ig_permalink_to_fb_xpost(
                    token,
                    ig_permalink,
                    _page_id_for_xpost,
                    instagram_user_id=creative_cfg.get("instagram_user_id"),
                )
            except Exception as _exc:  # noqa: BLE001
                _xpost = {"error": {"message": f"auto-translate failed: {_exc}"}}

            if isinstance(_xpost, dict) and _xpost.get("error"):
                # Auto-translate failed (e.g. IG account not accessible);
                # surface clearly so user knows the original failure mode
                # (2708016) will surface if they retry as-is.
                auto_fixes.append(
                    "Auto-translate IG permalink → FB cross-post failed: "
                    + str(_xpost["error"].get("message") or _xpost["error"])
                    + ". The IG-direct + POLITICAL combination will be rejected "
                    "by Meta with subcode 2708016. Supply creative.object_story_id "
                    "of the FB cross-post manually."
                )
            elif isinstance(_xpost, dict) and _xpost.get("object_story_id"):
                _new_oid = _xpost["object_story_id"]
                _ig_info = _xpost.get("ig_media") or {}
                auto_fixes.append(
                    f"Auto-translated IG permalink {ig_permalink!r} → FB "
                    f"cross-post object_story_id={_new_oid}. IG media id "
                    f"{_ig_info.get('id')} ({_ig_info.get('media_product_type')}). "
                    "Meta requires this for political ads (subcode 2708016 "
                    "rejects IG-direct path)."
                )
                oid = _new_oid
                ig_permalink = ""  # remove the IG-direct field so downstream uses oid
            else:
                _reason = (
                    (_xpost or {}).get("reason")
                    if isinstance(_xpost, dict)
                    else "no match"
                )
                auto_fixes.append(
                    f"Auto-translate IG permalink → FB cross-post: no match found. "
                    f"Reason: {_reason}. The IG post may not have been shared to the "
                    "Facebook Page (toggle 'Share to Facebook' was off). Cross-post "
                    "the IG media to the FB Page in Business Suite, then re-run with "
                    "the resulting creative.object_story_id."
                )

        # --- Pre-flight: is_eligible_for_promotion check on object_story_id ---
        # Real-world incident (2026-05-28): user spent 6 hours hitting subcode
        # 1815159 "Creative and objective mismatch" because the cross-post
        # specified as object_story_id had Meta flag is_eligible_for_promotion=
        # false. No code combination (objective×goal×placement) bypasses a
        # post-level ineligibility flag — Meta refuses the ad regardless.
        # Pre-flight check via page-scoped token, surface eligible-list so
        # user can pick another post without burning an MCP roundtrip.
        if oid and not ig_media_id and not ig_permalink:
            try:
                from facebook_ads_mcp import services as _svc
                # Derive the page id from object_story_id (format: pageid_postid)
                _page_id = ""
                if "_" in oid:
                    _page_id = oid.split("_", 1)[0].strip()
                # Page-scoped token gives us read access to is_eligible_for_promotion
                # even when user-token would 403 with "pages_read_engagement"
                # required. Reuses existing get_page_access_token caching.
                _page_tok = (
                    _svc.get_page_access_token(_page_id, token) if _page_id else None
                )
                _tok_to_use = _page_tok or token
                _probe = _svc._get(
                    f"{_svc.BASE_URL}/{oid}",
                    {
                        "access_token": _tok_to_use,
                        # video_buying_eligibility is Meta's INTENDED signal for
                        # video/Reel boostability; is_eligible_for_promotion is
                        # documented in one sentence, is not authoritative, and
                        # is widely unreliable for Reels (Meta redirects real
                        # eligibility to video_buying_eligibility / the UI). We
                        # fetch both and only hard-block when they agree.
                        "fields": "id,is_eligible_for_promotion,video_buying_eligibility,attachments{media_type,target{url}}",
                    },
                )
            except Exception:  # noqa: BLE001 — probe is best-effort
                _probe = None

            # Decide whether to HARD-BLOCK. is_eligible_for_promotion=false is
            # necessary but NOT sufficient on its own for video/Reels:
            #   - video_buying_eligibility is the authoritative video signal. If
            #     it is present and NOT "ELIGIBLE", that confirms the block.
            #     If it says ELIGIBLE (or is absent/unavailable), the false from
            #     is_eligible_for_promotion is likely a stale/unreliable Reel
            #     reading — demote to a warning instead of killing the sync.
            #   - options.allow_ineligible_post=true always demotes to a warning
            #     (escape hatch for a confirmed false-negative).
            _iefp_false = (
                isinstance(_probe, dict)
                and not _probe.get("error")
                and _probe.get("is_eligible_for_promotion") is False
            )
            _att_pf = (_probe.get("attachments") or {}).get("data") or [] if isinstance(_probe, dict) else []
            _is_video_pf = str((_att_pf[0] or {}).get("media_type") or "").lower() == "video" if _att_pf else False
            _vbe = _probe.get("video_buying_eligibility") if isinstance(_probe, dict) else None
            # video_buying_eligibility is a LIST of goal-scoped hints, not an
            # overall verdict. Observed live: eligible AND ineligible reels both
            # carry INELIGIBLE_VIDEO_LENGTH_FOR_COMPLETED_VIEW_BUYING — it only
            # means "don't use a completed-view goal (THRUPLAY)", NOT "can't be
            # boosted". So these goal-scoped reasons must NOT count as a block.
            # Only treat video_buying_eligibility as confirming a block if it
            # contains a reason OTHER than the known goal-scoped advisories.
            _VBE_ADVISORY_ONLY = {
                "INELIGIBLE_VIDEO_LENGTH_FOR_COMPLETED_VIEW_BUYING",
            }
            _vbe_list = _vbe if isinstance(_vbe, list) else ([_vbe] if _vbe else [])
            _vbe_blocking_reasons = [
                str(x).upper() for x in _vbe_list
                if str(x).upper() not in _VBE_ADVISORY_ONLY and str(x).upper() != "ELIGIBLE"
            ]
            _vbe_confirms_block = bool(_vbe_blocking_reasons)
            _override = _truthy_sync_option(options.get("allow_ineligible_post"))

            # For a video where video_buying_eligibility does NOT confirm the
            # block, treat is_eligible_for_promotion=false as advisory only.
            _soft_only = _is_video_pf and not _vbe_confirms_block

            if _iefp_false and (_override or _soft_only):
                # The object_story_id path is what fails for these video posts
                # (subcode 1487472). The Meta "Boost reel" UI works because it
                # builds a video_data creative from the underlying video instead.
                # Do the same: switch this creative to video_data so it actually
                # gets created (verified working where object_story_id failed).
                if _is_video_pf:
                    _vd = _resolve_video_data_from_post(oid, _page_id, _page_tok, token)
                    if _vd:
                        _video_data_override = _vd
                        auto_fixes.append(
                            f"FB post {oid} (video/Reel) is not promotable via object_story_id "
                            f"(is_eligible_for_promotion=false). Switched creative to a video_data "
                            f"spec built from the post's video_id={_vd.get('video_id')} — same as "
                            "Meta's 'Boost reel' UI does. NOTE: this is a new creative from the same "
                            "video; it does not carry the original post's likes/comments."
                        )
                    else:
                        auto_fixes.append(
                            f"WARNING: FB post {oid} has is_eligible_for_promotion=false and could "
                            "not resolve a video_data fallback; proceeding with object_story_id "
                            "(Meta may reject at ad-create with subcode 1487472)."
                        )
                else:
                    auto_fixes.append(
                        f"WARNING: FB post {oid} has is_eligible_for_promotion=false but "
                        "allow_ineligible_post=true is set; proceeding. Meta may reject at ad-create."
                    )
                _iefp_false = False  # do not hard-block

            if _iefp_false:
                # Get a small eligible-list to surface in the error.
                _eligible_examples: List[str] = []
                if _page_id and _page_tok:
                    try:
                        _posts_resp = _svc._get(
                            f"{_svc.BASE_URL}/{_page_id}/published_posts",
                            {
                                "access_token": _page_tok,
                                "fields": "id,is_eligible_for_promotion,message",
                                "limit": 30,
                            },
                        )
                        if isinstance(_posts_resp, dict):
                            for p in (_posts_resp.get("data") or [])[:30]:
                                if p.get("is_eligible_for_promotion"):
                                    _eligible_examples.append(p.get("id", ""))
                                if len(_eligible_examples) >= 5:
                                    break
                    except Exception:  # noqa: BLE001
                        pass
                _att = (_probe.get("attachments") or {}).get("data") or []
                _att_mtype = (_att[0] or {}).get("media_type") if _att else None
                return _sync_fail_with_rollback(
                    auto_fixes,
                    {
                        "message": (
                            f"Pre-flight: FB post {oid} has "
                            f"is_eligible_for_promotion=false (media_type={_att_mtype!r}, "
                            f"video_buying_eligibility={_vbe!r}). "
                            "Meta refuses to boost this post regardless of the campaign "
                            "objective, optimization goal, account, or audience. "
                            "Reason is post-level (e.g. policy review pending, "
                            "cross-post sharing restriction, age-gated content, "
                            "or the original poster disabled promotion). To see Meta's "
                            "exact reason, open this post in Ads Manager / Business Suite "
                            "-> Boost. Pick a different eligible post, "
                            "or set options.allow_ineligible_post=true to override if you "
                            "believe this is a stale API reading. "
                            + (
                                f"Eligible post IDs on page {_page_id}: "
                                f"{_eligible_examples}. "
                                if _eligible_examples
                                else ""
                            )
                            + "Use facebook_get_page_posts and filter on "
                            "is_eligible_for_promotion to list all eligible posts."
                        ),
                        "object_story_id": oid,
                        "is_eligible_for_promotion": False,
                        "video_buying_eligibility": _vbe,
                        "page_id": _page_id,
                        "eligible_post_examples": _eligible_examples,
                    },
                    "preflight_creative",
                    token,
                    options,
                    campaign_id=campaign_id,
                    adset_id=adset_id,
                )

        # Pre-flight IG media-type guard. When the brief gave us
        # source_instagram_media_id, fetch the media's type and route correctly
        # BEFORE Meta rejects with a cryptic error. Per spec §11:
        #   - IMAGE (single photo) → source_instagram_media_id works
        #   - CAROUSEL_ALBUM       → switch to instagram_permalink_url
        #   - VIDEO / Reel         → Meta blocks API ads on IG videos
        #                            (error_subcode=1815279). Suggest the
        #                            permalink path; Meta accepts permalink
        #                            for some feed videos but not Reels.
        if ig_media_id and not ig_permalink:
            try:
                from facebook_ads_mcp import services as _svc
                _probe = _svc._get(
                    f"{_svc.BASE_URL}/{ig_media_id}",
                    {"access_token": token, "fields": "id,media_type,permalink,media_product_type"},
                )
            except Exception:  # noqa: BLE001 — probe is best-effort
                _probe = None

            if isinstance(_probe, dict) and not _probe.get("error"):
                mtype = (_probe.get("media_type") or "").upper()
                mproduct = (_probe.get("media_product_type") or "").upper()
                ig_perma_probed = (_probe.get("permalink") or "").strip()
                is_reel = mproduct == "REELS" or "/reel/" in ig_perma_probed.lower()

                if mtype == "VIDEO" or is_reel:
                    # Direct source_instagram_media_id is tried FIRST for IG
                    # Reels/videos. Live-verified 2026-08-13: Meta ACCEPTS reel
                    # media ids on this account path (creative + political ad
                    # both created). The old pre-flight here assumed a hard
                    # 1815279 block and pre-routed via an FB cross-post searched
                    # on the brief's page_id — wrong Page whenever the media
                    # belongs to a different Page's IG account — producing a
                    # SYNTHETIC "(1815279)" failure for media Meta would have
                    # accepted. A real Meta 1815279 rejection is now handled
                    # reactively after create_creative below (cross-post lookup
                    # via the Page actually linked to the media's IG account).
                    pass

                if mtype == "CAROUSEL_ALBUM" and ig_perma_probed:
                    # source_instagram_media_id rejects carousels with
                    # "creative/objective mismatch" or similar. Auto-route to
                    # the permalink path and report the swap in auto_fixes.
                    auto_fixes.append(
                        f"Pre-flight: IG post {ig_media_id} is CAROUSEL_ALBUM; "
                        f"swapped source_instagram_media_id → instagram_permalink_url={ig_perma_probed} "
                        "(Meta accepts permalink for IG carousels, rejects media-id)."
                    )
                    ig_permalink = ig_perma_probed
                    ig_media_id = ""  # disable the media-id path

                # --- Optimization goal × media_type sanity check ---
                # Video-only optimization goals (THRUPLAY, VIDEO_VIEWS,
                # TWO_SECOND_CONTINUOUS_VIDEO_VIEWS) require a video creative.
                # Pairing them with an IMAGE or CAROUSEL_ALBUM IG post causes
                # Meta to reject at create_ad with error_subcode=1815159
                # ("Creative and objective mismatch") — only AFTER campaign +
                # ad set are already written (orphan cost). Catch it here.
                video_only_goals = {"THRUPLAY", "VIDEO_VIEWS", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"}
                adset_doc = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
                og_now = str(adset_doc.get("optimization_goal") or "").strip().upper()
                if og_now in video_only_goals and mtype in ("IMAGE", "CAROUSEL_ALBUM"):
                    return _sync_fail_with_rollback(
                        auto_fixes,
                        {
                            "message": (
                                f"Pre-flight: optimization_goal={og_now!r} requires a video "
                                f"creative, but IG post {ig_media_id} is {mtype}. Meta would "
                                "reject at create_ad with error_subcode=1815159 ('Creative and "
                                "objective mismatch'). Options: (a) keep this post and switch "
                                "adset.optimization_goal to 'REACH', 'IMPRESSIONS', or "
                                "'AD_RECALL_LIFT' (all valid under OUTCOME_AWARENESS for any "
                                "media type); (b) keep THRUPLAY and pick an IG VIDEO/Reel "
                                "post instead (usable directly via "
                                "source_instagram_media_id); (c) for a FB Page video post use "
                                "creative.object_story_id with format <page_id>_<post_id>."
                            ),
                            "optimization_goal": og_now,
                            "ig_media_id": ig_media_id,
                            "media_type": mtype,
                            "permalink": ig_perma_probed,
                            "error_subcode": 1815159,
                        },
                        "preflight_creative",
                        token,
                        options,
                        campaign_id=campaign_id,
                        adset_id=adset_id,
                    )

        # --- Political-ad byline auto-fetch (subcode 2708016 prevention) ---
        # Meta rejects political ads (ISSUES_ELECTIONS_POLITICS) with
        # error_subcode=2708016 when the creative lacks a "Paid for by"
        # byline string. Bylines are registered on the FB Page in Business
        # Manager and surface back via the Ad Library API. If the brief
        # didn't supply one, look up the most recent byline this Page used
        # in a prior political ad and reuse it — same value Meta already
        # accepted before. Brief-supplied bylines always win.
        creative_bylines = str(creative_cfg.get("bylines") or "").strip()
        auth_cat_for_creative = str(creative_cfg.get("authorization_category") or "").upper().strip()
        # Graph v26 Shop-ads default: creative.destination_spec (dict/JSON/enum
        # string) or options.shop_destination_opt_out=true → forwarded to every
        # creative shape below so the brief controls WEBSITE_AND_SHOP routing.
        _creative_dest_spec = creative_cfg.get("destination_spec")
        if not _creative_dest_spec and _truthy_sync_option((doc.get("options") or {}).get("shop_destination_opt_out")):
            _creative_dest_spec = "WEBSITE_AND_SHOP_OPT_OUT"
            auto_fixes.append("Creative destination_spec=WEBSITE_AND_SHOP_OPT_OUT (options.shop_destination_opt_out): ad stays website-only, not auto-sent to the Meta Shop.")
        if not creative_bylines and auth_cat_for_creative == "POLITICAL":
            page_for_byline = str(
                creative_cfg.get("page_id") or page_id_creative or ""
            ).strip()
            if page_for_byline:
                try:
                    from facebook_ads_mcp import services as _svc
                    _archive = _svc._get(
                        f"{_svc.BASE_URL}/ads_archive",
                        {
                            "access_token": token,
                            "search_page_ids": page_for_byline,
                            "ad_active_status": "ALL",
                            "ad_reached_countries": "IN",
                            "fields": "bylines",
                            "limit": 5,
                        },
                    )
                except Exception:  # noqa: BLE001 — best-effort
                    _archive = None
                if isinstance(_archive, dict) and not _archive.get("error"):
                    for row in (_archive.get("data") or []):
                        b = str((row or {}).get("bylines") or "").strip()
                        if b:
                            creative_bylines = b
                            auto_fixes.append(
                                f"Auto-fetched political byline from prior approved ad on page {page_for_byline}: {b!r} "
                                "(creative.bylines was missing; required to avoid subcode 2708016)."
                            )
                            break
                if not creative_bylines:
                    auto_fixes.append(
                        f"Could not auto-fetch byline for page {page_for_byline}. "
                        "Sync may fail at create_creative with subcode 2708016. "
                        "Pass creative.bylines explicitly in the document — Meta requires this "
                        "'Paid for by' string for all ISSUES_ELECTIONS_POLITICS ads."
                    )

        # --- Preferred path for POLITICAL Instagram posts: IG-DIRECT creative ---
        # When we have an Instagram media id and a Page (with disclaimer), build
        # the creative directly from the IG media + Page identity, instead of
        # converting to an FB cross-post (resolve_ig_permalink_to_fb_xpost) or
        # re-uploading via video_data. VERIFIED (validate_only, 2026-05-30) this
        # is accepted for political ads AND works for IG-only posts that have no
        # FB cross-post at all. Falls through to the legacy paths on any error.
        _ig_direct_cr = None
        _ig_direct_media = ig_media_id
        if (
            not oid
            and not _video_data_override
            and str(creative_cfg.get("authorization_category") or "").strip().upper() == "POLITICAL"
        ):
            _pg = str(creative_cfg.get("page_id") or page_id_creative or "").strip()
            _iguser = str(creative_cfg.get("instagram_user_id") or "").strip()
            # If we only have a permalink, resolve the IG media id from it.
            if not _ig_direct_media and ig_permalink and _iguser:
                try:
                    from facebook_ads_mcp import services as _svc
                    import re as _re
                    _sc = _re.search(r"/(?:p|reel)/([^/?]+)", ig_permalink)
                    if _sc:
                        _mres = _svc.get_instagram_media(
                            token, instagram_user_id=_iguser, caption_query=_sc.group(1))
                        for _m in (_mres.get("data") or []) if isinstance(_mres, dict) else []:
                            if _sc.group(1) in (_m.get("permalink") or ""):
                                _ig_direct_media = str(_m.get("id") or "").strip()
                                break
                except Exception:  # noqa: BLE001
                    _ig_direct_media = ig_media_id
            if _ig_direct_media and _pg and _iguser:
                _ig_direct_cr = _create_ig_direct_creative(
                    account_id, token, name=cname, ig_media_id=_ig_direct_media,
                    page_id=_pg, instagram_user_id=_iguser,
                    link=creative_cfg.get("link_url") or creative_cfg.get("link"),
                    authorization_category="POLITICAL",
                    bylines=creative_bylines or creative_cfg.get("bylines"),
                )
                if _has_api_error(_ig_direct_cr):
                    auto_fixes.append(
                        "IG-direct political creative not accepted "
                        f"({_api_error_message(_ig_direct_cr)}); falling back to "
                        "FB cross-post / video_data path."
                    )
                    _ig_direct_cr = None
                else:
                    auto_fixes.append(
                        f"Built creative DIRECTLY from Instagram media {_ig_direct_media} "
                        f"with Page {_pg} for the political disclaimer (no FB cross-post "
                        "needed). This is the documented 'Use IG post as ad' path and "
                        "works even for IG-only posts."
                    )

        if _ig_direct_cr:
            cr = _ig_direct_cr
        elif _video_data_override:
            # Non-promotable video post -> build a video_data creative from the
            # post's video (UI-style), instead of the rejected object_story_id.
            _vd_page = str(creative_cfg.get("page_id") or page_id_creative or _page_id or "").strip()
            _vd_spec: Dict[str, Any] = {"page_id": _vd_page, "video_data": dict(_video_data_override)}
            if not _vd_spec["video_data"].get("call_to_action"):
                _vd_spec["video_data"]["call_to_action"] = {
                    "type": DEFAULT_CTA,
                    "value": {"link": f"https://www.facebook.com/{_vd_page}"},
                }
            cr = services.create_creative_with_instagram_fallback(
                account_id,
                token,
                name=cname,
                object_story_spec=json.dumps(_vd_spec),
                page_id=_vd_page,
                instagram_user_id=creative_cfg.get("instagram_user_id"),
                instagram_actor_id=creative_cfg.get("instagram_actor_id"),
                authorization_category=creative_cfg.get("authorization_category"),
                bylines=creative_bylines or None,
                destination_spec=_creative_dest_spec,
            )
        else:
            cr = services.create_creative_with_instagram_fallback(
                account_id,
                token,
                name=cname,
                object_story_id=oid or None,
                source_instagram_media_id=ig_media_id or None,
                instagram_permalink_url=ig_permalink or None,
                page_id=creative_cfg.get("page_id") or page_id_creative,
                instagram_user_id=creative_cfg.get("instagram_user_id"),
                instagram_actor_id=creative_cfg.get("instagram_actor_id"),
                authorization_category=creative_cfg.get("authorization_category"),
                bylines=creative_bylines or None,
                destination_spec=_creative_dest_spec,
            )

        # --- Reactive 1815279 fallback (real Meta rejection only) ---
        # "Instagram video must be uploaded to Facebook": reroute via the FB
        # cross-post video_data path, searching the Page actually linked to the
        # media's IG account first (briefs often name a different Page than the
        # one the IG account cross-posts to), then the brief's Page.
        _cr_err = (cr.get("error") or {}) if isinstance(cr, dict) else {}
        if (
            _has_api_error(cr)
            and ig_media_id
            and (
                str(_cr_err.get("error_subcode") or "") == "1815279"
                or "must be uploaded to facebook" in _api_error_message(cr).lower()
            )
        ):
            from facebook_ads_mcp import services as _svc
            _perma = ""
            _mprod = ""   # media_product_type (REELS/FEED/AD/STORY)
            _mtype = ""   # media_type (VIDEO/IMAGE/CAROUSEL_ALBUM)
            _ig_owner = str(creative_cfg.get("instagram_user_id") or "").strip()
            try:
                _mprobe = _svc._get(
                    f"{_svc.BASE_URL}/{ig_media_id}",
                    {"access_token": token, "fields": "permalink,owner,media_product_type,media_type"},
                )
                if isinstance(_mprobe, dict) and not _mprobe.get("error"):
                    _perma = str(_mprobe.get("permalink") or "").strip()
                    _mprod = str(_mprobe.get("media_product_type") or "").strip().upper()
                    _mtype = str(_mprobe.get("media_type") or "").strip().upper()
                    _ig_owner = _ig_owner or str((_mprobe.get("owner") or {}).get("id") or "").strip()
            except Exception:  # noqa: BLE001 — probe is best-effort
                pass
            # Meta's OWN boost verdict for the media (separate call so an API
            # version without the field cannot poison the base probe). Live
            # 2026-09-08: reel Dc-4BTwgY0Z returned eligible_to_boost=true while
            # create_creative got 1815279 → the failure was NOT content
            # ineligibility, and the old message blamed licensed music wrongly.
            _eligible: Optional[bool] = None
            try:
                _eprobe = _svc._get(
                    f"{_svc.BASE_URL}/{ig_media_id}",
                    {"access_token": token, "fields": "boost_eligibility_info"},
                )
                if isinstance(_eprobe, dict) and not _eprobe.get("error"):
                    _bei = _eprobe.get("boost_eligibility_info")
                    if isinstance(_bei, dict) and "eligible_to_boost" in _bei:
                        _eligible = bool(_bei.get("eligible_to_boost"))
            except Exception:  # noqa: BLE001 — probe is best-effort
                pass
            _linked = _svc.find_page_for_ig_user(token, _ig_owner) if _ig_owner else {}
            _brief_pg = str(creative_cfg.get("page_id") or page_id_creative or "").strip()
            _pages_to_try = [p for p in dict.fromkeys([_linked.get("page_id") or "", _brief_pg]) if p]
            _vd = None
            _new_oid = ""
            _vd_pg = ""
            _fb_post_promotable = False
            _lookup_notes: List[str] = []
            if not _perma:
                _lookup_notes.append("could not read the IG media permalink, so no Page was searched")
            if _perma and not _pages_to_try:
                _lookup_notes.append("no Facebook Page is linked to the media's IG account and the brief named none")
            for _pg_try in _pages_to_try:
                if not _perma:
                    break
                try:
                    _xp = _svc.resolve_ig_permalink_to_fb_xpost(
                        token, _perma, _pg_try,
                        instagram_user_id=_ig_owner or None,
                    )
                except Exception as _xe:  # noqa: BLE001
                    _xp = {"error": {"message": str(_xe)}}
                if isinstance(_xp, dict) and _xp.get("error"):
                    _lookup_notes.append(f"page {_pg_try}: {_api_error_message(_xp) or _xp.get('error')}")
                    continue
                _new_oid = (_xp.get("object_story_id") if isinstance(_xp, dict) else "") or ""
                if not _new_oid:
                    _lookup_notes.append(
                        f"page {_pg_try}: {str((_xp or {}).get('reason') or 'no cross-post found')}"
                    )
                    continue
                _fb_post_promotable = bool(((_xp.get("fb_post") or {}).get("is_eligible_for_promotion")))
                _ptok = _svc.get_page_access_token(_pg_try, token) or token
                _vd = _resolve_video_data_from_post(_new_oid, _pg_try, _ptok, token)
                if _vd or _fb_post_promotable:
                    _vd_pg = _pg_try
                    break
                _lookup_notes.append(
                    f"page {_pg_try}: cross-post {_new_oid} found but its video could not be read"
                )
            _rescued_via_post = False
            if _new_oid and _vd_pg and _fb_post_promotable:
                # Preferred rescue: the FB copy is itself promotable, so boost
                # it directly (object_story_id) — keeps the original post's
                # likes/comments, and is the same path the "Post N" campaigns on
                # this account already use. Falls back to video_data below on
                # any Meta rejection.
                _cr_post = services.create_creative_with_instagram_fallback(
                    account_id,
                    token,
                    name=cname,
                    object_story_id=_new_oid,
                    page_id=_vd_pg,
                    instagram_user_id=creative_cfg.get("instagram_user_id"),
                    instagram_actor_id=creative_cfg.get("instagram_actor_id"),
                    authorization_category=creative_cfg.get("authorization_category"),
                    bylines=creative_bylines or None,
                    destination_spec=_creative_dest_spec,
                )
                if not _has_api_error(_cr_post):
                    cr = _cr_post
                    _rescued_via_post = True
                    auto_fixes.append(
                        f"Meta rejected IG media {ig_media_id} with 1815279"
                        + (" although boost_eligibility_info says eligible_to_boost=true" if _eligible is True else "")
                        + f". Auto-routed to the promotable FB cross-post {_new_oid} on page {_vd_pg} "
                        "(object_story_id) — same video, original post's social proof kept."
                    )
                else:
                    _lookup_notes.append(
                        f"FB post {_new_oid} rejected as creative: {_api_error_message(_cr_post)}"
                    )
            if _rescued_via_post:
                pass
            elif _vd:
                auto_fixes.append(
                    f"Meta rejected IG media {ig_media_id} with 1815279 (video must be on "
                    f"Facebook). Auto-routed via FB cross-post {_new_oid} on page {_vd_pg} "
                    f"and rebuilt the creative as video_data (video_id={_vd.get('video_id')}). "
                    "New creative — no original-post social proof."
                )
                _vd_spec2: Dict[str, Any] = {"page_id": _vd_pg, "video_data": dict(_vd)}
                if not _vd_spec2["video_data"].get("call_to_action"):
                    _vd_spec2["video_data"]["call_to_action"] = {
                        "type": DEFAULT_CTA,
                        "value": {"link": f"https://www.facebook.com/{_vd_pg}"},
                    }
                cr = services.create_creative_with_instagram_fallback(
                    account_id,
                    token,
                    name=cname,
                    object_story_spec=json.dumps(_vd_spec2),
                    page_id=_vd_pg,
                    instagram_user_id=creative_cfg.get("instagram_user_id"),
                    instagram_actor_id=creative_cfg.get("instagram_actor_id"),
                    authorization_category=creative_cfg.get("authorization_category"),
                    bylines=creative_bylines or None,
                    destination_spec=_creative_dest_spec,
                )
            else:
                # IMPORTANT: cross-posting is NOT a Meta requirement. Since
                # 2023-06-27 Meta removed the "re-upload the IG video to Facebook
                # first" rule — source_instagram_media_id references the reel
                # directly (Marketing API changelog). So a 1815279 here is NOT a
                # blanket "reels can't be boosted via API" rule; it means THIS
                # specific reel is boost-INELIGIBLE. The dominant cause is
                # licensed/third-party music, a filter, or interactive stickers
                # (Meta's boost-ineligibility list); it can also be a
                # rights/collab/region restriction. Cross-posting only "works" as
                # a side-effect (it puts the video in FB's library), it is not the
                # actual fix — surface the real cause so the user acts correctly.
                # Eligibility probe: surface Meta's OWN verdict
                # (boost_eligibility_info.eligible_to_boost — DOES exist on the IG
                # media node, v25) + Meta's error_user_* reason + why the FB
                # cross-post rescue could not run. Message built by the pure
                # helper so it stays unit-testable without the sync pipeline.
                auto_fixes.append(
                    _reel_boost_ineligible_message(
                        ig_media_id,
                        _cr_err,
                        media_product_type=_mprod,
                        media_type=_mtype,
                        permalink=_perma,
                        pages=_pages_to_try or "[]",
                        eligible_to_boost=_eligible,
                        lookup_notes="; ".join(_lookup_notes),
                    )
                )

        if _has_api_error(cr):
            return _sync_fail_with_rollback(
                auto_fixes, cr.get("error"), "create_creative", token, options,
                campaign_id=campaign_id, adset_id=adset_id,
            )
        if isinstance(cr, dict) and cr.get("_instagram_fallback_used"):
            auto_fixes.append(f"Instagram identity fallback used: {cr.get('_instagram_fallback_used')}")
        creative_id = str(cr.get("id") or "").strip()
    elif mode == "WHATSAPP":
        wa = str(creative_cfg.get("whatsapp_phone_e164") or creative_cfg.get("whatsapp_number") or "").strip()
        if not page_id_creative or not wa:
            return _sync_fail_with_rollback(
                auto_fixes,
                {"message": "WHATSAPP mode requires page_id and whatsapp_phone_e164"},
                "creative",
                token,
                options,
                campaign_id=campaign_id,
                adset_id=adset_id,
            )
        spec = {
            "page_id": page_id_creative,
            "link_data": {
                "link": f"https://wa.me/{re.sub(r'[^0-9]', '', wa)}",
                "message": str(creative_cfg.get("message") or ""),
                "call_to_action": {"type": "WHATSAPP_MESSAGE", "value": {"whatsapp_number": wa}},
            },
        }
        if creative_cfg.get("instagram_user_id") or creative_cfg.get("instagram_actor_id"):
            cr = services.create_creative_with_instagram_fallback(
                account_id, token, name=cname, object_story_spec=json.dumps(spec),
                page_id=page_id_creative,
                instagram_user_id=creative_cfg.get("instagram_user_id"),
                instagram_actor_id=creative_cfg.get("instagram_actor_id"),
                destination_spec=_creative_dest_spec,
            )
        else:
            cr = services.create_creative(account_id, token, name=cname, object_story_spec=json.dumps(spec), destination_spec=_creative_dest_spec)
        if _has_api_error(cr) and not strict:
            auto_fixes.append(f"WHATSAPP creative failed, retry LEARN_MORE: {_api_error_message(cr)}")
            spec["link_data"]["call_to_action"] = {"type": DEFAULT_CTA, "value": {"link": spec["link_data"]["link"]}}
            cr = services.create_creative(account_id, token, name=cname, object_story_spec=json.dumps(spec), destination_spec=_creative_dest_spec)
        if _has_api_error(cr):
            return _sync_fail_with_rollback(
                auto_fixes, cr.get("error"), "create_creative", token, options,
                campaign_id=campaign_id, adset_id=adset_id,
            )
        if isinstance(cr, dict) and cr.get("_instagram_fallback_used"):
            auto_fixes.append(f"Instagram identity fallback used: {cr.get('_instagram_fallback_used')}")
        creative_id = str(cr.get("id") or "").strip()
    else:
        link = str(creative_cfg.get("link_url") or "").strip()
        if not page_id_creative or not link:
            return _sync_fail_with_rollback(
                auto_fixes,
                {"message": "LINK_AD requires page_id and link_url"},
                "creative",
                token,
                options,
                campaign_id=campaign_id,
                adset_id=adset_id,
            )
        msg = str(creative_cfg.get("message") or "")
        # Headline (link_data.name, ≤40 chars in Ads Manager) and description
        # (link_data.description, ≤30 chars) are separate elements in the ad
        # preview. Accept both naming conventions briefs use.
        headline = (
            creative_cfg.get("headline")
            or creative_cfg.get("link_headline")
            or creative_cfg.get("name")
        )
        description = (
            creative_cfg.get("description")
            or creative_cfg.get("link_description")
        )
        if strict and not (headline and str(headline).strip()):
            return _sync_fail_with_rollback(
                auto_fixes,
                {"message": "strict_mode: document.creative.headline is required for LINK_AD"},
                "creative",
                token,
                options,
                campaign_id=campaign_id,
                adset_id=adset_id,
            )
        if not (headline and str(headline).strip()):
            auto_fixes.append(
                "LINK_AD created without headline — Meta will fall back to the page name; "
                "recommend supplying creative.headline in the brief."
            )
        if not (description and str(description).strip()):
            auto_fixes.append(
                "LINK_AD created without description — Meta will leave the description blank; "
                "recommend supplying creative.description in the brief."
            )
        # Resolve CTA shape from the document. The brief's "call_to_action" can be:
        #   - {"type": "CALL_NOW", "value": {"phone_number": "+91..."}}  (CALL_NOW)
        #   - {"type": "SIGN_UP"}                                         (lead form, value injected)
        #   - just a string (legacy "cta_type")
        _cta_doc = creative_cfg.get("call_to_action")
        if isinstance(_cta_doc, dict):
            cta_for_spec = _cta_doc.get("type") or creative_cfg.get("cta_type") or DEFAULT_CTA
            cta_value_for_spec = _cta_doc.get("value") if isinstance(_cta_doc.get("value"), dict) else None
        else:
            cta_for_spec = creative_cfg.get("cta_type") if strict else str(creative_cfg.get("cta_type") or DEFAULT_CTA)
            cta_value_for_spec = None
        # Lead form id can live at creative.lead_gen_form_id or .leadgen_form_id
        lead_gen_form_id = creative_cfg.get("lead_gen_form_id") or creative_cfg.get("leadgen_form_id")
        spec = build_link_object_story_spec(
            page_id_creative, link, msg, cta_for_spec, creative_cfg.get("image_hash"), strict=strict,
            cta_value=cta_value_for_spec, lead_gen_form_id=lead_gen_form_id,
            name=headline, description=description,
        )
        if lead_gen_form_id:
            auto_fixes.append(
                f"Embedded lead_gen_form_id={lead_gen_form_id} in creative.call_to_action.value "
                "(Meta requires it on the creative for LEAD_GENERATION ads)."
            )
        raw_spec = creative_cfg.get("object_story_spec")
        if raw_spec:
            if isinstance(raw_spec, str):
                try:
                    raw_spec = json.loads(raw_spec)
                except (TypeError, ValueError):
                    raw_spec = None
            if isinstance(raw_spec, dict):
                spec = _deep_merge(spec, raw_spec)
        # Pre-flight: image hashes are account-scoped on Meta. If the brief's
        # image_hash was uploaded to a different ad account than the one this
        # campaign targets, the API call below succeeds but the resulting ad
        # shows "Image not found" in Ads Manager. Verify the hash resolves in
        # this account before we create the creative. On any Graph error
        # (e.g. token lacks ads_read on this account), we proceed — only an
        # explicit empty-data response from Meta blocks.
        _img_hash_pf = creative_cfg.get("image_hash")
        if _img_hash_pf:
            _hash_check = services.check_image_hash_in_account(
                account_id, token, _img_hash_pf,
            )
            if _hash_check.get("known") and not _hash_check.get("found"):
                _act_str = str(account_id).strip()
                if not _act_str.startswith("act_"):
                    _act_str = f"act_{_act_str}"
                _msg = (
                    f"Image hash {_img_hash_pf} is not in account {_act_str}'s "
                    "image library. Image hashes are account-scoped — re-run "
                    f"facebook_upload_image against {_act_str}, or pass an "
                    "image_url so Meta can re-fetch the bytes into this account."
                )
                auto_fixes.append(_msg)
                return _sync_fail_with_rollback(
                    auto_fixes,
                    {"message": _msg, "type": "image_hash_account_mismatch",
                     "image_hash": _img_hash_pf, "account_id": _act_str},
                    "create_creative", token, options,
                    campaign_id=campaign_id, adset_id=adset_id,
                )
        # Always go through the Instagram fallback when a page is set: the
        # fallback tries explicit IG identity first, then auto-discovers via
        # /{page_id}/instagram_business_accounts and the page-backed IG
        # endpoint, falling back to FB-only as the last attempt. So briefs
        # that omit IG still get an Instagram identity when the page has one.
        cr = services.create_creative_with_instagram_fallback(
            account_id,
            token,
            name=cname,
            object_story_spec=json.dumps(spec),
            page_id=page_id_creative,
            instagram_user_id=creative_cfg.get("instagram_user_id"),
            instagram_actor_id=creative_cfg.get("instagram_actor_id"),
            destination_spec=_creative_dest_spec,
        )
        if _has_api_error(cr):
            return _sync_fail_with_rollback(
                auto_fixes, cr.get("error"), "create_creative", token, options,
                campaign_id=campaign_id, adset_id=adset_id,
            )
        if isinstance(cr, dict) and cr.get("_instagram_fallback_used"):
            auto_fixes.append(f"Instagram identity fallback used: {cr.get('_instagram_fallback_used')}")
        creative_id = str(cr.get("id") or "").strip()

    if strict:
        ad_name = str(ad_cfg.get("name") or "").strip()
    else:
        ad_name = str(ad_cfg.get("name") or f"ad-{adset_id}").strip()

    # Ad-level tracking_specs: when the ad set has a pixel attached and the
    # caller didn't opt out, fire a matching attribution event on the ad
    # itself. This is separate from the ad set's optimization goal — the
    # funnel report still attributes correctly even when the goal is e.g.
    # LINK_CLICKS but the user wants to count landing-page-views.
    ad_tracking_specs: Optional[List[Dict[str, Any]]] = None
    if not options.get("skip_ad_tracking_specs") and tracking.get("pixel_id"):
        ad_tracking_specs = _build_ad_tracking_specs(
            str(tracking.get("pixel_id") or ""),
            str(ads_cfg.get("optimization_goal") or ""),
        )
        if ad_tracking_specs:
            auto_fixes.append(
                f"Set ad-level tracking_specs: action.type={ad_tracking_specs[0]['action.type'][0]} "
                f"on pixel {tracking.get('pixel_id')}"
            )

    ad_resp = services.create_ad(
        account_id,
        token,
        adset_id,
        ad_name,
        creative_id=creative_id,
        status=str(ad_cfg.get("status") or "PAUSED"),
        tracking_specs=ad_tracking_specs,
    )
    if _has_api_error(ad_resp):
        return _sync_fail_with_rollback(
            auto_fixes, ad_resp.get("error"), "create_ad", token, options,
            campaign_id=campaign_id, adset_id=adset_id, creative_id=creative_id,
        )
    ad_id = str(ad_resp.get("id") or "").strip()

    _pause_default = False if strict else True
    if options.get("pause_other_adsets_in_campaign", _pause_default):
        pause_other_adsets_in_campaign(account_id, token, campaign_id, adset_id, auto_fixes)

    verify_report = None
    _verify_default = False if strict else True
    if options.get("verify_after_create", _verify_default):
        # Do not assert link-style CTA on EXISTING_POST creatives (often no link_data / CTA on the creative object).
        # Read CTA from both the legacy creative.cta_type AND the modern
        # creative.call_to_action.type shape — whichever the brief used.
        _cta_doc_for_verify = creative_cfg.get("call_to_action")
        _cta_modern = (
            (_cta_doc_for_verify.get("type") if isinstance(_cta_doc_for_verify, dict) else None)
            or creative_cfg.get("cta_type")
        )
        if mode == "EXISTING_POST":
            _cta_expect = None
        elif strict:
            _cta_expect = _cta_modern
        else:
            _cta_expect = _cta_modern or DEFAULT_CTA
        # Lead ads have their CTA forced by build_link_object_story_spec when
        # lead_gen_form_id is present (SIGN_UP is the safe default). Adjust
        # expectation to match what was actually written.
        if creative_cfg.get("lead_gen_form_id") or creative_cfg.get("leadgen_form_id"):
            _LEAD_CTAS = {"SIGN_UP", "GET_QUOTE", "SUBSCRIBE", "APPLY_NOW", "LEARN_MORE", "BOOK_TRAVEL", "DOWNLOAD"}
            if str(_cta_expect or "").upper() not in _LEAD_CTAS:
                _cta_expect = "SIGN_UP"
        og_verify = ads_cfg.get("optimization_goal")
        skip_pixel_expect = (
            tracking.get("pixel_id")
            and mode == "EXISTING_POST"
            and services.is_post_engagement_on_post(og_verify, destination_type)
        )
        expected = {
            "pixel_id": (
                None
                if skip_pixel_expect
                else (str(tracking["pixel_id"]).strip() if tracking.get("pixel_id") else None)
            ),
            "destination_type": destination_type,
            "require_targeting": True,
            # IG-direct and video_data creatives legitimately do NOT carry an
            # object_story_id (they use source_instagram_media_id / video_data),
            # so don't demand an existing-post creative for those — otherwise
            # verify falsely reports ok=false on a perfectly good ad.
            "expect_existing_post": (
                mode == "EXISTING_POST"
                and not _ig_direct_cr
                and not _video_data_override
            ),
            "expect_link_ad": mode == "LINK_AD",
            "cta_type": _cta_expect,
        }
        verify_report = services.verify_ad_setup(token, campaign_id=campaign_id, adset_id=adset_id, ad_id=ad_id, expected=expected)
        if not strict:
            for issue in verify_report.get("issues") or []:
                if issue.get("level") == "adset" and "pixel_id" in (issue.get("message") or "") and tracking.get("pixel_id"):
                    # Don't try to re-attach for CALL goals — pixel attribution
                    # doesn't apply (see the main pixel-attach branch).
                    if _opt_goal_for_pixel in _NO_PIXEL_ATTACH_GOALS:
                        auto_fixes.append(
                            f"Verify reported pixel mismatch but {_opt_goal_for_pixel} "
                            "goal doesn't use website pixel — re-attach skipped."
                        )
                        continue
                    ap2 = services.attach_pixel_to_adset(
                        adset_id,
                        token,
                        str(tracking["pixel_id"]).strip(),
                        custom_event_type=tracking.get("custom_event_type"),
                        page_id=tracking.get("page_id") or page_id_creative,
                    )
                    if _has_api_error(ap2):
                        auto_fixes.append(f"Re-attach pixel failed: {_api_error_message(ap2)}")
                    elif isinstance(ap2, dict) and ap2.get("_pixel_not_attached"):
                        auto_fixes.append(str(ap2.get("reason") or "Pixel attach skipped for this ad set type."))
                    else:
                        auto_fixes.append("Re-ran attach_pixel_to_adset after verify reported pixel mismatch")
                if issue.get("level") == "creative" and "CTA" in (issue.get("message") or "") and mode == "LINK_AD":
                    # Skip the CTA-rewrite if this creative is a lead ad —
                    # it MUST keep call_to_action.value.lead_gen_form_id, and
                    # the rewrite path drops that field, breaking the lead
                    # form linkage.
                    _has_lead_form = bool(
                        creative_cfg.get("lead_gen_form_id")
                        or creative_cfg.get("leadgen_form_id")
                    )
                    if _has_lead_form:
                        auto_fixes.append(
                            "Skipped post-verify CTA rewrite (creative has lead_gen_form_id; "
                            "rewrite would drop the lead form linkage)."
                        )
                    else:
                        link = str(creative_cfg.get("link_url") or "").strip()
                        spec2 = build_link_object_story_spec(
                            page_id_creative, link, str(creative_cfg.get("message") or ""), DEFAULT_CTA, creative_cfg.get("image_hash"),
                        )
                        cr2 = services.create_creative(
                            account_id,
                            token,
                            name=cname + "-cta-fix",
                            object_story_spec=json.dumps(spec2),
                            instagram_user_id=creative_cfg.get("instagram_user_id"),
                            instagram_actor_id=creative_cfg.get("instagram_actor_id"),
                            destination_spec=_creative_dest_spec,
                        )
                        if not _has_api_error(cr2):
                            services.update_ad(ad_id, token, creative_id=str(cr2.get("id")))
                            auto_fixes.append("Updated ad creative to force CTA LEARN_MORE after verify")
            verify_report = services.verify_ad_setup(token, campaign_id=campaign_id, adset_id=adset_id, ad_id=ad_id, expected=expected)

    # --- Save doc_id mapping ---
    if doc_id:
        if not mapping_action:
            mapping_action = "created"
        _save_doc_mapping(
            doc_id, user_id, account_id,
            campaign_id=campaign_id, adset_id=adset_id,
            ad_id=ad_id, creative_id=creative_id or "",
            doc_hash=doc_hash,
            doc_json=json.dumps(doc, default=str),
        )
        if not strict:
            auto_fixes.append(f"doc_id mapping {mapping_action}: doc_id={doc_id!r} → campaign={campaign_id}, adset={adset_id}, ad={ad_id}")

    promoted_snapshot = promoted_object if isinstance(promoted_object, dict) else None
    result = {
        "ok": bool(verify_report.get("ok") if verify_report else True),
        "campaign_id": campaign_id,
        "adset_id": adset_id,
        "creative_id": creative_id,
        "ad_id": ad_id,
        "auto_fixes": auto_fixes,
        "verify": verify_report,
    }
    if strict:
        result["strict_mode"] = True
        result["strict_debug"] = {
            "user_input": user_input_snapshot,
            "document_used": doc,
            "final_targeting": targeting,
            "final_promoted_object": promoted_snapshot,
            "final_destination_type": destination_type,
            "creative_mode": mode,
        }
    if doc_id:
        result["doc_id"] = doc_id
        result["mapping_action"] = mapping_action
    if strict and result.get("ok"):
        _clear_strict_preview_ok(account_id, doc_hash)
    return result


# ---------------------------------------------------------------------------
# Batch document sync — one call, many campaigns
# ---------------------------------------------------------------------------

_POST_NUM_PLACEHOLDERS = ("{post_number}", "{post}", "{n}")

# Shorthand keys a posts[] item may carry, and the document section each one
# belongs to. Anything else the item carries must be a full section dict
# (campaign/adset/creative/ad/targeting/...) and is deep-merged as-is.
_POST_CREATIVE_SHORTHAND = (
    "instagram_permalink_url",
    "source_instagram_media_id",
    "object_story_id",
    "instagram_url",
    "url",
)
_POST_NAME_SHORTHAND = {
    "campaign_name": ("campaign", "name"),
    "adset_name": ("adset", "name"),
    "ad_name": ("ad", "name"),
}


def _deep_merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursive dict merge: override wins; nested dicts merge key-by-key."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _apply_post_number_to_name(name: str, post_number: int) -> str:
    """Make an entity name unique per post, generically.

    Priority: explicit placeholder ({post_number}/{post}/{n}) > an existing
    "Post <digits>" token in the template (common brief naming — renumbered)
    > append " | Post <N>". Never returns identical names for two posts.
    """
    s = str(name or "").strip()
    if not s:
        return f"Post {post_number}"
    for ph in _POST_NUM_PLACEHOLDERS:
        if ph in s:
            return s.replace(ph, str(post_number))
    renumbered, hits = re.subn(r"(?i)\bpost\s*\d+\b", f"Post {post_number}", s, count=1)
    if hits:
        return renumbered
    return f"{s} | Post {post_number}"


def _post_item_to_document(template: Dict[str, Any], item: Dict[str, Any], post_number: int) -> Dict[str, Any]:
    """Build the per-post document: template deep-copy + per-post overrides."""
    doc = json.loads(json.dumps(template))
    item = dict(item or {})
    item.pop("post_number", None)

    creative_over: Dict[str, Any] = {}
    for key in _POST_CREATIVE_SHORTHAND:
        val = str(item.pop(key, "") or "").strip()
        if not val:
            continue
        if key in ("instagram_url", "url"):
            key = "instagram_permalink_url"
        creative_over[key] = val

    name_over: Dict[str, Dict[str, Any]] = {}
    for key, (section, field_name) in _POST_NAME_SHORTHAND.items():
        val = str(item.pop(key, "") or "").strip()
        if val:
            name_over.setdefault(section, {})[field_name] = val

    # Whatever remains in the item must be full document sections.
    section_over = {k: v for k, v in item.items() if isinstance(v, dict)}

    for over in (({"creative": creative_over} if creative_over else {}), name_over, section_over):
        if over:
            doc = _deep_merge_dict(doc, over)

    # Number every entity name so the batch never produces name collisions.
    for section in ("campaign", "adset", "ad"):
        sec = doc.get(section)
        if isinstance(sec, dict) and sec.get("name"):
            sec["name"] = _apply_post_number_to_name(sec["name"], post_number)
    return doc


_CAPTION_NAME_WORDS = 50
_CAPTION_NAME_MAX_CHARS = 350  # Meta's entity-name hard cap is 400; keep margin.
_IG_SHORTCODE_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_\-]+)")


def _caption_to_name(caption: str, words: int = _CAPTION_NAME_WORDS) -> str:
    """First N whitespace-separated words of a caption, clipped for Meta names."""
    name = " ".join(str(caption or "").split()[:words]).strip()
    if len(name) > _CAPTION_NAME_MAX_CHARS:
        name = name[:_CAPTION_NAME_MAX_CHARS].rstrip()
    return name


def _ig_media_caption_index(token: str, page_id: str, instagram_user_id: str) -> Dict[str, str]:
    """ONE paged crawl of the IG account's media → {shortcode|media_id: caption}.

    Serves caption lookups for every post in a batch with a single Graph
    crawl, instead of the agent fetching captions post-by-post (which hit
    lookup caps and degraded names to shortcode fallbacks).
    """
    res = services.get_instagram_media(
        token,
        page_id=page_id or None,
        instagram_user_id=instagram_user_id or None,
        fields="id,caption,permalink",
    )
    idx: Dict[str, str] = {}
    for m in (res.get("data") or []) if isinstance(res, dict) else []:
        cap = str(m.get("caption") or "")
        mid = str(m.get("id") or "")
        if mid:
            idx[mid] = cap
        pm = _IG_SHORTCODE_RE.search(str(m.get("permalink") or ""))
        if pm:
            idx[pm.group(1)] = cap
    return idx


def sync_document_campaigns_batch(
    account_id: str, token: str, document: Any, posts: Any,
    user_id: str = "", user_confirmed: bool = False,
    time_budget_seconds: Any = None,
) -> Dict[str, Any]:
    """Create one campaign + ad set + creative + ad PER posts[] item, in ONE call.

    Why: agent loops burn one LLM iteration per tool call, and multi-post briefs
    ("5 reels -> 5 campaigns, same settings") exhausted the iteration budget by
    driving sync_document_campaign once per post. Here plain code does the loop:
    the shared `document` template is deep-copied per post, the post's creative
    source + names are merged in, and the full existing sync flow (validation,
    pre-flights, political bylines, rollback) runs per post.

    Per-post failures do NOT abort the batch — each post gets an independent
    result and the per-post rollback inside sync_document_campaign still applies.

    posts[] item keys (all optional except one creative source):
      post_number                              — defaults to list position (1-based)
      instagram_permalink_url | instagram_url | url — IG post/Reel permalink
      source_instagram_media_id                — IG media id (posts/Reels/videos)
      object_story_id                          — FB page post (page_id_post_id)
      campaign_name / adset_name / ad_name     — full per-post name overrides
      campaign / adset / creative / ad / targeting — dict deep-merge overrides

    Naming: template names may carry {post_number} (or {post}/{n}); an existing
    "Post <digits>" token is renumbered; otherwise " | Post <N>" is appended.
    """
    if isinstance(document, str):
        document = json.loads(document)
    if not isinstance(document, dict):
        raise ValueError("document must be a JSON object (the shared template)")
    if isinstance(posts, str):
        posts = json.loads(posts)
    if not isinstance(posts, list) or not posts:
        raise ValueError("posts must be a non-empty JSON array of per-post objects")

    # The per-call time budget must stay UNDER the smallest timeout on the path
    # between the chat service and this server — in production that is the
    # reverse proxy's ~60s read timeout, NOT the chat side's 300s
    # AGENT_MCP_CALL_TIMEOUT. With the old 240s default the proxy cut the
    # connection mid-batch: the work completed here but the agent received an
    # EMPTY response, reported the posts as "not created", and re-sent them —
    # every retry created real duplicate campaigns (2026-08-18 incident: 21
    # campaigns for 7 posts). 45s keeps each chunk's response deliverable;
    # untouched posts come back as continuation.remaining_posts and the agent
    # immediately re-calls with them.
    try:
        budget = float(time_budget_seconds if time_budget_seconds is not None
                       else os.environ.get("FACEBOOK_BATCH_TIME_BUDGET_SECONDS", "45"))
    except (TypeError, ValueError):
        budget = 45.0
    started = time.monotonic()

    # Duplicate guard (idempotency). If a response is ever lost anyway (proxy
    # blip, chat-side crash) the agent WILL re-send already-created posts; the
    # guard makes that harmless. One Graph call prefetches the account's recent
    # campaigns; each post is skipped as already_exists when its final campaign
    # name matches exactly OR its IG shortcode appears in an existing campaign
    # name (covers renamed variants). Best-effort: prefetch failure disables the
    # guard, never the batch. Opt out via document.options.allow_duplicate_campaigns
    # or per-post item key allow_duplicate=true.
    _allow_dupes = bool((document.get("options") or {}).get("allow_duplicate_campaigns"))
    _existing: List[Tuple[str, str]] = []  # (campaign name casefolded, campaign id)
    if not _allow_dupes:
        try:
            _win_h = float(os.environ.get("FACEBOOK_BATCH_DEDUP_WINDOW_HOURS", "48"))
        except (TypeError, ValueError):
            _win_h = 48.0
        try:
            _cut = datetime.now(_dt_tz.utc) - timedelta(hours=_win_h)
            _resp = services.get_campaigns(
                account_id, token,
                fields="id,name,created_time,effective_status", limit=200,
            )
            for _c in (_resp.get("data") or []) if isinstance(_resp, dict) else []:
                if str(_c.get("effective_status") or "").upper() == "DELETED":
                    continue
                _ct = str(_c.get("created_time") or "").replace("+0000", "+00:00")
                try:
                    if datetime.fromisoformat(_ct.replace("Z", "+00:00")) < _cut:
                        continue
                except ValueError:
                    continue
                _existing.append(
                    ((_c.get("name") or "").strip().casefold(), str(_c.get("id")))
                )
        except Exception as _exc:  # noqa: BLE001 — guard is best-effort
            logger.warning(
                "batch dedup guard: campaign prefetch failed (%s); guard disabled "
                "for this call", _exc,
            )

    # Caption-based naming: name the campaign/adset/ad with the first 50 words
    # of the post's OWN IG caption. Briefs like "the name must be only the post
    # caption" previously depended on the AGENT fetching every caption
    # (rate-capped → shortcode fallback names like "... Reel DcK3Z6dDTOx").
    # One media crawl per IG account now serves every post in the batch.
    #
    # DEFAULT ON (owner directive 2026-08-19: "ab se caption name hi dega"):
    # options.caption_names unset → captions are used whenever the template
    # carries the IG identity (creative.page_id / instagram_user_id). Pass
    # options.caption_names=false when a brief dictates its own naming scheme.
    # Explicit per-post campaign_name / adset_name / ad_name overrides always
    # win either way; caption-missing posts keep template naming with a note.
    _cn_opt = (document.get("options") or {}).get("caption_names")
    _caption_names = True if _cn_opt is None else bool(_cn_opt)
    _caption_idx: Optional[Dict[str, str]] = None
    if _caption_names:
        _tmpl_cr = document.get("creative") or {}
        _cn_page = str(_tmpl_cr.get("page_id") or "").strip()
        _cn_ig = str(_tmpl_cr.get("instagram_user_id") or "").strip()
        if not (_cn_page or _cn_ig):
            # No IG identity in the template → nothing to crawl. Silent skip
            # keeps non-IG batches (link ads, FB posts) on template naming.
            _caption_names = False
    if _caption_names:
        try:
            _caption_idx = _ig_media_caption_index(token, _cn_page, _cn_ig)
        except Exception as _exc:  # noqa: BLE001 — naming is best-effort
            logger.warning(
                "caption_names: IG media crawl failed (%s); keeping template "
                "naming for this call", _exc,
            )
            _caption_idx = None
    _caption_used: Dict[str, str] = {}  # caption-name -> shortcode that took it

    results: List[Dict[str, Any]] = []
    remaining: List[Dict[str, Any]] = []
    for idx, raw_item in enumerate(posts):
        if budget > 0 and (time.monotonic() - started) > budget and results:
            # Pin each untouched item's post_number to its ORIGINAL position so
            # names continue "Post 21..50" on the next call, not restart at 1.
            for j, rem in enumerate(posts[idx:], start=idx):
                rem_item = dict(rem) if isinstance(rem, dict) else {}
                rem_item.setdefault("post_number", j + 1)
                remaining.append(rem_item)
            break
        item = raw_item if isinstance(raw_item, dict) else {}
        try:
            post_number = int(item.get("post_number") or (idx + 1))
        except (TypeError, ValueError):
            post_number = idx + 1
        entry: Dict[str, Any] = {"post_number": post_number}
        # Read explicit per-post name overrides BEFORE _post_item_to_document
        # pops them off the item — caption naming must not clobber these.
        _explicit_names = {
            sec for sec, key in (
                ("campaign", "campaign_name"), ("adset", "adset_name"), ("ad", "ad_name"))
            if item.get(key)
        }
        _caption_note = ""
        try:
            doc = _post_item_to_document(document, item, post_number)
            _crv = doc.get("creative") or {}
            _scm = _IG_SHORTCODE_RE.search(str(_crv.get("instagram_permalink_url") or ""))
            _sc = _scm.group(1).casefold() if _scm else ""
            if _caption_idx is not None:
                _key = (_scm.group(1) if _scm else "") or str(
                    _crv.get("source_instagram_media_id") or "")
                _cap_name = _caption_to_name(_caption_idx.get(_key, ""))
                if _cap_name:
                    # Two different posts can share identical first-50-words —
                    # keep names unique so the dedup guard can't mistake the
                    # second post for a retry of the first.
                    _prior_sc = _caption_used.get(_cap_name.casefold())
                    if _prior_sc is not None and _prior_sc != _sc:
                        _cap_name = f"{_cap_name} — {_scm.group(1) if _scm else post_number}"
                        _caption_note = (
                            "caption_names: another post in this batch has the "
                            "same caption; appended the shortcode to keep the "
                            "name unique"
                        )
                    else:
                        _caption_used[_cap_name.casefold()] = _sc
                    for _secn in ("campaign", "adset", "ad"):
                        if _secn not in _explicit_names:
                            doc.setdefault(_secn, {})["name"] = _cap_name
                else:
                    _caption_note = (
                        "caption_names: no caption found for this post (media "
                        "not on the connected IG account, or empty caption) — "
                        "kept template naming"
                    )
            _cname = str(((doc.get("campaign") or {}).get("name")) or "").strip().casefold()
            if not _allow_dupes and not item.get("allow_duplicate"):
                _hit_id = next(
                    (
                        _eid for _ename, _eid in _existing
                        if (_cname and _ename == _cname) or (_sc and _sc in _ename)
                    ),
                    None,
                )
                if _hit_id:
                    entry.update({
                        "ok": True,
                        "skipped": "already_exists",
                        "campaign_id": _hit_id,
                        "note": (
                            "A campaign for this post already exists in this "
                            f"account (id={_hit_id}, created within the last "
                            "dedup window) — NOT creating a duplicate. Report "
                            "it to the user as already created. Force with "
                            "options.allow_duplicate_campaigns=true or the "
                            "post item key allow_duplicate=true."
                        ),
                    })
                    results.append(entry)
                    continue
            res = sync_document_campaign(
                account_id, token, doc, user_id=user_id, user_confirmed=user_confirmed,
            )
        except Exception as exc:  # noqa: BLE001 — one bad post must not kill the batch
            entry.update({"ok": False, "error": {"message": str(exc)}})
            results.append(entry)
            continue
        if isinstance(res, dict) and res.get("campaign_id") and not res.get("error"):
            # "ok" = all entities created. sync's own ok flag folds in the
            # post-create verify, which reports false for ads sitting in
            # Meta's mandatory review (PENDING_REVIEW — every political ad)
            # — that is delivery state, not a creation failure. Surface it
            # separately as verify_ok so 5 created campaigns never read as
            # "succeeded: 0/5".
            entry.update({
                "ok": bool(res.get("ad_id")),
                "verify_ok": bool(res.get("ok", True)),
                "campaign_id": res.get("campaign_id"),
                "adset_id": res.get("adset_id"),
                "creative_id": res.get("creative_id"),
                "ad_id": res.get("ad_id"),
                "auto_fixes": res.get("auto_fixes") or [],
            })
            if not entry["verify_ok"] and isinstance(res.get("verify"), dict):
                entry["verify"] = res["verify"]
            # Register the fresh campaign (name AND shortcode) so a duplicate
            # WITHIN this same batch (same post sent twice in posts[]) is also
            # caught — even when per-post numbering makes the names differ.
            if entry.get("campaign_id"):
                if _cname:
                    _existing.append((_cname, str(entry["campaign_id"])))
                if _sc:
                    _existing.append((_sc, str(entry["campaign_id"])))
        else:
            entry.update({
                "ok": False,
                "error": (res.get("error") if isinstance(res, dict) else None)
                or {"message": "sync did not return entity ids", "raw_phase": (res or {}).get("phase") if isinstance(res, dict) else None},
                "auto_fixes": (res.get("auto_fixes") if isinstance(res, dict) else None) or [],
            })
        if _caption_note:
            entry["auto_fixes"] = list(entry.get("auto_fixes") or []) + [_caption_note]
        results.append(entry)

    succeeded = sum(1 for r in results if r.get("ok"))
    skipped = sum(1 for r in results if r.get("skipped"))
    out = {
        "ok": succeeded == len(results) and not remaining,
        "total": len(posts),
        "processed": len(results),
        "succeeded": succeeded,
        "skipped_already_exists": skipped,
        "failed": len(results) - succeeded,
        "results": results,
    }
    if remaining:
        out["continuation"] = {
            "remaining_posts": remaining,
            "message": (
                f"Time budget reached after {len(results)} of {len(posts)} posts. "
                "The remaining posts were NOT started. IMMEDIATELY call "
                "facebook_sync_document_campaigns_batch again with the SAME "
                "document and posts=continuation.remaining_posts to create the rest. "
                "Do NOT re-send already-processed posts (that would duplicate ads)."
            ),
        }
    return out
