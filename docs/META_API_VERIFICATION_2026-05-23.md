# Meta Marketing API — Verification Report (2026-05-23)

**Status:** Verification of existing knowledge against authoritative sources
**Companion to:** [META_ADS_MASTER_REFERENCE.md](META_ADS_MASTER_REFERENCE.md) (do not modify that file directly)
**Methodology:** SDK enum source + Meta docs + community/SDK cross-check via 2 parallel research agents

---

## 1. Verification Methodology

Three sources cross-referenced:

| Source | What It Provides | Confidence |
|--------|-----------------|------------|
| Facebook SDK codegen (`enum_types.json`) | Authoritative enum values (570 KB, 893 entries) | 100% |
| Meta Marketing API official docs | Rule semantics, error references, guides | 80% (truncated on WebFetch) |
| Community / SDK inference / past sessions | Edge cases, undocumented behaviors | 60% |

**Raw source dump:** `/tmp/meta_enums.json` (fetchable anytime via:
`curl -L https://raw.githubusercontent.com/facebook/facebook-business-sdk-codegen/main/api_specs/specs/enum_types.json`)

**Research reports backing this file:**
- `/tmp/meta_creative_research_2026-05-23.md` — ad/creative level (336 lines)
- `/tmp/meta_cross_level_verification_2026-05-23.md` — cross-level rules (275 lines)

---

## 2. Overall Confidence Assessment

| Layer of Knowledge | Confidence | Why |
|--------------------|------------|-----|
| Enum value lists (which strings exist) | **100%** | SDK source = ground truth |
| Which enum is valid for which combo | **60-80%** | Some UI-verified, some inferred |
| Error subcodes (specific numbers) | **30%** | Most NOT in public Meta docs |
| Cross-level rules (campaign × adset × ad) | **70%** | Verified in principle |
| v25 breaking changes list | **75%** | Multiple sources confirm |
| Behavioral edge cases | **40%** | Need real production telemetry |

**Bottom line:** ~75% overall. Good enough to build `combo_rules.py` v1. Not enough to claim "production-grade" without continuous telemetry.

---

## 3. Critical Discoveries — Errors in Existing Knowledge

### 3.1 `TWO_SECOND_CONTINUOUS_VIDEO_VIEWS` is NOT in SDK enum

**Affected file:** [meta_validation.py:241-244](../meta_validation.py)

Current code lists it as a valid `optimization_goal`. SDK does not contain this string. Likely a UI label that maps to `THRUPLAY` internally.

**Action:** Verify with a synthetic Meta `?_validate=true` request. If rejected, remove from `_GOALS_PREFER_UNDEFINED_DESTINATION`.

### 3.2 `VIDEO_VIEWS` is an objective, not an optimization_goal

The SDK enum has `VIDEO_VIEWS` only in the legacy objective list, not in optimization_goal. The optimization_goal equivalent is `THRUPLAY`.

**Action:** Audit `production_flow.py` for any place treating `VIDEO_VIEWS` as opt_goal.

### 3.3 Internal subcodes (1815715, 2446466, 1815704) not publicly documented

These appear throughout our error handling and comments. Background agent could not find any of them in:
- Meta error reference docs
- Stack Overflow / community threads
- phwd/fbec error-code corpus
- SDK source

**Closest documented neighbor:** `1815762` ("Invalid Optimization Sub Event for Objective")

**Action:** Treat these subcodes as "internal empirical observations" — keep them as fallback matchers but stop asserting them as canonical Meta contracts in user-facing error messages.

### 3.4 Pixel + EXISTING_POST behavior misstated

Our docs say "pixel ignored". Per Meta tracking-spec docs:
> "Pixels optimized by specifying the pixel ID in the promoted_object are automatically tracked, so you do not need to specify the same pixel in tracking_specs."

Reality: adset-level `promoted_object.pixel_id` STILL fires. Only redundant ad-level `tracking_specs` overrides are ignored.

**Action:** Update error/log messages in connector to reflect this.

### 3.5 `page_id` not required for EXISTING_POST creatives

When using top-level `object_story_id`, the post itself carries page identity. Existing docs mark `page_id` as required.

**Action:** Loosen validator — `page_id` is required for `object_story_spec` only, not `object_story_id`.

---

## 4. SDK-Verified Enum Counts (Today, 2026-05-23)

| Enum | SDK Count | We Had Documented | Gap |
|------|-----------|-------------------|-----|
| Campaign objectives (ODAX + legacy) | 21 | 6 ODAX + ~10 legacy ≈ 16 | 5 missing (LOCAL_AWARENESS, OFFER_CLAIMS, EVENT_RESPONSES, PAGE_LIKES, POST_ENGAGEMENT, PRODUCT_CATALOG_SALES) |
| Optimization goals | **32** | 17 | **15 missing** |
| Billing events | 11 | 6 | 5 missing (CLICKS, LISTING_INTERACTION, OFFER_CLAIMS, PAGE_LIKES, PURCHASE) |
| Destination types | 23 | 23 | ✅ All match |
| Special ad categories | 7 | 7 | ✅ All match |
| Authorization categories | **3** | 2 | 1 missing (`POLITICAL_WITH_DIGITALLY_CREATED_MEDIA`) |
| CTA types | **101** | ~30 | **71 missing** |
| FB placement positions | 22 | ~10 | Many missing |
| IG placement positions | 14 | ~5 | Many missing |
| Publisher platforms | 7 | 5 | 2 missing (OCULUS, THREADS) |

