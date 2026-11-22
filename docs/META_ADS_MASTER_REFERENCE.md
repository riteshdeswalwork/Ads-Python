# Meta Ads — Master Reference (UI-Verified)

**Compiled:** 2026-05-23
**For:** Aditya / Facebook Ads MCP Connector
**Status:** Production-grade — UI-verified + SDK-source-of-truth
**Meta API version:** Marketing API v25.0 (released Feb 18, 2026; mandatory May 19, 2026)

---

## Table of Contents

1. [3-Level Ad Structure](#1-3-level-ad-structure)
2. [Valid Combo Matrix — UI Verified](#2-valid-combo-matrix--ui-verified)
3. [Forbidden Combos](#3-forbidden-combos--auto-fix-actions)
4. [Intent-Preservation Rules](#4-intent-preservation-rules)
5. [Billing Event Rules](#5-billing-event-rules)
6. [Creative Mode Rules](#6-creative-mode-rules)
7. [Special Ad Categories](#7-special-ad-categories-compliance)
8. [Pixel Data Gating](#8-pixel-data-gating-runtime)
9. [Placement Rules](#9-placement-rules-v25)
10. [Budget + Bid Rules](#10-budget--bid-rules)
11. [Schedule Rules](#11-schedule-rules)
12. [Targeting Limits](#12-targeting-hard-limits)
13. [Error Subcodes](#13-error-subcode-to-fix-mapping)
14. [v25 Breaking Changes](#14-v25-breaking-changes-may-19-2026)
15. [Auto-Fix Architecture](#15-auto-fix-architecture-migration-0055)
16. [SDK Authoritative Enums](#16-sdk-authoritative-enums)
17. [Pending UI Verification](#17-pending-ui-verification)

---

## 1. 3-Level Ad Structure

```
📊 CAMPAIGN  (top — "kya achieve karna hai?")
   │
   └── 📋 AD SET  (middle — "kisko dikhana hai aur kaise?")
       │
       └── 🎨 AD  (bottom — "actual creative kya hai?")
```

| Level | API Object | Key Decisions |
|---|---|---|
| Campaign | `/campaigns` | objective, special_ad_categories, buying_type, bid_strategy |
| Ad Set | `/adsets` | optimization_goal, billing_event, destination_type, targeting, schedule, budget |
| Ad | `/ads` | creative_id, status |
| Creative | `/adcreatives` | mode, page_id, image/video, message, CTA |

---

## 2. Valid Combo Matrix — UI Verified

### 2.1 OUTCOME_AWARENESS ✅ Fully Verified

**Total UI options: 5** (4 unique API enums)

| UI Label | API `optimization_goal` | Destination | Creative Needed |
|---|---|---|---|
| Maximise reach of ads ⭐ | `REACH` | UNDEFINED | Any |
| Maximise number of impressions | `IMPRESSIONS` | UNDEFINED | Any |
| Maximise ad recall lift | `AD_RECALL_LIFT` | UNDEFINED | Any |
| Maximise ThruPlay views | `THRUPLAY` | UNDEFINED, ON_VIDEO | **Video required** |
| Maximise 2-second continuous video plays | `THRUPLAY` (billing=IMPRESSIONS) | UNDEFINED | **Video required** |

### 2.2 OUTCOME_TRAFFIC ⚠️ Partially Verified (Website destination only)

**Total UI options at Website: 5**

| UI Label | API `optimization_goal` | Destination |
|---|---|---|
| Maximise number of landing page views ⭐ | `LANDING_PAGE_VIEWS` | WEBSITE |
| Maximise number of link clicks | `LINK_CLICKS` | WEBSITE |
| Maximise daily unique reach | `REACH` | WEBSITE |
| Maximise number of conversations | `CONVERSATIONS` | WEBSITE (cross-conv) |
| Maximise number of impressions | `IMPRESSIONS` | WEBSITE |

### 2.3 OUTCOME_ENGAGEMENT ⚠️ Partially Verified

**Engagement type sub-filter UI mechanic**

When `Conversion location = "On your ad"` + `Engagement type = "Video views"`:

| UI Label | API `optimization_goal` | Destination |
|---|---|---|
| Maximise ThruPlay views ⭐ | `THRUPLAY` | **ON_VIDEO required** |
| Maximise 2-second continuous video plays | `THRUPLAY` (billing=IMPRESSIONS) | UNDEFINED (⚠️ unverified — see note) |

> ⚠️ **Live-verified 2026-08-05:** under `OUTCOME_ENGAGEMENT`, `THRUPLAY` with an
> ABSENT destination_type (= UNDEFINED) is HARD-REJECTED by Meta — code 100,
> subcode 2490408 "Performance goal isn't available", blame=`optimization_goal`
> (misleading; the missing destination is the real cause). `ON_VIDEO` must be
> sent explicitly. This rule is NOT in Meta's official docs (the official
> destination_type page only maps objectives→destinations; no official
> objective+goal+destination matrix exists). The 2-second-plays UNDEFINED row
> above predates this finding and has no live verification — treat with caution.
> The connector auto-injects ON_VIDEO when absent (`production_flow.py`
> matrix-driven injection + `required_destination_for()` in `meta_validation.py`).

### 2.4 OUTCOME_LEADS ⚠️ Partially Verified (Website destination)

**Total UI options at Website: 5-6** (some pixel-gated)

| UI Label | API `optimization_goal` | Destination | Notes |
|---|---|---|---|
| Maximise number of leads | `LEAD_GENERATION` | WEBSITE | May grey out if no pixel LEAD events 28d |
| Maximise number of conversion leads | `OFFSITE_CONVERSIONS` (custom_event_type=LEAD) | WEBSITE | ⚠️ Pixel-gated |
| Maximise number of landing page views | `LANDING_PAGE_VIEWS` | WEBSITE | — |
| Maximise number of link clicks | `LINK_CLICKS` | WEBSITE | — |
| Maximise daily unique reach | `REACH` | WEBSITE | — |
| Maximise number of impressions | `IMPRESSIONS` | WEBSITE | — |

### 2.5 OUTCOME_SALES ⚠️ Partially Verified (Website destination)

**Total UI options at Website: 6**

| UI Label | API `optimization_goal` | Destination | Notes |
|---|---|---|---|
| Maximise number of conversions ⭐ | `OFFSITE_CONVERSIONS` | WEBSITE | Meta-Recommended (pixel-gated) |
| Maximise value of conversions | `VALUE` | WEBSITE | ROAS (needs value+currency) |
| Maximise number of landing page views | `LANDING_PAGE_VIEWS` | WEBSITE | Warming |
| Maximise number of link clicks | `LINK_CLICKS` | WEBSITE | Top-funnel |
| Maximise daily unique reach | `REACH` | WEBSITE | Volume |
| Maximise number of impressions | `IMPRESSIONS` | WEBSITE | Volume |

### 2.6 OUTCOME_APP_PROMOTION ❌ Not Yet Verified

Requires registered Meta SDK app for UI to surface dropdown.

---

## 3. Forbidden Combos + Auto-Fix Actions

| Forbidden Combo | Auto-Fix Action | Meta Error |
|---|---|---|
| `OUTCOME_ENGAGEMENT` + `THRUPLAY` (no ON_VIDEO dest) | **Inject `destination_type=ON_VIDEO`, keep objective** (2026-08-05; the combo is VALID with ON_VIDEO) | 2490408 |
| `OUTCOME_TRAFFIC` + `THRUPLAY` | Change objective → `OUTCOME_AWARENESS` | Rejected |
| `OUTCOME_LEADS` + `THRUPLAY` | Change objective → `OUTCOME_AWARENESS` | Rejected |
| `OUTCOME_AWARENESS` + `OFFSITE_CONVERSIONS` | Change objective → `OUTCOME_SALES` | Awareness can't pixel-track |
| `OUTCOME_AWARENESS` + `VALUE` | Change objective → `OUTCOME_SALES` | Value is sales-only |
| `OUTCOME_TRAFFIC` + `OFFSITE_CONVERSIONS` | Change objective → `OUTCOME_SALES` | Use sales for conversions |
| `OUTCOME_TRAFFIC` + `VALUE` | Change objective → `OUTCOME_SALES` | Value is sales-only |
| `OUTCOME_TRAFFIC` + `LEAD_GENERATION` | Change objective → `OUTCOME_LEADS` | Lead gen needs leads obj |
| `OUTCOME_ENGAGEMENT` + `OFFSITE_CONVERSIONS` | Change objective → `OUTCOME_SALES` | Engagement ≠ conversion |
| `OUTCOME_ENGAGEMENT` + `VALUE` | Change objective → `OUTCOME_SALES` | Value is sales-only |
| `OUTCOME_ENGAGEMENT` + `APP_INSTALLS` | Change objective → `OUTCOME_APP_PROMOTION` | App goal needs app obj |
| `OUTCOME_LEADS` + `VALUE` | Change objective → `OUTCOME_SALES` | Value is sales-only |
| `OUTCOME_LEADS` + `REACH` | Use `LEAD_GENERATION` goal | Leads needs lead goal |
| `OUTCOME_SALES` + `PAGE_LIKES` | Change objective → `OUTCOME_ENGAGEMENT` | Page likes is engagement |
| `OUTCOME_SALES` + `POST_ENGAGEMENT` | Change objective → `OUTCOME_ENGAGEMENT` | Engagement obj for engagement |
| `OUTCOME_APP_PROMOTION` + `REACH` | Change objective → `OUTCOME_AWARENESS` | Awareness for reach |
| `OUTCOME_APP_PROMOTION` + `LEAD_GENERATION` | Change objective → `OUTCOME_LEADS` | Lead gen ≠ app install |
| `OUTCOME_APP_PROMOTION` + `POST_ENGAGEMENT` | Change objective → `OUTCOME_ENGAGEMENT` | Wrong objective family |
| Any objective + `destination_type=ON_AD` (non-leads) | Strip destination_type | 1815715 |
| `OUTCOME_AWARENESS` + `destination_type=ON_AD` | Strip destination_type | ON_AD = leads-only |
| `OUTCOME_TRAFFIC` + `destination_type=WEBSITE` + EXISTING_POST creative | Strip destination_type | Existing post handles its own link |
| `ISSUES_ELECTIONS_POLITICS` + EU country | 🚨 HARD BLOCK with user error | TTPA ban Oct 2025 |

---

## 4. Intent-Preservation Rules

When user gives an invalid combo, **preserve their CLEAREST intent** by changing the OBJECTIVE (not the goal):

```python
GOAL_TO_REQUIRED_OBJ = {
    # Awareness intents
    "THRUPLAY":                     "OUTCOME_AWARENESS",     # video views
    "AD_RECALL_LIFT":               "OUTCOME_AWARENESS",     # memory

    # Engagement intents
    "POST_ENGAGEMENT":              "OUTCOME_ENGAGEMENT",
    "PAGE_LIKES":                   "OUTCOME_ENGAGEMENT",
    "EVENT_RESPONSES":              "OUTCOME_ENGAGEMENT",
    "PROFILE_VISIT":                "OUTCOME_ENGAGEMENT",
    "PROFILE_AND_PAGE_ENGAGEMENT":  "OUTCOME_ENGAGEMENT",

    # Traffic intents
    "LANDING_PAGE_VIEWS":           "OUTCOME_TRAFFIC",
    "VISIT_INSTAGRAM_PROFILE":      "OUTCOME_TRAFFIC",

    # Leads intents
    "LEAD_GENERATION":              "OUTCOME_LEADS",
    "QUALITY_LEAD":                 "OUTCOME_LEADS",
    "SUBSCRIBERS":                  "OUTCOME_LEADS",
    "REMINDERS_SET":                "OUTCOME_LEADS",
    "QUALITY_CALL":                 "OUTCOME_LEADS",
    "MEANINGFUL_CALL_ATTEMPT":      "OUTCOME_LEADS",

    # Sales intents
    "OFFSITE_CONVERSIONS":          "OUTCOME_SALES",
    "VALUE":                        "OUTCOME_SALES",
    "ADVERTISER_SILOED_VALUE":      "OUTCOME_SALES",
    "MESSAGING_PURCHASE_CONVERSION":"OUTCOME_SALES",
    "MESSAGING_APPOINTMENT_CONVERSION":"OUTCOME_SALES",

    # App intents
    "APP_INSTALLS":                 "OUTCOME_APP_PROMOTION",
    "APP_INSTALLS_AND_OFFSITE_CONVERSIONS":"OUTCOME_APP_PROMOTION",
    "IN_APP_VALUE":                 "OUTCOME_APP_PROMOTION",

    # Universal (keep current objective)
    "LINK_CLICKS":                  None,
    "CONVERSATIONS":                None,  # valid for ENGAGEMENT/TRAFFIC/LEADS/SALES
    "REACH":                        None,
    "IMPRESSIONS":                  None,
}
```

**Rule:**
- If goal maps to specific objective → **force objective change** (preserve user intent)
- If goal is universal → **keep objective**, pick default goal from `OBJECTIVE_ADSET_DEFAULTS`

---

## 5. Billing Event Rules

**Universal safe default: `IMPRESSIONS`** for almost every goal.

| optimization_goal | Acceptable billing_event | Recommended |
|---|---|---|
| `REACH` / `IMPRESSIONS` / `AD_RECALL_LIFT` | IMPRESSIONS | IMPRESSIONS |
| `LINK_CLICKS` | IMPRESSIONS or LINK_CLICKS | IMPRESSIONS |
| `LANDING_PAGE_VIEWS` | IMPRESSIONS | IMPRESSIONS |
| `POST_ENGAGEMENT` | IMPRESSIONS or POST_ENGAGEMENT | IMPRESSIONS |
| `PAGE_LIKES` | IMPRESSIONS or PAGE_LIKES | IMPRESSIONS |
| `THRUPLAY` | IMPRESSIONS or THRUPLAY | IMPRESSIONS |
| `OFFSITE_CONVERSIONS` / `VALUE` | IMPRESSIONS | IMPRESSIONS |
| `APP_INSTALLS` | IMPRESSIONS or APP_INSTALLS | IMPRESSIONS |
| `LEAD_GENERATION` / `QUALITY_LEAD` | IMPRESSIONS | IMPRESSIONS |
| `CONVERSATIONS` | IMPRESSIONS | IMPRESSIONS |
| `QUALITY_CALL` / `MEANINGFUL_CALL_ATTEMPT` | IMPRESSIONS | IMPRESSIONS |
| `EVENT_RESPONSES` | IMPRESSIONS | IMPRESSIONS |

---

## 6. Creative Mode Rules

| Creative Mode | Compatible Objectives | Required Fields |
|---|---|---|
| `LINK_AD` | All | `image_hash` OR `video_id` OR `asset_feed_spec`, `link_url`, `page_id` |
| `EXISTING_POST` | All except APP_PROMOTION | `object_story_id` OR `source_instagram_media_id`, `page_id` |
| `CAROUSEL` | All | `child_attachments` (2-10), `page_id` |
| `COLLECTION` | TRAFFIC, SALES | `instant_experience_id` + cover image/video, `page_id` |
| `DCO` | All | `asset_feed_spec`, `page_id` |
| `DPA` | SALES, TRAFFIC | `template_url_spec`, `product_set_id` in promoted_object |
| `LEAD_FORM` | LEADS only | `lead_form_id`, `page_id`, `destination_type=ON_AD` |
| `WHATSAPP` | TRAFFIC, ENGAGEMENT, LEADS, SALES | `page_id` (WhatsApp-linked), `destination_type=WHATSAPP` |
| `INSTAGRAM_EXISTING` | All except APP | `source_instagram_media_id`, `page_id` |
| `BRANDED_CONTENT` | All | `branded_content_sponsor_page_id`, `object_story_id` of creator post |

### Critical creative rules:
- `EXISTING_POST` + `destination_type=WEBSITE` = ❌ INCOMPATIBLE (Meta rejects)
- `LEAD_FORM` requires `destination_type=ON_AD` always
- Politics campaign → creative MUST have `authorization_category=POLITICAL`

---

## 7. Special Ad Categories Compliance

### 7.1 Valid Values (7)

```
NONE
CREDIT
EMPLOYMENT
FINANCIAL_PRODUCTS_SERVICES   (newer 2024+)
HOUSING
ISSUES_ELECTIONS_POLITICS
ONLINE_GAMBLING_AND_GAMING    (newer)
```

### 7.2 Override Behavior

When `campaign.special_ad_categories` includes any of these:

| Category | Auto-Force | Auto-Strip |
|---|---|---|
| `HOUSING` | age 18-65, all genders | zips, custom_audiences, lookalike_audiences, multicultural_affinity, income, family_statuses; geo radius < 15mi (US) / 25km (CA) |
| `EMPLOYMENT` | Same as HOUSING | Same |
| `CREDIT` | Same as HOUSING | Same |
| `FINANCIAL_PRODUCTS_SERVICES` | Country-specific | Variable |
| `ISSUES_ELECTIONS_POLITICS` | creative.authorization_category=POLITICAL | 🚨 EU countries (TTPA ban Oct 2025) |
| `ONLINE_GAMBLING_AND_GAMING` | License upload required | Country-restricted |

### 7.3 Authorization Category Cascade

```python
SPECIAL_CATEGORY_TO_AUTH = {
    "ISSUES_ELECTIONS_POLITICS": "POLITICAL",
    # AI-edited political content:
    # Use "POLITICAL_WITH_DIGITALLY_CREATED_MEDIA" instead
}
```

### 7.4 India ECI Rules

- All political ads require ECI MCC approval (Media Certification & Monitoring Committee)
- 48-hour silent period before polling — Meta auto-blocks new ads
- "Paid for by" must match ECI-registered political entity
- Suvidha portal cross-reference

### 7.5 EU TTPA — 🚨 Total Ban

- Effective Oct 6, 2025: NO SIEP ads in any EU member state
- Penalty: up to 6% of annual turnover
- API rejection at sync time
- **Connector must preflight block** before Meta API call

---

## 8. Pixel Data Gating (Runtime)

These goals work but get **degraded delivery / UI-disabled** if tracking history insufficient:

| Goal | Requires | Fallback Action |
|---|---|---|
| `OFFSITE_CONVERSIONS` (LEADS) | Pixel with LEAD events in past 28 days | Warn → suggest `LEAD_GENERATION` + ON_AD |
| `OFFSITE_CONVERSIONS` (SALES) | Pixel with PURCHASE events in past 28 days | Warn → suggest `LANDING_PAGE_VIEWS` |
| `VALUE` (SALES) | PURCHASE events with `value` + `currency` parameters | Warn → suggest `OFFSITE_CONVERSIONS` |
| `QUALITY_LEAD` | CRM integration (offline conversions) | Warn → suggest `LEAD_GENERATION` |
| `IN_APP_VALUE` (APP) | App SDK with `fb_mobile_purchase` events | Warn → suggest `APP_INSTALLS` |

**UI evidence (verified 2026-05-22):** LEADS dropdown shows greyed-out `OFFSITE_CONVERSIONS` with tooltip:
> "This performance goal is not available because none of your pixels have had lead event activity in the past 28 days."

---

## 9. Placement Rules (v25)

### 9.1 Publisher Platforms (7)

```
AUDIENCE_NETWORK
FACEBOOK
INSTAGRAM
MESSENGER
OCULUS         (VR ads)
THREADS        (added 2024)
WHATSAPP       (top-level platform)
```

### 9.2 Facebook Positions (22 canonical UPPERCASE)

```
BIZ_DISCO_FEED, FACEBOOK_CONTEXTUAL_BUNDLE,
FB_REELS, FB_REELS_OVERLAY,
FEED, GROUPS, GROUP_MALL, GROUP_TAB,
INSTANT_ARTICLE, INSTREAM_REEL, INSTREAM_VIDEO,
JOBS_BROWSER, MARKETPLACE, NOTIFICATION,
PROFILE_FEED, PROFILE_REELS, RHC, SEARCH,
STORY, STORY_STICKER, SUGGESTED_VIDEO, VIDEO_FEEDS
```

### 9.3 Instagram Positions (14)

```
EFFECT_TRAY, EXPLORE, EXPLORE_HOME,
IGTV, IG_SEARCH, LEAD_GEN_MULTI_SUBMIT,
PROFILE_FEED, PROFILE_REELS, REELS,
REELS_INSTREAM, REELS_OVERLAY, SHOP,
STORY, STREAM
```

### 9.4 Placement Token Aliases (v25)

| User input | Canonical |
|---|---|
| `reels` (in facebook_positions) | `facebook_reels` |
| `reels` (in instagram_positions) | `reels` (unchanged) |
| `story` (in facebook_positions) | `story` |
| `stories` | `story` |
| `right_hand_column` | `right_hand_column` |
| `messenger_home` | **REMOVED Nov 11, 2025** |
| `video_feed` | `video_feeds` |

### 9.5 Creative × Placement Compatibility

| Creative Format | Compatible Placements |
|---|---|
| **9:16 vertical video** (1080×1920) | Stories (FB/IG), Reels (FB/IG), Messenger Story |
| **1:1 square image/video** (1080×1080) | Feed (FB/IG), Marketplace, Video Feeds, Audience Network |
| **1.91:1 landscape** (1200×628) | RHC, Feed fallback, Instant Article |
| **Carousel (2-10 cards)** | Feed, Stories (partial), IG Reels (1-card), Marketplace |
| **Collection** | FB/IG Feed (mobile only — requires catalog) |
| **Instream Video** (16:9 horizontal 15s-2min) | FB Instream, Audience Network Instream |

---

## 10. Budget + Bid Rules

### 10.1 Budget Format

| Field | Format | Constraint |
|---|---|---|
| `daily_budget` | **Minor units (paise/cents)** | INR ~₹40/day min; USD ~$1/day min |
| `lifetime_budget` | Minor units | Same scale |
| `daily × days` | Must be ≥ lifetime_budget | Else rejection |
| `bid_amount` | Minor units | Required for COST_CAP, LOWEST_COST_WITH_BID_CAP |

### 10.2 Currency Examples

- ₹100/day = `daily_budget: 10000` (INR paise)
- $50/day = `daily_budget: 5000` (USD cents)
- €25/day = `daily_budget: 2500` (EUR cents)

### 10.3 Bid Strategies (4)

| Strategy | Use Case | Requires |
|---|---|---|
| `LOWEST_COST_WITHOUT_CAP` | Default (95% cases) | Nothing |
| `LOWEST_COST_WITH_BID_CAP` | Max bid ceiling | `bid_amount` |
| `COST_CAP` | Target CPA | `bid_amount` |
| `LOWEST_COST_WITH_MIN_ROAS` | E-com ROAS | `bid_constraints.roas_average_floor` × 10000 |

### 10.4 ROAS Scaling

`bid_constraints.roas_average_floor` = integer × 10000
- 2.5× ROAS = `25000`
- 1.0× breakeven = `10000`
- 3.0× = `30000`

### 10.5 Minimum Budget by Currency (approx)

| Currency | Daily impressions min | Daily conversions min |
|---|---|---|
| USD | $1 | ~$5 |
| INR | ₹40 | ~₹100 |
| EUR | €1 | ~€5 |
| GBP | £1 | ~£4 |

**Learning-phase formula:** `target_CPA × 50 / 7` = min daily budget to exit learning phase in 1 week.

---

## 11. Schedule Rules

| Rule | Value |
|---|---|
| `start_time` / `end_time` format | ISO 8601 with TZ (e.g. `2026-05-24T00:00:00+0530`) |
| Daily budget min duration | None (open-ended) |
| Lifetime budget min duration | 24 hours |
| Maximum duration | 1 year (lifetime) |
| Past dates | ❌ Rejected — Meta blocks expired schedules |
| `end_time > start_time` | Mandatory |

---

## 12. Targeting Hard Limits

| Field | Min | Max | Notes |
|---|---|---|---|
| `age_min` | 13 | 65 | SAC forces 18-65 |
| `age_max` | 13 | 65 | 65 = "65+" |
| Audience size | 1,000 reach | — | Below = no delivery |
| Audience optimization floor | — | ~50,000 reach | Below = unstable conversion delivery |
| `geo.cities` count | 1 | 250 | |
| `geo.countries` count | 1 | 25 | |
| `geo.zips` count | 1 | 2,500 | SAC strips zips |
| City radius | 1 mi / 1 km | 50 mi / 80 km | SAC min 15 mi (US) |
| `flexible_spec` groups | 1 | 5 | AND between groups, OR inside |
| `custom_audiences` count | 0 | 500 | SAC strips for housing/employment/credit |
| Lookalike ratio | 0.01 (1%) | 0.10 (10%) | Country mandatory |
| CA activation minimum | 100 matched users | — | Below = unusable |
| CA retention | 1 day | 180 days (most types) | Lead-form CA = 90 days max |

### Targeting Spec Structure

```json
{
  "geo_locations": {
    "countries": ["IN", "US"],
    "regions": [{"key": "4264"}],
    "cities": [{"key": "2643743", "radius": 25, "distance_unit": "kilometer"}],
    "zips": [{"key": "IN:110001"}],
    "places": [{"key": "...", "radius": 10, "distance_unit": "mile"}],
    "custom_locations": [{"latitude": 28.61, "longitude": 77.21, "radius": 17, "distance_unit": "mile"}],
    "geo_markets": [{"key": "DMA:501"}],
    "location_types": ["home", "recent"]
  },
  "age_min": 18,
  "age_max": 65,
  "genders": [1, 2],
  "flexible_spec": [
    {"interests": [{"id": "...", "name": "..."}]},
    {"behaviors": [{"id": "...", "name": "..."}]}
  ],
  "custom_audiences": [{"id": "..."}],
  "excluded_custom_audiences": [{"id": "..."}],
  "publisher_platforms": ["facebook", "instagram"],
  "facebook_positions": ["feed", "facebook_reels"],
  "instagram_positions": ["stream", "story", "reels"],
  "device_platforms": ["mobile", "desktop"],
  "user_os": ["iOS", "Android"],
  "targeting_automation": {"advantage_audience": 1}
}
```

---

## 13. Error Subcode to Fix Mapping

| Subcode | Meaning | Auto-Fix Action |
|---|---|---|
| `100` | Invalid parameter (generic — check subcode) | Surface error_user_msg to user |
| `190` | Invalid OAuth token | Refresh token |
| `200` | Permissions error | Check page-account binding |
| `2446029` | optimization_goal invalid for objective | Auto-fix via intent-preservation |
| `2490408` | destination_type invalid for objective | Strip destination_type |
| `1815715` | destination_type ON_AD not allowed for goal | Strip destination_type |
| `1487079` | special_ad_categories required | Add `special_ad_categories=[]` if not set |
| `2446466` | creative authorization_category mismatch | Cascade campaign SAC → creative auth |
| `2069033` | Political ad missing disclaimer | Surface to user (no auto-fix) |
| `2447003` | Political authorization expired | Surface to user |
| `1487058` | Page not eligible for ad account | Trigger `facebook_resolve_account_for_page` |
| `1346046` | Disclaimer not registered | Surface to user |
| `1870194` | Geo radius below SAC minimum | Auto-set to 15 miles |
| `4` / `17` | Rate limit | Backoff per `X-Business-Use-Case-Usage` header |
| `1` / `2` | Server error | Exponential backoff: 1s, 2s, 4s, 8s |

### Error Response Structure

```json
{
  "error": {
    "message": "Invalid parameter",
    "type": "OAuthException",
    "code": 100,
    "error_subcode": 2490408,
    "is_transient": false,
    "error_user_title": "Invalid Destination Type",
    "error_user_msg": "Selected destination type is not allowed for OUTCOME_LEADS.",
    "fbtrace_id": "AaA0bBcC123dD456eEfF789"
  }
}
```

**Field priorities:**
- `error_user_msg` → show to end-user
- `is_transient: true` → retry with backoff
- `fbtrace_id` → include in Meta support tickets

---

## 14. v25 Breaking Changes (May 19, 2026)

| Field | Status | Workaround |
|---|---|---|
| `smart_promotion_type` (any value) | ❌ REMOVED | Use unified Advantage+ structure |
| `existing_customer_budget_percentage` | ❌ REMOVED | 2 ad sets with custom audience include/exclude |
| `messenger_home` placement | ❌ REMOVED (Nov 11, 2025) | Use other Messenger positions |
| 7d_view attribution | ❌ REMOVED (Jan 12, 2026) | Use 1d_view only |
| 28d_view attribution | ❌ REMOVED (Jan 12, 2026) | Use 1d_view only |
| EU + `ISSUES_ELECTIONS_POLITICS` | 🚨 BANNED (TTPA Oct 2025) | Block at preflight |

### Attribution Windows (2026 valid values)

| event_type | window_days |
|---|---|
| `CLICK_THROUGH` | 1, 7, 28 |
| `VIEW_THROUGH` | 1 (only) |
| `ENGAGED_VIDEO_VIEW` | 1 |

### Advantage+ Unified Structure (new way)

Advantage+ campaigns identified by 3 automation levers all ON:

1. **Budget automation** — campaign-level budget, CBO on (no ad-set budgets)
2. **Audience automation** — targeting only `geo_locations` + `targeting_automation.advantage_audience=1`
3. **Placement automation** — no `publisher_platforms` / position filtering

When all 3 set → Meta auto-marks `advantage_state_info` = `ADVANTAGE_PLUS_SALES` / `ADVANTAGE_PLUS_APP` / `ADVANTAGE_PLUS_LEADS`.

**Performance benchmark (Black Friday 2024):**
- Advantage+ → 3.14 ROAS
- Manual → 2.70 ROAS
- **Threshold:** Need 50 conversions/ad-set/week for Advantage+ to win

---

## 15. Auto-Fix Architecture (Migration 0055)

### 15.1 4-Tier Auto-Fix System

#### TIER 1: Whitelist Matrix

```python
# meta_validation.py
_VALID_OBJ_GOAL_DEST_MATRIX: Dict[str, Dict[str, frozenset]] = {
    "OUTCOME_AWARENESS": {
        "REACH":          frozenset({"UNDEFINED"}),
        "IMPRESSIONS":    frozenset({"UNDEFINED"}),
        "AD_RECALL_LIFT": frozenset({"UNDEFINED"}),
        "THRUPLAY":       frozenset({"UNDEFINED", "ON_VIDEO"}),
    },
    "OUTCOME_TRAFFIC": {
        "LANDING_PAGE_VIEWS": frozenset({"WEBSITE"}),
        "LINK_CLICKS":        frozenset({"WEBSITE", "APP", "MESSENGER", "WHATSAPP", "INSTAGRAM_PROFILE"}),
        "REACH":              frozenset({"WEBSITE", "UNDEFINED"}),
        "CONVERSATIONS":      frozenset({"MESSENGER", "WHATSAPP", "INSTAGRAM_DIRECT"}),
        "IMPRESSIONS":        frozenset({"WEBSITE", "UNDEFINED"}),
    },
    "OUTCOME_ENGAGEMENT": {
        "POST_ENGAGEMENT":             frozenset({"UNDEFINED", "ON_POST", "ON_PAGE"}),
        "PAGE_LIKES":                  frozenset({"ON_PAGE", "UNDEFINED"}),
        "EVENT_RESPONSES":             frozenset({"ON_EVENT"}),
        "THRUPLAY":                    frozenset({"ON_VIDEO"}),  # UNDEFINED removed 2026-08-05: Meta rejects absent destination (2490408)
        "CONVERSATIONS":               frozenset({"MESSENGER", "WHATSAPP", "INSTAGRAM_DIRECT"}),
        "PROFILE_VISIT":               frozenset({"FACEBOOK_PAGE", "INSTAGRAM_PROFILE"}),
        "PROFILE_AND_PAGE_ENGAGEMENT": frozenset({"INSTAGRAM_PROFILE_AND_FACEBOOK_PAGE"}),
    },
    "OUTCOME_LEADS": {
        "LEAD_GENERATION":     frozenset({"ON_AD", "WEBSITE"}),
        "OFFSITE_CONVERSIONS": frozenset({"WEBSITE", "APP"}),  # pixel-gated
        "QUALITY_LEAD":        frozenset({"ON_AD", "WEBSITE"}),
        "CONVERSATIONS":       frozenset({"MESSENGER", "INSTAGRAM_DIRECT"}),
        "QUALITY_CALL":        frozenset({"PHONE_CALL"}),
        "MEANINGFUL_CALL_ATTEMPT": frozenset({"PHONE_CALL"}),
        "SUBSCRIBERS":         frozenset({"ON_AD"}),
        "REMINDERS_SET":       frozenset({"ON_AD"}),
        "LANDING_PAGE_VIEWS":  frozenset({"WEBSITE"}),
        "LINK_CLICKS":         frozenset({"WEBSITE", "APP"}),
    },
    "OUTCOME_SALES": {
        "OFFSITE_CONVERSIONS":           frozenset({"WEBSITE", "APP", "SHOP_AUTOMATIC"}),
        "VALUE":                         frozenset({"WEBSITE", "APP"}),
        "ADVERTISER_SILOED_VALUE":       frozenset({"WEBSITE"}),
        "LANDING_PAGE_VIEWS":            frozenset({"WEBSITE"}),
        "LINK_CLICKS":                   frozenset({"WEBSITE", "APP"}),
        "CONVERSATIONS":                 frozenset({"MESSENGER"}),
        "MESSAGING_PURCHASE_CONVERSION": frozenset({"MESSENGER"}),
    },
    "OUTCOME_APP_PROMOTION": {
        "APP_INSTALLS":                         frozenset({"APP"}),
        "APP_INSTALLS_AND_OFFSITE_CONVERSIONS": frozenset({"APP"}),
        "IN_APP_VALUE":                         frozenset({"APP"}),
        "OFFSITE_CONVERSIONS":                  frozenset({"APP"}),
        "LINK_CLICKS":                          frozenset({"APP"}),
    },
}
```

#### TIER 2: Combo-Aware Auto-Fix Function

```python
def _auto_fix_invalid_combo(camp, ads, creative, fixes):
    """
    Detect (objective, optimization_goal, destination_type) combos
    and auto-fix BEFORE Meta API call.

    Strategy (priority order):
    1. If user intent clear (specific goal) → preserve goal, change objective
    2. If goal invalid for objective → auto-pick closest valid goal
    3. If destination invalid → strip (Meta defaults to UNDEFINED)
    4. Log every auto-fix in fixes[] for transparency
    """
    obj = _norm(camp.get("objective"))
    og = _norm(ads.get("optimization_goal"))
    dt = _norm(ads.get("destination_type"))

    valid_dests = _VALID_OBJ_GOAL_DEST_MATRIX.get(obj, {}).get(og)

    if valid_dests is None:
        # (obj, og) combo invalid — auto-fix using intent preservation
        new_obj, new_og = _auto_fix_objective_goal_combo(obj, og)
        if new_obj != obj:
            camp["objective"] = new_obj
            fixes.append(
                f"Auto-corrected objective {obj} → {new_obj} to preserve "
                f"optimization_goal={og} (Meta rejects {obj}+{og})"
            )
        elif new_og != og:
            ads["optimization_goal"] = new_og
            fixes.append(
                f"Auto-corrected optimization_goal {og} → {new_og} (invalid for {obj})"
            )
        return

    # (obj, og) valid — check destination
    if dt and dt not in valid_dests:
        if "UNDEFINED" in valid_dests:
            ads.pop("destination_type", None)
            fixes.append(
                f"Stripped destination_type={dt} (invalid for {obj}+{og}); "
                f"Meta will treat as UNDEFINED."
            )
        else:
            new_dt = sorted(valid_dests)[0]
            ads["destination_type"] = new_dt
            fixes.append(f"Changed destination_type {dt} → {new_dt} for {obj}+{og}")


def _auto_fix_objective_goal_combo(obj, og):
    """Intent-preserving objective+goal correction."""
    required_obj = GOAL_TO_REQUIRED_OBJ.get(og)
    if required_obj and required_obj != obj:
        return required_obj, og  # change objective, preserve goal
    # Goal not clearly mapped; keep objective, pick valid goal for it
    obj_defaults = OBJECTIVE_ADSET_DEFAULTS.get(obj, {})
    new_og = obj_defaults.get("optimization_goal", "REACH")
    return obj, new_og
```

#### TIER 3: Pixel-Gating Soft Notes

```python
def _check_pixel_data_gating(camp, ads, tracking, notes):
    """Warn user when pixel data insufficient — don't block."""
    obj = _norm(camp.get("objective"))
    og = _norm(ads.get("optimization_goal"))
    pixel_id = tracking.get("pixel_id") if isinstance(tracking, dict) else None

    if og == "OFFSITE_CONVERSIONS" and not pixel_id:
        if obj == "OUTCOME_LEADS":
            notes.append(
                "⚠️ OUTCOME_LEADS + OFFSITE_CONVERSIONS requires Meta Pixel with LEAD events. "
                "No tracking.pixel_id supplied. Consider LEAD_GENERATION + destination_type=ON_AD instead."
            )
        elif obj == "OUTCOME_SALES":
            notes.append(
                "⚠️ OUTCOME_SALES + OFFSITE_CONVERSIONS requires Meta Pixel with PURCHASE events. "
                "Without pixel_id, Meta cannot optimize. Consider LANDING_PAGE_VIEWS instead."
            )

    if og == "VALUE":
        notes.append(
            "ℹ️ optimization_goal=VALUE requires PURCHASE events with `value` and `currency`. "
            "Meta falls back to OFFSITE_CONVERSIONS if value data insufficient."
        )
```

#### TIER 4: Wire into `validate_and_normalize_document()`

Insert after existing `_strip_invalid_destination_type` call (production_flow.py ~line 2180):

```python
# Combo-aware auto-fix
if isinstance(_camp_pre, dict) and isinstance(_ads_pre, dict):
    _creative_pre_for_combo = out.get("creative") if isinstance(out.get("creative"), dict) else {}
    _auto_fix_invalid_combo(_camp_pre, _ads_pre, _creative_pre_for_combo, fixes)
    out["campaign"] = _camp_pre
    out["adset"] = _ads_pre

# Pixel-gating warnings (non-blocking)
_tracking_pre = out.get("tracking") if isinstance(out.get("tracking"), dict) else {}
notes_collected = out.get("validation_notes", [])
_check_pixel_data_gating(_camp_pre, _ads_pre, _tracking_pre, notes_collected)
out["validation_notes"] = notes_collected
```

### 15.2 Transparency Guarantee

Every auto-fix logged in `fixes[]` array, surfaced via:
1. **Agent's response** (visible in chat)
2. **Audit log** (DB record for debugging)
3. **MCP tool response** (downstream tools see trail)

**Key principle:** Never silent fix. Always log.

### 15.3 Opt-Out for Power Users

```python
options = {
  "no_auto_fix": True   # Disable all combo auto-fixes; hard-fail on invalid
}
```

When set:
- Skip `_auto_fix_invalid_combo`
- `validate_document_for_sync` returns `blocking_error` for any forbidden combo
- User gets clear failure instead of silent correction

### 15.4 Example — Before vs After

#### Scenario: User submits `OUTCOME_ENGAGEMENT + THRUPLAY`

> **UPDATED 2026-08-05.** Earlier revisions of this doc claimed "Meta rejects
> ENGAGEMENT+THRUPLAY" and auto-switched the objective to OUTCOME_AWARENESS.
> That was wrong: the combo is VALID — Meta only rejects it when
> destination_type is absent or not ON_VIDEO (subcode 2490408). Current
> behavior keeps the user's objective and injects the required destination.

**Before (the June failure pattern, from live agent runs):**
```
❌ Agent sends destination_type=ON_AD → strip layer drops it →
   payload reaches Meta with NO destination_type →
   Meta rejects: code 100, subcode 2490408 "Performance goal isn't available"
→ Old api_errors hint then told the agent to fall back to POST_ENGAGEMENT
   (silent goal switch — wrong campaign created)
```

**After:**
```
✅ Connector auto-fixes (objective UNCHANGED):
   adset.destination_type → ON_VIDEO

✅ Logged in fixes[]:
   "Set destination_type=ON_VIDEO for goal=THRUPLAY under OUTCOME_ENGAGEMENT
    (Meta requires this destination and rejects an absent one - subcode 2490408)."

→ Ad set creates successfully; THRUPLAY and OUTCOME_ENGAGEMENT both preserved
```

---

## 16. SDK Authoritative Enums

**Source:** `github.com/facebook/facebook-business-sdk-codegen/api_specs/specs/enum_types.json` (Facebook's official SDK source)

### 16.1 Campaign Objectives (21 — only 6 ODAX for new campaigns)

```
# Current (ODAX) — use these for new campaigns:
OUTCOME_AWARENESS
OUTCOME_TRAFFIC
OUTCOME_ENGAGEMENT
OUTCOME_LEADS
OUTCOME_SALES
OUTCOME_APP_PROMOTION

# Legacy (still in enum, but rejected for new campaigns):
APP_INSTALLS, BRAND_AWARENESS, CONVERSIONS, EVENT_RESPONSES,
LEAD_GENERATION, LINK_CLICKS, LOCAL_AWARENESS, MESSAGES,
OFFER_CLAIMS, PAGE_LIKES, POST_ENGAGEMENT, PRODUCT_CATALOG_SALES,
REACH, STORE_VISITS, VIDEO_VIEWS
```

### 16.2 Optimization Goals (32 values)

```
ADVERTISER_SILOED_VALUE, AD_RECALL_LIFT, APP_INSTALLS,
APP_INSTALLS_AND_OFFSITE_CONVERSIONS, AUTOMATIC_OBJECTIVE,
CONVERSATIONS, DERIVED_EVENTS, ENGAGED_PAGE_VIEWS, ENGAGED_USERS,
EVENT_RESPONSES, IMPRESSIONS, IN_APP_VALUE, LANDING_PAGE_VIEWS,
LEAD_GENERATION, LINK_CLICKS, MEANINGFUL_CALL_ATTEMPT,
MESSAGING_APPOINTMENT_CONVERSION, MESSAGING_PURCHASE_CONVERSION,
NONE, OFFSITE_CONVERSIONS, PAGE_LIKES, POST_ENGAGEMENT,
PROFILE_AND_PAGE_ENGAGEMENT, PROFILE_VISIT, QUALITY_CALL,
QUALITY_LEAD, REACH, REMINDERS_SET, SUBSCRIBERS, THRUPLAY,
VALUE, VISIT_INSTAGRAM_PROFILE
```

### 16.3 Billing Events (11)

```
APP_INSTALLS, CLICKS, IMPRESSIONS, LINK_CLICKS,
LISTING_INTERACTION, NONE, OFFER_CLAIMS, PAGE_LIKES,
POST_ENGAGEMENT, PURCHASE, THRUPLAY
```

### 16.4 Destination Types (23)

```
APP, APPLINKS_AUTOMATIC, FACEBOOK, FACEBOOK_LIVE, FACEBOOK_PAGE,
IMAGINE, INSTAGRAM_DIRECT, INSTAGRAM_LIVE, INSTAGRAM_PROFILE,
INSTAGRAM_PROFILE_AND_FACEBOOK_PAGE,
MESSAGING_INSTAGRAM_DIRECT_MESSENGER,
MESSAGING_INSTAGRAM_DIRECT_MESSENGER_WHATSAPP,
MESSAGING_INSTAGRAM_DIRECT_WHATSAPP, MESSAGING_MESSENGER_WHATSAPP,
MESSENGER, ON_AD, ON_EVENT, ON_PAGE, ON_POST, ON_VIDEO,
SHOP_AUTOMATIC, WEBSITE, WHATSAPP
```

(Plus `UNDEFINED` for omitted field; `PHONE_CALL` accepted in practice but not in formal enum.)

### 16.5 Bid Strategies (4)

```
COST_CAP, LOWEST_COST_WITHOUT_CAP, LOWEST_COST_WITH_BID_CAP,
LOWEST_COST_WITH_MIN_ROAS
```

### 16.6 Buying Types (7)

```
AUCTION, DEPRECATED_REACH_BLOCK, FIXED_CPM, MIXED,
REACHBLOCK, RESEARCH_POLL, RESERVED
```

### 16.7 Special Ad Categories (7)

```
NONE, CREDIT, EMPLOYMENT, FINANCIAL_PRODUCTS_SERVICES, HOUSING,
ISSUES_ELECTIONS_POLITICS, ONLINE_GAMBLING_AND_GAMING
```

### 16.8 Authorization Categories (3)

```
NONE, POLITICAL, POLITICAL_WITH_DIGITALLY_CREATED_MEDIA
```

### 16.9 Pacing Types (6)

```
DAY_PARTING, DISABLED, NO_PACING, PROBABILISTIC_PACING,
PROBABILISTIC_PACING_V2, STANDARD
```

### 16.10 Status Values

**Configured (settable):**
```
ACTIVE, ARCHIVED, DELETED, PAUSED
```

**Effective (Meta-computed):**
- Campaign: `ACTIVE, ARCHIVED, DELETED, IN_PROCESS, PAUSED, WITH_ISSUES`
- AdSet: `ACTIVE, ARCHIVED, CAMPAIGN_PAUSED, DELETED, IN_PROCESS, PAUSED, WITH_ISSUES`
- Ad: `ACTIVE, ADSET_PAUSED, ARCHIVED, CAMPAIGN_PAUSED, DELETED, DISAPPROVED, IN_PROCESS, PAUSED, PENDING_BILLING_INFO, PENDING_REVIEW, PREAPPROVED, WITH_ISSUES`

### 16.11 Pixel Custom Event Types (29 — for AdPromotedObject)

```
ACHIEVEMENT_UNLOCKED, ADD_PAYMENT_INFO, ADD_TO_CART, ADD_TO_WISHLIST,
AD_IMPRESSION, COMPLETE_REGISTRATION, CONTACT, CONTENT_VIEW,
CUSTOMIZE_PRODUCT, D2_RETENTION, D7_RETENTION, DONATE, FIND_LOCATION,
INITIATED_CHECKOUT, LEAD, LEVEL_ACHIEVED, LISTING_INTERACTION,
MESSAGING_CONVERSATION_STARTED_7D, OTHER, PURCHASE, RATE, SCHEDULE,
SEARCH, SERVICE_BOOKING_REQUEST, SPENT_CREDITS, START_TRIAL,
SUBMIT_APPLICATION, SUBSCRIBE, TUTORIAL_COMPLETION
```

### 16.12 Attribution Windows (2026)

```
1d_click, 7d_click, 28d_click, 1d_view, 1d_ev (engaged video view)
default
# iOS SKAdNetwork:
skan_click, skan_click_second_postback, skan_click_third_postback,
skan_view, skan_view_second_postback, skan_view_third_postback
```

🚨 `7d_view` and `28d_view` REMOVED Jan 12, 2026.

---

## 17. Pending UI Verification

These were added to earlier matrix from **SDK enum inference**, but **not yet UI-verified**:

| Objective | Goal | Status |
|---|---|---|
| AWARENESS | `ENGAGED_USERS` | ❌ NOT in UI (confirmed by user 2026-05-22) — REMOVED |
| TRAFFIC | `QUALITY_CALL`, `MEANINGFUL_CALL_ATTEMPT` | ⚠️ Pending Calls destination test |
| TRAFFIC | `VISIT_INSTAGRAM_PROFILE` | ⚠️ Pending IG profile destination test |
| ENGAGEMENT | `POST_ENGAGEMENT`, `PAGE_LIKES`, `EVENT_RESPONSES`, `PROFILE_VISIT`, `PROFILE_AND_PAGE_ENGAGEMENT`, `CONVERSATIONS` | ⚠️ Pending various engagement types |
| LEADS | `CONVERSATIONS`, `QUALITY_CALL`, `MEANINGFUL_CALL_ATTEMPT`, `SUBSCRIBERS`, `REMINDERS_SET`, `QUALITY_LEAD` | ⚠️ Pending non-Website destinations |
| SALES | `MESSAGING_PURCHASE_CONVERSION`, `CONVERSATIONS`, `ADVERTISER_SILOED_VALUE` | ⚠️ Pending Messenger destination |
| APP_PROMOTION | All 5 goals | ⚠️ Entire objective not yet verified |

### Verification Plan (next 30 minutes of UI testing)

1. **TRAFFIC + Calls destination** dropdown
2. **TRAFFIC + Messenger destination** dropdown
3. **TRAFFIC + Instagram profile** dropdown
4. **ENGAGEMENT + Post engagement type** dropdown
5. **ENGAGEMENT + Messenger destination** dropdown
6. **LEADS + Instant forms (On your ad)** dropdown
7. **LEADS + Messenger** dropdown
8. **LEADS + Calls** dropdown
9. **SALES + Messenger** dropdown
10. **APP_PROMOTION** entire flow (requires SDK-registered app)

---

## Coverage Summary

| Objective | UI Verified? | Coverage |
|---|---|---|
| OUTCOME_AWARENESS | ✅ Fully | 5 options confirmed |
| OUTCOME_TRAFFIC | ⚠️ Partial | Website destination only (5 options) |
| OUTCOME_ENGAGEMENT | ⚠️ Minimal | Video views type only (2 options) |
| OUTCOME_LEADS | ⚠️ Partial | Website destination only (5-6 options) |
| OUTCOME_SALES | ⚠️ Partial | Website destination only (6 options) |
| OUTCOME_APP_PROMOTION | ❌ None | No verification |

---

## Real-World Verified Examples (2026-05-22)

### Example 1: Successful Rekha Gupta political video ad

```json
{
  "campaign": {
    "name": "Rekha Gupta Hauslon Ki Udaan THRUPLAY",
    "objective": "OUTCOME_AWARENESS",
    "daily_budget": 10000,
    "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
    "special_ad_categories": ["ISSUES_ELECTIONS_POLITICS"],
    "status": "PAUSED"
  },
  "adset": {
    "optimization_goal": "THRUPLAY",
    "billing_event": "IMPRESSIONS",
    "start_time": "2026-05-23T00:00:00+0530",
    "end_time": "2026-05-31T23:59:59+0530",
    "status": "PAUSED"
  },
  "targeting": {
    "geo_locations": {
      "cities": [{"name": "Delhi", "radius": 25, "distance_unit": "kilometer"}]
    },
    "age_min": 18,
    "age_max": 65
  },
  "creative": {
    "mode": "EXISTING_POST",
    "page_id": "1165086893354722",
    "object_story_id": "1165086893354722_122105468859308207",
    "authorization_category": "POLITICAL"
  }
}
```

**Result:** ✅ Successfully created (campaign_id: 6979046242305, verified ok=true, ~33-39M reach)

### Example 2: Forbidden combo that today's connector blocked

```json
// User's initial brief had:
{
  "campaign": {"objective": "OUTCOME_ENGAGEMENT"},  // ❌ Wrong
  "adset": {"optimization_goal": "THRUPLAY"}       // Want video views
}
// Auto-fix (Migration 0055) would correct to:
{
  "campaign": {"objective": "OUTCOME_AWARENESS"},  // ✅ Auto-corrected
  "adset": {"optimization_goal": "THRUPLAY"}       // Preserved user intent
}
```

---

## Production-Grade Strategic Patterns

### Pattern 1: CAPI Mandatory in 2026

Browser pixel loses 30-60% of events to:
- Ad blockers, Safari ITP, Firefox tracking protection, iOS 14+ ATT

**Solution:** Server-side Conversions API. Use shared `event_id` for dedup. Aim for Event Match Quality (EMQ) ≥ 7/10.

### Pattern 2: Advantage+ Threshold Rule

- **Above 50 conversions/ad-set/week:** Advantage+ wins → 3.14 ROAS vs 2.70 manual
- **Below 50/week:** Manual ABO + tight targeting wins

### Pattern 3: Layered Attribution

- **Optimization layer** (Meta uses for bidding): `attribution_spec` on ad set = 7d_click + 1d_view
- **Reporting layer** (you query in Insights): `action_attribution_windows` per use case (1d_click strict, 28d_click B2B)
- **iOS purchase campaigns:** Optimize on `1d_click only` (view-attribution unreliable post-SKAN)

### Pattern 4: Always Exclude Existing Customers from Prospecting

```python
"excluded_custom_audiences": [{"id": "<purchasers_180d_CA_id>"}]
# Saves 15-30% of spend on people who already converted
```

### Pattern 5: Pre-flight Every Creation with validate_only

```
POST /act_<id>/campaigns?execution_options[validate_only]=true
```

Same response as actual creation, doesn't persist. Catch schema errors before committing budget.

---

## File Sources & Citations

### Primary Authoritative Sources
- **Facebook Business SDK Codegen** — `github.com/facebook/facebook-business-sdk-codegen/api_specs/specs/enum_types.json` (verbatim enum values)
- **Meta Marketing API v25.0 Changelog** — `developers.facebook.com/docs/marketing-api/marketing-api-changelog/version25.0`
- **Meta Destination Type Reference** — `developers.facebook.com/docs/marketing-api/adset/destination_type/`
- **Meta SIEP Policy** — `transparency.meta.com/policies/ad-standards/SIEP-advertising/SIEP/`

### Cross-Reference Sources
- Releasebot.io v25 release notes
- PPC.land Meta updates
- Postman Meta Marketing API collection
- Jon Loomer Meta ads deep-dives
- Pipeboard.co video ad specs
- Adamigo.ai political compliance

### Real-World Verification
- Multiple successful ad creations through MCP connector on 2026-05-22
- Direct UI screenshots from Ads Manager (act_1935333496731674)
- Live Meta API error responses analyzed

---

## Document Maintenance

**Update triggers:**
- New Meta API version released (annually + quarterly minor)
- New SDK enum values appear
- UI dropdown changes detected
- Meta deprecation announcements

**Verification SLA:**
- Critical changes (breaking) — verify within 24 hours
- New features — verify within 1 week
- Quarterly full UI re-verification recommended

---

**End of Master Reference**

*This document is the single source of truth for Meta Ads valid combos in BOL7's Facebook Ads MCP Connector.*
