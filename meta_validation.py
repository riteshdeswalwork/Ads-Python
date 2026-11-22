"""
Pre-flight validation for campaign objective, optimization goal, pixel usage, and related document fields.
Reduces wasted Meta API calls and surfaces clear messages to agents/users.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import services


# (campaign_objective_upper, optimization_goal_upper) pairs Meta hard-rejects.
# Sourced verbatim from META_ADS_MASTER_REFERENCE.md §3 "Forbidden Combos".
# UI-verified matrix: OUTCOME_ENGAGEMENT + THRUPLAY is VALID under "Video Views"
# engagement type with "On your ad" destination — Ads Manager exposes
# "Maximise ThruPlay views" there. So it's not in this list.
_FORBIDDEN_OBJECTIVE_OPTIMIZATION: Tuple[Tuple[str, str], ...] = (
    # Video-view goals — only AWARENESS/ENGAGEMENT
    ("OUTCOME_TRAFFIC", "THRUPLAY"),
    ("OUTCOME_LEADS", "THRUPLAY"),
    # Conversion goals — only SALES
    ("OUTCOME_AWARENESS", "OFFSITE_CONVERSIONS"),
    ("OUTCOME_AWARENESS", "VALUE"),
    ("OUTCOME_TRAFFIC", "OFFSITE_CONVERSIONS"),
    ("OUTCOME_TRAFFIC", "VALUE"),
    ("OUTCOME_ENGAGEMENT", "OFFSITE_CONVERSIONS"),
    ("OUTCOME_ENGAGEMENT", "VALUE"),
    ("OUTCOME_LEADS", "VALUE"),
    # Lead-gen — only LEADS
    ("OUTCOME_TRAFFIC", "LEAD_GENERATION"),
    # App goal — only APP_PROMOTION
    ("OUTCOME_ENGAGEMENT", "APP_INSTALLS"),
    # Engagement-side goals — not in SALES
    ("OUTCOME_SALES", "PAGE_LIKES"),
    ("OUTCOME_SALES", "POST_ENGAGEMENT"),
    # APP_PROMOTION constraints
    ("OUTCOME_APP_PROMOTION", "REACH"),
    ("OUTCOME_APP_PROMOTION", "LEAD_GENERATION"),
    ("OUTCOME_APP_PROMOTION", "POST_ENGAGEMENT"),
)


# For each forbidden pair, suggest 2-3 valid alternatives the user can pick.
# Format: (objective, goal) -> [{label, patch, explanation}]
# 'patch' is a flat-key partial document the caller can merge to fix the brief.
_SUGGESTIONS_FOR_FORBIDDEN: Dict[Tuple[str, str], List[Dict[str, Any]]] = {
    ("OUTCOME_TRAFFIC", "THRUPLAY"): [
        {"label": "Switch objective to OUTCOME_AWARENESS",
         "patch": {"campaign.objective": "OUTCOME_AWARENESS"},
         "explanation": "ThruPlay (video views) works under Awareness for reach-based delivery."},
        {"label": "Switch objective to OUTCOME_ENGAGEMENT",
         "patch": {"campaign.objective": "OUTCOME_ENGAGEMENT"},
         "explanation": "ThruPlay also works under Engagement (Video Views type, On your ad)."},
        {"label": "Keep TRAFFIC, change goal to LINK_CLICKS",
         "patch": {"adset.optimization_goal": "LINK_CLICKS"},
         "explanation": "Use Traffic for clicks-to-website instead of video views."},
    ],
    ("OUTCOME_LEADS", "THRUPLAY"): [
        {"label": "Switch objective to OUTCOME_AWARENESS",
         "patch": {"campaign.objective": "OUTCOME_AWARENESS"},
         "explanation": "ThruPlay works under Awareness for video reach."},
        {"label": "Keep LEADS, change goal to LEAD_GENERATION",
         "patch": {"adset.optimization_goal": "LEAD_GENERATION", "adset.destination_type": "ON_AD"},
         "explanation": "For lead form submissions, use LEAD_GENERATION + ON_AD destination."},
        {"label": "Keep LEADS, change goal to QUALITY_CALL (phone leads)",
         "patch": {"adset.optimization_goal": "QUALITY_CALL", "adset.destination_type": "PHONE_CALL"},
         "explanation": "Optimize for phone calls instead of video views."},
    ],
    ("OUTCOME_AWARENESS", "OFFSITE_CONVERSIONS"): [
        {"label": "Switch objective to OUTCOME_SALES",
         "patch": {"campaign.objective": "OUTCOME_SALES"},
         "explanation": "OFFSITE_CONVERSIONS is a SALES-only goal (needs pixel + purchase events)."},
        {"label": "Keep AWARENESS, change goal to REACH",
         "patch": {"adset.optimization_goal": "REACH"},
         "explanation": "Awareness optimizes for reach, not conversions."},
        {"label": "Keep AWARENESS, change goal to IMPRESSIONS",
         "patch": {"adset.optimization_goal": "IMPRESSIONS"},
         "explanation": "Maximize impressions instead of conversions under Awareness."},
    ],
    ("OUTCOME_AWARENESS", "VALUE"): [
        {"label": "Switch objective to OUTCOME_SALES",
         "patch": {"campaign.objective": "OUTCOME_SALES"},
         "explanation": "VALUE optimization (ROAS) is SALES-only — needs purchase events with value+currency."},
        {"label": "Keep AWARENESS, change goal to REACH",
         "patch": {"adset.optimization_goal": "REACH"},
         "explanation": "Use Awareness with REACH for reach-based delivery."},
    ],
    ("OUTCOME_TRAFFIC", "OFFSITE_CONVERSIONS"): [
        {"label": "Switch objective to OUTCOME_SALES",
         "patch": {"campaign.objective": "OUTCOME_SALES"},
         "explanation": "OFFSITE_CONVERSIONS is SALES-only — needs pixel + conversion events."},
        {"label": "Keep TRAFFIC, change goal to LANDING_PAGE_VIEWS",
         "patch": {"adset.optimization_goal": "LANDING_PAGE_VIEWS"},
         "explanation": "Optimize for actual landing-page loads (still uses pixel)."},
        {"label": "Keep TRAFFIC, change goal to LINK_CLICKS",
         "patch": {"adset.optimization_goal": "LINK_CLICKS"},
         "explanation": "Optimize for raw link clicks (no pixel needed)."},
    ],
    ("OUTCOME_TRAFFIC", "VALUE"): [
        {"label": "Switch objective to OUTCOME_SALES",
         "patch": {"campaign.objective": "OUTCOME_SALES"},
         "explanation": "VALUE/ROAS optimization is SALES-only."},
        {"label": "Keep TRAFFIC, change goal to LINK_CLICKS",
         "patch": {"adset.optimization_goal": "LINK_CLICKS"},
         "explanation": "Maximize traffic via link clicks."},
    ],
    ("OUTCOME_ENGAGEMENT", "OFFSITE_CONVERSIONS"): [
        {"label": "Switch objective to OUTCOME_SALES",
         "patch": {"campaign.objective": "OUTCOME_SALES"},
         "explanation": "Conversions belong under Sales — needs pixel + purchase events."},
        {"label": "Keep ENGAGEMENT, change goal to POST_ENGAGEMENT",
         "patch": {"adset.optimization_goal": "POST_ENGAGEMENT"},
         "explanation": "Engagement optimizes for likes/comments/shares, not conversions."},
        {"label": "Keep ENGAGEMENT, change goal to CONVERSATIONS",
         "patch": {"adset.optimization_goal": "CONVERSATIONS", "adset.destination_type": "MESSENGER"},
         "explanation": "Optimize for Messenger/WhatsApp conversation starts."},
    ],
    ("OUTCOME_ENGAGEMENT", "VALUE"): [
        {"label": "Switch objective to OUTCOME_SALES",
         "patch": {"campaign.objective": "OUTCOME_SALES"},
         "explanation": "VALUE/ROAS is SALES-only."},
        {"label": "Keep ENGAGEMENT, change goal to POST_ENGAGEMENT",
         "patch": {"adset.optimization_goal": "POST_ENGAGEMENT"},
         "explanation": "Default engagement optimization."},
    ],
    ("OUTCOME_LEADS", "VALUE"): [
        {"label": "Switch objective to OUTCOME_SALES",
         "patch": {"campaign.objective": "OUTCOME_SALES"},
         "explanation": "VALUE optimization is SALES-only."},
        {"label": "Keep LEADS, change goal to LEAD_GENERATION",
         "patch": {"adset.optimization_goal": "LEAD_GENERATION", "adset.destination_type": "ON_AD"},
         "explanation": "Optimize for lead form submissions."},
        {"label": "Keep LEADS, change goal to QUALITY_LEAD",
         "patch": {"adset.optimization_goal": "QUALITY_LEAD"},
         "explanation": "Optimize for higher-quality leads (needs CRM integration)."},
    ],
    ("OUTCOME_TRAFFIC", "LEAD_GENERATION"): [
        {"label": "Switch objective to OUTCOME_LEADS",
         "patch": {"campaign.objective": "OUTCOME_LEADS", "adset.destination_type": "ON_AD"},
         "explanation": "Lead-gen forms require Leads objective + ON_AD destination."},
        {"label": "Keep TRAFFIC, change goal to LINK_CLICKS",
         "patch": {"adset.optimization_goal": "LINK_CLICKS"},
         "explanation": "Drive traffic to lead landing page (not Instant Form)."},
    ],
    ("OUTCOME_ENGAGEMENT", "APP_INSTALLS"): [
        {"label": "Switch objective to OUTCOME_APP_PROMOTION",
         "patch": {"campaign.objective": "OUTCOME_APP_PROMOTION",
                   "adset.destination_type": "APP"},
         "explanation": "App-install optimization requires App Promotion objective."},
        {"label": "Keep ENGAGEMENT, change goal to POST_ENGAGEMENT",
         "patch": {"adset.optimization_goal": "POST_ENGAGEMENT"},
         "explanation": "Optimize for engagement on the ad itself."},
    ],
    ("OUTCOME_SALES", "PAGE_LIKES"): [
        {"label": "Switch objective to OUTCOME_ENGAGEMENT",
         "patch": {"campaign.objective": "OUTCOME_ENGAGEMENT"},
         "explanation": "Page Likes is an engagement goal."},
        {"label": "Keep SALES, change goal to OFFSITE_CONVERSIONS",
         "patch": {"adset.optimization_goal": "OFFSITE_CONVERSIONS",
                   "adset.destination_type": "WEBSITE"},
         "explanation": "Sales optimizes for conversion events on website."},
    ],
    ("OUTCOME_SALES", "POST_ENGAGEMENT"): [
        {"label": "Switch objective to OUTCOME_ENGAGEMENT",
         "patch": {"campaign.objective": "OUTCOME_ENGAGEMENT"},
         "explanation": "Post Engagement belongs under Engagement objective."},
        {"label": "Keep SALES, change goal to OFFSITE_CONVERSIONS",
         "patch": {"adset.optimization_goal": "OFFSITE_CONVERSIONS",
                   "adset.destination_type": "WEBSITE"},
         "explanation": "Sales optimizes for conversions, not engagement."},
    ],
    ("OUTCOME_APP_PROMOTION", "REACH"): [
        {"label": "Switch objective to OUTCOME_AWARENESS",
         "patch": {"campaign.objective": "OUTCOME_AWARENESS"},
         "explanation": "Reach optimization is Awareness's primary goal."},
        {"label": "Keep APP_PROMOTION, change goal to APP_INSTALLS",
         "patch": {"adset.optimization_goal": "APP_INSTALLS",
                   "adset.destination_type": "APP"},
         "explanation": "App Promotion optimizes for installs."},
    ],
    ("OUTCOME_APP_PROMOTION", "LEAD_GENERATION"): [
        {"label": "Switch objective to OUTCOME_LEADS",
         "patch": {"campaign.objective": "OUTCOME_LEADS",
                   "adset.destination_type": "ON_AD"},
         "explanation": "Lead-gen is a Leads objective goal."},
        {"label": "Keep APP_PROMOTION, change goal to APP_INSTALLS",
         "patch": {"adset.optimization_goal": "APP_INSTALLS",
                   "adset.destination_type": "APP"},
         "explanation": "App Promotion's primary goal is installs."},
    ],
    ("OUTCOME_APP_PROMOTION", "POST_ENGAGEMENT"): [
        {"label": "Switch objective to OUTCOME_ENGAGEMENT",
         "patch": {"campaign.objective": "OUTCOME_ENGAGEMENT"},
         "explanation": "Post Engagement belongs under Engagement objective."},
        {"label": "Keep APP_PROMOTION, change goal to APP_INSTALLS",
         "patch": {"adset.optimization_goal": "APP_INSTALLS",
                   "adset.destination_type": "APP"},
         "explanation": "Optimize for app installs."},
    ],
}


def _norm(s: Any) -> str:
    return str(s or "").strip().upper()


# --- Special-ad-category combo restrictions (layered ON TOP of the
# category-agnostic _FORBIDDEN_OBJECTIVE_OPTIMIZATION table) ---
#
# Some objective×optimization_goal pairs are valid for a normal campaign but
# REJECTED when the campaign declares a restricted special ad category.
# Political ads (ISSUES_ELECTIONS_POLITICS) are the live example: Meta returns
# subcode 2490408 "Performance goal isn't available" for SOME OUTCOME_ENGAGEMENT
# optimization goals under this category. NOTE: POST_ENGAGEMENT IS accepted by
# Meta for OUTCOME_ENGAGEMENT + ISSUES_ELECTIONS_POLITICS (verified 2026-06-02 by
# a live create), so it MUST stay in the allow-list below — do not remove it.
# The category-agnostic table can't catch category-specific restrictions; that
# is why this allow-list layer exists.
#
# Model: an ALLOW-LIST of optimization goals per (category, objective). A goal
# NOT in the allow-list for a (category, objective) is blocked pre-flight.
# Objectives absent from a category's map are not restricted by this layer.
# Source: Meta "About special ad categories" + observed 2490408 rejections.
_CATEGORY_ALLOWED_OPTIMIZATION: Dict[str, Dict[str, frozenset]] = {
    "ISSUES_ELECTIONS_POLITICS": {
        "OUTCOME_AWARENESS": frozenset({"REACH", "IMPRESSIONS", "AD_RECALL_LIFT", "THRUPLAY"}),
        "OUTCOME_TRAFFIC": frozenset({"LINK_CLICKS", "LANDING_PAGE_VIEWS", "REACH", "IMPRESSIONS"}),
        "OUTCOME_ENGAGEMENT": frozenset({"POST_ENGAGEMENT", "THRUPLAY", "REACH", "IMPRESSIONS"}),
    },
}


def validate_special_ad_category_combo(
    special_ad_categories: Any, objective: Any, optimization_goal: Any
) -> Optional[Dict[str, Any]]:
    """Block (category × objective × optimization_goal) combos Meta rejects for
    restricted categories (e.g. political subcode 2490408), BEFORE any campaign
    is created. Returns a blocking error dict {"message","user_options"} when
    the goal is not allow-listed for the (category, objective); else None.
    """
    og = _norm(optimization_goal)
    obj = _norm(objective)
    if not og or not obj:
        return None
    cats = special_ad_categories
    if isinstance(cats, str):
        cats = [cats]
    if not isinstance(cats, (list, tuple)):
        return None
    for cat in cats:
        amap = _CATEGORY_ALLOWED_OPTIMIZATION.get(_norm(cat))
        if not amap:
            continue
        allowed = amap.get(obj)
        if allowed is None:
            continue  # objective not constrained by this category layer
        if og not in allowed:
            sorted_allowed = sorted(allowed)
            opts = [
                {
                    "label": f"Change optimization_goal to {g}",
                    "patch": {"adset.optimization_goal": g},
                    "explanation": f"{g} is permitted for {obj} under {_norm(cat)}.",
                }
                for g in sorted_allowed
            ]
            return {
                "message": (
                    f"Invalid combination under special ad category {_norm(cat)!r}: "
                    f"objective {obj!r} cannot use optimization_goal {og!r}. Meta rejects "
                    f"this with subcode 2490408 ('Performance goal isn't available'). "
                    f"Allowed goals for {obj} under {_norm(cat)}: {sorted_allowed}. "
                    f"{len(opts)} alternative(s) — see user_options."
                ),
                "user_options": opts,
                "error_subcode_hint": 2490408,
            }
    return None


def validate_objective_and_optimization(objective: Any, optimization_goal: Any) -> Optional[Dict[str, Any]]:
    """
    Return a blocking error dict {"message": "...", "user_options": [...]} if the
    pair is known-invalid, else None.

    The 'user_options' list (when present) contains 2-3 structured alternatives
    the caller can surface to the user — never apply silently. Each option has:
        label:       short human-readable summary of what changes
        patch:       flat-key partial document to merge (e.g. "campaign.objective" -> new value)
        explanation: longer reason why this fix makes sense

    Backward-compat: callers that only read 'message' continue to work.
    """
    obj = _norm(objective)
    og = _norm(optimization_goal)
    if not og:
        return None
    pair = (obj, og)
    if pair in _FORBIDDEN_OBJECTIVE_OPTIMIZATION:
        suggestions = list(_SUGGESTIONS_FOR_FORBIDDEN.get(pair, []))
        return {
            "message": (
                f"Invalid combination: campaign objective {obj!r} cannot use optimization_goal {og!r}. "
                f"Meta rejects this pair. {len(suggestions)} alternative(s) available — see user_options."
            ),
            "user_options": suggestions,
        }
    return None


def validate_pixel_tracking_vs_creative(
    tracking: Dict[str, Any],
    creative_mode: str,
    optimization_goal: Any,
    destination_type: Any,
) -> List[str]:
    """
    Non-blocking notes explaining what will happen to pixel / website events in sync.
    """
    notes: List[str] = []
    if not isinstance(tracking, dict) or not str(tracking.get("pixel_id") or "").strip():
        return notes
    mode = _norm(creative_mode)
    og = _norm(optimization_goal)
    dt = _norm(destination_type)
    if services.is_post_engagement_on_post(og, dt) and mode == "EXISTING_POST":
        notes.append(
            "Document requests a pixel, but Meta does not attach website pixels to POST_ENGAGEMENT + ON_POST + existing-post creatives the same way as link ads. "
            "The server will skip pixel attach for this ad set; verify_after_create may flag pixel_id unless you set tracking expectations accordingly."
        )
    elif mode == "EXISTING_POST" and og == "POST_ENGAGEMENT" and dt in ("ON_POST", "ON_PAGE", "ON_VIDEO", "ON_EVENT", "ON_AD"):
        notes.append(
            "With on-post engagement and an existing post, pixel attach is often skipped by Meta; website conversion tracking in Ads Manager may not match a typical WEBSITE destination ad set."
        )
    return notes


# Meta-documented length limits for the link-ad creative fields. Meta accepts
# longer values but truncates in the ad preview; we surface a soft warning so
# the user knows what will be visible.
_HEADLINE_MAX = 40   # link_data.name
_DESCRIPTION_MAX = 30   # link_data.description
_MESSAGE_RECOMMENDED_MAX = 125  # link_data.message (primary text)


def validate_creative_lengths(creative: Dict[str, Any], *, strict: bool = False) -> List[str]:
    """Validate creative text lengths.

    Default mode: soft-warn (returns advisory notes; existing tests rely on
    this).

    Strict mode: phrases the same findings as blocking errors so callers
    that opt in (production_flow with options.strict_mode=true) can reject
    the document before sending it to Meta — avoids silent truncation in
    high-stakes flows like sales/leads where a clipped CTA hurts performance.
    Both modes return ``List[str]``.
    """
    if not isinstance(creative, dict):
        return []
    notes: List[str] = []
    headline = str(creative.get("headline") or creative.get("link_headline") or "").strip()
    if len(headline) > _HEADLINE_MAX:
        if strict:
            notes.append(
                f"BLOCKING: creative.headline is {len(headline)} chars; Meta hard-cap is {_HEADLINE_MAX}. "
                f"Tighten to ≤{_HEADLINE_MAX} chars before retrying."
            )
        else:
            notes.append(
                f"creative.headline is {len(headline)} chars; Meta truncates to ~{_HEADLINE_MAX} in the ad preview. "
                f"Consider tightening: '{headline[:_HEADLINE_MAX]}...'"
            )
    description = str(creative.get("description") or creative.get("link_description") or "").strip()
    if len(description) > _DESCRIPTION_MAX:
        if strict:
            notes.append(
                f"BLOCKING: creative.description is {len(description)} chars; Meta hard-cap is {_DESCRIPTION_MAX}. "
                f"Tighten to ≤{_DESCRIPTION_MAX} chars before retrying."
            )
        else:
            notes.append(
                f"creative.description is {len(description)} chars; Meta truncates to ~{_DESCRIPTION_MAX} in the ad preview. "
                f"Consider tightening: '{description[:_DESCRIPTION_MAX]}...'"
            )
    message = str(creative.get("message") or "").strip()
    if len(message) > _MESSAGE_RECOMMENDED_MAX:
        if strict:
            notes.append(
                f"BLOCKING: creative.message is {len(message)} chars; strict-mode max is {_MESSAGE_RECOMMENDED_MAX}. "
                f"Tighten the primary text before retrying."
            )
        else:
            notes.append(
                f"creative.message is {len(message)} chars; Meta recommends keeping primary text ≤{_MESSAGE_RECOMMENDED_MAX} for feed placements (longer values get a 'See more' fold)."
            )
    return notes


# Lead-friendly CTAs Meta accepts on lead-generation ads.
_LEAD_CTAS = frozenset({"SIGN_UP", "GET_QUOTE", "SUBSCRIBE", "APPLY_NOW", "BOOK_TRAVEL", "DOWNLOAD"})

# CTAs that imply the destination should be a phone link.
_CALL_CTAS = frozenset({"CALL_NOW", "CALL"})

# CTAs that only make sense for WhatsApp destinations.
_WHATSAPP_CTAS = frozenset({"WHATSAPP_MESSAGE"})


def validate_cta_destination_compatibility(
    creative: Dict[str, Any],
    objective: str,
    destination_type: Optional[str],
) -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Return (blocking_error, soft_notes) for CTA-vs-destination mismatches."""
    if not isinstance(creative, dict):
        return None, []
    cta_type_raw = creative.get("cta_type")
    cta_doc = creative.get("call_to_action")
    if isinstance(cta_doc, dict) and cta_doc.get("type"):
        cta = _norm(cta_doc.get("type"))
    else:
        cta = _norm(cta_type_raw)
    if not cta:
        return None, []
    obj = _norm(objective)
    dt = _norm(destination_type)
    notes: List[str] = []
    if cta in _CALL_CTAS:
        if dt and dt not in ("WEBSITE", "PHONE_CALL"):
            return (
                {"message": (
                    f"CTA {cta!r} requires destination_type WEBSITE or PHONE_CALL, but adset.destination_type is {dt!r}. "
                    "Either change CTA (e.g. LEARN_MORE) or set destination_type=PHONE_CALL with a tel: link."
                )},
                notes,
            )
    if cta in _WHATSAPP_CTAS:
        if dt and dt != "WHATSAPP":
            return (
                {"message": (
                    f"CTA {cta!r} only works with destination_type=WHATSAPP, but adset.destination_type is {dt!r}."
                )},
                notes,
            )
    if cta in _LEAD_CTAS and obj and obj != "OUTCOME_LEADS":
        notes.append(
            f"CTA {cta!r} is typically used with OUTCOME_LEADS; current objective is {obj!r}. Meta may still accept, but consider matching the CTA to the objective for better delivery."
        )
    return None, notes