---

## 5. New Optimization Goals (Missing From Our Matrix)

Discovered in SDK, not in existing META_ADS_MASTER_REFERENCE matrix:

```
ADVERTISER_SILOED_VALUE          AUTOMATIC_OBJECTIVE
APP_INSTALLS_AND_OFFSITE_CONVERSIONS    DERIVED_EVENTS
ENGAGED_PAGE_VIEWS               ENGAGED_USERS
IN_APP_VALUE                     MEANINGFUL_CALL_ATTEMPT (NEW — replaces QUALITY_CALL?)
MESSAGING_APPOINTMENT_CONVERSION MESSAGING_PURCHASE_CONVERSION
NONE                             PROFILE_AND_PAGE_ENGAGEMENT
PROFILE_VISIT                    QUALITY_LEAD (NEW)
REMINDERS_SET                    SUBSCRIBERS
```

**Notable additions to investigate:**
- `MEANINGFUL_CALL_ATTEMPT` — possible replacement for `QUALITY_CALL`?
- `QUALITY_LEAD` — quality lead optimization, new in 2025/2026
- `MESSAGING_PURCHASE_CONVERSION` — direct purchase via messaging
- `PROFILE_VISIT` — Instagram profile visit goal
- `REMINDERS_SET` — reminder-based ads

---

## 6. New CTA Types Discovered (Not in Old Matrix)

71 of 101 CTAs were missing from our documentation. Notable new ones:

**AI-era CTAs:** `SHOP_WITH_AI`, `TRY_ON_WITH_AI`

**Channel/follow CTAs:** `JOIN_CHANNEL`, `VIEW_CHANNEL`, `FOLLOW_PAGE`, `FOLLOW_USER`, `FOLLOW_NEWS_STORYLINE`

**Messaging:** `BUY_VIA_MESSAGE`, `CHAT_WITH_US`, `START_A_CHAT`, `WHATSAPP_MESSAGE`, `VIDEO_CALL`, `AUDIO_CALL`

**Commerce:** `PURCHASE_GIFT_CARDS`, `SEND_A_GIFT`, `SEND_GIFT_MONEY`, `VIEW_CART`, `VIEW_IN_CART`, `BROWSE_SHOP`

**Gaming:** `PLAY_GAME`, `PLAY_GAME_ON_FACEBOOK`

**Other notable:** `EVENT_RSVP`, `RAISE_MONEY`, `MAKE_AN_APPOINTMENT`, `BOOK_A_CONSULTATION`, `INQUIRE_NOW`, `WATCH_LIVE_VIDEO`, `LISTEN_NOW`, `LISTEN_MUSIC`, `RECORD_NOW`

---

## 7. v25 Breaking Changes — CRITICAL FOR PRODUCTION

**Mandatory deadline:** May 19, 2026 (already passed as of today 2026-05-23)

### 7.1 Instagram Field Renames (PRODUCTION RISK)

| Pre-v25 (DEPRECATED) | v25 (REQUIRED) |
|----------------------|-----------------|
| `instagram_actor_id` | `instagram_user_id` |
| `instagram_story_id` | `source_instagram_media_id` |
| `effective_instagram_story_id` | `effective_instagram_media_id` |

**Action:** Grep codebase for old names — replace immediately.

### 7.2 Standard Enhancements Migration

Old: `degrees_of_freedom_spec` with `STANDARD_ENHANCEMENTS` flag
New: `creative_features_spec` object with `standard_enhancements` sub-spec

### 7.3 Already-Removed (no longer in API)

- `smart_promotion_type` (removed)
- `existing_customer_budget_percentage` (use 2 ad sets with include/exclude CA)
- `messenger_home` placement (removed)
- 7d/28d view attribution (deprecated)

### 7.4 Advantage+ Shopping Campaigns (ASC) Legacy

Cannot CREATE / DUPLICATE / UPDATE legacy ASC campaigns in v25.
Must use unified Advantage+ structure (campaign-level CBO + advantage_audience + no manual placements).

---

## 8. 10 New Cross-Level Rules Discovered

### R-A — Third authorization_category value
`POLITICAL_WITH_DIGITALLY_CREATED_MEDIA` — required when AI-generated visuals used in political ads. **Not currently in our validators.**

### R-B — Budget mutual exclusivity
- `daily_budget` XOR `lifetime_budget` (cannot have both)
- Cannot SWAP one for other after creation
- Ad scheduling REQUIRES `lifetime_budget`
- **Source:** facebook.com/business/help/715077473436542

### R-C — CBO precludes adset-level budgets
If campaign has Advantage Campaign Budget enabled, individual adsets cannot have own daily/lifetime budgets.
- **Source:** developers.facebook.com/docs/marketing-api/bidding/guides/advantage-campaign-budget/

