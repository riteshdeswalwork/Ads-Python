"""
Human-readable explanations for Meta Graph / Marketing API errors.
Attached as `why_failed` alongside raw `error` objects.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def explain_graph_error(err: Any) -> Dict[str, Any]:
    """
    Build a short summary, likely causes, and actionable hints from a Graph API error dict.
    Safe to call with partial / non-dict input.
    """
    if not isinstance(err, dict):
        return {
            "summary": str(err) if err else "Unknown error",
            "likely_causes": [],
            "what_to_try": ["Check the raw error.message from Meta."],
            "error_subcode": None,
        }
    code = err.get("code")
    sub = err.get("error_subcode")
    title = (err.get("error_user_title") or "").strip()
    user_msg = (err.get("error_user_msg") or err.get("error_user_message") or "").strip()
    msg = (err.get("message") or "").strip()
    summary_parts = [p for p in (title, user_msg, msg) if p]
    summary = summary_parts[0] if summary_parts else "Meta API rejected the request."

    causes: List[str] = []
    hints: List[str] = []

    if sub == 1885364 or "location" in (user_msg + msg).lower() or "audience targeting is incomplete" in (user_msg + msg).lower():
        causes.append("Targeting is missing at least one country/region or valid custom audience.")
        hints.append("Add geo_countries (or regions/cities) in document.targeting, or use a saved audience with pre-merge enabled.")
    if sub == 1870189 or "maximum age" in (user_msg + msg).lower() and "advantage" in (user_msg + msg).lower():
        causes.append("Advantage+ audience conflicts with a narrow max age in the saved audience.")
        hints.append("Set options.saved_audience_advantage_audience to 0 (default) or adset.advantage_audience=0 when applying saved audiences.")
    if sub == 1815595 or "audience network" in (user_msg + msg).lower() and "position" in (user_msg + msg).lower():
        causes.append("One or more Audience Network placement values are invalid for this API version.")
        hints.append("Use only allowed audience_network_positions (e.g. classic, instream_video, rewarded_video); avoid deprecated values like banner if Meta rejects them.")
    if sub == 1815715 or "destination type" in (user_msg + msg).lower():
        causes.append("This optimization goal does not allow the selected destination_type.")
        hints.append("Try destination_type UNDEFINED or omit it; or pick a destination Meta lists in the error (WEBSITE, APP, MESSENGER, etc.).")
    if sub == 2490408 or "performance goal" in (user_msg + msg).lower():
        causes.append("destination_type is missing or incompatible with the optimization goal (Meta blames optimization_goal, but the destination is usually the real issue).")
        hints.append("Retry with the destination_type this goal requires BEFORE switching goal or objective — e.g. THRUPLAY under OUTCOME_ENGAGEMENT requires destination_type=ON_VIDEO (omitting it triggers this error); existing-post boosts require ON_POST. Do not silently fall back to a different optimization_goal.")
    if sub == 2490562 or "deprecated" in (user_msg + msg).lower() and "placement" in (user_msg + msg).lower():
        causes.append("A placement (e.g. video_feeds) is deprecated for this API version.")
        hints.append("Remove deprecated facebook_positions; use feed, story, facebook_reels, etc.")
    _low = (user_msg + msg).lower()
    if ("explore" in _low and "instagram" in _low) or ("messenger" in _low and "stor" in _low and "position" in _low):
        causes.append("Graph API v26 removed the Instagram Explore feed and Messenger Stories placements (enforced on every version from 2026-10-27).")
        hints.append("Drop 'explore' from instagram_positions and 'story' from messenger_positions, or omit manual placements (Advantage+).")
    if sub == 1870227 or "require_explicit_advantage_audience" in _low or ("advantage_audience" in _low and "explicit" in _low):
        causes.append("Meta v26 requires targeting.targeting_automation.advantage_audience to be an explicit 0 or 1 on this ad set (mandatory for HOUSING / EMPLOYMENT / CREDIT special ad categories).")
        hints.append("Pass targeting.targeting_automation.advantage_audience=0 (keep targeting as-is) or =1 (let Meta widen); the document-sync flow fills 0 automatically.")
    # 2026-09-22: an agent read these two subcodes as "the post is not
    # eligible" and told the user their reels were unboostable. In fact the
    # object_story_id values did not exist — the posts were fine. Name the
    # wrong-id case so the honest cause is offered before eligibility is blamed.
    if sub in (1487790, 2446187) \
            or "promoted object is invalid" in (user_msg + msg).lower() \
            or "post can't be boosted" in (user_msg + msg).lower():
        causes.append(
            "The promoted object (object_story_id) may not exist on this Page — a wrong or "
            "invented post id produces this error, not only a genuine eligibility block."
        )
        hints.append(
            "Verify the id first: call facebook_get_page_posts and use an 'id' from its response "
            "VERBATIM (format {page_id}_{post_id}). Never construct or guess a post id. Only if the "
            "post exists and is_eligible_for_promotion is false is this a real eligibility problem."
        )
    if code == 100 and "instagram" in (user_msg + msg).lower():
        causes.append("Instagram account ID is not accepted for this creative.")
        hints.append("Use instagram_user_id (Graph v22+) instead of instagram_actor_id, or omit IG fields for Facebook-only delivery.")
    # ---- Expanded Meta error subcode coverage (May 2026) -------------------
    if sub == 1885073:
        causes.append("Creative uses asset_feed_spec but the ad set has is_dynamic_creative=false (or the field is missing).")
        hints.append("Set adset.is_dynamic_creative=true when the creative carries asset_feed_spec; or remove asset_feed_spec for a static creative.")
    if sub == 1487079:
        causes.append("One or more interest / behavior targeting IDs are invalid or expired.")
        hints.append("Re-resolve the targeting names via facebook_batch_interest_search / facebook_batch_targeting_category_search and retry with fresh IDs.")
    if sub == 1487746:
        causes.append("Daily budget is below Meta's minimum for this currency / objective combination.")
        hints.append("Raise daily_budget. Indicative minimums: ~$1/day for Awareness/Engagement; ~$5/day for Conversions/Sales/Leads; multiplied for higher-CPC currencies.")
    if sub == 2446073:
        causes.append("Schedule conflict — start_time/end_time overlap an existing window or end_time is too close to start_time.")
        hints.append("Ensure end_time is at least 24h after start_time and not in the past; use ISO 8601 with timezone (e.g. 2026-06-01T00:00:00+0000).")
    if sub == 1885058:
        causes.append("Ad creative was rejected during review (policy violation or unsupported asset).")
        hints.append("Review Meta's ad policy notes in error_user_msg and adjust message/headline/imagery; resubmit with a new creative.")
    if sub == 1885364:
        causes.append("Targeting is incomplete — no countries/regions/audience selected.")
        hints.append("Add at least one of: geo_countries, geo_regions, geo_cities, custom_audiences, or saved_audience_id.")
    if sub == 1885365:
        causes.append("Selected publisher_platforms / placements are not compatible with this objective+optimization_goal.")
        hints.append("Use Advantage+ Placements (omit publisher_platforms) or pick a placement Meta supports for the goal — see error_user_msg.")
    if sub == 1885399:
        causes.append("Lead form is not active or not approved for this Page yet.")
        hints.append("Verify the form status via facebook_get_lead_forms; create or republish the form in Ads Manager UI before attaching to ads.")
    if sub == 2069032:
        causes.append("Wrong token type for this endpoint (e.g. user token used where Page token required).")
        hints.append("Pass a Page access token from facebook_get_pages.access_token, or use a System User token for ad account operations.")
    if sub == 2069033:
        causes.append("Token does not have the required permission scope.")
        hints.append("Re-OAuth the user with ads_management + ads_read + pages_show_list + business_management scopes.")
    if sub == 2470004:
        causes.append("Special ad category is required but missing.")
        hints.append("If the campaign touches credit, employment, housing, or politics, set campaign.special_ad_categories and special_ad_category_country.")
    if sub == 2474015:
        causes.append("Conversion event is not active on the pixel — no events received in the last 7 days.")
        hints.append("Send a test event via facebook_send_conversion_event or fire the event from the website; pick a different pixel/event.")
    if sub == 1487748:
        causes.append("Lifetime budget is too small for the chosen schedule duration.")
        hints.append("Increase lifetime_budget OR shorten end_time — Meta requires roughly daily-min × days.")
    if sub == 1487090:
        causes.append("Bid amount is outside Meta's accepted range for this auction.")
        hints.append("Remove bid_amount to let Meta auto-bid (LOWEST_COST_WITHOUT_CAP), or pick a value Meta returns in error_user_msg.")
    if sub == 2461012:
        causes.append("Custom audience is too small to deliver (under Meta's privacy floor).")
        hints.append("Pick a larger custom_audience or rely on lookalike + interest targeting until the audience grows.")
    if sub == 2461103:
        causes.append("Lookalike source audience is not yet ready (still building) or below the seed-size floor.")
        hints.append("Wait a few hours after creating the seed CA before running lookalike; ensure the seed has at least 100 valid users in the target country.")
    if sub == 1815605:
        causes.append("Page-backed Instagram account is not configured.")
        hints.append("Use facebook_get_page_backed_instagram_accounts to fetch the auto-generated IG actor, then pass it as instagram_user_id.")
    if sub == 80004:
        causes.append("App-level rate limit reached (call volume too high).")
        hints.append("Back off and retry after 10–15 minutes; consider batching via async tools (facebook_create_campaign_async).")
    if sub == 80003:
        causes.append("User-level rate limit reached (this user has hit Meta's per-user call cap).")
        hints.append("Wait an hour before more high-volume operations on this account; spread bulk creation across multiple sessions.")
    if sub == 2446079:
        causes.append("Transient targeting-resolution failure inside Meta — usually clears within seconds.")
        hints.append("Retry the same call after 2–10 seconds (the MCP also auto-retries POSTs via _post_with_rate_limit_retry).")
    if sub == 1487390:
        causes.append("Account is restricted or in a 'fix issues' state in Ads Manager.")
        hints.append("Open Ads Manager → Account Quality and resolve outstanding issues; some operations are blocked until cleared.")

    if not causes:
        if msg:
            causes.append(f"Meta returned: {msg[:200]}")
        else:
            causes.append("See error_user_msg / message from Meta.")

    if not hints:
        hints.append("Retry with a smaller change (placements, destination, or optimization_goal) or check Meta Marketing API docs for this error_subcode.")

    return {
        "summary": summary,
        "likely_causes": causes[:5],
        "what_to_try": hints[:6],
        "error_subcode": sub,
        "error_code": code,
    }


def attach_why_failed_to_response(resp: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """If resp contains Graph-style error, add why_failed. Mutates and returns resp."""
    if not isinstance(resp, dict):
        return resp
    e = resp.get("error")
    if isinstance(e, dict) and e:
        resp["why_failed"] = explain_graph_error(e)
    return resp