# Universal (objective → valid destination_types) matrix sourced from Meta's
# official destination_type documentation:
# https://developers.facebook.com/docs/marketing-api/adset/destination_type/
#
# This is NOT current-ad-specific — it covers ALL objectives the Marketing
# API supports, so any ad type (awareness/traffic/engagement/leads/sales/
# app-promotion) gets the same pre-flight protection. If Meta updates these
# enums, this single dict is the only place to change (or, in steady state,
# Meta's own structuredError carries the canonical list and the agent
# retries from `what_to_try`).
#
# Structure:
#   _VALID_OBJECTIVE_DESTINATIONS[objective] = frozenset of valid destination_type values
# A destination value of "UNDEFINED" means "omit destination_type" — Meta
# treats absent destination_type as UNDEFINED, which is universally accepted.
_VALID_OBJECTIVE_DESTINATIONS: Dict[str, frozenset] = {
    "OUTCOME_AWARENESS": frozenset({
        "UNDEFINED", "WEBSITE", "MESSENGER", "WHATSAPP",
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP",
        "MESSAGING_INSTAGRAM_DIRECT_WHATSAPP",
        "MESSAGING_MESSENGER_WHATSAPP",
        "INSTAGRAM_DIRECT",
    }),
    "OUTCOME_TRAFFIC": frozenset({
        "UNDEFINED", "WEBSITE", "MESSENGER", "WHATSAPP",
        "PHONE_CALL", "APP", "INSTAGRAM_PROFILE",
    }),
    "OUTCOME_ENGAGEMENT": frozenset({
        "UNDEFINED", "MESSENGER", "WHATSAPP", "PHONE_CALL", "INSTAGRAM_DIRECT",
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER",
        "MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP",
        "MESSAGING_INSTAGRAM_DIRECT_WHATSAPP",
        "MESSAGING_MESSENGER_WHATSAPP",
        "ON_POST", "ON_EVENT", "ON_VIDEO", "ON_PAGE",
    }),
    "OUTCOME_LEADS": frozenset({
        "ON_AD", "LEAD_FROM_MESSENGER", "LEAD_FROM_IG_DIRECT",
        "PHONE_CALL", "UNDEFINED", "WEBSITE", "APP",
    }),
    "OUTCOME_SALES": frozenset({
        "WEBSITE", "MESSENGER", "PHONE_CALL", "APP",
        "SHOP_AUTOMATIC", "APPLINKS_AUTOMATIC", "FACEBOOK", "UNDEFINED",
    }),
    "OUTCOME_APP_PROMOTION": frozenset({
        "UNDEFINED", "APP",
    }),
}