### R-D — LOWEST_COST_WITH_MIN_ROAS eligibility
Requires:
- Pixel or SDK set up
- ≥30 attributed click-through purchases with values in past 7 days (web)
- ≥15 attributed app sales in same period (app)

Currently allows creation but **silently stops delivery** if not eligible. Pre-flight check recommended.
- **Source:** facebook.com/business/help/1113453135474912 + 571188993373447

### R-E — DPA template_data + link_data conflict
Error code `#1885858` "Unsupported Ad Format In Dynamic Creative Ad"
Cannot mix dynamic catalog (`template_data` + `product_set_id`) with manual `link_data` in same creative.
- **Source:** developers.facebook.com/community/threads/754307932810192/

### R-F — Carousel + asset_customization_rules conflict
Same error `#1885858` when adding per-placement asset rules to carousel.
Workaround: turn ad off/on in Ads Manager.

### R-G — Reels non-9:16 video rejection
Letterboxed or rejected with "Inappropriate Aspect Ratio".
- **Source:** facebook.com/business/help/682655495435254

### R-H — Branded content + ISSUES_ELECTIONS_POLITICS (UNVERIFIED)
Conflict suspected but not documented. Open question.

### R-I — Special Ad Category targeting restrictions ⚠️ CRITICAL GAP

When `campaign.special_ad_categories != [NONE]`:
- `excluded_custom_audiences` NOT allowed
- Age narrowing NOT allowed (must be 18-65)
- Gender targeting NOT allowed
- Zip codes NOT allowed (city/region only)
- Detailed targeting on protected attributes blocked

**This is a production-grade gap in our validators.** Rekha Gupta political ads worked because they targeted broad locations and 18-65 age range — but any narrower targeting would have failed.

- **Source:** developers.facebook.com/docs/marketing-api/audiences/special-ad-category/

### R-J — saved_audience_id + custom_audiences merge logic
Cannot use both simultaneously without `saved_audience_id` controlling. Saved audience uses AND logic with other targeting.
- **Source:** developers.facebook.com/docs/marketing-api/reference/saved-audience/

---

## 9. Refined Cross-Level Rules (12 Known)

| Rule | Original Wording | Refined Wording | Confidence |
|------|-------------------|-----------------|------------|
| 1. THRUPLAY+AWARENESS+ON_AD rejected | Specific to THRUPLAY | **AWARENESS rejects ON_AD for any opt_goal** (broader) | VERIFIED (mechanism) / Subcode INFERRED |
| 2. TRAFFIC+LANDING_PAGE_VIEWS+non-WEBSITE | Same | Confirmed via destination_type matrix | VERIFIED |
| 3. ISSUES_ELECTIONS_POLITICS → POLITICAL auth | Same | Confirmed; add 3rd value `POLITICAL_WITH_DIGITALLY_CREATED_MEDIA` | VERIFIED |
| 4. Pixel+EXISTING_POST → pixel ignored | Too strong | **Adset pixel still fires; ad-level overrides ignored** | REFINED |
| 5. CALL_NOW → PHONE_CALL required | "MUST" | Change to "expected" (not in formal enum) | VERIFIED WITH CAVEAT |
| 6. WHATSAPP_MESSAGE → WHATSAPP destination | Same | Quoted from Meta CTWA docs | VERIFIED |
| 7. Lead form → OUTCOME_LEADS | "ONLY" | **Mostly OUTCOME_LEADS; messenger lead-gen via OUTCOME_ENGAGEMENT exists** | VERIFIED WITH CAVEAT |
| 8. APP_PROMOTION → APP destination | Same | Confirmed; phrase as "OUTCOME_APP_PROMOTION requires APP" | VERIFIED |
| 9. THRUPLAY/VIDEO_VIEWS → video required | Same | Confirmed | VERIFIED |
| 10. QUALITY_CALL → no pixel attach | Subcode 1815704 asserted | **QUALITY_CALL not in public enum; rule physically true but subcode unverifiable** | UNVERIFIABLE |
| 11. EU + political = ban (Oct 6, 2025) | Same | Verbatim quote secured | VERIFIED |
| 12. India + political = ECI 48h silent | Same | Statute verified; **Meta API enforcement = soft-warn only** | VERIFIED (policy) / INFERRED (API enforcement) |

---

## 10. Open Questions (Need UI Verification or Production Testing)

1. Does `TWO_SECOND_CONTINUOUS_VIDEO_VIEWS` work via API? (UI label only?)
2. Is subcode 1815715 a real Meta subcode or internal codebase value?
3. Is subcode 2446466 documented anywhere?
4. Does `QUALITY_CALL` exist as a public optimization_goal?
5. Is `MEANINGFUL_CALL_ATTEMPT` a replacement for `QUALITY_CALL`?
6. When is `POLITICAL_WITH_DIGITALLY_CREATED_MEDIA` required exactly?
7. Does `LOWEST_COST_WITH_MIN_ROAS` reject at creation OR silently fail at delivery?
8. Is branded content + ISSUES_ELECTIONS_POLITICS hard-blocked?
9. What are the 10 remaining UI dropdown screenshots showing? (TRAFFIC+Calls/Messenger/IG, ENGAGEMENT+Messenger, LEADS+Forms/Messenger/Calls, SALES+Messenger, APP_PROMOTION)

