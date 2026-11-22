"""
Facebook Ads MCP HTTP — JSON-RPC (initialize, tools/list, tools/call).
Production features: audit logging, idempotency, dry-run, token auto-resolution, Celery async.
"""
import json
import logging
import time

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt

from . import mcp_protocol
from .mcp_logging import (
    emit_fb_ads_mcp_audit,
    generate_request_id,
    save_audit_to_db,
    redact_for_log,
    safe_json_dumps,
)

from .services import (
    META_GRAPH_API_VERSION,
    get_ad_accounts,
    get_insights,
    get_campaigns,
    get_ads,
    get_ad_sets,
    get_spend,
    get_businesses,
    get_pages,
    get_pixels,
    create_campaign,
    update_campaign,
    pause_campaign,
    resume_campaign,
    delete_campaign,
    get_campaign_insights,
    create_adset,
    targeting_search,
    build_targeting_spec,
    update_adset,
    pause_adset,
    resume_adset,
    delete_adset,
    update_budget,
    create_ad,
    update_ad,
    pause_ad,
    resume_ad,
    delete_ad,
    get_ad_creatives,
    get_campaign_performance,
    get_adset_performance,
    get_ad_performance,
    get_ad_level_data_from_all_active_accounts,
    get_clicks_impressions,
    upload_image,
    upload_video,
    post_page_photo,
    post_page_feed,
    post_page_video,
    create_creative,
    delete_creative,
    compare_campaigns,
    compare_adsets,
    compare_ads,
    roas_analysis,
    budget_recommendation,
    send_conversion_event,
    get_product_catalogs,
    get_catalog,
    get_catalog_products,
    get_business_owned_ad_accounts,
    get_business_owned_pages,
    get_business_client_ad_accounts,
    get_page_extension_info,
    get_page_backed_instagram_accounts,
    get_catalog_product_sets,
    get_catalog_product_feeds,
)

logger = logging.getLogger("facebook_ads_mcp")

# Response header so operators can verify the Marketing API MCP is hit (not WhatsApp mcp/<provider>/).
MCP_SERVER_HEADER_ID = "facebook-ads-marketing-api"


def _stamp_marketing_mcp_response(response):
    response["X-MCP-Server"] = MCP_SERVER_HEADER_ID
    response["X-MCP-Marketing-Tool-Count"] = str(len(marketing_tools_from_registry()))
    return response