# Granular per-(objective, goal) → valid destinations matrix.
# Sourced from META_ADS_MASTER_REFERENCE.md §15.1.
# This is FINER than _VALID_OBJECTIVE_DESTINATIONS — it lets us catch
# (TRAFFIC, LANDING_PAGE_VIEWS, MESSENGER) which the broader matrix would
# allow because TRAFFIC accepts MESSENGER for OTHER goals.
#
# Usage: lookup (obj, goal) → frozenset of destinations. If pair not in this
# dict, fall back to the objective-only matrix above. UNDEFINED is always
# semantically valid (omit destination_type).
_OBJECTIVE_GOAL_DESTINATIONS: Dict[Tuple[str, str], frozenset] = {
    # AWARENESS
    ("OUTCOME_AWARENESS", "REACH"):          frozenset({"UNDEFINED"}),
    ("OUTCOME_AWARENESS", "IMPRESSIONS"):    frozenset({"UNDEFINED"}),
    ("OUTCOME_AWARENESS", "AD_RECALL_LIFT"): frozenset({"UNDEFINED"}),
    ("OUTCOME_AWARENESS", "THRUPLAY"):       frozenset({"UNDEFINED", "ON_VIDEO"}),

    # TRAFFIC
    ("OUTCOME_TRAFFIC", "LINK_CLICKS"):
        frozenset({"WEBSITE", "APP", "MESSENGER", "WHATSAPP", "INSTAGRAM_PROFILE"}),
    ("OUTCOME_TRAFFIC", "LANDING_PAGE_VIEWS"): frozenset({"WEBSITE"}),
    ("OUTCOME_TRAFFIC", "REACH"):              frozenset({"WEBSITE", "UNDEFINED"}),
    ("OUTCOME_TRAFFIC", "CONVERSATIONS"):
        frozenset({"MESSENGER", "WHATSAPP", "INSTAGRAM_DIRECT"}),
    ("OUTCOME_TRAFFIC", "IMPRESSIONS"):        frozenset({"WEBSITE", "UNDEFINED"}),

    # ENGAGEMENT
    ("OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT"):
        frozenset({"UNDEFINED", "ON_POST", "ON_PAGE"}),
    ("OUTCOME_ENGAGEMENT", "PAGE_LIKES"):
        frozenset({"ON_PAGE", "UNDEFINED"}),
    ("OUTCOME_ENGAGEMENT", "EVENT_RESPONSES"): frozenset({"ON_EVENT"}),
    # Meta hard-rejects an ABSENT destination_type for this pair (subcode
    # 2490408 "Performance goal isn't available", blame=optimization_goal —
    # observed live). ON_VIDEO is the only accepted value; UNDEFINED is NOT.
    ("OUTCOME_ENGAGEMENT", "THRUPLAY"):
        frozenset({"ON_VIDEO"}),
    ("OUTCOME_ENGAGEMENT", "CONVERSATIONS"):
        frozenset({"MESSENGER", "WHATSAPP", "INSTAGRAM_DIRECT"}),

    # LEADS
    ("OUTCOME_LEADS", "LEAD_GENERATION"):     frozenset({"ON_AD", "WEBSITE"}),
    ("OUTCOME_LEADS", "OFFSITE_CONVERSIONS"): frozenset({"WEBSITE", "APP"}),
    ("OUTCOME_LEADS", "QUALITY_LEAD"):        frozenset({"ON_AD", "WEBSITE"}),
    ("OUTCOME_LEADS", "CONVERSATIONS"):
        frozenset({"MESSENGER", "INSTAGRAM_DIRECT"}),
    ("OUTCOME_LEADS", "QUALITY_CALL"):        frozenset({"PHONE_CALL"}),
    ("OUTCOME_LEADS", "MEANINGFUL_CALL_ATTEMPT"): frozenset({"PHONE_CALL"}),
    ("OUTCOME_LEADS", "LANDING_PAGE_VIEWS"):  frozenset({"WEBSITE"}),
    ("OUTCOME_LEADS", "LINK_CLICKS"):         frozenset({"WEBSITE", "APP"}),

    # SALES
    ("OUTCOME_SALES", "OFFSITE_CONVERSIONS"):
        frozenset({"WEBSITE", "APP", "SHOP_AUTOMATIC"}),
    ("OUTCOME_SALES", "VALUE"):               frozenset({"WEBSITE", "APP"}),
    ("OUTCOME_SALES", "LANDING_PAGE_VIEWS"):  frozenset({"WEBSITE"}),
    ("OUTCOME_SALES", "LINK_CLICKS"):         frozenset({"WEBSITE", "APP"}),
    ("OUTCOME_SALES", "CONVERSATIONS"):       frozenset({"MESSENGER"}),

    # APP_PROMOTION
    ("OUTCOME_APP_PROMOTION", "APP_INSTALLS"):
        frozenset({"APP"}),
    ("OUTCOME_APP_PROMOTION", "OFFSITE_CONVERSIONS"):
        frozenset({"APP"}),
    ("OUTCOME_APP_PROMOTION", "LINK_CLICKS"):
        frozenset({"APP"}),
}