---

## 11. Action Items (Prioritized)

### P0 — Production Risk (Do This Week)

1. **Grep codebase for v25 field name violations:**
   ```bash
   grep -rn "instagram_actor_id\|instagram_story_id\|effective_instagram_story_id" \
     MCP_SERVICEs/facebook_ads_mcp/
   ```
   Replace with new names.

2. **Implement R-I (Special Ad Category targeting restrictions)** in pre-flight validator. Otherwise political/credit/housing/etc. ads with narrow targeting will fail at Meta.

3. **Verify `TWO_SECOND_CONTINUOUS_VIDEO_VIEWS`** with synthetic `?_validate=true` — remove if not real.

### P1 — Knowledge Base Update (Next 1-2 Weeks)

4. Update [META_ADS_MASTER_REFERENCE.md](META_ADS_MASTER_REFERENCE.md) with verified delta:
   - §6: Structural-path column for creative modes
   - §6: Reels and DCO/asset_feed_spec rows
   - §14: 4 v25 Instagram renames + creative_features_spec
   - §16: Expand to 32 opt_goals, 101 CTAs, 3 auth categories
   - New §: WhatsApp ≠ pixel rule
   - New §: Special Ad Category targeting restrictions (R-I)
   - New §: 10 new cross-level rules (R-A through R-J)

5. Update [meta_validation.py](../meta_validation.py):
   - Refine subcode assertions (treat as internal empirical, not canonical)
   - Add `POLITICAL_WITH_DIGITALLY_CREATED_MEDIA` to auth categories
   - Add R-I validator (special ad category targeting restrictions)

### P2 — Production-Grade System (Next Month)

6. **Build `combo_rules.py`** — single source of truth (adset + ad/creative + cross-level)
7. **Add `facebook_get_combo_rules` MCP tool** — agent fetches at session start
8. **Synthetic validation battery** — 300 brief variations to Meta `?_validate=true` to populate truth matrix
9. **Monthly SDK enum diff cron** — alert on changes:
   ```bash
   curl -L https://raw.githubusercontent.com/.../enum_types.json > today.json
   diff yesterday.json today.json
   ```

### P3 — Continuous Improvement

10. Production error subcode catalog — grep last 6 months of logs, map to root causes
11. UI verification of remaining 10 dropdowns (when user has time)
12. Re-verify quarterly — Meta changes things silently

---

## 12. Sources (Authoritative)

### Primary (100% Confidence)