# Tool definitions with inputSchema — all call real Facebook Marketing API (Graph API version from services.META_GRAPH_API_VERSION, v26.0 default).
# Required: access token with ads_read permission from a Facebook App with Marketing API access.
# Order: all GET → all CREATE → all UPDATE → all DELETE
TOOLS = [
    # ---------- COMBO QUICK REFERENCE (call once at session start) ----------
    {
        "name": "facebook_get_combo_quickref",
        "description": (
            "Returns a compact JSON quick-reference of valid Meta Ads combinations "
            "(objective × optimization_goal × destination_type) plus forbidden pairs "
            "and Meta's documented constraints. **CALL THIS ONCE at session start** "
            "and cache the result.\n\n"
            "Use it to validate any user brief CLIENT-SIDE before constructing the "
            "facebook_sync_document_campaign payload — this prevents wasted Meta API "
            "calls on combos Meta will reject anyway (which can lead to rate-limiting "
            "or account abuse signals).\n\n"
            "Returned fields:\n"
            "  valid_combinations:        objective -> list of valid optimization_goals\n"
            "  forbidden_combinations:    [[objective, goal], ...] (16 known-bad pairs)\n"
            "  destination_requirements:  goal -> list of valid destination_types\n"
            "  objective_destinations:    objective -> list of valid destination_types (fallback)\n"
            "  always_safe_destination:   'UNDEFINED' (when in doubt, omit destination_type)\n"
            "  usage_hint + special_ad_categories_warning: text guidance\n\n"
            "If your generated brief fails any check, ASK the user which alternative "
            "they want (the response includes 2-3 user_options per forbidden combo). "
            "Never silently substitute — that's the production-grade contract.\n\n"
            "No auth/token needed. Response ~4 KB."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    # ---------- GET (read/list) ----------
    {
        "name": "facebook_get_accounts",
        "description": "Get Facebook ad accounts. Real API: GET /me/adaccounts. Pass the user's Graph API access token in arguments.token only (no env fallback).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Facebook Graph API access token from the end user (required for every call)"},
                "limit": {"type": "integer", "description": "Max number of accounts to return (default 100, max 500)"},
            },
            "required": ["token"],
        },
        "_discovery": [["account_id", ["data", "*", "account_id"]]],
    },
    {
        "name": "facebook_get_all_ad_accounts_from_facebook",
        "description": "Get all ad accounts from Facebook only (live API, no database). Use when you need the list from the Facebook account. Aggregates /me/adaccounts and business owned/client accounts. Pass token (required).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "token": {"type": "string", "description": "Facebook Graph API access token (required; use token from Facebook OAuth, not user_id)"},
                "limit": {"type": "integer", "description": "Max accounts to return (default 500)"},
            },
            "required": ["token"],
        },
    },
    {"name": "facebook_get_businesses", "description": "List businesses the user has access to. Real API: GET /me/businesses.", "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["token"]}},
    {"name": "facebook_get_pages", "description": "List Facebook Pages the user has access to. Real API: GET /me/accounts.", "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["token"]}, "_discovery": [["page_id", ["data", "*", "id"]]]},
    {"name": "facebook_resolve_account_for_page", "description": "Given a page_id, find the first active ad account in the user's portfolios where that page is bound (via the 3-tier check: promote_pages, client_pages, page-side ad_accounts). Use this BEFORE syncing when a brief gives you a page_id (or page_name) but no account_id. Returns {ok, account_id, account_name, via, tried_accounts}. Stops at the first match (no wasted API calls).", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID (numeric)"}, "token": {"type": "string"}}, "required": ["page_id", "token"]}, "_discovery": [["account_id", ["account_id"]]]},
    {"name": "facebook_get_saved_audiences", "description": "List saved audiences for an ad account. Real API: GET /act_{id}/saved_audiences. To apply one to an ad set, use facebook_update_adset with saved_audience_id (do not put saved-audience ids in custom_audiences).", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_get_custom_audiences", "description": "List custom audiences for an ad account. Real API: GET /act_{id}/customaudiences. Returns id, name, subtype, approximate counts and delivery status. Use these IDs inside targeting.custom_audiences (NOT saved_audiences). Distinct from saved_audiences (reusable targeting specs); custom audiences are user-list audiences (uploaded emails/phones, website visitors, app activity, etc.).", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer", "description": "Max audiences to return (default 100, max 500)"}}, "required": ["account_id", "token"]}, "_discovery": [["audience_id", ["data", "*", "id"]]]},
    {"name": "facebook_get_political_disclaimers", "description": "List political ad disclaimers (Page Trust+Transparency 'paid for by' bylines) registered for a Page. Required for ads in ISSUES_ELECTIONS_POLITICS special category. Disclaimers themselves are registered via Ads Manager UI — this tool surfaces existing ids so the LLM can attach a registered disclaimer to a political ad. Auto-retries with page-scoped token on Meta error #190.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer", "description": "Max disclaimers to return (default 50, max 500)"}}, "required": ["page_id", "token"]}},
    {"name": "facebook_get_lead_forms", "description": "List lead (Instant) forms on a Page. Real API: GET /{page_id}/leadgen_forms. Use returned id as lead_gen_form_id inside the creative's call_to_action.value when building lead-ad creatives. Useful for avoiding duplicate-name collisions when re-running a brief that says 'use existing form: <name>'. Auto-retries with the page-scoped access token on Meta error #190.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID"}, "token": {"type": "string"}, "limit": {"type": "integer", "description": "Max forms to return (default 100, max 500)"}}, "required": ["page_id", "token"]}},
    {"name": "facebook_get_page_posts", "description": "List ALL posts published by a Page (auto-paginates through full history, hard-capped at 10000 posts). Use post id (page_id_post_id format) as object_story_id when creating creative from existing post. Real API: GET /{page_id}/posts.\n\nCAPTION SEARCH (recommended when the brief gives a caption snippet): pass caption_query=\"some text from the post caption\" and the server returns ONLY posts whose message contains the query (case-insensitive substring). Response includes a 'search' object: {query, pages_scanned, posts_scanned, matched}.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string"}, "token": {"type": "string"}, "fields": {"type": "string"}, "caption_query": {"type": "string", "description": "Case-insensitive substring to find a specific post by its message text. When set, the response is filtered to matching posts only."}}, "required": ["page_id", "token"]}, "_discovery": [["object_story_id", ["data", "*", "id"]]]},
    {"name": "facebook_get_pixels", "description": "List pixels for an ad account. Real API: GET /act_{id}/adspixels.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}, "_discovery": [["pixel_id", ["data", "*", "id"]]]},
    {
        "name": "facebook_get_insights",
        "description": "Get ads performance report (impressions, clicks, spend, cpc, cpm, ctr) for an ad account. Use date_preset OR date_start+date_end (YYYY-MM-DD) for custom range (e.g. last 3 years). Real API: GET /act_{id}/insights.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Ad account ID (required; numeric or act_XXX)"},
                "token": {"type": "string", "description": "Facebook Graph API access token from the end user (required)"},
                "breakdowns": {"type": "string", "description": "Comma-separated Meta breakdowns for platform/audience split - e.g. publisher_platform (facebook vs instagram vs audience_network), platform_position, device_platform, age, gender, country, region. Rows are returned once per breakdown combination."}, "date_preset": {"type": "string", "description": "Preset: last_7d, last_30d, this_month, last_90d (default last_30d). Ignored if date_start+date_end set."},
                "date_start": {"type": "string", "description": "Start date YYYY-MM-DD for custom range (e.g. 2022-03-03 for 3 years ago). Use with date_end."},
                "date_end": {"type": "string", "description": "End date YYYY-MM-DD for custom range (e.g. 2025-03-03). Use with date_start."},
                "limit": {"type": "integer", "description": "Max rows (default 100, max 500)"},
            },
            "required": ["account_id", "token"],
        },
    },
    {
        "name": "facebook_get_campaigns",
        "_discovery": [["campaign_id", ["data", "*", "id"]]],
        "description": "List campaigns for a Facebook ad account (id, name, status, objective, budget, start_time, stop_time). Use start_time/stop_time from response as date_start/date_end in get_campaign_insights. Real API: GET /act_{id}/campaigns.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Ad account ID (required; numeric or act_XXX)"},
                "token": {"type": "string", "description": "Facebook Graph API access token from the end user (required)"},
                "limit": {"type": "integer", "description": "Max campaigns to return (default 100, max 500)"},
            },
            "required": ["account_id", "token"],
        },
    },
    {"name": "facebook_get_campaign_insights", "description": "Get insights for a single campaign. Only campaign_id required (returns lifetime insights by default). Optional: date_preset (e.g. last_30d, lifetime) OR date_start+date_end to filter. GET /{campaign_id}/insights.", "inputSchema": {"type": "object", "properties": {"breakdowns": {"type": "string", "description": "Comma-separated Meta breakdowns for platform/audience split - e.g. publisher_platform (facebook vs instagram vs audience_network), platform_position, device_platform, age, gender, country, region. Rows are returned once per breakdown combination."}, "campaign_id": {"type": "string"}, "token": {"type": "string"}, "date_preset": {"type": "string", "description": "Optional. Default lifetime. Or last_7d, last_30d, etc."}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "fields": {"type": "string"}}, "required": ["campaign_id", "token"]}},
    {"name": "facebook_get_campaign_performance", "description": "Get campaign-level performance (insights by campaign). Use date_preset OR date_start+date_end.", "inputSchema": {"type": "object", "properties": {"breakdowns": {"type": "string", "description": "Comma-separated Meta breakdowns for platform/audience split - e.g. publisher_platform (facebook vs instagram vs audience_network), platform_position, device_platform, age, gender, country, region. Rows are returned once per breakdown combination."}, "account_id": {"type": "string"}, "token": {"type": "string"}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {
        "name": "facebook_get_ad_sets",
        "_discovery": [["adset_id", ["data", "*", "id"]]],
        "description": "List ad sets for a Facebook ad account (id, name, status, campaign_id, budget, optimization_goal). Real API: GET /act_{id}/adsets. Pass token in arguments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Ad account ID (required; numeric or act_XXX)"},
                "token": {"type": "string", "description": "Facebook Graph API access token from the end user (required)"},
                "limit": {"type": "integer", "description": "Max ad sets to return (default 100, max 500)"},
            },
            "required": ["account_id", "token"],
        },
    },
    {"name": "facebook_get_adset_performance", "description": "Get ad set-level performance. Use date_preset OR date_start+date_end.", "inputSchema": {"type": "object", "properties": {"breakdowns": {"type": "string", "description": "Comma-separated Meta breakdowns for platform/audience split - e.g. publisher_platform (facebook vs instagram vs audience_network), platform_position, device_platform, age, gender, country, region. Rows are returned once per breakdown combination."}, "account_id": {"type": "string"}, "token": {"type": "string"}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {
        "name": "facebook_get_ads",
        "_discovery": [["ad_id", ["data", "*", "id"]]],
        "description": "List ads for a Facebook ad account (id, name, status, creative, adset_id, campaign_id). Real API: GET /act_{id}/ads. Optional fields (Graph fields string) and filtering (JSON array, e.g. [{\"field\":\"id\",\"operator\":\"EQUAL\",\"value\":\"ad_id\"}]). Pass token in arguments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Ad account ID (required; numeric or act_XXX)"},
                "token": {"type": "string", "description": "Facebook Graph API access token from the end user (required)"},
                "limit": {"type": "integer", "description": "Max ads to return (default 100, max 500)"},
                "fields": {"type": "string", "description": "Optional. Graph API fields param (e.g. id,name,creative{id,object_story_id})"},
                "filtering": {"type": "string", "description": "Optional. JSON array string for ad filtering"},
            },
            "required": ["account_id", "token"],
        },
    },
    {"name": "facebook_get_ad_creatives", "description": "List creatives for an ad account. GET /act_{id}/adcreatives.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}, "fields": {"type": "string"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_get_ad_performance", "description": "Get ad-level performance. Use date_preset OR date_start+date_end.", "inputSchema": {"type": "object", "properties": {"breakdowns": {"type": "string", "description": "Comma-separated Meta breakdowns for platform/audience split - e.g. publisher_platform (facebook vs instagram vs audience_network), platform_position, device_platform, age, gender, country, region. Rows are returned once per breakdown combination."}, "account_id": {"type": "string"}, "token": {"type": "string"}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {
        "name": "facebook_get_spend",
        "description": "Get spend summary for an ad account (total spend, impressions, clicks). Use date_preset OR date_start+date_end for custom range (e.g. last 3 years). Real API: GET /act_{id}/insights. Pass token in arguments.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Ad account ID (required; numeric or act_XXX)"},
                "token": {"type": "string", "description": "Facebook Graph API access token from the end user (required)"},
                "date_preset": {"type": "string", "description": "last_7d, last_30d, this_month, last_90d (default last_30d). Ignored if date_start+date_end set."},
                "date_start": {"type": "string", "description": "Start date YYYY-MM-DD for custom range. Use with date_end."},
                "date_end": {"type": "string", "description": "End date YYYY-MM-DD for custom range. Use with date_start."},
            },
            "required": ["account_id", "token"],
        },
    },
    {"name": "facebook_get_clicks_impressions", "description": "Get clicks and impressions summary for an ad account. date_preset OR date_start+date_end.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}}, "required": ["account_id", "token"]}},
    # ---------- Comparison & analysis ----------
    {"name": "facebook_compare_campaigns", "description": "Compare campaigns by performance (impressions, clicks, spend, CTR). Optional campaign_ids to filter. Returns side-by-side comparison and top by CTR/spend.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "campaign_ids": {"type": "array", "items": {"type": "string"}, "description": "Optional list of campaign IDs to compare"}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_compare_adsets", "description": "Compare ad sets by performance. Optional adset_ids to filter. Returns comparison and top by CTR/spend.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "adset_ids": {"type": "array", "items": {"type": "string"}}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_compare_ads", "description": "Compare ads by performance. Optional ad_ids to filter. Returns comparison and top by CTR/spend.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "ad_ids": {"type": "array", "items": {"type": "string"}}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_roas_analysis", "description": "ROAS (Return on Ad Spend) analysis by campaign, adset, or ad. Uses actions/action_values from insights. level: campaign, adset, or ad.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "level": {"type": "string", "description": "campaign, adset, or ad"}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_budget_recommendation", "description": "Budget recommendations: scale top performers (high CTR), review or pause low performers. level: campaign or adset.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "level": {"type": "string", "description": "campaign or adset"}, "date_preset": {"type": "string"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["account_id", "token"]}},
    # ---------- Instagram & Lead Form & Reach ----------
    {"name": "facebook_get_instagram_accounts", "description": "Get Instagram business account linked to a Facebook Page. Returns instagram_business_account ID. Use returned id as instagram_actor_id when creating creatives.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID"}, "token": {"type": "string"}}, "required": ["page_id", "token"]}, "_discovery": [["instagram_user_id", ["data", "*", "id"]]]},
    {"name": "facebook_get_page_backed_instagram_accounts", "description": "Get Page-backed Instagram account(s) for a Facebook Page. Use returned id as instagram_actor_id for ad creatives when API returns 'valid Instagram account id' error. GET /{page_id}/page_backed_instagram_accounts.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID"}, "token": {"type": "string"}}, "required": ["page_id", "token"]}},
    {"name": "facebook_get_instagram_media", "description": "List ALL media (posts/reels) on an IG Business Account (auto-paginates full history, hard-capped at 10000). Real API: GET /{ig-user-id}/media. Use the returned 'id' as creative.source_instagram_media_id when building an ad from an existing IG post (with creative.mode='EXISTING_POST'). Either page_id (Page linked to the IG Business account) or instagram_user_id must be supplied; page_id auto-resolves the IG account via the linked Business account.\n\nCAPTION SEARCH (recommended when the brief gives a caption snippet): pass caption_query=\"some text from the post caption\" and the server returns ONLY media whose caption contains the query (case-insensitive substring). Works with Hindi/Devanagari and any Unicode text. Response includes a 'search' object: {query, pages_scanned, items_scanned, matched}.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID linked to the IG Business account. Either this or instagram_user_id is required."}, "instagram_user_id": {"type": "string", "description": "IG Business Account ID. Either this or page_id is required."}, "token": {"type": "string"}, "fields": {"type": "string", "description": "Comma-separated IG media fields. Default: id,caption,media_type,media_url,permalink,timestamp,thumbnail_url"}, "caption_query": {"type": "string", "description": "Case-insensitive substring to find a specific post by its caption. When set, the response is filtered to matching media only."}}, "required": ["token"]}, "_discovery": [["instagram_media_id", ["data", "*", "id"]]]},
    {"name": "facebook_get_post_insights", "description": "Per-post view/engagement metrics (views, reach, impressions, ...) for Instagram media OR Facebook Page posts in ONE call — USE THIS for questions like 'posts with under 5000 views'. Real API: GET /{container}/media|published_posts?fields=...,insights.metric(...) with full pagination (field expansion: ~100 posts per round-trip, no per-post calls). platform='instagram' (default) resolves the IG Business Account from page_id (or takes instagram_user_id); platform='facebook' walks the Page's published posts. IG METRIC MEANINGS: total_views = the number DISPLAYED in the Instagram app on the post (includes replays, Facebook cross-post views and paid/boosted views) — use it for any 'posts under/over X views' question; views = Instagram-surface only (can be 100x smaller on boosted posts); facebook_views = the FB cross-post share. Metrics are free-form: if Meta rejects one (#100), the server parses Meta's own valid-values list and retries with the intersection (summary.metrics_auto_adjusted); if none survive, the error returns with valid_metrics to pick from. Server-side filter: where={\"metric\": \"views\", \"lt\": 5000} (ops lt/lte/gt/gte/eq) returns only matching posts plus a summary {items_scanned, matched, metrics_used}. Each returned row = post fields + one top-level key per metric. READ-ONLY. LARGE ACCOUNTS: responses over ~23KB get truncated by the agent loop — for accounts with 100+ posts pass fields='id,permalink,timestamp' (skip caption) and/or page with sort+limit (e.g. sort='views', limit=50).", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID. For platform=instagram it auto-resolves the linked IG Business Account; for platform=facebook its posts are read directly."}, "instagram_user_id": {"type": "string", "description": "IG Business Account ID (platform=instagram only). Either this or page_id is required unless post_ids is given."}, "platform": {"type": "string", "description": "'instagram' (default) or 'facebook'."}, "post_ids": {"type": "array", "items": {"type": "string"}, "description": "Fetch insights for just these post/media IDs instead of crawling the whole account."}, "metrics": {"type": "string", "description": "Comma-separated insight metrics. Defaults: instagram='total_views,views,reach', facebook='post_video_views,post_clicks,post_reactions_like_total' (v25 removed post_impressions*). Auto-adjusts if Meta rejects a metric."}, "fields": {"type": "string", "description": "Comma-separated base post fields. Defaults: instagram='id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count', facebook='id,message,created_time,permalink_url'."}, "where": {"type": "object", "description": "Server-side numeric filter on ONE metric: {\"metric\": \"views\", \"lt\": 5000}. Ops: lt, lte, gt, gte, eq."}, "sort": {"type": "string", "description": "Metric name to sort by, '-' prefix for descending (e.g. '-views'). Rows missing the metric sort last."}, "limit": {"type": "integer", "description": "Max rows returned AFTER filter+sort."}, "token": {"type": "string"}}, "required": ["token"]}},
    {"_remember": [{"kind": "fb_lookalike_audience", "path": ["id"], "label_path": ["name"]}], "name": "facebook_create_lookalike_audience", "description": "Create a lookalike audience from an existing custom audience seed. POST /act_{id}/customaudiences with subtype=LOOKALIKE. Pass origin_audience_id (seed CA id from facebook_get_custom_audiences), country (ISO-2 code, e.g. 'IN'), and optional ratio (0.01=top 1% most similar, default; max 0.10). The new audience needs a few hours to populate before it can deliver. Use the returned id in adset.targeting.custom_audiences.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "name": {"type": "string", "description": "Display name for the lookalike audience"}, "origin_audience_id": {"type": "string", "description": "Source custom audience ID (the seed)"}, "country": {"type": "string", "description": "ISO 3166-1 alpha-2 country code where the lookalike will be modeled (e.g. 'IN' for India)"}, "ratio": {"type": "number", "description": "Lookalike size as fraction of country population. 0.01=top 1% (default, smallest+highest quality), max 0.10"}, "description": {"type": "string"}}, "required": ["account_id", "token", "name", "origin_audience_id", "country"]}},
    {"_remember": [{"kind": "fb_custom_audience", "path": ["id"], "label_path": ["name"]}], "name": "facebook_create_custom_audience", "description": "Create a custom audience and (optionally) seed it with hashed users in one call. Two-step Meta API: POST /act_{id}/customaudiences (create container) + POST /{audience_id}/users (upload SHA-256 hashed users). Pass schema=EMAIL or PHONE (auto-resolved if you only set users_email or users_phone). Provide users via `users_email`/`users_phone` (JSON array or newline-separated string), or via `users_url` (public URL to a UTF-8 .txt file with one email/phone per line — server fetches and hashes). Server normalizes (lowercase-trim for emails, digits-only for phones) before SHA-256.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "name": {"type": "string", "description": "Audience name shown in Ads Manager"}, "schema": {"type": "string", "description": "EMAIL | PHONE | EMAIL_SHA256 | PHONE_SHA256 (aliases auto-resolved)"}, "description": {"type": "string"}, "users_email": {"type": "string", "description": "JSON array of email strings, OR newline-separated emails. Server hashes (SHA-256 of lowercase trimmed)."}, "users_phone": {"type": "string", "description": "JSON array of phone strings (with country code), OR newline-separated. Server hashes (SHA-256 of digits-only)."}, "users_url": {"type": "string", "description": "Public URL to a UTF-8 .txt file with one email/phone per line. Server detects schema from contents (presence of '@')."}, "customer_file_source": {"type": "string", "description": "Default USER_PROVIDED_ONLY. Other Meta values: PARTNER_PROVIDED_ONLY, BOTH_USER_AND_PARTNER_PROVIDED."}}, "required": ["account_id", "token", "name"]}},
    {"name": "facebook_create_lead_form", "description": "Create a Facebook Lead Form (Instant Form) on a Page. POST /{page_id}/leadgen_forms. Standard question types: FULL_NAME, EMAIL, PHONE, WHATSAPP_NUMBER, COMPANY_NAME, JOB_TITLE, CITY, STATE, ZIP, COUNTRY, DOB, GENDER. For multiple-choice questions use type=CUSTOM with {label,key,options:[{key,value},...]}. NB: Meta REJECTS legacy 'PHONE_NUMBER' — use 'PHONE' (PHONE_NUMBER/WHATSAPP/MOBILE aliases are auto-rewritten by the MCP). Custom-question option entries that are plain strings are auto-rewritten to {key,value} objects. The returned form id is auto-remembered for this conversation.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string"}, "token": {"type": "string", "description": "Page access token required"}, "name": {"type": "string"}, "questions": {"type": "string", "description": "JSON array of question objects e.g. [{\"type\":\"FULL_NAME\"},{\"type\":\"PHONE\"},{\"type\":\"EMAIL\"},{\"type\":\"CUSTOM\",\"label\":\"Business Type\",\"key\":\"biz_type\",\"options\":[{\"key\":\"b2b\",\"value\":\"B2B\"},{\"key\":\"b2c\",\"value\":\"B2C\"}]}]"}, "privacy_policy_url": {"type": "string"}, "context_card_title": {"type": "string", "description": "Intro screen title"}, "context_card_body": {"type": "string", "description": "Intro screen body text"}, "thank_you_title": {"type": "string"}, "thank_you_body": {"type": "string"}, "follow_up_url": {"type": "string", "description": "URL for thank you page button"}}, "required": ["page_id", "name", "token"]}, "_remember": [{"kind": "fb_lead_form", "path": ["id"], "label_path": ["name"]}]},
    {"name": "facebook_get_reach_estimate", "description": "Get audience size/reach estimate for a targeting spec. GET /act_{id}/reachestimate. Use to research audience before creating ad set.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "targeting_spec": {"type": "string", "description": "Full targeting JSON spec"}, "optimization_goal": {"type": "string"}}, "required": ["account_id", "targeting_spec", "token"]}},
    # ---------- CREATE ----------
    {"name": "facebook_create_campaign", "description": "Create a campaign. POST /act_{id}/campaigns.\n\nVALID OBJECTIVES (use OUTCOME_* in 2024+; legacy labels are auto-normalized in non-strict mode): OUTCOME_AWARENESS, OUTCOME_TRAFFIC, OUTCOME_ENGAGEMENT, OUTCOME_LEADS, OUTCOME_SALES, OUTCOME_APP_PROMOTION.\n\nCONDITIONAL REQUIREMENTS:\n- If the campaign is for political/social/credit/housing/employment topics: special_ad_categories MUST be set (e.g. [\"ISSUES_ELECTIONS_OR_POLITICS\"]) and special_ad_category_country MUST be set (e.g. [\"IN\"]).\n- For CBO (campaign-level budget) set daily_budget OR lifetime_budget (cents). Without either => ad-set budget mode.\n- Meta may reject some objective+optimization_goal combinations downstream — see facebook_create_adset description for valid pairs.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "name": {"type": "string"}, "objective": {"type": "string"}, "status": {"type": "string"}, "daily_budget": {"type": "integer", "description": "Campaign daily budget in cents for CBO (e.g. 50000 = Rs 500)"}, "lifetime_budget": {"type": "integer", "description": "Campaign lifetime budget in cents for CBO"}, "is_adset_budget_sharing_enabled": {"type": "boolean", "description": "Default false. If true + budget causes error, server auto-retries with two-step flow."}, "bid_strategy": {"type": "string", "description": "e.g. LOWEST_COST_WITHOUT_CAP, COST_CAP. Auto-set via update if CBO two-step needed."}, "special_ad_categories": {"type": "array", "items": {"type": "string"}}, "special_ad_category_country": {"type": "array", "items": {"type": "string"}, "description": "ISO codes e.g. [\"IN\"] with political/social categories"}}, "required": ["account_id", "name", "objective", "token"]}},
    {"name": "facebook_targeting_search", "description": "Search targeting options for ad creation. Types: adgeolocation (locations), adinterest (interests), adTargetingCategory. If account_id is set, uses GET act_{id}/targetingsearch first, then falls back to global /search. For adgeolocation use location_types: country, region, city, zip, country_group.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string", "description": "Ad account id (numeric or act_). Recommended for production."}, "token": {"type": "string"}, "search_type": {"type": "string", "description": "adgeolocation | adinterest | adTargetingCategory"}, "q": {"type": "string", "description": "Search query (e.g. city name, country, interest)"}, "location_types": {"type": "string", "description": "JSON array e.g. [\"country\"] or [\"region\"], [\"city\"] for adgeolocation"}, "country_code": {"type": "string", "description": "Country code (e.g. US, IN) for region/city search"}, "region_id": {"type": "string", "description": "Region ID when searching within a region"}, "limit": {"type": "integer", "description": "Max results (default 50, max 200)"}}, "required": ["search_type", "token"]}},
    {"name": "facebook_batch_interest_search", "description": "Search multiple interests in batch. Returns {found: [{id, name, query}], missing: [query]}. Use to validate interest list from a document before creating ad set.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string", "description": "Ad account id (numeric or act_)"}, "token": {"type": "string"}, "interest_names": {"type": "array", "items": {"type": "string"}, "description": "List of interest names to search e.g. [\"Digital marketing\", \"Social media\", \"Content creation\"]"}}, "required": ["account_id", "token", "interest_names"]}},
    {"name": "facebook_batch_targeting_category_search", "description": "Search multiple behaviors AND demographics in batch (Ads Manager calls these 'Detailed Targeting' axes). Returns {found: [{id, name, path, query}], missing: [query]}. The 'path' array indicates Meta's hierarchy, e.g. [\"Behaviors\", \"Digital activities\"] or [\"Demographics\", \"Education\"] — use path[0] to route each result to the correct flexible_spec axis. Use this to validate behaviors/demographics from a brief before creating ad set.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string", "description": "Ad account id (numeric or act_)"}, "token": {"type": "string"}, "category_names": {"type": "array", "items": {"type": "string"}, "description": "List of behavior/demographic names to search e.g. [\"Engaged Shoppers\", \"Small business owners\", \"Frequent Buyers\", \"Marketing Professionals\"]"}}, "required": ["account_id", "token", "category_names"]}},
    {"name": "facebook_attach_pixel", "description": "Attach or merge Meta Pixel into an ad set promoted_object (pixel_id, optional custom_event_type, page_id). Diff-based POST when allowed. For POST_ENGAGEMENT + ON_POST, skips API call; skipped is [{field,reason}] for pixel_id/custom_event_type. Auto-maps PAGE_VIEW to CONTENT_VIEW.", "inputSchema": {"type": "object", "properties": {"adset_id": {"type": "string"}, "token": {"type": "string"}, "pixel_id": {"type": "string"}, "custom_event_type": {"type": "string", "description": "e.g. CONTENT_VIEW, PURCHASE, LEAD, ADD_TO_CART. Note: PAGE_VIEW is auto-mapped to CONTENT_VIEW."}, "page_id": {"type": "string"}}, "required": ["adset_id", "pixel_id", "token"]}},
    {"name": "facebook_verify_ad_setup", "description": "Post-creation verification: GET campaign, ad set, ad (+ creative) and compare to expected (pixel_id, destination_type, CTA, link vs existing post). Returns ok, issues[], fetched objects.", "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}, "campaign_id": {"type": "string"}, "adset_id": {"type": "string"}, "ad_id": {"type": "string"}, "expected": {"type": "object", "description": "Optional: pixel_id, destination_type, cta_type, expect_link_ad, expect_existing_post, require_targeting"}}, "required": ["token"]}},
    {"name": "facebook_preview_document_campaign", "description": "Read-only plan: validate document, resolve interest/behavior queries and build the same targeting spec as sync; returns summary_for_user, normalized_document, resolved_targeting, document_hash_sha256. Does NOT create or update anything in Meta. strict_mode: strict_validate (outcome objectives only; errors like Invalid objective: ENGAGEMENT with Use: OUTCOME_ENGAGEMENT); default targeting is RAW (deterministic) unless you set targeting_mode RESOLVE with options.allow_targeting_resolve=true (Meta search IDs can vary). Optional options.strict_validator_version must equal server strict_validator_version in responses. Responses include strict_meta_platform_limitations (attribution/placement expansion/learning phase are not fully controllable via API). strict_preview_token_cached + strict_preview hash flow for sync. ValueError responses may include JSON-RPC error.data.relaxed_fix_suggestions. Non-strict: default targeting_mode RESOLVE.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "document": {"description": "Same JSON as facebook_sync_document_campaign"}, "user_id": {"type": "string"}, "brief_pdf_base64": {"type": "string", "x-attachment-base64": True, "x-attachment-mime": ["application/pdf"], "description": "Auto-filled from a chat-attached PDF brief — preview uses it the same way sync does."}, "brief_pdf_path": {"type": "string", "x-attachment-path": True, "x-attachment-mime": ["application/pdf"], "description": "Auto-filled absolute on-disk path of a chat-attached PDF brief."}}, "required": ["account_id", "token"]}},
    {"name": "facebook_sync_document_campaign", "description": "Production document sync: one new campaign + ad set + creative + ad in a single call. SINGLE POST ONLY — for 2+ posts/reels you MUST use facebook_sync_document_campaigns_batch instead (one call creates ALL campaigns; this single tool is capped at ~5 calls per run and the run will stall). Pre-merges saved_audience. On failure after campaign create, the campaign is deleted (set options.keep_campaign_on_sync_failure=true to keep for inspection).\n\nALWAYS call facebook_preview_document_campaign FIRST (read-only) to validate the document and surface blocking_errors before this destructive call.\n\nDOCUMENT SHAPE (all keys optional unless noted; required-but-conditional in CAPS):\n  campaign: {NAME, OBJECTIVE (OUTCOME_*), special_ad_categories, special_ad_category_country, daily_budget, lifetime_budget, bid_strategy, status}\n  adset:    {NAME, OPTIMIZATION_GOAL, billing_event (default IMPRESSIONS), destination_type, daily_budget, lifetime_budget, start_time, end_time, promoted_object {page_id, pixel_id, custom_event_type, application_id, product_set_id}, is_dynamic_creative (true if creative uses asset_feed_spec), page_name (optional: case-insensitive page name lookup if you don't have page_id; mismatch errors loudly instead of picking the wrong page)}\n  targeting:{geo_countries, geo_regions, geo_cities, geo_custom_locations, age_min, age_max, genders, flexible_spec, custom_audiences, excluded_custom_audiences, excluded_geo_locations}\n  placements:{publisher_platforms, device_platforms, facebook_positions, instagram_positions, messenger_positions, audience_network_positions, threads_positions}\n  creative: {NAME, MODE (LINK_AD|EXISTING_POST|CAROUSEL|COLLECTION|DCO|DPA|LEAD_FORM|WHATSAPP), page_id, page_name (optional alternative — see adset.page_name), link_url, message, image_hash, headline, description, CTA_TYPE (when MODE=LINK_AD), object_story_id (when MODE=EXISTING_POST), carousel_cards, asset_feed_spec, instant_experience_id, source_instagram_media_id, branded_content_sponsor_page_id, authorization_category=POLITICAL}\n  tracking: {pixel_id, CUSTOM_EVENT_TYPE (required if pixel_id set: CONTENT_VIEW|PURCHASE|LEAD|ADD_TO_CART|...)}\n  ad:       {NAME}\n  options:  {strict_mode, strict_preview_document_hash, keep_campaign_on_sync_failure, require_user_confirmation, targeting_mode (RAW|RESOLVE), allow_targeting_resolve, saved_audience_advantage_audience, allow_risky_objective_optimization, page_name (optional global fallback for page resolution), shop_destination_opt_out (Graph v26: true keeps website creatives out of the Meta Shop; or set creative.destination_spec)}\n\nGRAPH v26 (enforced on all versions from 2026-10-27): instagram_positions 'explore' and messenger_positions 'story' no longer exist — they are stripped with an auto_fix (error in strict_mode). HOUSING/EMPLOYMENT/CREDIT ad sets always get an explicit targeting_automation.advantage_audience (0 unless the brief sets 1).\n\nMETA VALID-COMBINATION CHEAT SHEET (objective → optimization_goal → destination_type). Pre-flight validator rejects bad combos before any Meta call:\n  OUTCOME_AWARENESS  → REACH/IMPRESSIONS/AD_RECALL_LIFT/THRUPLAY/VIDEO_VIEWS → destination_type OMIT (UNDEFINED) — Meta rejects ON_AD/ON_POST here (subcode 1815715).\n  OUTCOME_TRAFFIC    → LINK_CLICKS/LANDING_PAGE_VIEWS → WEBSITE (most common) or ON_AD; QUALITY_CALL → PHONE_CALL.\n  OUTCOME_ENGAGEMENT → POST_ENGAGEMENT/PAGE_LIKES/EVENT_RESPONSES/VIDEO_VIEWS/THRUPLAY/TWO_SECOND_CONTINUOUS_VIDEO_VIEWS → omit destination_type or ON_AD/ON_POST/ON_PAGE/ON_EVENT/ON_VIDEO. THRUPLAY under ENGAGEMENT is valid (UI: Engagement → Video Views → On your ad → Maximise ThruPlay views).\n  OUTCOME_LEADS      → LEAD_GENERATION → ON_AD (Instant Form); OFFSITE_CONVERSIONS → WEBSITE; CONVERSATIONS → MESSENGER/WHATSAPP/INSTAGRAM_DIRECT.\n  OUTCOME_SALES      → OFFSITE_CONVERSIONS/VALUE → WEBSITE or APP.\n  OUTCOME_APP_PROMOTION → APP_INSTALLS/OFFSITE_CONVERSIONS/VALUE → APP only.\nRule of thumb: when in doubt, OMIT destination_type — Meta treats absence as UNDEFINED which most goals accept. The connector also auto-clears destination_type when the goal prefers UNDEFINED (THRUPLAY/VIDEO_VIEWS/POST_ENGAGEMENT/PAGE_LIKES/EVENT_RESPONSES); look for the auto_fixes message to confirm.\n\nPAGE RESOLUTION: if the brief gives a Page by *name* (e.g. \"Rekha Gupta Fans Club\"), pass it in creative.page_name (or options.page_name). The connector calls facebook_get_pages, requires a case-insensitive match, and ERRORS LOUDLY with the list of accessible Pages if the requested name is not connected to this user/Business — preventing the previous silent-substitution bug where the wrong Page was used.\n\nTARGETING — non-city locations: \"Connaught Place\" (neighborhood), pincode \"110001\" (zip), \"Maharashtra\" (region) etc. can be passed in targeting.geo_cities and the auto-resolver will route them into the correct geo_locations.<neighborhoods|subcities|zips|regions|countries> key. Only entries that don't match ANY Meta location type are dropped (with a clear auto_fix message).\n\nSTRICT MODE: pass options.strict_preview_document_hash returned by preview to gate the sync against doc drift.\nWHY_FAILED: on Meta API error, response includes structuredError with {code, subcode, summary, likely_causes, what_to_try} for the LLM to self-correct.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "document": {"description": "JSON: campaign, adset, targeting, placements, creative, tracking, ad, options (keep_campaign_on_sync_failure, require_user_confirmation, strict_mode, strict_preview_document_hash, strict_validator_version, targeting_mode, allow_targeting_resolve, saved_audience_advantage_audience, allow_risky_objective_optimization, ...)"}, "user_id": {"type": "string"}, "user_confirmed": {"type": "boolean", "description": "Set true after the user explicitly approved the plan (required to create when options.require_user_confirmation is true)"}, "brief_pdf_base64": {"type": "string", "x-attachment-base64": True, "x-attachment-mime": ["application/pdf"], "description": "Auto-filled from a chat-attached PDF brief. Server-side parser merges its fields into ``document`` when present (LLM-supplied document keys win on conflict)."}, "brief_pdf_path": {"type": "string", "x-attachment-path": True, "x-attachment-mime": ["application/pdf"], "description": "Auto-filled absolute on-disk path of a chat-attached PDF brief — alternative to base64 for large files."}}, "required": ["account_id", "token"]}},
    {"name": "facebook_sync_document_campaigns_batch", "description": "BATCH document sync: create one campaign + ad set + creative + ad PER item in posts[] — all in ONE call. Handles ANY size brief (50+ posts): the server works within a ~45-second time budget per call (kept under proxy timeouts so the response ALWAYS reaches you) and, if posts remain, returns continuation.remaining_posts — IMMEDIATELY call this tool again with the SAME document and posts=continuation.remaining_posts until no continuation is returned (never re-send processed posts; that duplicates ads). USE THIS (not repeated facebook_sync_document_campaign calls) whenever a brief gives MULTIPLE posts/reels with the same settings (e.g. '5 reels -> 5 campaigns'): the server loops in plain code, so one tool call replaces dozens and the run never exhausts its iteration budget.\n\nHOW: pass `document` = the SHARED template (same shape as facebook_sync_document_campaign: campaign/adset/targeting/placements/creative/tracking/ad/options) written ONCE, and `posts` = a JSON array with one small object per post.\n\nposts[] item keys (one creative source required, rest optional):\n  instagram_permalink_url | instagram_url | url — IG post/Reel permalink\n  source_instagram_media_id — IG media id (works for posts, Reels, videos)\n  object_story_id — FB page post (page_id_post_id)\n  post_number — defaults to list position (1-based)\n  campaign_name / adset_name / ad_name — full per-post name overrides\n  campaign / adset / creative / ad / targeting — dict deep-merge overrides for anything that differs per post\n\nNAMING: template names may contain {post_number} (or {post}/{n}); an existing 'Post <digits>' token is auto-renumbered per post (template 'Post 1 | Delhi | ThruPlay' -> 'Post 2 | Delhi | ThruPlay', ...); otherwise ' | Post <N>' is appended. Names are always unique per post.\n\nFAILURE ISOLATION: a failed post does NOT abort the batch — its campaign is rolled back (same as single sync) and the loop continues. CAPTION NAMING IS ON BY DEFAULT for existing-IG-post batches: the SERVER fetches every caption in one IG media crawl and names campaign+adset+ad with the first 50 caption words (no shortcode fallbacks, no per-post agent lookups) whenever creative.page_id or creative.instagram_user_id is in the template — do NOT fetch captions yourself and do NOT invent shortcode-based names. Set document.options.caption_names=false ONLY when the brief dictates its own naming scheme; explicit per-post campaign_name/adset_name/ad_name always win. DUPLICATE GUARD: before creating each post the server checks the account's recent campaigns (48h window); if a campaign for that post already exists (same name or same IG shortcode in the name) the post is returned as skipped=already_exists WITH the existing campaign_id instead of creating a twin — treat skipped posts as already created, do NOT retry them. Override with document.options.allow_duplicate_campaigns=true or post item allow_duplicate=true. Response: {ok, total, succeeded, skipped_already_exists, failed, results:[{post_number, ok, skipped?, campaign_id, adset_id, creative_id, ad_id, error, auto_fixes}]} — report this table to the user per post.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "document": {"description": "SHARED template JSON — same shape as facebook_sync_document_campaign's document; written once for the whole batch"}, "posts": {"type": "array", "description": "One object per post; see tool description for item keys", "items": {"type": "object"}}, "user_id": {"type": "string"}, "user_confirmed": {"type": "boolean", "description": "Set true after the user explicitly approved the plan (applies to every post in the batch)"}}, "required": ["account_id", "token", "document", "posts"]}},
    {"name": "facebook_create_adset", "description": "Create an ad set with full audience and location targeting. POST /act_{id}/adsets. Requires ads_management.\n\nIMPLICIT REQUIREMENTS (LLM must satisfy or call will fail in strict mode):\n- optimization_goal — required in strict mode; auto-defaulted from objective in non-strict. Valid examples: LINK_CLICKS (Traffic), POST_ENGAGEMENT (Engagement), REACH (Awareness), OFFSITE_CONVERSIONS (Sales w/ pixel), LEAD_GENERATION (Leads), QUALITY_CALL (Call), THRUPLAY (Video).\n- billing_event — defaults IMPRESSIONS; only set explicitly for goals that support alternates (LINK_CLICKS allowed for Traffic, etc.).\n- destination_type — auto-set per objective (WEBSITE for SALES/LEADS, APP for APP_PROMOTION). Override to PHONE_CALL for call ads, MESSENGER/WHATSAPP for messaging ads.\n- promoted_object — required for OUTCOME_SALES (page_id+pixel_id+custom_event_type), OUTCOME_LEADS (page_id), OUTCOME_APP_PROMOTION (application_id+object_store_url), DPA (page_id+product_set_id), PHONE_CALL goals (page_id).\n- is_dynamic_creative — set TRUE if the linked creative will use asset_feed_spec (Dynamic Creative Optimization). Mismatch => Meta error 1885073.\n- For carousel/collection/DCO creatives, page_id is required on the creative — make sure facebook_get_pages was called first.\n\nFORBIDDEN PAIRS (validated pre-API; full matrix in facebook_sync_document_campaign):\n- OUTCOME_TRAFFIC + THRUPLAY → use OUTCOME_AWARENESS or OUTCOME_ENGAGEMENT for video-view goals\n- OUTCOME_LEADS + THRUPLAY → use OUTCOME_AWARENESS or OUTCOME_ENGAGEMENT for video-view goals\n- OUTCOME_TRAFFIC + LANDING_PAGE_VIEWS + destination_type ≠ WEBSITE\n- OUTCOME_AWARENESS + (any goal) + destination_type=ON_AD/ON_POST → OMIT destination_type (Meta rejects with subcode 1815715)\n- CALL_NOW CTA without destination_type=PHONE_CALL\n- WHATSAPP_MESSAGE CTA without destination_type=WHATSAPP\n- THRUPLAY / VIDEO_VIEWS / POST_ENGAGEMENT / PAGE_LIKES with non-UNDEFINED destination → connector auto-clears destination_type and logs the fix; Meta then accepts.\n\nBudget: daily_budget or lifetime_budget (cents). Location: geo_countries (e.g. [\"US\",\"IN\"]), geo_regions, geo_cities (with radius 2-80 km), geo_custom_locations (lat/long + radius km), geo_country_groups (e.g. worldwide, europe). Audience: age_min (13-65), age_max, genders (1=male,2=female), flexible_spec (interests), custom_audiences. Radius standard 2-80 km. Pass targeting as JSON or use structured params.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "campaign_id": {"type": "string"}, "name": {"type": "string"}, "status": {"type": "string"}, "daily_budget": {"type": "integer"}, "lifetime_budget": {"type": "integer"}, "billing_event": {"type": "string"}, "optimization_goal": {"type": "string"}, "targeting": {"type": "string", "description": "Full targeting JSON string (optional if using structured params below)"}, "geo_countries": {"type": "string", "description": "JSON array of country codes e.g. [\"US\",\"IN\"] for international"}, "geo_country_groups": {"type": "string", "description": "JSON array e.g. [\"worldwide\",\"europe\",\"asia\"]"}, "geo_regions": {"type": "string", "description": "JSON array of {key: region_id} from targeting_search"}, "geo_cities": {"type": "string", "description": "JSON array of {key, radius, distance_unit} from targeting_search; radius 2-80 km. NOTE: cities ALWAYS get a radius (default 25 km) — Ads Manager shows 'X (+25 km)' and delivery includes surrounding areas OUTSIDE the city/state. For exact administrative boundaries (briefs like 'only Delhi, no nearby areas') use geo_regions with the region key from targeting_search instead. City-states (city name == its region name) are auto-upgraded to the region."}, "geo_custom_locations": {"type": "string", "description": "JSON array of {latitude, longitude, radius, distance_unit} or {address_string, radius}; radius 2-80 km"}, "geo_radius_km": {"type": "number", "description": "Default radius in km (min 2, max 80) for cities/custom_locations"}, "geo_location_types": {"type": "string", "description": "JSON array [\"home\",\"recent\"] for people who live or recently in area"}, "age_min": {"type": "integer", "description": "Minimum age 13-65"}, "age_max": {"type": "integer", "description": "Maximum age 13-65"}, "genders": {"type": "string", "description": "JSON array [1,2] for male, female"}, "flexible_spec": {"type": "string", "description": "JSON array of interest/behavior specs from targeting_search"}, "custom_audiences": {"type": "string", "description": "JSON array of {id: custom_audience_id}"}, "excluded_geo_locations": {"type": "string", "description": "JSON object to exclude locations"}, "excluded_custom_audiences": {"type": "string", "description": "JSON array of {id} to exclude"}, "bid_strategy": {"type": "string"}, "bid_amount": {"type": "integer"}, "bid_constraints": {"type": "string"}, "promoted_object": {"type": "string", "description": "JSON object e.g. {\"page_id\": \"123\"} for lead/engagement campaigns"}, "page_id": {"type": "string", "description": "Page ID for promoted_object shortcut (used if promoted_object not set)"}, "destination_type": {"type": "string", "description": "Conversion location: WEBSITE, WHATSAPP, MESSENGER, INSTAGRAM_DIRECT, PHONE_CALL, APP, ON_AD, etc."}, "start_time": {"type": "string", "description": "Ad set start time ISO 8601 e.g. 2025-03-13T00:00:00+0000"}, "end_time": {"type": "string", "description": "Ad set end time ISO 8601 e.g. 2025-03-31T23:59:59+0000"}, "publisher_platforms": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"facebook\", \"instagram\"]"}, "device_platforms": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"mobile\"]"}, "facebook_positions": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"feed\", \"story\"]"}, "instagram_positions": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"stream\", \"story\", \"reels\"]"}, "messenger_positions": {"type": "array", "items": {"type": "string"}, "description": "With publisher_platforms including messenger, e.g. [\"messenger_home\"]. Aliases: messenger_inbox -> messenger_home."}, "is_dynamic_creative": {"type": "boolean", "description": "Set true to enable Dynamic Creative Optimization. Required when the ad's creative uses asset_feed_spec — Meta combines the asset variants per impression to pick the best-performing combination."}, "product_set_id": {"type": "string", "description": "Catalog product set ID for Dynamic Product Ads (DPA). When set, the adset's promoted_object binds to this product_set_id (with optional page_id). Get product set IDs via facebook_get_catalog_product_sets."}}, "required": ["account_id", "campaign_id", "name", "token"]}},
    {"name": "facebook_create_ad", "description": "Create an ad. POST /act_{id}/ads. Requires ads_management.\n\nParams: adset_id, name, creative_id (from facebook_create_creative), status (PAUSED/ACTIVE), tracking_specs (optional).\n\ntracking_specs RULES (Meta error_subcode 1634005 / 1634034 if you violate these):\n- Every entry MUST have an object identifier alongside action.type. The most common shape is {\"action.type\":[<event>],\"fb_pixel\":[<pixel_id>]}. Without fb_pixel (or fb_page / application / leadgen_form / offer / event / object — whichever fits the event), Meta returns \"Action query missing object\" (subcode 1634005).\n- action.type values must be compatible with the adset's optimization_goal. Examples: LINK_CLICKS → \"link_click\"; LANDING_PAGE_VIEWS → \"landing_page_view\"; OFFSITE_CONVERSIONS/VALUE → \"offsite_conversion\"; LEAD_GENERATION → \"offsite_conversion\" or \"lead\" with the right object; CONVERSATIONS → \"onsite_conversion.messaging_first_reply\". Mismatched action.type → \"Invalid action type\" (subcode 1634034).\n- Easiest path: skip tracking_specs unless you specifically need custom funnel attribution. The sync flow auto-builds the correct tracking_specs from tracking.pixel_id + adset.optimization_goal when you use facebook_sync_document_campaign.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "adset_id": {"type": "string"}, "name": {"type": "string"}, "creative_id": {"type": "string"}, "status": {"type": "string"}, "tracking_specs": {"type": "string", "description": "JSON-stringified list of Meta tracking specs for funnel attribution. e.g. [{\"action.type\":[\"landing_page_view\"],\"fb_pixel\":[\"<pixel_id>\"]}]. Auto-built by sync flow when tracking.pixel_id is set."}}, "required": ["account_id", "adset_id", "name", "creative_id", "token"]}},
    {"name": "facebook_upload_image", "description": "Upload an image to the ad account, returning {hash} for use as creative.image_hash. POST /act_{id}/adimages. If the user attached an image in chat, the bytes are auto-supplied — call this tool with account_id only. If the user only pasted a public URL in their text, pass that as `url`. Either path, the resulting `hash` MUST be set on creative.image_hash in the next sync_document_campaign call. PNG/JPEG/GIF. Requires ads_management. The returned hash is auto-remembered for this conversation; the agent can recall it later via the system-prompt assets section without re-uploading.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "url": {"type": "string", "description": "Public image URL — only when no chat attachment is present"}, "image_base64": {"type": "string", "x-image-base64": True, "description": "Auto-filled from the chat-attached image — do not emit yourself."}, "filename": {"type": "string", "description": "Original filename e.g. bol7_creative.png"}}, "required": ["account_id", "token"]}, "_remember": [{"kind": "fb_image", "path": ["hash"]}, {"kind": "fb_image", "path": ["images", "*", "hash"]}]},
    {"name": "facebook_upload_video", "description": "Upload a video to the ad account. POST /act_{id}/advideos. Pass video_url (public URL) OR rely on a chat-attached video — when the user attaches an MP4 the loop auto-supplies it via video_base64. Requires ads_management. The returned video id is auto-remembered for this conversation.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "video_url": {"type": "string", "x-video-url": True, "description": "Public video URL — optional when the user attaches a video in chat."}, "video_base64": {"type": "string", "x-attachment-base64": True, "x-attachment-mime": ["video/"], "description": "Auto-filled from chat-attached video. Do NOT emit yourself."}, "filename": {"type": "string"}}, "required": ["account_id", "token"]}, "_remember": [{"kind": "fb_video", "path": ["id"]}]},
    {"name": "facebook_create_creative", "description": "Create an ad creative. POST /act_{id}/adcreatives.\n\nFORMAT MODES (pick one path; conditional fields):\n- LINK_AD (single image link): page_id + link_url + message + image_hash (from facebook_upload_image) + cta_type (CTA enum below). headline + description fill link_data.name/description.\n- EXISTING_POST: object_story_id only (page_id_post_id). Boosts an already-published Page post.\n- CAROUSEL: page_id + message + carousel_cards (JSON list of 2-10 cards, each with link + image_hash or video_id; optional name/description/call_to_action).\n- COLLECTION (Instant Experience): page_id + instant_experience_id + a single-image OR carousel cover.\n- DCO (Dynamic Creative): page_id + asset_feed_spec (bodies[], titles[], descriptions[], images[], videos[], ad_formats[], call_to_action_types[]). The adset MUST have is_dynamic_creative=true.\n- DPA (Dynamic Product Ads): page_id + product_set_id is set on the adset's promoted_object; creative usually uses a DPA template via object_story_spec.\n- LEAD_FORM: object_story_spec.link_data.call_to_action.type=LEAD_GENERATION + value.lead_gen_form_id (from facebook_create_lead_form / facebook_get_lead_forms).\n- BRANDED_CONTENT: branded_content_sponsor_page_id (both pages must consent in Business Suite).\n- INSTAGRAM_EXISTING: source_instagram_media_id only (do NOT also pass instagram_actor_id).\n\nCTA enum (for LINK_AD): SHOP_NOW, LEARN_MORE, SIGN_UP, GET_OFFER, CONTACT_US, GET_QUOTE, SUBSCRIBE, APPLY_NOW, BOOK_TRAVEL, DOWNLOAD, MESSAGE_PAGE, CALL_NOW (requires destination_type=PHONE_CALL on adset), WHATSAPP_MESSAGE (requires destination_type=WHATSAPP), SEND_MESSAGE (Messenger).\n\nFor political ads: set authorization_category=POLITICAL.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "name": {"type": "string"}, "object_story_spec": {"type": "string"}, "object_story_id": {"type": "string", "description": "Existing post ID (page_id_post_id) to use as creative"}, "instagram_actor_id": {"type": "string", "description": "Instagram business account ID for Instagram placement"}, "instagram_user_id": {"type": "string", "description": "Instagram user ID (API v22+); use if instagram_actor_id fails"}, "page_id": {"type": "string"}, "link_url": {"type": "string"}, "message": {"type": "string", "description": "Body copy / primary text"}, "image_hash": {"type": "string"}, "headline": {"type": "string", "description": "Link headline (Meta link_data.name, ≤40 chars). Required for visible headline in the ad preview."}, "description": {"type": "string", "description": "Link description (Meta link_data.description, ≤30 chars). Required for visible description in the ad preview."}, "authorization_category": {"type": "string", "description": "POLITICAL for social issues / elections / politics campaigns"}, "bylines": {"type": "string", "description": "'Paid for by' disclaimer string Meta requires on EVERY political/issues ad creative (special_ad_categories includes ISSUES_ELECTIONS_POLITICS). Without it the API rejects the creative with error 100 / subcode 2708016. Example: 'BOL7 TECHNOLOGIES PRIVATE LIMITED'. Find existing bylines for a Page by querying ads_archive?search_page_ids=<page_id>&fields=bylines. facebook_sync_document_campaign auto-fetches and reuses this from prior approved ads when missing."}, "source_instagram_media_id": {"type": "string", "description": "Instagram media ID to use as creative (existing IG post). Do NOT pass instagram_actor_id with this — Meta rejects that combination. Get IG media IDs from /{ig_account_id}/media."}, "carousel_cards": {"type": "string", "description": "JSON list of 2-10 carousel cards. Each card requires `link` and `image_hash` (or `video_id`); optional `name` (headline), `description`, `call_to_action`. Example: [{\"link\":\"https://...\",\"image_hash\":\"abc\",\"name\":\"Card 1\",\"call_to_action\":{\"type\":\"SHOP_NOW\",\"value\":{\"link\":\"https://...\"}}}, ...]. Requires page_id and message; takes priority over image_hash when both are set."}, "instant_experience_id": {"type": "string", "description": "Canvas / Instant Experience ID. When set, the creative becomes a Collection ad (cover image/video opens into the full-screen Canvas). Pre-create the Canvas in Ads Manager; this tool only attaches it to the cover."}, "branded_content_sponsor_page_id": {"type": "string", "description": "Sponsor Page ID for Branded Content / Paid Partnership ads. Setting this declares the paid partnership; both the brand page and sponsor page must consent in Meta Business Suite."}, "asset_feed_spec": {"type": "string", "description": "Dynamic Creative Optimization payload — JSON dict with arrays for bodies, titles, descriptions, images, videos, ad_formats, call_to_action_types. Meta combines variants per impression to pick the winner. When set, takes priority over single-image and carousel; the adset must also have is_dynamic_creative=true."}, "destination_spec": {"type": "string", "description": "Graph v26: creatives with a website link are auto-sent to the advertiser's Meta Shop too (WEBSITE_AND_SHOP). Pass WEBSITE_AND_SHOP_OPT_OUT (or JSON {\"destination_type\": \"...\"}) to keep the ad website-only. Omit to accept Meta's default."}}, "required": ["account_id", "name", "token"]}},
    # ---------- Page & Instagram posts ----------
    {"name": "facebook_post_page_photo", "description": "Post to Facebook Page: photo-only (url + caption) OR full post with link preview (message + link + optional picture, name, description). Use url+caption for image post; use message+link for post with link/button preview. The image is auto-attached from any image uploaded in the chat — do NOT ask the user for the URL. When the chat service's public URL is reachable (production deploys) the URL path is used; otherwise the raw bytes are uploaded directly so local-dev posts work too. Requires pages_manage_posts.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID"}, "token": {"type": "string", "description": "Page access token"}, "url": {"type": "string", "x-image-url": True, "description": "Public image URL (for photo-only post; use with caption). Auto-filled from chat-attached image if available."}, "caption": {"type": "string", "description": "Caption for photo post (full text, hashtags)"}, "message": {"type": "string", "description": "Full post text for feed post (with link): headline, body, contact, hashtags"}, "link": {"type": "string", "description": "URL for link preview and CTA button (e.g. WhatsApp, website)"}, "picture": {"type": "string", "description": "Image URL for feed post (optional). Auto-derived from the chat-attached image when not supplied."}, "image_base64": {"type": "string", "x-image-base64": True, "description": "Auto-filled from the chat-attached image. Do NOT emit yourself. Used as a fallback when the public URL isn't reachable (local-dev setups)."}, "filename": {"type": "string", "description": "Original filename for the bytes-upload path. Auto-filled from the chat attachment."}, "name": {"type": "string", "description": "Link preview title"}, "description": {"type": "string", "description": "Link preview description"}}, "required": ["page_id", "token"]}},
    {"name": "facebook_post_page_video", "description": "Post a video to a Facebook Page. POST /{page-id}/videos. Pass video_url (public video URL) OR rely on a chat-attached video. Optional description. The video_url field is auto-filled from any video attached in the chat — do NOT ask the user for the URL when a video is already attached. Requires pages_manage_posts.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string", "description": "Facebook Page ID"}, "token": {"type": "string"}, "video_url": {"type": "string", "x-video-url": True, "description": "Public video URL — optional when the user attaches a video in chat."}, "video_base64": {"type": "string", "x-attachment-base64": True, "x-attachment-mime": ["video/"], "description": "Auto-filled from chat-attached video. Do NOT emit yourself."}, "description": {"type": "string", "description": "Optional description for the video"}}, "required": ["page_id", "token"]}},
    {"name": "facebook_post_instagram_photo", "description": "Publish a single photo to an Instagram feed. Two-step: POST /{ig-user-id}/media (image_url + caption) then POST /{ig-user-id}/media_publish. Pass instagram_user_id directly OR page_id (auto-resolves linked IG Business Account). Requires instagram_basic + instagram_content_publish; IG account must be Business or Creator linked to the Page.", "inputSchema": {"type": "object", "properties": {"instagram_user_id": {"type": "string", "description": "IG Business Account ID. If omitted, page_id is used to resolve."}, "page_id": {"type": "string", "description": "Facebook Page ID with linked IG Business Account (alternative to instagram_user_id)."}, "token": {"type": "string"}, "image_url": {"type": "string", "x-image-url": True, "description": "Public image URL (JPEG; PNG may be converted by Meta). Auto-filled from chat-attached image if available."}, "caption": {"type": "string", "description": "Caption with hashtags / @mentions"}}, "required": ["token", "image_url"]}},
    {"name": "facebook_post_instagram_video", "description": "Publish a single video to an Instagram feed. Async container flow: POST /media (media_type=VIDEO, video_url) -> poll status_code until FINISHED -> POST /media_publish. Pass instagram_user_id OR page_id. Requires instagram_content_publish.", "inputSchema": {"type": "object", "properties": {"instagram_user_id": {"type": "string"}, "page_id": {"type": "string"}, "token": {"type": "string"}, "video_url": {"type": "string", "x-video-url": True, "description": "Public MP4 URL — optional if user attached video in chat."}, "video_base64": {"type": "string", "x-attachment-base64": True, "x-attachment-mime": ["video/"], "description": "Auto-filled from chat-attached video — do not emit yourself."}, "caption": {"type": "string"}, "thumb_offset": {"type": "integer", "description": "Optional cover frame offset in ms"}}, "required": ["token"]}},
    {"name": "facebook_post_instagram_reel", "description": "Publish a Reel. Async container flow: POST /media (media_type=REELS, video_url, share_to_feed) -> poll FINISHED -> POST /media_publish. Pass instagram_user_id OR page_id. Requires instagram_content_publish.", "inputSchema": {"type": "object", "properties": {"instagram_user_id": {"type": "string"}, "page_id": {"type": "string"}, "token": {"type": "string"}, "video_url": {"type": "string", "x-video-url": True, "description": "Public MP4 URL (Reel-compliant: 9:16, ≤90s typical) — optional if user attached video in chat."}, "video_base64": {"type": "string", "x-attachment-base64": True, "x-attachment-mime": ["video/"], "description": "Auto-filled from chat-attached video — do not emit yourself."}, "caption": {"type": "string"}, "share_to_feed": {"type": "boolean", "description": "Default true; also surfaces the Reel on the IG feed grid"}, "cover_url": {"type": "string", "description": "Optional public URL for cover image"}, "audio_name": {"type": "string", "description": "Optional audio attribution name"}}, "required": ["token", "video_url"]}},
    {"name": "facebook_post_instagram_story", "description": "Publish a Story (photo OR video). POST /media (media_type=STORIES, image_url|video_url) -> poll if video -> POST /media_publish. Pass exactly one of image_url or video_url. Pass instagram_user_id OR page_id. Requires instagram_content_publish.", "inputSchema": {"type": "object", "properties": {"instagram_user_id": {"type": "string"}, "page_id": {"type": "string"}, "token": {"type": "string"}, "image_url": {"type": "string", "x-image-url": True, "description": "Public image URL (use for photo Story) — optional if user attached image in chat."}, "video_url": {"type": "string", "x-video-url": True, "description": "Public MP4 URL (use for video Story) — optional if user attached video in chat."}, "image_base64": {"type": "string", "x-image-base64": True, "description": "Auto-filled from chat-attached image — do not emit yourself."}, "video_base64": {"type": "string", "x-attachment-base64": True, "x-attachment-mime": ["video/"], "description": "Auto-filled from chat-attached video — do not emit yourself."}}, "required": ["token"]}},
    {"name": "facebook_post_instagram_carousel", "description": "Publish a 2-10 item Instagram carousel. Per-item: POST /media (is_carousel_item=true, image_url|video_url) — videos polled to FINISHED. Then parent POST /media (media_type=CAROUSEL, children=ids, caption) -> POST /media_publish. Pass instagram_user_id OR page_id. Requires instagram_content_publish.", "inputSchema": {"type": "object", "properties": {"instagram_user_id": {"type": "string"}, "page_id": {"type": "string"}, "token": {"type": "string"}, "items": {"description": "JSON list of 2-10 entries; each entry: {image_url} or {video_url}. Mixed types allowed.", "type": "string"}, "caption": {"type": "string"}}, "required": ["token", "items"]}},
    # ---------- UPDATE (including pause/resume/update_budget) ----------
    {"name": "facebook_update_campaign", "description": "Update a campaign (name, status, objective, daily_budget, lifetime_budget, bid_strategy). POST /{campaign_id}. Requires ads_management. When skip_objective_when_adsets_exist is true (default), if the campaign already has ad sets and objective differs from current, objective is omitted so name/status/budget/bid_strategy still apply; response includes _objective_change_skipped and _fix_objective_instructions (create a new campaign via document sync for a new objective). Set skip_objective_when_adsets_exist=false to send objective anyway (Meta may return 1885073).", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}, "token": {"type": "string"}, "name": {"type": "string"}, "status": {"type": "string"}, "objective": {"type": "string", "description": "e.g. OUTCOME_ENGAGEMENT, OUTCOME_AWARENESS"}, "daily_budget": {"type": "integer", "description": "Campaign daily budget in cents for CBO"}, "lifetime_budget": {"type": "integer"}, "bid_strategy": {"type": "string", "description": "e.g. LOWEST_COST_WITHOUT_CAP"}, "skip_objective_when_adsets_exist": {"type": "boolean", "description": "Default true: skip objective in API payload when campaign has ad sets and objective would change"}}, "required": ["campaign_id", "token"]}},
    {"name": "facebook_pause_campaign", "description": "Set campaign status to PAUSED. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["campaign_id", "token"]}},
    {"name": "facebook_resume_campaign", "description": "Set campaign status to ACTIVE. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["campaign_id", "token"]}},
    {"name": "facebook_update_adset", "description": "Smart diff-based update: GET ad set; merge full targeting; POST only changes unless force_update=true. Response includes meta_diff (placements requested vs actual, before/detail) after refetch, changes_applied, skipped as {field,reason} objects. POST_ENGAGEMENT+ON_POST strips pixel fields into skipped. saved_audience_id / geo_cities_preset paths unchanged.", "inputSchema": {"type": "object", "properties": {"adset_id": {"type": "string"}, "token": {"type": "string"}, "saved_audience_id": {"type": "string", "description": "Saved audience Graph ID; applies its targeting to this ad set (not the same as custom_audiences)"}, "replace_placements": {"type": "boolean", "description": "If true with saved_audience_id, overwrite placements with saved audience only"}, "geo_cities_preset": {"type": "string", "description": "bol7_top_10_in = exact BOL7 doc top 10 Indian cities (replaces geo_locations.cities only)"}, "force_update": {"type": "boolean", "description": "Default false. If true, skip equality diff and POST all merged desired fields (pixel strip rules still apply)"}, "name": {"type": "string"}, "status": {"type": "string"}, "daily_budget": {"type": "integer"}, "lifetime_budget": {"type": "integer"}, "start_time": {"type": "string"}, "end_time": {"type": "string"}, "optimization_goal": {"type": "string"}, "bid_strategy": {"type": "string"}, "bid_amount": {"type": "integer", "description": "Bid cap / target cost in account minor units (e.g. INR paise)"}, "bid_constraints": {"type": "string", "description": "JSON object e.g. ROAS floor for VALUE optimization"}, "destination_type": {"type": "string"}, "billing_event": {"type": "string"}, "promoted_object": {"type": "string", "description": "JSON object"}, "targeting": {"type": "string", "description": "Optional base targeting JSON merged with structured fields"}, "advantage_audience": {"type": "integer", "description": "0 = set targeting_automation.advantage_audience to 0"}, "geo_countries": {"type": "string"}, "geo_country_groups": {"type": "string"}, "geo_regions": {"type": "string"}, "geo_cities": {"type": "string"}, "geo_custom_locations": {"type": "string"}, "geo_radius_km": {"type": "number"}, "geo_location_types": {"type": "string"}, "age_min": {"type": "integer"}, "age_max": {"type": "integer"}, "genders": {"type": "string"}, "flexible_spec": {"type": "string"}, "custom_audiences": {"type": "string"}, "excluded_geo_locations": {"type": "string"}, "excluded_custom_audiences": {"type": "string"}, "publisher_platforms": {"type": "array", "items": {"type": "string"}}, "device_platforms": {"type": "array", "items": {"type": "string"}}, "facebook_positions": {"type": "array", "items": {"type": "string"}}, "instagram_positions": {"type": "array", "items": {"type": "string"}}, "messenger_positions": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"messenger_home\"] with publisher_platforms including messenger"}, "audience_network_positions": {"type": "array", "items": {"type": "string"}, "description": "e.g. classic, instream_video, rewarded_video"}, "threads_positions": {"type": "array", "items": {"type": "string"}, "description": "e.g. threads_stream"}, "user_os": {"type": "array", "items": {"type": "string"}, "description": "e.g. [\"Android\"] or [\"iOS\"] — restrict to specific mobile OS. Auto-preserved from current targeting when device_platforms is set."}}, "required": ["adset_id", "token"]}},
    {"name": "facebook_pause_adset", "description": "Set ad set status to PAUSED. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"adset_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["adset_id", "token"]}},
    {"name": "facebook_resume_adset", "description": "Set ad set status to ACTIVE. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"adset_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["adset_id", "token"]}},
    {"name": "facebook_update_budget", "description": "Update ad set budget (daily_budget or lifetime_budget in cents). POST /{adset_id}. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"adset_id": {"type": "string"}, "token": {"type": "string"}, "daily_budget": {"type": "integer"}, "lifetime_budget": {"type": "integer"}}, "required": ["adset_id", "token"]}},
    {"name": "facebook_update_ad", "description": "Diff-based update: GET ad; POST only changes unless force_update=true. Returns meta_diff after refetch (tracking_specs / fields), changes_applied, or No changes needed. Optional tracking_specs JSON. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"ad_id": {"type": "string"}, "token": {"type": "string"}, "force_update": {"type": "boolean", "description": "If true, POST all provided fields even when equal to current"}, "name": {"type": "string"}, "status": {"type": "string"}, "adset_id": {"type": "string"}, "creative_id": {"type": "string", "description": "Switch ad to this creative (e.g. with Instagram)"}, "tracking_specs": {"type": "string", "description": "JSON array; Meta Ad tracking_specs (e.g. pixel + event types)"}}, "required": ["ad_id", "token"]}},
    {"name": "facebook_pause_ad", "description": "Set ad status to PAUSED. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"ad_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["ad_id", "token"]}},
    {"name": "facebook_resume_ad", "description": "Set ad status to ACTIVE. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"ad_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["ad_id", "token"]}},
    # ---------- DELETE ----------
    {"name": "facebook_delete_campaign", "description": "Delete a campaign. DELETE /{campaign_id}. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"campaign_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["campaign_id", "token"]}},
    {"name": "facebook_delete_adset", "description": "Delete an ad set. DELETE /{adset_id}. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"adset_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["adset_id", "token"]}},
    {"name": "facebook_delete_ad", "description": "Delete an ad. DELETE /{ad_id}. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"ad_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["ad_id", "token"]}},
    {"name": "facebook_delete_creative", "description": "Delete an ad creative. DELETE /{creative_id}. Requires ads_management.", "inputSchema": {"type": "object", "properties": {"creative_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["creative_id", "token"]}},
    # ---------- Conversions API ----------
    {
        "name": "facebook_send_conversion_event",
        "description": "Send a server-side event to Conversions API. POST /{pixel_id}/events. Params: pixel_id, event_name, event_time (unix), action_source (website|app|phone_call|chat|physical_store|system_generated|other). Optional: user_data (JSON), custom_data (JSON), event_id, event_source_url.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pixel_id": {"type": "string"},
                "token": {"type": "string"},
                "event_name": {"type": "string"},
                "event_time": {"type": "integer", "description": "Unix timestamp (seconds)"},
                "action_source": {"type": "string"},
                "user_data": {"type": "string", "description": "JSON object e.g. {\"em\":\"hash\",\"ph\":\"hash\"}"},
                "custom_data": {"type": "string", "description": "JSON object e.g. {\"value\":10,\"currency\":\"USD\"}"},
                "event_id": {"type": "string"},
                "event_source_url": {"type": "string"},
            },
            "required": ["pixel_id", "event_name", "event_time", "action_source", "token"],
        },
    },
    # ---------- Catalog API ----------
    {"name": "facebook_get_product_catalogs", "description": "List product catalogs. GET /me/product_catalogs or /{business_id}/owned_product_catalogs. Optional business_id. Requires catalog_management.", "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}, "business_id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["token"]}, "_discovery": [["catalog_id", ["data", "*", "id"]]]},
    {"name": "facebook_get_catalog", "description": "Get a single product catalog. GET /{catalog_id}. Requires catalog_management.", "inputSchema": {"type": "object", "properties": {"catalog_id": {"type": "string"}, "token": {"type": "string"}, "fields": {"type": "string"}}, "required": ["catalog_id", "token"]}},
    {"name": "facebook_get_catalog_products", "description": "List products in a catalog. GET /{catalog_id}/products. Optional filter (JSON). Requires catalog_management.", "inputSchema": {"type": "object", "properties": {"catalog_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}, "filter": {"type": "string"}, "fields": {"type": "string"}}, "required": ["catalog_id", "token"]}},
    # ---------- Business Management API ----------
    {"name": "facebook_get_business_owned_ad_accounts", "description": "List ad accounts owned by a business. GET /{business_id}/owned_ad_accounts. Requires business_management.", "inputSchema": {"type": "object", "properties": {"business_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["business_id", "token"]}},
    {"name": "facebook_get_business_owned_pages", "description": "List Facebook Pages owned by a business. GET /{business_id}/owned_pages. Requires business_management.", "inputSchema": {"type": "object", "properties": {"business_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["business_id", "token"]}},
    {"name": "facebook_get_business_client_ad_accounts", "description": "List client ad accounts (agency). GET /{business_id}/client_ad_accounts. Requires business_management.", "inputSchema": {"type": "object", "properties": {"business_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["business_id", "token"]}},
    # ---------- Meta Business Extension ----------
    {"name": "facebook_get_page_extension_info", "description": "Get Page with connected Instagram Business Account. GET /{page_id}. Fields: instagram_business_account. Requires pages_show_list.", "inputSchema": {"type": "object", "properties": {"page_id": {"type": "string"}, "token": {"type": "string"}, "fields": {"type": "string"}}, "required": ["page_id", "token"]}},
    # ---------- Commerce Platform ----------
    {"name": "facebook_get_catalog_product_sets", "description": "List product sets in a catalog. GET /{catalog_id}/product_sets. Commerce Platform. Requires catalog_management.", "inputSchema": {"type": "object", "properties": {"catalog_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["catalog_id", "token"]}},
    {"name": "facebook_get_catalog_product_feeds", "description": "List product feeds for a catalog. GET /{catalog_id}/product_feeds. Commerce Platform. Requires catalog_management.", "inputSchema": {"type": "object", "properties": {"catalog_id": {"type": "string"}, "token": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["catalog_id", "token"]}},
    # ---------- PRODUCTION TOOLS ----------
    {"name": "facebook_get_performance_summary", "description": "Aggregated performance dashboard: all campaigns with totals for CTR, CPC, spend, conversions, ROAS. Returns { campaigns: [...], totals: { spend, impressions, clicks, ctr, cpc, conversions, roas } }.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "date_preset": {"type": "string", "description": "last_7d, last_30d, this_month (default last_7d)"}, "date_start": {"type": "string"}, "date_end": {"type": "string"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_auto_optimize", "description": "Auto-optimize account: pause low CTR ads, scale high-performer budgets, reduce low-performer budgets. Supports dry_run=true to preview without applying. Returns list of actions taken.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "min_impressions": {"type": "integer", "description": "Min impressions before acting (default 1000)"}, "min_spend": {"type": "number", "description": "Min spend ($) before acting (default 5.0)"}, "dry_run": {"type": "boolean", "description": "Preview actions without applying (default false)"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_validate_creative", "description": "READ-ONLY probe: ask Meta whether an existing Instagram media (source_instagram_media_id) or Facebook Page post (object_story_id) can be used as an ad creative, using execution_options=[\"validate_only\"]. Meta runs full validation and COMMITS NOTHING (no creative/ad, nothing in Ads Manager). Returns verdict ACCEPTED/REJECTED plus Meta's raw response (error code/subcode/error_user_msg on rejection, e.g. 1815279). Pass adset_id to validate at ad level against an existing ad set (checks objective/optimization fit too; the ad set is not modified).", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "source_instagram_media_id": {"type": "string", "description": "IG media id to probe (reel/video/photo)."}, "object_story_id": {"type": "string", "description": "FB Page post id (page_id_post_id) to probe instead."}, "instagram_user_id": {"type": "string", "description": "Optional IG Business Account id for identity."}, "page_id": {"type": "string", "description": "Optional Page id (used with object_story_id)."}, "adset_id": {"type": "string", "description": "Optional existing ad set id for an ad-level validate_only probe."}, "name": {"type": "string"}, "token": {"type": "string"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_check_token", "description": "Check if a Facebook access token is valid, its expiry, scopes, and app_id. Uses /debug_token endpoint.", "inputSchema": {"type": "object", "properties": {"token": {"type": "string"}}, "required": ["token"]}},
    {"name": "facebook_exchange_long_lived_token", "description": "Exchange a short-lived token for a long-lived one (~60 days). Optionally saves to DB if user_id provided.", "inputSchema": {"type": "object", "properties": {"token": {"type": "string", "description": "Short-lived Facebook access token"}, "app_id": {"type": "string", "description": "Facebook App ID"}, "app_secret": {"type": "string", "description": "Facebook App Secret"}, "user_id": {"type": "string", "description": "Optional: save long-lived token to DB for this user"}}, "required": ["token", "app_id", "app_secret"]}},
    # ---------- ASYNC (Celery) TOOLS ----------
    {"name": "facebook_create_campaign_async", "description": "Async campaign creation via Celery. Returns task_id immediately. Use facebook_get_task_status to check progress. Uses production_flow.sync_document_campaign internally. Pass user_confirmed=true if document uses require_user_confirmation.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "document": {"description": "Campaign config JSON (same as facebook_sync_document_campaign)"}, "campaign_config": {"description": "Alias for document"}, "user_id": {"type": "string"}, "user_confirmed": {"type": "boolean"}}, "required": ["account_id", "token"]}},
    {"name": "facebook_get_task_status", "description": "Check status of an async task by task_id. Returns status (pending/running/success/failed), result, or error.", "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "token": {"type": "string"}}, "required": ["task_id", "token"]}},
    {"name": "facebook_sync_performance", "description": "Async: pull latest insights and store in DB for historical tracking. Returns task_id. Use facebook_get_task_status to check.", "inputSchema": {"type": "object", "properties": {"account_id": {"type": "string"}, "token": {"type": "string"}, "user_id": {"type": "string"}}, "required": ["account_id", "token"]}},
]


def is_facebook_marketing_mcp_tool_name(name) -> bool:
    """
    True only for tools served by this MCP (names start with facebook_).
    Bol7/Meta WhatsApp MCP tools (send_message, create_template, …) never match — they are a different server.
    """
    n = (name or "").strip() if name is not None else ""
    return bool(n) and n.startswith("facebook_")


def marketing_tools_from_registry() -> list:
    """Subset of TOOLS that belong to Facebook Marketing API MCP only."""
    return [t for t in TOOLS if isinstance(t, dict) and is_facebook_marketing_mcp_tool_name(t.get("name"))]


def tools_list_dicts_for_mcp(protocol_version: str = "") -> list:
    """Shape for JSON-RPC tools/list — never includes WhatsApp/Bol7 tools.

    Carries optional ``_discovery`` metadata so the loop_agent can register
    ID-yielding tools without hardcoding (No-hardcoding rule). Shape:
    ``_discovery: [["<arg_name>", ["data", "*", "<json_path>"]]]``.

    Every entry also carries the 2026-07-28 ``title`` + ``annotations``
    (harmless extras for legacy clients). On modern requests the private
    ``_discovery`` / ``_remember`` hints move under the spec-reserved ``_meta``
    extension point; legacy requests keep them top-level (byte-compatible with
    the current chat client). Order is deterministic (registry order).
    """
    from .tool_dispatch import _IDEMPOTENT_TOOLS
    modern = mcp_protocol.is_modern(protocol_version)
    out = []
    for t in marketing_tools_from_registry():
        entry = {
            "name": t["name"],
            "title": t.get("title") or mcp_protocol.tool_title(t["name"]),
            "description": t["description"],
            "inputSchema": t.get("inputSchema") or {"type": "object", "properties": {}},
            "annotations": mcp_protocol.tool_annotations(
                t["name"], idempotent_tools=_IDEMPOTENT_TOOLS, explicit=t.get("annotations")
            ),
        }
        hints = {k: t[k] for k in ("_discovery", "_remember") if t.get(k)}
        if hints:
            if modern:
                entry["_meta"] = hints
            else:
                entry.update(hints)
        out.append(entry)
    return out


def _content_result(text):
    """MCP tools/call result format."""
    return {"content": [{"type": "text", "text": text}]}


def _error_response(code, message, req_id=None):
    return JsonResponse(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}},
        status=200,
        json_dumps_params={"separators": (",", ":")},
    )


def _error_dict(code, message, req_id=None, data=None):
    """Same as _error_response but returns dict (for SSE/stdio). Optional JSON-RPC error.data for structured hints."""
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _get_token(request, args):
    """Resolve token from tools/call arguments only. Never read env or Django settings."""
    return (args.get("token") or "").strip()


def _get_token_from_args(args):
    """Resolve token from tools/call arguments only. Never read env or Django settings."""
    return (args.get("token") or "").strip()


def handle_mcp_request_from_body(body, request=None):
    """Handle JSON-RPC (initialize, tools/list, tools/call). Returns response dict. Used by HTTP, SSE, stdio."""
    from .auth_tokens import ensure_refreshed_user_token, resolve_dynamic_app_credentials, resolve_mcp_access_token
    from .tool_dispatch import run_tool
    if not isinstance(body, dict):
        return _error_dict(-32600, "JSON-RPC body must be an object.", None)
    method = body.get("method")
    params = body.get("params") or {}
    req_id = body.get("id")
    audit_req_id = generate_request_id()
    t0 = time.monotonic()

    _hdr_user_id = ""
    if request is not None:
        try:
            from app.tenant_http import mcp_resolve_user_id
            _hdr_user_id = mcp_resolve_user_id(request) or ""
        except Exception:
            _hdr_user_id = ""
    logger.info(
        "JSON-RPC request: method=%s id=%s req_id=%s user_id=%s params_keys=%s",
        method, req_id, audit_req_id, _hdr_user_id, list(params.keys()) if isinstance(params, dict) else [],
    )
    emit_fb_ads_mcp_audit("REQUEST", request=request, body=body, method=method, request_id=audit_req_id)

    # MCP 2026-07-28: stateless version negotiation (header + params._meta) and
    # routing-header checks. Legacy callers with no version are served as
    # 2025-03-26 so nothing existing breaks. See mcp_protocol.py.
    protocol_version, proto_err = mcp_protocol.negotiate(request, body)
    if proto_err:
        logger.warning("MCP protocol negotiation failed: %s", proto_err.get("message"))
        result = _error_dict(proto_err["code"], proto_err["message"], req_id, data=proto_err.get("data"))
        emit_fb_ads_mcp_audit("ERROR", response=result, method=method, request_id=audit_req_id)
        return result

    def _ok(result_obj):
        return mcp_protocol.stamp_result_meta(
            {"jsonrpc": "2.0", "id": req_id, "result": result_obj}, protocol_version
        )

    if method == "server/discover":
        result = _ok(mcp_protocol.discover_result())
        emit_fb_ads_mcp_audit("RESPONSE", response=result, method=method, request_id=audit_req_id)
        return result
    if method == "initialize":
        # Legacy (pre-2026-07-28) handshake — kept for older clients. Echo the
        # requested version when supported, else advertise the fallback.
        logger.info("MCP server initialized (legacy handshake)")
        result = _ok({
            "protocolVersion": mcp_protocol.initialize_version(params.get("protocolVersion") if isinstance(params, dict) else None),
            "capabilities": mcp_protocol.server_capabilities(),
            "serverInfo": mcp_protocol.server_info(),
        })
        emit_fb_ads_mcp_audit("RESPONSE", response=result, method=method, request_id=audit_req_id)
        return result
    if method == "notifications/initialized":
        return {"jsonrpc": "2.0", "id": req_id, "result": None}
    verified_user_id = None
    if method in ("tools/call", "tasks/get", "tasks/result", "task/status"):
        from .auth_tokens import require_facebook_identity
        try:
            verified_user_id = require_facebook_identity(request, body)
        except ValueError as exc:
            return _error_dict(-32001, str(exc), req_id)
    if method in ("tasks/get", "tasks/result"):
        # Official tasks extension (2026-07-28). tasks/get → Task descriptor
        # {taskId,status,pollInterval}; tasks/result → the finished tool result
        # (isError on failure). Legacy task/status below is unchanged.
        from .models import FacebookAdsMcpTaskResult
        task_id = (params.get("taskId") or params.get("task_id") or "").strip() if isinstance(params, dict) else ""
        if not task_id:
            return _error_dict(-32602, f"{method} requires params.taskId", req_id)
        try:
            tr = FacebookAdsMcpTaskResult.objects.get(task_id=task_id, user_id=verified_user_id)
        except FacebookAdsMcpTaskResult.DoesNotExist:
            return _error_dict(-32602, f"Task {task_id} not found", req_id, data={"taskId": task_id})
        desc = mcp_protocol.task_descriptor(task_id, tr.status, created_at=tr.created_at)
        if method == "tasks/get":
            if desc["status"] == "failed":
                desc["statusMessage"] = tr.error_message or f"Task ended with status={tr.status}"
            return _ok(desc)
        if desc["status"] == "working":
            return _error_dict(-32602, f"Task {task_id} is still working; poll tasks/get first", req_id, data=desc)
        if desc["status"] == "completed":
            payload = {}
            if tr.result_json:
                try:
                    payload = json.loads(tr.result_json)
                except json.JSONDecodeError:
                    payload = {"raw": tr.result_json}
            return _ok(mcp_protocol.tool_call_result(payload, protocol_version, legacy_text=json.dumps(payload, indent=2, default=str)))
        failure = {"error": {"message": tr.error_message or f"Task ended with status={tr.status}", "task": desc}}
        return _ok(mcp_protocol.tool_call_result(failure, protocol_version, legacy_text=json.dumps(failure, indent=2, default=str)))
    if method == "task/status":
        # Async-job polling protocol used by loop_agent.mcp_client._poll_async_tool.
        # Returns {done, result, poll_after_ms, error} so the client can poll until terminal.
        from .models import FacebookAdsMcpTaskResult
        task_id = (params.get("task_id") or "").strip() if isinstance(params, dict) else ""
        if not task_id:
            return _error_dict(-32602, "task/status requires task_id", req_id)
        try:
            tr = FacebookAdsMcpTaskResult.objects.get(task_id=task_id, user_id=verified_user_id)
        except FacebookAdsMcpTaskResult.DoesNotExist:
            return {"jsonrpc": "2.0", "id": req_id, "result": {"done": True, "error": f"Task {task_id} not found"}}
        if tr.status in ("pending", "running", "started"):
            return {"jsonrpc": "2.0", "id": req_id, "result": {"done": False, "poll_after_ms": 2000}}
        if tr.status == "success":
            payload = None
            if tr.result_json:
                try:
                    payload = json.loads(tr.result_json)
                except json.JSONDecodeError:
                    payload = {"raw": tr.result_json}
            return {"jsonrpc": "2.0", "id": req_id, "result": {"done": True, "result": payload or {}}}
        # failed / unknown terminal state
        return {"jsonrpc": "2.0", "id": req_id, "result": {"done": True, "error": tr.error_message or f"Task ended with status={tr.status}"}}
    if method == "tools/list":
        tools = tools_list_dicts_for_mcp(protocol_version)
        logger.info("tools/list -> returning %d Marketing API tools (WhatsApp/Bol7 MCP is separate)", len(tools))
        result = _ok(mcp_protocol.tools_list_result(tools, protocol_version))
        emit_fb_ads_mcp_audit("RESPONSE", response=result, method=method, request_id=audit_req_id)
        return result
    if method == "tools/call":
        name = (params.get("name") or "").strip()
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error_dict(-32602, "arguments must be an object", req_id)
        args = dict(args, user_id=verified_user_id)
        if not name:
            _m = mcp_protocol.tool_input_error_result("tools/call requires params.name", protocol_version)
            return _ok(_m) if _m else _error_dict(-32602, "tools/call requires params.name", req_id)
        if not is_facebook_marketing_mcp_tool_name(name):
            _msg = (
                "This MCP server is Facebook Ads (Marketing API) only. "
                f"Tool {name!r} is not available here. "
                "WhatsApp/Bol7 tools (send_message, create_template, …) use a different MCP: "
                "/api/mcp/bol7/ or /api/mcp/meta/ (not /api/mcp/facebook-ads/)."
            )
            _m = mcp_protocol.tool_input_error_result(_msg, protocol_version)
            return _ok(_m) if _m else _error_dict(-32602, _msg, req_id)
        try:
            token, tmeta = resolve_mcp_access_token(args, request=request)
        except ValueError as e:
            logger.warning(
                "tools/call %s auth resolve failed: %s (arg_user_id=%s header_user_id=%s)",
                name, e, (args.get("user_id") or ""), _hdr_user_id,
            )
            _modern_err = mcp_protocol.tool_input_error_result(str(e), protocol_version)
            return _ok(_modern_err) if _modern_err else _error_dict(-32602, str(e), req_id)
        logger.info(
            "tools/call %s auth resolved source=%s user_id=%s",
            name, tmeta.get("auth_source", ""), tmeta.get("user_id", ""),
        )
        user_id = verified_user_id
        try:
            data = run_tool(name, args, token)
            err = data.get("error") if isinstance(data, dict) else None
            if (
                isinstance(err, dict)
                and int(err.get("code") or 0) == 190
                and not tmeta.get("token_refreshed")
            ):
                pid = (args.get("credential_user_id") or "").strip() or None
                if not pid and tmeta.get("auth_source") == "database.FacebookAdsCredential":
                    pid = (tmeta.get("user_id") or "").strip() or None
                aid, asec = resolve_dynamic_app_credentials(args)
                token2, tmeta2 = ensure_refreshed_user_token(
                    token, persist_user_id=pid, force=True, app_id=aid, app_secret=asec
                )
                if tmeta2.get("token_refreshed") and token2 != token:
                    data = run_tool(name, args, token2)
            duration_ms = int((time.monotonic() - t0) * 1000)
            has_error = isinstance(data, dict) and "error" in data
            status = "error" if has_error else "success"
            if has_error:
                logger.warning("tools/call %s -> API error: %s", name, data["error"].get("message", ""))
            else:
                logger.info("tools/call %s -> success", name)
            text = json.dumps(data, indent=2, default=str)
            result = _ok(mcp_protocol.tool_call_result(data, protocol_version, legacy_text=text))
            emit_fb_ads_mcp_audit("RESPONSE", response=result, method=method, tool_name=name, request_id=audit_req_id, duration_ms=duration_ms)
            save_audit_to_db(
                request_id=audit_req_id, user_id=user_id, method=method, tool_name=name,
                request_payload=safe_json_dumps(redact_for_log(args)),
                response_summary=text[:2000], status=status, duration_ms=duration_ms,
            )
            return result
        except ValueError as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("tools/call %s -> ValueError: %s", name, e)
            msg = str(e)
            err_data = None
            if name in ("facebook_preview_document_campaign", "facebook_sync_document_campaign"):
                from . import production_flow as _pf

                sug = _pf.suggest_relaxed_fixes_for_strict_error(msg)
                if sug:
                    err_data = {
                        "relaxed_fix_suggestions": sug,
                        "strict_validator_version": _pf.STRICT_VALIDATOR_VERSION,
                    }
            # 2026-07-28: bad inputs are a tool result with isError (the model
            # can read and fix them); legacy keeps the JSON-RPC -32602 error.
            _modern_err = mcp_protocol.tool_input_error_result(msg, protocol_version, data=err_data)
            result = _ok(_modern_err) if _modern_err else _error_dict(-32602, msg, req_id, data=err_data)
            emit_fb_ads_mcp_audit("ERROR", response=result, method=method, tool_name=name, request_id=audit_req_id, duration_ms=duration_ms, extra=f"ValueError: {e}")
            save_audit_to_db(
                request_id=audit_req_id, user_id=user_id, method=method, tool_name=name,
                request_payload=safe_json_dumps(redact_for_log(args)),
                response_summary=str(e)[:2000], status="error", duration_ms=duration_ms,
            )
            return result
        except Exception as e:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.exception("Facebook Ads MCP tools/call error: %s", e)
            result = _error_dict(-32603, f"Tool execution failed: {e}", req_id)
            emit_fb_ads_mcp_audit("ERROR", response=result, method=method, tool_name=name, request_id=audit_req_id, duration_ms=duration_ms, extra=f"Exception: {e}")
            save_audit_to_db(
                request_id=audit_req_id, user_id=user_id, method=method, tool_name=name,
                request_payload=safe_json_dumps(redact_for_log(args)),
                response_summary=str(e)[:2000], status="error", duration_ms=duration_ms,
            )
            return result
    return _error_dict(-32601, f"Method not found: {method}", req_id)


def get_mcp_tools_list_dict():
    """Used by app.meta_views for GET discovery — Marketing API tools only (never WhatsApp/Bol7)."""
    return {"tools": tools_list_dicts_for_mcp()}


@csrf_exempt
@require_http_methods(["GET", "POST", "DELETE"])
def mcp_endpoint(request):
    """
    Streamable HTTP (MCP 2026-07-28): ONE URL, POST JSON-RPC 2.0 only.
    GET / DELETE answer 405 with a pointer to /info/ (the old GET help blob
    lives there); ``Origin`` is validated (403). Legacy ``initialize`` and the
    private ``task/status`` are still answered for pre-upgrade clients.
    """
    if not mcp_protocol.origin_allowed(request):
        resp = JsonResponse(
            {"error": "Origin not allowed", "hint": "Set MCP_ALLOWED_ORIGINS (comma-separated scheme://host) on the MCP service."},
            status=403, json_dumps_params={"separators": (",", ":")},
        )
        return _stamp_marketing_mcp_response(resp)
    if request.method in ("GET", "DELETE"):
        base = request.build_absolute_uri("/").rstrip("/")
        resp = JsonResponse({
            "error": f"{request.method} is not supported on the MCP endpoint (POST JSON-RPC only).",
            "info": f"{base}/api/mcp/facebook-ads/info/",
            "preferredProtocolVersion": mcp_protocol.LATEST_PROTOCOL_VERSION,
        }, status=405, json_dumps_params={"separators": (",", ":")})
        resp["Allow"] = "POST"
        return _stamp_marketing_mcp_response(resp)
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError as e:
        return _error_response(-32700, f"Parse error: {e}", None)

    result = handle_mcp_request_from_body(body, request=request)
    err = result.get("error") if isinstance(result, dict) else None
    status = mcp_protocol.http_status_for_error(err.get("code")) if isinstance(err, dict) else 200
    if isinstance(err, dict) and err.get("code") == -32001:
        status = 401
    resp = JsonResponse(result, status=status, json_dumps_params={"separators": (",", ":")})
    negotiated, _ = mcp_protocol.negotiate(request, body)
    if negotiated:
        resp["MCP-Protocol-Version"] = negotiated
    return _stamp_marketing_mcp_response(resp)


# The legacy two-URL HTTP+SSE transport (/sse/ + /messages/) was removed in the
# MCP 2026-07-28 upgrade (deprecated since 2025-03-26, nothing in our stack used
# it). Streamable HTTP = ONE POST endpoint; info lives at /info/.


@require_http_methods(["GET"])
def mcp_info_endpoint(request):
    """GET /api/mcp/facebook-ads/info/ — server info and advertised transports."""
    base = request.build_absolute_uri("/").rstrip("/")
    return JsonResponse({
        "name": "Facebook Ads MCP",
        "provider": "facebook_ads",
        "version": mcp_protocol.SERVER_VERSION,
        "protocolVersions": list(mcp_protocol.SUPPORTED_PROTOCOL_VERSIONS),
        "preferredProtocolVersion": mcp_protocol.LATEST_PROTOCOL_VERSION,
        "description": "Facebook Ads (Marketing API) only — facebook_* tools. WhatsApp MCP is separate (/api/mcp/bol7 or /meta).",
        "tools": [t["name"] for t in marketing_tools_from_registry()],
        "extensions": [mcp_protocol.TASKS_EXTENSION],
        "transports": {
            "streamable_http": {
                "url": f"{base}/api/mcp/facebook-ads/",
                "method": "POST",
                "description": "Single URL, POST JSON-RPC only (server/discover, tools/list, tools/call, tasks/get, tasks/result; legacy initialize + task/status still answered). GET/DELETE → 405.",
                "headers": ["MCP-Protocol-Version", "Mcp-Method", "Mcp-Name"],
            },
            "stdio": {
                "command": "python -m facebook_ads_mcp.stdio_server",
                "description": "Run as subprocess; pass token in tool arguments (args.token).",
            },
        },
    }, json_dumps_params={"separators": (",", ":")})