# Goals where omitting destination_type (UNDEFINED) is the conventional
# Meta-recommended setting because the goal optimizes against engagement
# on the ad itself (not a downstream destination). Driven by the fact
# that THRUPLAY / VIDEO_VIEWS under OUTCOME_AWARENESS rejects ON_AD with
# subcode 1815715. Universal across all video-view briefs.
_GOALS_PREFER_UNDEFINED_DESTINATION: frozenset = frozenset({
    "THRUPLAY",
    "VIDEO_VIEWS",
    "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS",
    "POST_ENGAGEMENT",
    "PAGE_LIKES",
    "EVENT_RESPONSES",
})


def required_destination_for(objective: Any, optimization_goal: Any) -> Optional[str]:
    """Return the single destination_type Meta REQUIRES for this pair, if any.

    Matrix-driven: when the granular (objective, goal) entry exists, does NOT
    allow UNDEFINED, and names exactly one destination, that destination must
    be sent explicitly — Meta rejects an absent destination_type for such
    pairs (e.g. ENGAGEMENT+THRUPLAY -> ON_VIDEO, subcode 2490408). Returns
    None when the pair is uncatalogued, allows UNDEFINED, or has multiple
    valid destinations (no safe single choice).
    """
    obj = _norm(objective)
    og = _norm(optimization_goal)
    if not obj or not og:
        return None
    granular = _OBJECTIVE_GOAL_DESTINATIONS.get((obj, og))
    if granular and "UNDEFINED" not in granular and len(granular) == 1:
        return next(iter(granular))
    return None