- [Facebook Business SDK Codegen](https://github.com/facebook/facebook-business-sdk-codegen/blob/main/api_specs/specs/enum_types.json) — enum source of truth
- Local snapshot: `/tmp/meta_enums.json` (570 KB, fetched 2026-05-23)

### Secondary (80% Confidence — Meta-Owned)

- [destination_type matrix](https://developers.facebook.com/docs/marketing-api/adset/destination_type/)
- [Ad Creative reference](https://developers.facebook.com/docs/marketing-api/reference/ad-creative/)
- [Object Story Spec](https://developers.facebook.com/docs/marketing-api/reference/ad-creative-object-story-spec/)
- [Asset Feed Spec](https://developers.facebook.com/docs/marketing-api/ad-creative/asset-feed-spec/)
- [Click-to-WhatsApp](https://developers.facebook.com/docs/marketing-api/ad-creative/messaging-ads/click-to-whatsapp/)
- [Lead Ads Create](https://developers.facebook.com/docs/marketing-api/guides/lead-ads/create/)
- [Call Ads](https://developers.facebook.com/docs/marketing-api/call-ads/)
- [v25.0 Changelog](https://developers.facebook.com/docs/marketing-api/marketing-api-changelog/version25.0/)
- [Political ads API](https://developers.facebook.com/ads/blog/post/v2/2018/08/20/API-politics-national-importance/)
- [Special Ad Category targeting](https://developers.facebook.com/docs/marketing-api/audiences/special-ad-category/)
- [CBO / Advantage Budget](https://developers.facebook.com/docs/marketing-api/bidding/guides/advantage-campaign-budget/)
- [Tracking Specs](https://developers.facebook.com/docs/marketing-api/tracking-specs/)

### Policy / Regulatory (Verified Authoritative)

- [EU political ad ban announcement (Meta)](https://about.fb.com/news/2025/07/ending-political-electoral-and-social-issue-advertising-in-the-eu/)
- [EU TTPA effective date (EU Commission)](https://commission.europa.eu/news-and-media/news/new-eu-rules-political-advertising-come-effect-2025-10-10_en)
- [ECI silent period rules (PIB India)](https://www.pib.gov.in/PressReleasePage.aspx?PRID=2253528)

### Tertiary (60% Confidence — Community)

- [Carousel/asset-customization #1885858 thread](https://developers.facebook.com/community/threads/754307932810192/)
- [Swipe Insight v25 summary](https://web.swipeinsight.app/posts/facebook-launches-graph-api-v25-and-marketing-api-v25-updates-22544)
- [Facebook Reels specs (postfa.st)](https://postfa.st/sizes/facebook/reels)
- [Pipeboard 2026 video specs](https://pipeboard.co/blog/video-ads-facebook-specs)

---

## 13. How This File Should Evolve

**Do NOT edit [META_ADS_MASTER_REFERENCE.md](META_ADS_MASTER_REFERENCE.md) directly.**

This verification file is the **delta layer**. Workflow:

1. New finding from production → add to this file (or new dated `META_API_VERIFICATION_<date>.md`)
2. Periodically (quarterly) — merge confirmed findings INTO master reference with full attribution
3. Master reference = stable knowledge base
4. Verification files = changelog / delta / open questions

This separation:
- Master ref stays clean and authoritative
- Verification files document the *learning process*
- Future engineers see WHY a rule exists, not just THAT it exists
- Easy rollback if a "verified" rule turns out wrong

---

**End of initial verification report. Open questions section has 9 items pending — these are next research priorities.**

---

# Addendum — Ad Creative Deep-Dive (2026-05-23, Session 2)

**Trigger:** User directed verification of https://developers.facebook.com/documentation/ads-commerce/marketing-api/creative
**Method:** Direct page WebFetch returned JS-shell stub. Verified via 14 SDK spec files + 3 reachable Meta reference pages + WebSearch snippets.
**Detail report:** `/tmp/meta_creative_deepdive_2026-05-23.md` (489 lines)

---

## A1. Confirmed: Current Graph API version is v25.0

VERIFIED across 4 reachable Meta reference pages today:
- `ad-creative-object-story-spec` → "Graph API Reference v25.0"
- `ad-creative-link-data` → "Meta Graph API v25.0"
- `ad-creative-features-spec` → "Graph API Reference v25.0"
- `ad-creative-platform-customization-override` → "v25.0"

No v26+ surfaced anywhere. **Our codebase targeting v25.0 is correct.**

---

## A2. Critical Discovery — AdCreative has 76 top-level fields, not ~20

The SDK ground truth shows the AdCreative object exposes **76 fields**. Our existing META_ADS_MASTER_REFERENCE covers roughly 15-20. **Coverage gap: ~75%.**

### Major Field Categories Previously Undocumented

| Category | Fields We Missed |
|----------|------------------|
| **Advantage+ AI auto-sourcing** | `creative_sourcing_spec`, `media_sourcing_spec`, `recommender_settings`, `contextual_multi_ads`, `auto_update`, `dynamic_ad_voice` |
| **Regulatory disclaimers** | `regional_regulation_disclaimer_spec` (5 regions), `ad_disclaimer_spec`, `effective_authorization_category` |
| **Cross-platform routing** | `omnichannel_link_spec`, `applink_treatment`, `link_deep_link_url`, `enable_direct_install`, `enable_launch_instant_app` |
| **Platform-specific customization** | `platform_customizations.instagram`, `portrait_customizations`, `facebook_branded_content`, `instagram_branded_content` |
| **Identity overrides** | `actor_id`, `use_page_actor_override`, `effective_authorization_category` |
| **DCO/Flexibility** | `degrees_of_freedom_spec` (8 sub-fields), `creative_features_spec` (v25 new — replaces STANDARD_ENHANCEMENTS) |
| **Interactive content** | `interactive_components_spec`, `messenger_sponsored_message`, `marketing_message_structured_spec` |
| **Catalog/DPA** | `template_url`, `template_url_spec`, `product_set_id`, `product_data`, `dynamic_ad_voice`, `categorization_criteria`, `category_media_source` |
| **Read-only resolved** | `effective_object_story_id`, `effective_instagram_media_id`, `effective_authorization_category` |
| **Source provenance** | `source_facebook_post_id`, `source_instagram_media_id`, `photo_album_source_object_story_id` |

---

## A3. POST `/adcreatives` Has Zero Hard-Required Parameters

VERIFIED from `AdAccount.json` codegen (POST endpoint `adcreatives`):

> All 61 declared POST parameters carry `"required": false` in the schema.

**Validity is enforced by mutually-exclusive groupings**, not single-field requirements:

1. **`object_story_id` XOR `object_story_spec`** — pick one
2. **Inside `object_story_spec`** — exactly one of: `link_data`, `photo_data`, `video_data`, `text_data`, `template_data`, `product_data`
3. **`link_data` for carousel** — requires `child_attachments` (2-10) + `link` + `message`
4. **Lead form** — requires `link_data.call_to_action.value.lead_gen_form_id`
5. **WhatsApp CTM** — requires `call_to_action.type=WHATSAPP_MESSAGE` + `value.app_destination="WHATSAPP"`

**Implication for our validator:** Don't validate "required field" — validate "required combination". Our existing checks may be over-strict on some fields.

---

## A4. `object_story_id` vs `object_story_spec` — Mutually Exclusive (Confirmed)

VERIFIED via SDK + community + GitHub issue trail.

| Use | When |
|-----|------|
| `object_story_id` only | Referencing an EXISTING published Page/Instagram post. Format: `{page_id}_{post_id}`. |
| `object_story_spec` only | Creating an UNPUBLISHED Page post inline (Meta creates it). Must contain `page_id` + exactly one sub-spec. |
| Both | Returns OAuthException — community-confirmed wording: `"the object story spec is ill formed"` |

**Read-only field:** `effective_object_story_id` — Meta backfills after creation regardless of which path used.

---

## A5. `format_option` Enum — 6 Values (Authoritative)

VERIFIED via SDK `AdCreativeLinkData_format_option`:

| Value | Meaning | Default? |
|-------|---------|----------|
| `single_image` | Plain single-image link ad (no carousel chrome) | |
| `carousel_images_multi_items` | Standard 2-10 card swipeable carousel | ✅ Default |
| `carousel_images_single_item` | Single-card carousel (uses carousel CTA chrome) | |
| `carousel_slideshows` | Auto-generated slideshow from carousel images (Slideshow Ad delivery) | |
| `carousel_ar_effects` | Carousel where each card carries an AR/Spark AR effect | |
| `collection_video` | Collection Ad with video cover (vs image cover) | |

**Our validator doesn't currently restrict `format_option` — should add allowed-values check.**

---

## A6. `object_type` Enum — 13 Values With Semantics

VERIFIED enum values; INFERRED semantics (Meta hasn't published per-value prose).

### Client-Set (Legacy Flows)
| Value | Purpose |
|-------|---------|
| `APPLICATION` | App-install creative (legacy path); `object_id` = app ID |
| `DOMAIN` | Generic web-link creative; `object_url` carries destination |
| `EVENT` | Event-promotion creative; `object_id` = Event ID |
| `OFFER` | Facebook Offer object; `object_id` = offer ID |
| `PAGE` | Page-promotion creative; `object_id` = Page ID |
| `PHOTO` | Photo creative; `object_id` = photo node ID |
| `VIDEO` | Video creative; `object_id` = video node ID |
| `STORE_ITEM` | Catalog product creative (DPA / Advantage+ catalog) |

### Auto-Set (Modern Flows)
| Value | When Meta Sets |
|-------|---------------|
| `SHARE` | Most modern `object_story_spec` / `object_story_id` creatives resolve here |
| `STATUS` | text_data path |

### Read-Only Sentinels (NEVER set by client)
| Value | Meaning |
|-------|---------|
| `INVALID` | Underlying object failed validation or no longer exists |
| `POST_DELETED` | Underlying post was deleted |
| `PRIVACY_CHECK_FAIL` | Underlying post failed privacy/visibility check |

**Production implication:** When fetching existing ads, agent should treat `POST_DELETED` / `PRIVACY_CHECK_FAIL` as **terminal errors** — cannot resurrect. Current code may not handle these gracefully.

---

## A7. URL Field Disambiguation (Critical for Production)

Meta has **5 different URL fields** on AdCreative. Each has specific use:

| Field | Purpose | When Required |
|-------|---------|---------------|
| `link_url` | Top-level fallback destination URL for non-story creatives | Legacy creatives without object_story_spec |
| `object_url` | Destination URL when `object_type=DOMAIN` (legacy backwards-compat) | DOMAIN object creatives only |
| `template_url` | Single URL with template tokens like `{{product.retailer_id}}` | DPA web-only destinations |
| `template_url_spec` | Multi-platform variant: object with android/ios/ipad/iphone/web/windows_phone | DPA with mobile deeplinks |
| `link_destination_display_url` | Cosmetic display URL shown to user (does NOT control click destination) | Optional - for clean display when actual URL is long tracking URL |

**Inside `object_story_spec.link_data`:**
- `link` — the actual destination URL for Page-post-backed ads
- `call_to_action.value.link` — typically duplicated from `link_data.link` (Meta requires match for some CTA types)

---

## A8. Behavior Flags — Critical Production Details

### `auto_update` (bool)
When `true`, Meta automatically refreshes underlying post-content when source object changes (catalog item, Page post, IG media). Used for dynamic/catalog ads.

### `use_page_actor_override` (bool)
When `true`, the `actor_id` at AdCreative level overrides the page inferred from `object_story_id`/`object_story_spec.page_id`. **Used for branded content** where sponsor page wants to "speak for" the content page.

### `applink_treatment` — 4 values (VERIFIED enum)
| Value | Behavior |
|-------|----------|
| `automatic` | Meta decides at delivery: app or web |
| `deeplink_with_appstore_fallback` | Try app; if not installed → App Store / Play Store |
| `deeplink_with_web_fallback` | Try app; if not installed → web URL |
| `web_only` | Always open web URL, even if app installed |

Pairs with `omnichannel_link_spec` and `link_deep_link_url` to control click routing.

### `dynamic_ad_voice` — 2 values (VERIFIED enum)
| Value | Meaning |
|-------|---------|
| `dynamic` | DPA picks best Page/voice per impression |
| `story_owner` | Always speak as the Page that owns the underlying post |

---

## A9. v25 Advantage+ Specs — New Production Capabilities We Should Surface

### `creative_sourcing_spec` — "Give Meta a URL, let it build the creative"

This is the Advantage+ AI authoring spec. Fields:
- `source_url` — URL Meta should crawl to source assets
- `enable_social_feedback_preservation` (bool) — preserves likes/comments across creative iterations
- `destination_screenshot_spec` — auto-captures destination URL as preview thumbnail
- `dynamic_site_links_spec` — automatic site-link extensions
- `site_links_spec` — manual site-link extensions
- `website_media_spec` — controls which media Meta pulls from your site
- `website_summary_spec` — controls automatic description generation
- `associated_product_set_id` — links to catalog
- `pca_spec` — Performance Creative Automation (opaque Object)
- `brand`, `intent` — opaque Object

### `media_sourcing_spec` — Multi-source asset pool
Fields: `bodies`, `descriptions`, `images`, `videos`, `titles`, `related_media`, `push_metadata_ids`.
Complement to `asset_feed_spec`: where asset_feed_spec is YOUR uploaded assets, media_sourcing_spec declares the SOURCES Meta should ingest from.

### `recommender_settings` — AI creative recommendations
```
{ "preferred_events": list<string>, "product_sales_channel": string }
```
- `preferred_events` — conversion events Meta's recommender optimizes for
- `product_sales_channel` — `online`, `offline`, `omni` hint

### `contextual_multi_ads`
```
{ "enroll_status": string }
```
Enrolls creative in multi-advertiser-ads delivery experiment.

---

## A10. `regional_regulation_disclaimer_spec` — 5 Regulatory Regions

VERIFIED schema:
```
{
  "australia_finserv": Object,
  "india_finserv": Object,
  "singapore_universal": Object,
  "taiwan_finserv": Object,
  "taiwan_universal": Object
}
```

### Regulator → Trigger Map

| Region | Trigger | Regulator |
|--------|---------|-----------|
| `india_finserv` | Securities/investment ads targeting India | SEBI — requires verified Beneficiary + Payer + SEBI registration number |
| `australia_finserv` | Financial product/service ads to Australian users (since Feb 2025) | Australia Online Scams Code |
| `singapore_universal` | **ALL ads** targeting Singapore audiences | Beneficiary mandatory |
| `taiwan_universal` | **ALL ads** targeting Taiwan | Beneficiary mandatory |
| `taiwan_finserv` | Additionally for financial-services ads to Taiwan | Payer required |

**🚨 Production implication for BOL7:**
- India financial ads (securities, mutual funds, trading platforms) **MUST** include `india_finserv` block with verified SEBI Beneficiary/Payer.
- Singapore/Taiwan targeting needs `*_universal` block for ANY ad.
- Currently our validators **don't check this**. Production risk.

---

## A11. `omnichannel_link_spec` — Cross-Channel Conversion Optimization

VERIFIED schema + JSON example (Meta's own CCCO docs):
```json
{
  "applink_treatment": "deeplink_with_web_fallback",
  "omnichannel_link_spec": {
    "web": { "url": "<web_url>" },
    "app": {
      "application_id": "<application_id>",
      "platform_specs": {
        "android": { "url": "<android_deeplink>" },
        "ios":     { "url": "<ios_deeplink>" }
      }
    }
  }
}
```

**What it enables:** One creative declares web URL + iOS/Android deeplinks simultaneously. Meta routes each user optimally: app users → deeplink, non-app users → web. Powers Advantage+ Cross-Channel Conversion Optimization (CCCO) where campaign optimizes against both web and app conversion events from single creative.

---

## A12. `platform_customizations.instagram` — IG-Specific Media Override

VERIFIED schema + verbatim Meta prose:
> "The media specified here replaces the media originally defined in the ad creative when the ad displays in an associated placement."

Override sub-object:
- `caption_ids` (list<numeric string>)
- `image_crops` (AdsImageCrops)
- `image_hash` (string)
- `image_url` (string)
- `video_id` (numeric string)

**Use case:** Show FB users image_hash A, show IG users image_hash B — single ad set, different creative per surface.

**Production gap:** Our codebase has no support for platform-specific creative variants. Worth adding when we hit the use case.

---

## A13. `portrait_customizations` — Stories/Reels Aspect Override

VERIFIED schema:
```
{
  "carousel_delivery_mode": string,  // "single" | "multi"
  "specifications": list<Object>
}
```

For portrait (9:16) placements, lets you override carousel rendering mode and per-card aspect overrides. Specifications schema is opaque `Object` in codegen — needs UI verification.

---

## A14. Carousel — 15 Per-Card Fields (Not Just "image + link")

VERIFIED via `AdCreativeLinkDataChildAttachment.json` (1350 bytes):

```
android_url, ios_url, link              — destination URLs (web + deep links)
call_to_action                          — EACH CARD has its own CTA
caption, description, name              — copy
image_hash, video_id                    — media
ig_media_id                             — pull from IG instead of upload
image_crops                             — per-card cropping
marketing_message_buttons               — structured buttons (CTWA flows)
place_data                              — location data per card
picture                                 — picture URL (alt to image_hash)
static_card                             — non-clickable static card flag
```

**Production implication:** Our carousel validator only checks `child_attachments` length (2-10). Should also validate per-card schema (each needs `link` + `image_hash`/`video_id`/`ig_media_id`).

---

## A15. Required Combinations Per Creative Mode (Verified)

**Source:** Combined SDK ground truth + Meta rendered docs + JSON examples.

### Inline link ad
```
object_story_spec.page_id (required)
object_story_spec.link_data {
  link (required)
  message (required)
  name (recommended, headline)
  image_hash OR picture (required - one of)
  call_to_action {
    type: <one of 101 CTA values>
    value.link: must match link_data.link
  }
}
```

### Video ad
```
object_story_spec.page_id (required)
object_story_spec.video_data {
  video_id (required - must be pre-uploaded)
  image_url OR image_hash (required - thumbnail)
  title, message (required)
  call_to_action (required)
}
```

### Carousel
```
object_story_spec.page_id (required)
object_story_spec.link_data {
  link (required - top-level fallback)
  message (required)
  multi_share_optimized: true (default; required for 10 cards)
  format_option: "carousel_images_multi_items" (default)
  child_attachments: [
    { link, image_hash or video_id, name, call_to_action } × 2-10
  ]
}
```

### Lead form
```
object_story_spec.page_id (required)
object_story_spec.link_data {
  link: "https://fb.me/<LEADGEN_FORM_ID>" (cosmetic - actual form routed by value)
  message, name (required)
  call_to_action {
    type: SIGN_UP | LEARN_MORE | GET_QUOTE | APPLY_NOW | SUBSCRIBE
    value.lead_gen_form_id (required - this is the actual wiring)
  }
}
Campaign objective: OUTCOME_LEADS
Adset destination_type: ON_AD
```

### WhatsApp click-to-message
```
object_story_spec.page_id (required - Page must be WA-linked)
object_story_spec.link_data {
  link: "https://wa.me/<E164_NUMBER>"
  message, name (required)
  call_to_action {
    type: WHATSAPP_MESSAGE (required)
    value: { app_destination: "WHATSAPP", link: "https://wa.me/<E164_NUMBER>" }
  }
}
Adset destination_type: WHATSAPP
NO pixel attach
```

### Existing post (simplest)
```
object_story_id: "<page_id>_<post_id>"
```
Optional: `instagram_user_id` if you want override for IG run.
NOTE: `page_id` NOT required at top level when using `object_story_id`.

---

## A16. New Discoveries → Production Action Items

### Critical (P0)

1. **Validate `regional_regulation_disclaimer_spec` for India/Singapore/Taiwan/Australia** — currently 0% coverage. Production risk for finserv/Singapore/Taiwan briefs.

2. **Handle `POST_DELETED` and `PRIVACY_CHECK_FAIL` sentinels** — when fetching ads, these `object_type` values indicate terminal failure. Agent should not retry.

3. **Add `format_option` allowed-values validator** — currently we let any string through.

### Important (P1)

4. **Document `auto_update`, `use_page_actor_override`, `applink_treatment`** in our ad creation flow — currently silent on these. Need explicit handling.

5. **Support `platform_customizations.instagram`** for FB vs IG creative variants — needed when user wants different image per platform.

6. **Validate URL field hierarchy** — `link_url` vs `object_url` vs `template_url` vs `template_url_spec` — currently treated as interchangeable; they aren't.

7. **Lead form CTA enforcement** — confirm we set `value.lead_gen_form_id` not `value.link` for lead forms (community confirms latter fails).

### Nice-to-have (P2)

8. Surface `creative_sourcing_spec` for Advantage+ AI authoring (user gives URL, Meta builds creative).
9. Surface `omnichannel_link_spec` for cross-channel conversion ads.
10. Surface `dynamic_ad_voice` toggle (`dynamic` vs `story_owner`) for DPA campaigns.

---

## A17. Unverifiable Gaps (Open Questions)

From the deep-dive — these need UI verification or real Meta API testing:

1. Exact error wording when both `object_story_id` and `object_story_spec` are sent (only community fragments)
2. Per-region sub-schema for `regional_regulation_disclaimer_spec.*` (Meta page gated; codegen types as opaque `Object`)
3. `creative_sourcing_spec.pca_spec` / `intent` / `brand` inner schemas (opaque)
4. `media_sourcing_spec` per-element schemas (opaque)
5. `contextual_multi_ads.enroll_status` allowed values (no enum surfaced)
6. `portrait_customizations.specifications` per-element schema (opaque)

These can be resolved via:
- Synthetic `?_validate=true` testing
- UI screenshot inspection
- Wayback Machine when reachable

---

## A18. Updated Confidence Assessment

After this deep-dive, our overall knowledge confidence shifts:

| Layer | Before Session 2 | After Session 2 |
|-------|-----------------|------------------|
| Enum values exist | 100% | 100% |
| AdCreative field surface | ~25% covered | **~75% covered** |
| Required combinations per mode | 60% | **85%** |
| URL field semantics | 30% | **80%** |
| Behavior flag semantics | 20% | **75%** |
| Regulatory disclaimer requirements | 0% | **70%** (5 regions identified) |
| Advantage+ AI spec coverage | 5% | **60%** |

**Overall confidence: 75% → ~85%** on creative-side knowledge.

---

**End of Addendum — Session 2 (2026-05-23)**

Next research priority: Resolve the 6 opaque `Object` schemas via Wayback Machine + synthetic API testing.