def validate_objective_goal_destination_triple(
    objective: Any, optimization_goal: Any, destination_type: Optional[str],
) -> Optional[Dict[str, str]]:
    """Block (objective, goal, destination_type) combos Meta hard-rejects.

    Returns a blocking error dict or None.

    Two-tier check:
      1. If we have a granular (objective, goal) -> destinations entry in
         `_OBJECTIVE_GOAL_DESTINATIONS`, use it — this catches finer-grained
         issues like (TRAFFIC, LANDING_PAGE_VIEWS, MESSENGER) that the
         objective-only matrix would miss.
      2. Fall back to the objective-only matrix `_VALID_OBJECTIVE_DESTINATIONS`
         for combos we haven't catalogued granularly.

    Unknown objectives pass through (let Meta surface its own error).
    """
    obj = _norm(objective)
    og = _norm(optimization_goal)
    dt = _norm(destination_type)
    if not obj or not dt:
        return None

    # Tier 1: granular (objective, goal) -> destinations
    if og:
        granular = _OBJECTIVE_GOAL_DESTINATIONS.get((obj, og))
        if granular is not None:
            # No unconditional UNDEFINED escape: some pairs (e.g. ENGAGEMENT +
            # THRUPLAY) hard-reject an absent destination — the granular set
            # itself says whether UNDEFINED is acceptable.
            if dt in granular:
                return None
            hint = ""
            if "UNDEFINED" in granular:
                hint = (
                    " Tip: omit destination_type entirely (or set it to UNDEFINED) — "
                    "Meta treats absent destination_type as UNDEFINED."
                )
            return {"message": (
                f"Invalid combination: destination_type {dt!r} is not valid for "
                f"({obj}, {og}). Valid destinations for this goal: "
                f"{', '.join(sorted(granular))}.{hint}"
            )}

    # Tier 2: fall back to objective-only matrix
    valid_destinations = _VALID_OBJECTIVE_DESTINATIONS.get(obj)
    if valid_destinations is None:
        return None  # unknown objective — let Meta surface the real error
    if dt in valid_destinations:
        return None
    hint = ""
    if "UNDEFINED" in valid_destinations:
        hint = (
            " Tip: omit destination_type entirely (or set it to UNDEFINED) — "
            "Meta treats absent destination_type as UNDEFINED, which is "
            f"universally accepted under {obj}."
        )
    return {"message": (
        f"Invalid combination: destination_type {dt!r} is not valid for objective {obj!r}. "
        f"Valid destinations: {', '.join(sorted(valid_destinations))}.{hint}"
    )}


def validate_age_range_and_dates(adset: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Block invalid age ranges or date pairs."""
    if not isinstance(adset, dict):
        return None
    age_min = adset.get("age_min")
    age_max = adset.get("age_max")
    if age_min is not None:
        try:
            am = int(age_min)
        except (TypeError, ValueError):
            return {"message": f"adset.age_min must be an integer 13-65; got {age_min!r}."}
        if am < 13 or am > 65:
            return {"message": f"adset.age_min={am} is out of Meta's 13-65 range."}
    if age_max is not None:
        try:
            amx = int(age_max)
        except (TypeError, ValueError):
            return {"message": f"adset.age_max must be an integer 13-65; got {age_max!r}."}
        if amx < 13 or amx > 65:
            return {"message": f"adset.age_max={amx} is out of Meta's 13-65 range."}
    if age_min is not None and age_max is not None:
        try:
            if int(age_min) > int(age_max):
                return {"message": f"adset.age_min ({age_min}) cannot be greater than adset.age_max ({age_max})."}
        except (TypeError, ValueError):
            pass
    start_time = adset.get("start_time")
    end_time = adset.get("end_time")
    from datetime import datetime, timezone
    def _parse_dt(s: Any) -> Optional[datetime]:
        if not s:
            return None
        ss = str(s).replace("Z", "+0000")
        if len(ss) >= 5 and ss[-5] in "+-" and ss[-3] != ":":
            ss = ss[:-2] + ":" + ss[-2:]
        try:
            return datetime.fromisoformat(ss)
        except ValueError:
            return None
    s_dt = _parse_dt(start_time)
    e_dt = _parse_dt(end_time)
    if start_time and not s_dt:
        return {"message": f"adset.start_time {start_time!r} is not a valid ISO 8601 timestamp."}
    if end_time and not e_dt:
        return {"message": f"adset.end_time {end_time!r} is not a valid ISO 8601 timestamp."}
    if s_dt and e_dt and e_dt <= s_dt:
        return {"message": f"adset.end_time ({end_time}) must be after adset.start_time ({start_time})."}
    if e_dt and e_dt <= datetime.now(timezone.utc):
        return {"message": f"adset.end_time ({end_time}) is in the past — Meta rejects expired schedules."}
    return None


def validate_budgets_and_specials(camp: Dict[str, Any], adset: Dict[str, Any]) -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Block conflicting budget shapes; warn on suspect values."""
    notes: List[str] = []
    if not isinstance(camp, dict):
        camp = {}
    if not isinstance(adset, dict):
        adset = {}
    # Daily vs lifetime is exclusive at the same level. Inter-level (campaign
    # CBO + adset budget) is allowed.
    if camp.get("daily_budget") and camp.get("lifetime_budget"):
        return (
            {"message": "campaign.daily_budget and campaign.lifetime_budget are mutually exclusive — pick one."},
            notes,
        )
    if adset.get("daily_budget") and adset.get("lifetime_budget"):
        return (
            {"message": "adset.daily_budget and adset.lifetime_budget are mutually exclusive — pick one."},
            notes,
        )
    # Floor: Meta's documented minimum is roughly $1/day equivalent. We warn at
    # under 4000 in account minor units (₹40 / $0.40) since users frequently
    # forget the smallest-currency-unit convention.
    for level, cfg in (("campaign", camp), ("adset", adset)):
        for k in ("daily_budget", "lifetime_budget"):
            v = cfg.get(k)
            if v is None:
                continue
            try:
                iv = int(v)
            except (TypeError, ValueError):
                return ({"message": f"{level}.{k} must be an integer in account minor units (paise/cents); got {v!r}."}, notes)
            if iv > 0 and iv < 100:
                notes.append(
                    f"{level}.{k}={iv} looks low — remember it's in account minor units (paise/cents). "
                    f"e.g. ₹100/day = 10000."
                )
    # Special category enforcement: when the objective is one Meta flags as
    # restricted, the document needs special_ad_categories set.
    obj = _norm(camp.get("objective"))
    sac = camp.get("special_ad_categories")
    sac_list = sac if isinstance(sac, list) else []
    if obj in ("OUTCOME_LEADS", "OUTCOME_TRAFFIC") and not sac_list:
        notes.append(
            "campaign.special_ad_categories is empty — required for housing / employment / credit / political ads. "
            "Set [] explicitly when none apply, otherwise Meta may reject."
        )
    return None, notes


def validate_document_for_sync(
    doc: Dict[str, Any],
    *,
    strict: bool,
    creative_mode: str,
    destination_type: Optional[str],
) -> Dict[str, Any]:
    """
    Returns {
      "blocking_error": dict | None,  # if set, caller should abort before Meta writes
      "validation_notes": [...],
    }
    """
    camp = doc.get("campaign") if isinstance(doc.get("campaign"), dict) else {}
    ads = doc.get("adset") if isinstance(doc.get("adset"), dict) else {}
    tracking = doc.get("tracking") if isinstance(doc.get("tracking"), dict) else {}
    creative = doc.get("creative") if isinstance(doc.get("creative"), dict) else {}

    notes: List[str] = []
    obj = camp.get("objective")
    og = ads.get("optimization_goal")

    # Special-ad-category restriction (e.g. political 2490408) is MORE specific
    # than the category-agnostic pair table, so check it first.
    cat_block = validate_special_ad_category_combo(
        camp.get("special_ad_categories"), obj, og
    )
    if cat_block:
        opts_c = doc.get("options") if isinstance(doc.get("options"), dict) else {}
        if strict or not _truthy(opts_c.get("allow_risky_objective_optimization")):
            return {"blocking_error": cat_block, "validation_notes": notes}
        notes.append(
            "options.allow_risky_objective_optimization is true: skipping local "
            "special-ad-category combo block; Meta may still reject (2490408)."
        )

    block = validate_objective_and_optimization(obj, og)
    if block and strict:
        return {"blocking_error": block, "validation_notes": notes}
    if block and not strict:
        # Non-strict: block known-bad pairs unless document explicitly allows (still not guaranteed to work on Meta).
        opts = doc.get("options") if isinstance(doc.get("options"), dict) else {}
        if not _truthy(opts.get("allow_risky_objective_optimization")):
            return {"blocking_error": block, "validation_notes": notes}
        notes.append(
            "options.allow_risky_objective_optimization is true: skipping local block for objective+optimization; Meta may still reject."
        )

    # Universal (objective, destination_type) hard-reject check (covers
    # all ad types — data sourced from Meta destination_type docs).
    triple_block = validate_objective_goal_destination_triple(obj, og, destination_type)
    if triple_block:
        return {"blocking_error": triple_block, "validation_notes": notes}

    cta_block, cta_notes = validate_cta_destination_compatibility(creative, obj, destination_type)
    notes.extend(cta_notes)
    if cta_block:
        return {"blocking_error": cta_block, "validation_notes": notes}

    age_date_block = validate_age_range_and_dates(ads)
    if age_date_block:
        return {"blocking_error": age_date_block, "validation_notes": notes}

    budget_block, budget_notes = validate_budgets_and_specials(camp, ads)
    notes.extend(budget_notes)
    if budget_block:
        return {"blocking_error": budget_block, "validation_notes": notes}

    # §11 — Creative source mutual exclusion. Always block (Meta would reject).
    src_block = validate_creative_source_exclusion(creative)
    if src_block:
        return {"blocking_error": src_block, "validation_notes": notes}

    # §12 — Frequency control shape normalization (mutates `ads` in place).
    notes.extend(normalize_frequency_control_specs(ads))

    # §3 — billing × goal advisory (warn only, per spec confidence note).
    notes.extend(advise_billing_vs_goal(ads))

    notes.extend(validate_creative_lengths(creative))

    notes.extend(
        validate_pixel_tracking_vs_creative(
            tracking,
            creative_mode,
            og,
            destination_type,
        )
    )

    if strict and tracking.get("pixel_id") and _norm(creative_mode) == "LINK_AD":
        cet = tracking.get("custom_event_type")
        if cet is None or (isinstance(cet, str) and not cet.strip()):
            return {
                "blocking_error": {"message": "strict_mode: tracking.custom_event_type is required when pixel_id is set for LINK_AD"},
                "validation_notes": notes,
            }

    return {"blocking_error": None, "validation_notes": notes}


def _truthy(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


# ============================================================
# COMBO QUICK REFERENCE — for facebook_get_combo_quickref MCP tool
# ============================================================
# Agent calls this at session start to learn valid/forbidden combos UPFRONT
# (proactive), instead of finding out reactively via failed sync attempts.
# Data is assembled from existing constants (no duplication).
#
# Returned JSON is intentionally compact (~3 KB) so it fits comfortably in
# any LLM's context window with minimal token cost.


# Per-objective valid optimization goals.
# Built by inverting _OBJECTIVE_GOAL_DESTINATIONS keys.
def _valid_goals_by_objective() -> Dict[str, List[str]]:
    by_obj: Dict[str, set] = {}
    for (obj, goal), _dests in _OBJECTIVE_GOAL_DESTINATIONS.items():
        by_obj.setdefault(obj, set()).add(goal)
    # Ensure all 6 ODAX objectives present even if matrix is partial
    for obj in (
        "OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT",
        "OUTCOME_LEADS", "OUTCOME_SALES", "OUTCOME_APP_PROMOTION",
    ):
        by_obj.setdefault(obj, set())
    return {obj: sorted(goals) for obj, goals in by_obj.items()}


# Per-goal valid destinations across all objectives.
# Useful for the agent to know "if I pick goal X, what destinations work?"
def _valid_destinations_by_goal() -> Dict[str, List[str]]:
    by_goal: Dict[str, set] = {}
    for (_obj, goal), dests in _OBJECTIVE_GOAL_DESTINATIONS.items():
        by_goal.setdefault(goal, set()).update(dests)
    return {goal: sorted(dests) for goal, dests in by_goal.items()}


def get_combo_quickref() -> Dict[str, Any]:
    """Compact JSON quick-reference for agents to validate combos client-side.

    Returns a dict the agent can cache for the session. Contents:

      valid_combinations:        objective -> list of valid optimization_goals
      forbidden_combinations:    [[objective, goal], ...]  (16 entries)
      destination_requirements:  goal -> list of valid destination_types
      objective_destinations:    objective -> list of valid destination_types
                                 (fallback when goal isn't in granular matrix)
      usage_hint:                short instructions for the agent

    Source: same constants used by validate_*() — single source of truth.
    """
    return {
        "version": "1.0",
        "source": "meta_validation.py constants (single source of truth)",
        "valid_combinations": _valid_goals_by_objective(),
        "forbidden_combinations": [list(pair) for pair in _FORBIDDEN_OBJECTIVE_OPTIMIZATION],
        "destination_requirements": _valid_destinations_by_goal(),
        "objective_destinations": {
            obj: sorted(dests) for obj, dests in _VALID_OBJECTIVE_DESTINATIONS.items()
        },
        "always_safe_destination": "UNDEFINED",
        # (objective, goal) pairs where Meta REJECTS an absent destination_type
        # (subcode 2490408) and exactly one value is accepted — send it
        # explicitly. Derived from the granular matrix.
        "required_destinations": {
            f"{obj}+{goal}": next(iter(dests))
            for (obj, goal), dests in _OBJECTIVE_GOAL_DESTINATIONS.items()
            if "UNDEFINED" not in dests and len(dests) == 1
        },
        "usage_hint": (
            "Call this MCP tool ONCE at session start and cache the result. "
            "Before generating a facebook_sync_document_campaign payload, check: "
            "(1) optimization_goal is in valid_combinations[objective]; "
            "(2) (objective, goal) is NOT in forbidden_combinations; "
            "(3) destination_type is in destination_requirements[goal] OR objective_destinations[objective]. "
            "If any check fails, ask user which alternative they want (don't silently substitute). "
            "When unsure about destination_type, omit it — Meta usually treats absence as UNDEFINED. "
            "EXCEPTION: pairs listed in required_destinations MUST send that exact destination_type "
            "(e.g. OUTCOME_ENGAGEMENT+THRUPLAY requires ON_VIDEO; omitting it triggers subcode 2490408)."
        ),
        "special_ad_categories_warning": (
            "If campaign.special_ad_categories includes ISSUES_ELECTIONS_POLITICS / CREDIT / "
            "HOUSING / EMPLOYMENT / FINANCIAL_PRODUCTS_SERVICES / ONLINE_GAMBLING_AND_GAMING: "
            "also set special_ad_category_country (e.g. ['IN']). For political ads, set "
            "creative.authorization_category='POLITICAL'. EU countries do NOT allow political "
            "ads (banned since Oct 2025)."
        ),
    }


# =============================================================================
# §11, §12, §3 validators added per the Meta Ads Valid Combinations Reference.
# These run inside validate_document_for_sync and either auto-fix or block.
# =============================================================================

# §11 — Creative source mutual exclusion.
# Meta rejects combining two of: object_story_id, source_instagram_media_id,
# asset_feed_spec, carousel_cards, object_story_spec, creative_id with
# error_subcode 1487929 ("Ambiguous Promoted Object Fields").
_CREATIVE_SOURCE_FIELDS: Tuple[str, ...] = (
    "object_story_id",
    "source_instagram_media_id",
    "instagram_permalink_url",
    "asset_feed_spec",
    "carousel_cards",
    "object_story_spec",
    "creative_id",
)


def validate_creative_source_exclusion(creative: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Block if more than one creative source field is non-empty.

    Returns ``{"message": "..."}`` on conflict, ``None`` otherwise.
    """
    if not isinstance(creative, dict):
        return None
    set_fields: List[str] = []
    for f in _CREATIVE_SOURCE_FIELDS:
        v = creative.get(f)
        if v in (None, "", [], {}):
            continue
        if isinstance(v, str) and not v.strip():
            continue
        set_fields.append(f)
    if len(set_fields) <= 1:
        return None
    return {
        "message": (
            "Ambiguous creative source: " + " + ".join(set_fields) +
            " set together. Meta rejects this combination (error_subcode=1487929). "
            "Pick exactly ONE of: " + ", ".join(_CREATIVE_SOURCE_FIELDS) + "."
        ),
        "error_subcode": "1487929",
    }


# §12 — Frequency control spec normalization.
# Spec §12 confirms only event=IMPRESSIONS is reliable. Common agent mistakes:
#   - field aliases: "frequency" instead of "max_frequency", "interval" instead
#     of "interval_days"
#   - singular "IMPRESSION" instead of plural "IMPRESSIONS"
#   - non-int values
# Meta silently ignores the spec if shape is wrong → the cap never applies.
# This normalizer rewrites the spec in-place and returns a list of human-
# readable fix notes for the caller to surface in auto_fixes.
_FREQUENCY_EVENT_ALIASES = {
    "IMPRESSION": "IMPRESSIONS",
    "IMPRESSIONS": "IMPRESSIONS",
    "REACH": "REACH",
    "VIDEO_VIEW": "VIDEO_VIEWS",
    "VIDEO_VIEWS": "VIDEO_VIEWS",
}


def normalize_frequency_control_specs(adset: Dict[str, Any]) -> List[str]:
    """Rewrite ``adset['frequency_control_specs']`` to the shape Meta accepts.

    Returns a list of human-readable fix notes; empty list = nothing to fix.
    Always mutates ``adset`` when changes are needed.
    """
    if not isinstance(adset, dict):
        return []
    specs = adset.get("frequency_control_specs")
    if not isinstance(specs, list) or not specs:
        return []

    fixes: List[str] = []
    out: List[Dict[str, Any]] = []
    for i, s in enumerate(specs):
        if not isinstance(s, dict):
            fixes.append(f"frequency_control_specs[{i}] is not an object; dropped.")
            continue
        new: Dict[str, Any] = {}

        # event
        ev_raw = s.get("event") or s.get("event_type") or "IMPRESSIONS"
        ev_up = str(ev_raw).strip().upper()
        ev_canon = _FREQUENCY_EVENT_ALIASES.get(ev_up)
        if ev_canon is None:
            fixes.append(
                f"frequency_control_specs[{i}].event={ev_raw!r} is unknown; "
                "coercing to 'IMPRESSIONS' (only confirmed value per spec §12)."
            )
            ev_canon = "IMPRESSIONS"
        elif ev_canon != ev_up:
            fixes.append(f"frequency_control_specs[{i}].event {ev_up!r} -> {ev_canon!r} (Meta enum).")
        if ev_canon in ("REACH", "VIDEO_VIEWS"):
            fixes.append(
                f"frequency_control_specs[{i}].event={ev_canon!r} is sparsely documented; "
                "spec §12 confirms only IMPRESSIONS reliably. Meta may ignore the cap."
            )
        new["event"] = ev_canon

        # max_frequency (canonical) <- frequency / cap / count
        mf_raw = (
            s.get("max_frequency")
            if s.get("max_frequency") is not None
            else s.get("frequency")
            if s.get("frequency") is not None
            else s.get("cap")
            if s.get("cap") is not None
            else s.get("count")
        )
        try:
            mf = int(mf_raw) if mf_raw is not None else None
        except (TypeError, ValueError):
            mf = None
        if mf is None or mf <= 0:
            fixes.append(
                f"frequency_control_specs[{i}].max_frequency is missing/invalid ({mf_raw!r}); spec dropped."
            )
            continue
        if "frequency" in s and "max_frequency" not in s:
            fixes.append(f"frequency_control_specs[{i}]: 'frequency' -> 'max_frequency' (Meta field).")
        new["max_frequency"] = mf

        # interval_days (canonical) <- interval / interval_day / days
        iv_raw = (
            s.get("interval_days")
            if s.get("interval_days") is not None
            else s.get("interval")
            if s.get("interval") is not None
            else s.get("interval_day")
            if s.get("interval_day") is not None
            else s.get("days")
        )
        try:
            iv = int(iv_raw) if iv_raw is not None else 1
        except (TypeError, ValueError):
            iv = 1
        if iv <= 0:
            iv = 1
        if "interval" in s and "interval_days" not in s:
            fixes.append(f"frequency_control_specs[{i}]: 'interval' -> 'interval_days' (Meta field).")
        new["interval_days"] = iv

        out.append(new)

    if out != specs:
        adset["frequency_control_specs"] = out
        if not any("frequency_control_specs" in f for f in fixes):
            fixes.append("frequency_control_specs normalized.")
    return fixes


# §3 — billing_event × optimization_goal advisory.
# Spec §3 has a confidence note: only IMPRESSIONS is confirmed universal.
# Common-but-not-guaranteed pairings are listed below. This returns advisory
# notes (never blocks) so the agent sees the risk in auto_fixes.
_COMMON_BILLING_GOAL_PAIRS: Dict[str, frozenset] = {
    "IMPRESSIONS": frozenset(),  # universal
    "LINK_CLICKS": frozenset({"LINK_CLICKS", "LANDING_PAGE_VIEWS", "POST_ENGAGEMENT"}),
    "POST_ENGAGEMENT": frozenset({"POST_ENGAGEMENT", "PAGE_LIKES", "EVENT_RESPONSES"}),
    "VIDEO_VIEWS": frozenset({"VIDEO_VIEWS", "THRUPLAY", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"}),
}


def advise_billing_vs_goal(adset: Dict[str, Any]) -> List[str]:
    """Return advisory notes (not blocking) when billing_event and
    optimization_goal pair looks unusual per spec §3.
    """
    if not isinstance(adset, dict):
        return []
    be = (adset.get("billing_event") or "").strip().upper()
    og = (adset.get("optimization_goal") or "").strip().upper()
    if not be or be == "IMPRESSIONS":
        return []
    allowed = _COMMON_BILLING_GOAL_PAIRS.get(be)
    if allowed is None:
        return [f"billing_event={be!r} is non-standard; spec §3 confirms only "
                "IMPRESSIONS/LINK_CLICKS/POST_ENGAGEMENT/VIDEO_VIEWS."]
    if og and og not in allowed:
        return [
            f"billing_event={be!r} with optimization_goal={og!r} is not in the "
            "common-pairing table for §3. Meta may reject. Use billing_event='IMPRESSIONS' "
            "if you are unsure — it is universally compatible."
        ]
    return []
