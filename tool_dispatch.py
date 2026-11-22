"""
Tool dispatch: run a single tool by name with (args, token). Returns data dict or raises ValueError.
Used by views.handle_mcp_request_from_body for HTTP, SSE, stdio.

Production features:
- Idempotency: create tools cache results to prevent duplicates
- Dry-run: create/update/delete tools support dry_run=true to preview without executing
- Token resolution: auto-resolve from DB via user_id when arguments.token is absent
- Audit logging: all tool calls are logged with redacted args
"""
import json
import logging
from . import services
from .production_flow import strict_mode_from_document

logger = logging.getLogger("facebook_ads_mcp")


def _truthy_arg(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


def _should_skip_idempotency(name: str, args: dict) -> bool:
    """Skip idempotency cache when document strict_mode is on, or tools pass strict / skip_idempotency."""
    if _truthy_arg(args.get("skip_idempotency")) or _truthy_arg(args.get("skip_idempotency_cache")):
        return True
    if _truthy_arg(args.get("strict")):
        return True
    if name in ("facebook_sync_document_campaign", "facebook_preview_document_campaign"):
        doc = args.get("document")
        if isinstance(doc, str):
            try:
                doc = json.loads(doc)
            except (TypeError, ValueError):
                return False
        if isinstance(doc, dict) and strict_mode_from_document(doc):
            return True
    return False

# Tools that should be wrapped with idempotency caching
_IDEMPOTENT_TOOLS = frozenset({
    "facebook_create_campaign",
    "facebook_create_adset",
    "facebook_create_ad",
    "facebook_create_creative",
    "facebook_create_lead_form",
    "facebook_sync_document_campaign",
    "facebook_sync_document_campaigns_batch",
})

# Tools that support dry_run mode
_DRY_RUN_TOOLS = frozenset({
    "facebook_create_campaign",
    "facebook_create_adset",
    "facebook_create_ad",
    "facebook_create_creative",
    "facebook_update_campaign",
    "facebook_update_adset",
    "facebook_update_ad",
    "facebook_update_budget",
    "facebook_pause_campaign",
    "facebook_resume_campaign",
    "facebook_delete_campaign",
    "facebook_pause_adset",
    "facebook_resume_adset",
    "facebook_delete_adset",
    "facebook_pause_ad",
    "facebook_resume_ad",
    "facebook_delete_ad",
    "facebook_delete_creative",
    "facebook_auto_optimize",
})


def _strip(s):
    return (s or "").strip()


# Top-level ad set fields passed through to diff-based update (targeting is merged separately).
_ADSET_EXPLICIT_KEYS = frozenset({
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
    "promoted_object",
})


def _safe_args(args):
    """Return args dict with token and app secrets masked for logging."""
    safe = {
        k: v
        for k, v in args.items()
        if k not in ("token", "facebook_app_secret", "client_secret")
    }
    if "token" in args:
        safe["token"] = "***"
    if "facebook_app_secret" in args:
        safe["facebook_app_secret"] = "***"
    if "client_secret" in args:
        safe["client_secret"] = "***"
    return safe


def _is_dry_run(args):
    """Check if dry_run is set in args."""
    dr = args.get("dry_run")
    if dr is None:
        return False
    if isinstance(dr, bool):
        return dr
    if isinstance(dr, str):
        return dr.strip().lower() in ("true", "1", "yes")
    return bool(dr)


def _coerce_bool_arg(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes")
    return bool(val)


_MCP_AUTH_EXCLUDE_IDEMPOTENCY = frozenset(
    {
        "token",
        "dry_run",
        "user_id",
        "facebook_app_secret",
        "client_secret",
        "skip_token_refresh",
        "credential_user_id",
    }
)


def _build_idempotency_raw(name, args, token=""):
    """Build a stable string for idempotency hashing.

    The raw string is bound to the verified principal (``args['user_id']``,
    injected by the HTTP layer from the login JWT) and to the provider grant
    (hash of the access token actually used), so a cached create result can
    never be replayed to a different tenant or a different provider account
    even when the canonical arguments are identical.

    Excludes tokens / secrets / MCP-only auth fields, AND any loop-injected
    diagnostic field whose name starts with an underscore (e.g. ``_run_id``,
    ``_tool_call_id``, ``_idempotency_key``). Underscore-prefixed keys are
    a generic carrier the loop_agent uses for telemetry that must not affect
    intent-level dedup.
    """
    import hashlib
    filtered = {
        k: v
        for k, v in sorted(args.items())
        if k not in _MCP_AUTH_EXCLUDE_IDEMPOTENCY and not (isinstance(k, str) and k.startswith("_"))
    }
    principal = _strip(args.get("user_id"))
    grant = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest() if token else ""
    return json.dumps(
        {"principal": principal, "grant": grant, "tool": name, "arguments": filtered},
        sort_keys=True, default=str,
    )


def _dry_run_response(action, endpoint, payload, validation="passed"):
    """Build a dry-run preview response."""
    return {
        "dry_run": True,
        "action": action,
        "would_send": payload,
        "endpoint": endpoint,
        "validation": validation,
    }


def run_tool(name, args, token):
    """Run tool by name. Returns serializable data. Raises ValueError on validation error."""
    logger.info("tool_call: %s | args: %s", name, _safe_args(args))

    # Resolve token: arguments.token -> DB lookup -> settings fallback
    if not token and name != "facebook_send_conversion_event":
        # Try to resolve from user_id or settings
        user_id = _strip(args.get("user_id"))
        if user_id:
            try:
                from .auth_tokens import resolve_access_token
                token, meta = resolve_access_token(args)
                logger.info("Token resolved via %s for user_id=%s", meta.get("auth_source"), user_id)
            except ValueError:
                pass
        if not token:
            raise ValueError(
                "Token required: pass arguments.token (Facebook access token) or arguments.user_id (to use stored credentials). "
                "MCP calls Facebook API with this token."
            )

    account_id = _strip(args.get("account_id"))
    dry_run = _is_dry_run(args)

    # Idempotency check for create tools (skipped for document strict_mode or strict / skip_idempotency args)
    skip_idem = _should_skip_idempotency(name, args)
    if name in _IDEMPOTENT_TOOLS and not dry_run and not skip_idem:
        from .idempotency import get_cached_result
        raw = _build_idempotency_raw(name, args, token)
        cached = get_cached_result(name, raw)
        if cached is not None:
            logger.info("tool_call %s -> idempotency cache HIT", name)
            cached["_idempotent"] = True
            cached["_cached"] = True
            return cached

    # Execute tool
    result = _dispatch_tool(name, args, token, account_id, dry_run)

    # Save to idempotency cache on success
    if name in _IDEMPOTENT_TOOLS and not dry_run and not skip_idem:
        from .idempotency import save_success
        raw = _build_idempotency_raw(name, args, token)
        save_success(name, raw, result)

    return result


def _dispatch_tool(name, args, token, account_id, dry_run):
    """Core dispatch logic — routes tool name to service function."""

    # Quick combo reference (agent calls once at session start) —
    # no auth/token needed, returns compact JSON of valid combinations.
    if name == "facebook_get_combo_quickref":
        from . import meta_validation
        return meta_validation.get_combo_quickref()

    # GET / list
    if name == "facebook_get_accounts":
        return services.get_ad_accounts(token, limit=args.get("limit"))
    if name == "facebook_get_all_ad_accounts_from_facebook":
        return services.get_all_ad_accounts_from_facebook(token, limit=args.get("limit") or 500)
    if name == "facebook_get_businesses":
        return services.get_businesses(token, limit=args.get("limit"))
    if name == "facebook_get_pages":
        return services.get_pages(token, limit=args.get("limit"))
    if name == "facebook_resolve_account_for_page":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_resolve_account_for_page requires page_id")
        return services.resolve_account_for_page(pid, token)
    if name == "facebook_get_saved_audiences":
        if not account_id:
            raise ValueError("facebook_get_saved_audiences requires account_id")
        return services.get_saved_audiences(account_id, token, limit=args.get("limit"))
    if name == "facebook_get_custom_audiences":
        if not account_id:
            raise ValueError("facebook_get_custom_audiences requires account_id")
        return services.get_custom_audiences(account_id, token, limit=args.get("limit"))
    if name == "facebook_create_lookalike_audience":
        if not account_id:
            raise ValueError("facebook_create_lookalike_audience requires account_id")
        la_name = _strip(args.get("name"))
        origin = _strip(args.get("origin_audience_id"))
        country = _strip(args.get("country"))
        if not la_name:
            raise ValueError("facebook_create_lookalike_audience requires name")
        if not origin:
            raise ValueError("facebook_create_lookalike_audience requires origin_audience_id (seed custom audience id)")
        if not country:
            raise ValueError("facebook_create_lookalike_audience requires country (ISO-2 code)")
        if dry_run:
            return _dry_run_response(
                "create_lookalike_audience",
                f"POST /act_{account_id}/customaudiences",
                {"name": la_name, "origin_audience_id": origin, "country": country, "ratio": args.get("ratio") or 0.01},
            )
        return services.create_lookalike_audience(
            account_id, token,
            name=la_name,
            origin_audience_id=origin,
            country=country,
            ratio=args.get("ratio") if args.get("ratio") is not None else 0.01,
            description=_strip(args.get("description")) or None,
        )
    if name == "facebook_create_custom_audience":
        if not account_id:
            raise ValueError("facebook_create_custom_audience requires account_id")
        ca_name = _strip(args.get("name"))
        if not ca_name:
            raise ValueError("facebook_create_custom_audience requires name")
        if dry_run:
            return _dry_run_response(
                "create_custom_audience",
                f"POST /act_{account_id}/customaudiences",
                {"name": ca_name, "schema": args.get("schema"), "users_url": args.get("users_url")},
            )
        return services.create_custom_audience(
            account_id, token,
            name=ca_name,
            schema=_strip(args.get("schema")) or None,
            description=_strip(args.get("description")) or None,
            users_email=args.get("users_email"),
            users_phone=args.get("users_phone"),
            users_url=_strip(args.get("users_url")) or None,
            customer_file_source=_strip(args.get("customer_file_source")) or "USER_PROVIDED_ONLY",
        )
    if name == "facebook_get_lead_forms":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_get_lead_forms requires page_id")
        return services.get_lead_forms(pid, token, limit=args.get("limit"))
    if name == "facebook_get_political_disclaimers":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_get_political_disclaimers requires page_id")
        return services.get_political_disclaimers(pid, token, limit=args.get("limit"))
    if name == "facebook_get_page_posts":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_get_page_posts requires page_id")
        return services.get_page_posts(
            pid, token,
            fields=args.get("fields"),
            caption_query=args.get("caption_query"),
        )
    if name == "facebook_get_pixels":
        if not account_id:
            raise ValueError("facebook_get_pixels requires account_id")
        return services.get_pixels(account_id, token, limit=args.get("limit"))
    if name == "facebook_get_insights":
        if not account_id:
            raise ValueError("facebook_get_insights requires arguments.account_id")
        date_start, date_end = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_insights(
            account_id, token,
            date_preset=args.get("date_preset") or "last_30d",
            limit=args.get("limit"),
            time_start=date_start if date_start and date_end else None,
            time_end=date_end if date_start and date_end else None,
            breakdowns=args.get("breakdowns"),
        )
    if name == "facebook_get_campaigns":
        if not account_id:
            raise ValueError("facebook_get_campaigns requires arguments.account_id")
        return services.get_campaigns(account_id, token, limit=args.get("limit"))
    if name == "facebook_get_ads":
        if not account_id:
            raise ValueError("facebook_get_ads requires arguments.account_id")
        return services.get_ads(
            account_id,
            token,
            fields=args.get("fields"),
            limit=args.get("limit"),
            filtering=args.get("filtering"),
        )
    if name == "facebook_get_ad_sets":
        if not account_id:
            raise ValueError("facebook_get_ad_sets requires arguments.account_id")
        return services.get_ad_sets(account_id, token, limit=args.get("limit"))
    if name == "facebook_get_spend":
        if not account_id:
            raise ValueError("facebook_get_spend requires arguments.account_id")
        date_start, date_end = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_spend(
            account_id, token,
            date_preset=args.get("date_preset") or "last_30d",
            time_start=date_start if date_start and date_end else None,
            time_end=date_end if date_start and date_end else None,
        )

    # Campaign CRUD
    if name == "facebook_create_campaign":
        if not account_id:
            raise ValueError("facebook_create_campaign requires account_id")
        is_budget = args.get("is_adset_budget_sharing_enabled")
        if is_budget is None:
            is_budget = False
        elif isinstance(is_budget, str):
            is_budget = is_budget.strip().lower() in ("true", "1", "yes")
        if dry_run:
            return _dry_run_response("create_campaign", f"POST /act_{account_id}/campaigns", {
                "name": args.get("name") or "",
                "objective": args.get("objective") or "",
                "status": args.get("status") or "PAUSED",
                "special_ad_categories": args.get("special_ad_categories"),
                "special_ad_category_country": args.get("special_ad_category_country"),
                "is_adset_budget_sharing_enabled": bool(is_budget),
                "daily_budget": args.get("daily_budget"),
                "lifetime_budget": args.get("lifetime_budget"),
                "bid_strategy": _strip(args.get("bid_strategy")),
            })
        return services.create_campaign(
            account_id, token,
            name=args.get("name") or "",
            objective=args.get("objective") or "",
            status=args.get("status") or "PAUSED",
            special_ad_categories=args.get("special_ad_categories"),
            special_ad_category_country=args.get("special_ad_category_country"),
            is_adset_budget_sharing_enabled=bool(is_budget),
            daily_budget=args.get("daily_budget"),
            lifetime_budget=args.get("lifetime_budget"),
            bid_strategy=_strip(args.get("bid_strategy")),
        )
    if name == "facebook_update_campaign":
        cid = _strip(args.get("campaign_id"))
        if not cid:
            raise ValueError("facebook_update_campaign requires campaign_id")
        if dry_run:
            return _dry_run_response("update_campaign", f"POST /{cid}", {
                "name": args.get("name"), "status": args.get("status"),
                "daily_budget": args.get("daily_budget"), "lifetime_budget": args.get("lifetime_budget"),
                "bid_strategy": _strip(args.get("bid_strategy")),
                "objective": _strip(args.get("objective")),
                "skip_objective_when_adsets_exist": (
                    True if args.get("skip_objective_when_adsets_exist") is None
                    else _coerce_bool_arg(args.get("skip_objective_when_adsets_exist"))
                ),
            })
        ski = args.get("skip_objective_when_adsets_exist")
        if ski is None:
            skip_obj = True
        else:
            skip_obj = _coerce_bool_arg(ski)
        return services.update_campaign(
            cid, token,
            name=args.get("name"), status=args.get("status"),
            daily_budget=args.get("daily_budget"), lifetime_budget=args.get("lifetime_budget"),
            bid_strategy=_strip(args.get("bid_strategy")),
            objective=_strip(args.get("objective")),
            skip_objective_when_adsets_exist=skip_obj,
        )
    if name == "facebook_pause_campaign":
        cid = _strip(args.get("campaign_id"))
        if not cid:
            raise ValueError("facebook_pause_campaign requires campaign_id")
        if dry_run:
            return _dry_run_response("pause_campaign", f"POST /{cid}", {"status": "PAUSED"})
        return services.pause_campaign(cid, token)
    if name == "facebook_resume_campaign":
        cid = _strip(args.get("campaign_id"))
        if not cid:
            raise ValueError("facebook_resume_campaign requires campaign_id")
        if dry_run:
            return _dry_run_response("resume_campaign", f"POST /{cid}", {"status": "ACTIVE"})
        return services.resume_campaign(cid, token)
    if name == "facebook_delete_campaign":
        cid = _strip(args.get("campaign_id"))
        if not cid:
            raise ValueError("facebook_delete_campaign requires campaign_id")
        if dry_run:
            return _dry_run_response("delete_campaign", f"DELETE /{cid}", {})
        return services.delete_campaign(cid, token)
    if name == "facebook_get_campaign_insights":
        cid = _strip(args.get("campaign_id"))
        if not cid:
            raise ValueError("facebook_get_campaign_insights requires campaign_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_campaign_insights(
            cid, token,
            date_preset=args.get("date_preset") or "lifetime",
            fields=args.get("fields"),
            time_start=ds if ds and de else None,
            time_end=de if ds and de else None,
            breakdowns=args.get("breakdowns"),
        )

    # Targeting
    if name in ("facebook_targeting_search", "facebook_search_targeting"):
        st = _strip(args.get("search_type"))
        if not st:
            raise ValueError("facebook_targeting_search requires search_type (adgeolocation, adinterest, adTargetingCategory)")
        if account_id:
            return services.targeting_search_with_fallback(
                account_id, token,
                search_type=st,
                q=args.get("q"),
                location_types=args.get("location_types"),
                country_code=args.get("country_code"),
                region_id=args.get("region_id"),
                limit=args.get("limit"),
            )
        return services.targeting_search(
            token,
            search_type=st,
            q=args.get("q"),
            location_types=args.get("location_types"),
            country_code=args.get("country_code"),
            region_id=args.get("region_id"),
            limit=args.get("limit"),
        )
    if name == "facebook_batch_interest_search":
        if not account_id:
            raise ValueError("facebook_batch_interest_search requires account_id")
        names_list = args.get("interest_names")
        if isinstance(names_list, str):
            try:
                names_list = json.loads(names_list)
            except (TypeError, ValueError):
                names_list = [s.strip() for s in names_list.split(",") if s.strip()]
        if not isinstance(names_list, list) or not names_list:
            raise ValueError("facebook_batch_interest_search requires interest_names (array of strings)")
        return services.batch_interest_search(account_id, token, names_list)
    if name == "facebook_batch_targeting_category_search":
        if not account_id:
            raise ValueError("facebook_batch_targeting_category_search requires account_id")
        names_list = args.get("category_names")
        if isinstance(names_list, str):
            try:
                names_list = json.loads(names_list)
            except (TypeError, ValueError):
                names_list = [s.strip() for s in names_list.split(",") if s.strip()]
        if not isinstance(names_list, list) or not names_list:
            raise ValueError("facebook_batch_targeting_category_search requires category_names (array of strings)")
        return services.batch_targeting_category_search(account_id, token, names_list)
    if name == "facebook_attach_pixel":
        aid = _strip(args.get("adset_id"))
        if not aid:
            raise ValueError("facebook_attach_pixel requires adset_id")
        pid = _strip(args.get("pixel_id"))
        if not pid:
            raise ValueError("facebook_attach_pixel requires pixel_id")
        return services.attach_pixel_to_adset(
            aid, token, pid,
            custom_event_type=_strip(args.get("custom_event_type")),
            page_id=_strip(args.get("page_id")),
        )
    if name == "facebook_verify_ad_setup":
        exp = args.get("expected")
        if isinstance(exp, str) and exp.strip():
            try:
                exp = json.loads(exp)
            except (TypeError, ValueError):
                exp = None
        if exp is not None and not isinstance(exp, dict):
            exp = None
        return services.verify_ad_setup(
            token,
            campaign_id=_strip(args.get("campaign_id")) or None,
            adset_id=_strip(args.get("adset_id")) or None,
            ad_id=_strip(args.get("ad_id")) or None,
            expected=exp,
        )
    if name == "facebook_preview_document_campaign":
        if not account_id:
            raise ValueError("facebook_preview_document_campaign requires account_id")
        doc = args.get("document")
        if doc is None:
            raise ValueError("facebook_preview_document_campaign requires document (JSON object or string)")
        if dry_run:
            document = doc if isinstance(doc, dict) else json.loads(doc) if isinstance(doc, str) else doc
            return _dry_run_response("preview_document_campaign", "validate + targeting resolution only (no Meta writes)", {
                "document": document,
                "account_id": account_id,
            })
        from . import production_flow
        return production_flow.preview_document_campaign(
            account_id, token, doc, user_id=_strip(args.get("user_id")) or "",
        )

    if name == "facebook_sync_document_campaign":
        if not account_id:
            raise ValueError("facebook_sync_document_campaign requires account_id")
        doc = args.get("document")
        if doc is None:
            raise ValueError("facebook_sync_document_campaign requires document (JSON object or string)")
        if dry_run:
            document = doc if isinstance(doc, dict) else json.loads(doc) if isinstance(doc, str) else doc
            return _dry_run_response("sync_document_campaign", f"POST /act_{account_id}/campaigns (multi-step)", {
                "document": document,
                "account_id": account_id,
            })
        from . import production_flow
        user_confirmed = _coerce_bool_arg(args.get("user_confirmed"))
        return production_flow.sync_document_campaign(
            account_id,
            token,
            doc,
            user_id=_strip(args.get("user_id")) or "",
            user_confirmed=user_confirmed,
        )

    if name == "facebook_sync_document_campaigns_batch":
        if not account_id:
            raise ValueError("facebook_sync_document_campaigns_batch requires account_id")
        doc = args.get("document")
        if doc is None:
            raise ValueError("facebook_sync_document_campaigns_batch requires document (the shared template)")
        posts = args.get("posts")
        if posts is None:
            raise ValueError("facebook_sync_document_campaigns_batch requires posts (JSON array, one item per post)")
        if dry_run:
            document = doc if isinstance(doc, dict) else json.loads(doc) if isinstance(doc, str) else doc
            return _dry_run_response("sync_document_campaigns_batch", f"POST /act_{account_id}/campaigns × len(posts) (multi-step)", {
                "document": document,
                "posts": posts,
                "account_id": account_id,
            })
        from . import production_flow
        return production_flow.sync_document_campaigns_batch(
            account_id,
            token,
            doc,
            posts,
            user_id=_strip(args.get("user_id")) or "",
            user_confirmed=_coerce_bool_arg(args.get("user_confirmed")),
            time_budget_seconds=args.get("time_budget_seconds"),
        )

    # Instagram accounts
    if name == "facebook_get_instagram_accounts":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_get_instagram_accounts requires page_id")
        return services.get_instagram_accounts(pid, token)
    if name == "facebook_get_page_backed_instagram_accounts":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_get_page_backed_instagram_accounts requires page_id")
        return services.get_page_backed_instagram_accounts(pid, token)
    if name == "facebook_get_instagram_media":
        pid = _strip(args.get("page_id"))
        iuid = _strip(args.get("instagram_user_id"))
        if not pid and not iuid:
            raise ValueError("facebook_get_instagram_media requires page_id or instagram_user_id")
        return services.get_instagram_media(
            token,
            page_id=pid,
            instagram_user_id=iuid,
            fields=args.get("fields"),
            caption_query=args.get("caption_query"),
        )
    if name == "facebook_get_post_insights":
        pid = _strip(args.get("page_id"))
        iuid = _strip(args.get("instagram_user_id"))
        post_ids = args.get("post_ids")
        if not pid and not iuid and not post_ids:
            raise ValueError("facebook_get_post_insights requires page_id, instagram_user_id or post_ids")
        limit = args.get("limit")
        try:
            limit = int(limit) if limit is not None else None
        except (TypeError, ValueError):
            limit = None
        return services.get_post_insights(
            token,
            page_id=pid,
            instagram_user_id=iuid,
            platform=_strip(args.get("platform")) or "instagram",
            post_ids=post_ids,
            metrics=args.get("metrics"),
            fields=args.get("fields"),
            where=args.get("where"),
            sort=_strip(args.get("sort")) or None,
            limit=limit,
        )

    # Lead Form
    if name == "facebook_create_lead_form":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_create_lead_form requires page_id")
        q = args.get("questions")
        if isinstance(q, str):
            try:
                q = json.loads(q)
            except (TypeError, ValueError):
                pass
        if dry_run:
            return _dry_run_response("create_lead_form", f"POST /{pid}/leadgen_forms", {
                "name": args.get("name") or "Lead Form",
                "questions": q,
                "privacy_policy_url": args.get("privacy_policy_url"),
            })
        return services.create_lead_form(
            pid, token,
            name=args.get("name") or "Lead Form",
            questions=q,
            privacy_policy_url=args.get("privacy_policy_url"),
            context_card_title=args.get("context_card_title"),
            context_card_body=args.get("context_card_body"),
            thank_you_title=args.get("thank_you_title"),
            thank_you_body=args.get("thank_you_body"),
            follow_up_url=args.get("follow_up_url"),
        )

    # Reach Estimate
    if name == "facebook_get_reach_estimate":
        if not account_id:
            raise ValueError("facebook_get_reach_estimate requires account_id")
        ts = args.get("targeting_spec")
        if not ts:
            raise ValueError("facebook_get_reach_estimate requires targeting_spec (JSON)")
        return services.get_reach_estimate(account_id, token, targeting_spec=ts, optimization_goal=args.get("optimization_goal"))

    # Ad set CRUD
    if name == "facebook_create_adset":
        if not account_id:
            raise ValueError("facebook_create_adset requires account_id")
        targeting_arg = args.get("targeting")
        has_structured = any(
            args.get(k) is not None and (args.get(k) if not isinstance(args.get(k), str) else args.get(k).strip())
            for k in (
                "geo_countries", "geo_country_groups", "geo_regions", "geo_cities", "geo_custom_locations",
                "geo_radius_km", "geo_location_types", "age_min", "age_max", "genders",
                "flexible_spec", "custom_audiences", "excluded_geo_locations", "excluded_custom_audiences",
            )
        )
        if (
            has_structured
            or args.get("publisher_platforms")
            or args.get("device_platforms")
            or args.get("facebook_positions")
            or args.get("instagram_positions")
            or args.get("messenger_positions")
            or args.get("audience_network_positions")
            or args.get("threads_positions")
            or args.get("user_os")
        ):
            spec = services.build_targeting_spec(
                geo_countries=args.get("geo_countries"),
                geo_regions=args.get("geo_regions"),
                geo_cities=args.get("geo_cities"),
                geo_custom_locations=args.get("geo_custom_locations"),
                geo_country_groups=args.get("geo_country_groups"),
                geo_location_types=args.get("geo_location_types"),
                geo_radius_km=args.get("geo_radius_km"),
                age_min=args.get("age_min"),
                age_max=args.get("age_max"),
                genders=args.get("genders"),
                flexible_spec=args.get("flexible_spec"),
                custom_audiences=args.get("custom_audiences"),
                excluded_geo_locations=args.get("excluded_geo_locations"),
                excluded_custom_audiences=args.get("excluded_custom_audiences"),
                targeting_override=targeting_arg,
                publisher_platforms=args.get("publisher_platforms"),
                device_platforms=args.get("device_platforms"),
                facebook_positions=args.get("facebook_positions"),
                instagram_positions=args.get("instagram_positions"),
                messenger_positions=args.get("messenger_positions"),
                audience_network_positions=args.get("audience_network_positions"),
                threads_positions=args.get("threads_positions"),
                user_os=args.get("user_os"),
            )
            targeting_arg = json.dumps(spec) if spec else targeting_arg
        if dry_run:
            return _dry_run_response("create_adset", f"POST /act_{account_id}/adsets", {
                "campaign_id": args.get("campaign_id") or "",
                "name": args.get("name") or "",
                "status": args.get("status") or "PAUSED",
                "daily_budget": args.get("daily_budget"),
                "lifetime_budget": args.get("lifetime_budget"),
                "billing_event": args.get("billing_event") or "IMPRESSIONS",
                "optimization_goal": args.get("optimization_goal"),
                "targeting": targeting_arg,
                "bid_strategy": args.get("bid_strategy"),
            })
        # Some (objective, goal) pairs REQUIRE an explicit destination_type —
        # Meta rejects an absent one with subcode 2490408 (e.g. THRUPLAY under
        # OUTCOME_ENGAGEMENT needs ON_VIDEO). services.create_adset doesn't
        # know the campaign objective, so resolve it here. The campaign GET
        # only fires for goals that have a single-destination no-UNDEFINED
        # entry in the matrix — the common path pays nothing.
        destination_arg = args.get("destination_type")
        _og_arg = str(args.get("optimization_goal") or "").strip().upper()
        if not destination_arg and _og_arg:
            try:
                from .meta_validation import (
                    _OBJECTIVE_GOAL_DESTINATIONS,
                    required_destination_for,
                )
                goal_may_require = any(
                    og == _og_arg and "UNDEFINED" not in dests and len(dests) == 1
                    for (_obj, og), dests in _OBJECTIVE_GOAL_DESTINATIONS.items()
                )
                if goal_may_require:
                    camp = services.get_marketing_object(
                        str(args.get("campaign_id") or "").strip(), token, "objective"
                    )
                    destination_arg = required_destination_for(
                        (camp or {}).get("objective"), _og_arg
                    )
            except Exception:
                destination_arg = args.get("destination_type")
        return services.create_adset(
            account_id, token,
            campaign_id=args.get("campaign_id") or "",
            name=args.get("name") or "",
            status=args.get("status") or "PAUSED",
            daily_budget=args.get("daily_budget"),
            lifetime_budget=args.get("lifetime_budget"),
            billing_event=args.get("billing_event") or "IMPRESSIONS",
            optimization_goal=args.get("optimization_goal"),
            targeting=targeting_arg,
            bid_strategy=args.get("bid_strategy"),
            bid_amount=args.get("bid_amount"),
            bid_constraints=args.get("bid_constraints"),
            promoted_object=args.get("promoted_object"),
            page_id=args.get("page_id"),
            destination_type=destination_arg,
            start_time=args.get("start_time"),
            end_time=args.get("end_time"),
            is_dynamic_creative=args.get("is_dynamic_creative"),
            product_set_id=_strip(args.get("product_set_id")) or None,
        )
    if name == "facebook_update_adset":
        aid = _strip(args.get("adset_id"))
        if not aid:
            raise ValueError("facebook_update_adset requires adset_id")
        saved_audience = _strip(args.get("saved_audience_id"))
        if saved_audience:
            rp = args.get("replace_placements")
            if isinstance(rp, str):
                preserve = rp.strip().lower() not in ("true", "1", "yes")
            else:
                preserve = not bool(rp)
            if dry_run:
                return _dry_run_response(
                    "sync_adset_saved_audience",
                    f"POST /{aid}",
                    {"saved_audience_id": saved_audience, "preserve_placements": preserve},
                )
            po_sa = args.get("promoted_object")
            if isinstance(po_sa, str) and po_sa.strip():
                try:
                    po_sa = json.loads(po_sa)
                except (TypeError, ValueError):
                    pass
            return services.sync_adset_targeting_from_saved_audience(
                aid,
                saved_audience,
                token,
                preserve_placements=preserve,
                name=args.get("name"),
                status=args.get("status"),
                daily_budget=args.get("daily_budget"),
                lifetime_budget=args.get("lifetime_budget"),
                start_time=_strip(args.get("start_time")),
                end_time=_strip(args.get("end_time")),
                optimization_goal=_strip(args.get("optimization_goal")),
                bid_strategy=_strip(args.get("bid_strategy")),
                destination_type=_strip(args.get("destination_type")),
                billing_event=_strip(args.get("billing_event")),
                promoted_object=po_sa,
                publisher_platforms=args.get("publisher_platforms"),
                device_platforms=args.get("device_platforms"),
                facebook_positions=args.get("facebook_positions"),
                instagram_positions=args.get("instagram_positions"),
                messenger_positions=args.get("messenger_positions"),
                audience_network_positions=args.get("audience_network_positions"),
                threads_positions=args.get("threads_positions"),
                advantage_audience=args.get("advantage_audience"),
            )
        preset = _strip(args.get("geo_cities_preset"))
        if preset:
            if dry_run:
                return _dry_run_response(
                    "update_adset_geo_preset",
                    f"POST /{aid}",
                    {"geo_cities_preset": preset, "cities": services.GEO_CITIES_PRESETS.get(preset, [])},
                )
            return services.update_adset_replace_geo_cities_from_preset(
                aid,
                token,
                preset_key=preset,
                name=args.get("name"),
                status=args.get("status"),
                daily_budget=args.get("daily_budget"),
                lifetime_budget=args.get("lifetime_budget"),
                bid_strategy=_strip(args.get("bid_strategy")),
                optimization_goal=_strip(args.get("optimization_goal")),
                destination_type=_strip(args.get("destination_type")),
                billing_event=_strip(args.get("billing_event")),
            )
        targ = args.get("targeting")
        if isinstance(targ, str) and targ.strip():
            try:
                targ = json.loads(targ)
            except (TypeError, ValueError):
                pass
        targ_override = targ if isinstance(targ, dict) else None
        has_structured = any(
            args.get(k) is not None and (args.get(k) if not isinstance(args.get(k), str) else args.get(k).strip())
            for k in (
                "geo_countries", "geo_country_groups", "geo_regions", "geo_cities", "geo_custom_locations",
                "geo_radius_km", "geo_location_types", "age_min", "age_max", "genders",
                "flexible_spec", "custom_audiences", "excluded_geo_locations", "excluded_custom_audiences",
            )
        )
        wants_placement_update = any(
            args.get(k) is not None
            for k in (
                "publisher_platforms",
                "device_platforms",
                "facebook_positions",
                "instagram_positions",
                "messenger_positions",
                "audience_network_positions",
                "threads_positions",
                "user_os",
            )
        )
        if wants_placement_update and not has_structured and targ_override is None:
            cur = services.get_marketing_object(aid, token, "targeting")
            if isinstance(cur, dict) and "error" not in cur:
                tgot = cur.get("targeting")
                if isinstance(tgot, dict) and tgot:
                    targ_override = dict(tgot)
        spec = None
        spec_built = False
        if (
            has_structured
            or args.get("publisher_platforms")
            or args.get("device_platforms")
            or args.get("facebook_positions")
            or args.get("instagram_positions")
            or args.get("messenger_positions")
            or args.get("audience_network_positions")
            or args.get("threads_positions")
        ):
            spec = services.build_targeting_spec(
                geo_countries=args.get("geo_countries"),
                geo_regions=args.get("geo_regions"),
                geo_cities=args.get("geo_cities"),
                geo_custom_locations=args.get("geo_custom_locations"),
                geo_country_groups=args.get("geo_country_groups"),
                geo_location_types=args.get("geo_location_types"),
                geo_radius_km=args.get("geo_radius_km"),
                age_min=args.get("age_min"),
                age_max=args.get("age_max"),
                genders=args.get("genders"),
                flexible_spec=args.get("flexible_spec"),
                custom_audiences=args.get("custom_audiences"),
                excluded_geo_locations=args.get("excluded_geo_locations"),
                excluded_custom_audiences=args.get("excluded_custom_audiences"),
                targeting_override=targ_override,
                publisher_platforms=args.get("publisher_platforms"),
                device_platforms=args.get("device_platforms"),
                facebook_positions=args.get("facebook_positions"),
                instagram_positions=args.get("instagram_positions"),
                messenger_positions=args.get("messenger_positions"),
                audience_network_positions=args.get("audience_network_positions"),
                threads_positions=args.get("threads_positions"),
                user_os=args.get("user_os"),
            )
            if args.get("advantage_audience") is not None:
                try:
                    aa = int(args["advantage_audience"])
                    ta = {}
                    if isinstance(spec.get("targeting_automation"), dict):
                        ta = dict(spec["targeting_automation"])
                    ta["advantage_audience"] = aa
                    spec["targeting_automation"] = ta
                except (TypeError, ValueError):
                    pass
            spec_built = True

        merged_targeting = None
        if spec_built and isinstance(spec, dict) and spec:
            merged_targeting = spec
        elif "targeting" in args and isinstance(targ, dict) and targ:
            merged_targeting = targ

        # advantage_audience alone (no placement/geo args): fetch current targeting and merge automation only
        if merged_targeting is None and args.get("advantage_audience") is not None:
            cur = services.get_marketing_object(aid, token, "targeting")
            if isinstance(cur, dict) and "error" not in cur:
                tgot = cur.get("targeting")
                if isinstance(tgot, dict) and tgot:
                    merged_targeting = dict(tgot)
                elif isinstance(tgot, str) and tgot.strip():
                    try:
                        merged_targeting = dict(json.loads(tgot))
                    except (TypeError, ValueError):
                        merged_targeting = None

        if args.get("advantage_audience") is not None and isinstance(merged_targeting, dict):
            try:
                aa = int(args["advantage_audience"])
                ta = dict(merged_targeting.get("targeting_automation") or {})
                ta["advantage_audience"] = aa
                merged_targeting["targeting_automation"] = ta
            except (TypeError, ValueError):
                pass

        explicit_request = {}
        for k in _ADSET_EXPLICIT_KEYS:
            if k not in args:
                continue
            if k == "promoted_object":
                pv = args[k]
                if isinstance(pv, str) and pv.strip():
                    try:
                        explicit_request[k] = json.loads(pv)
                    except (TypeError, ValueError):
                        explicit_request[k] = pv
                else:
                    explicit_request[k] = pv
            elif k in ("optimization_goal", "destination_type", "billing_event", "bid_strategy"):
                explicit_request[k] = _strip(args[k]) if args[k] is not None else args[k]
            elif k in ("start_time", "end_time"):
                explicit_request[k] = _strip(args[k]) if args[k] is not None else args[k]
            else:
                explicit_request[k] = args[k]

        force_up = _truthy_arg(args.get("force_update"))
        if dry_run:
            plan = services.update_adset_with_smart_diff(
                aid,
                token,
                explicit_request,
                merged_targeting=merged_targeting,
                dry_run=True,
                force_update=force_up,
            )
            legacy = {
                "explicit_request": explicit_request,
                "merged_targeting": merged_targeting,
                "force_update": force_up,
            }
            out = _dry_run_response("update_adset", f"POST /{aid}", legacy, validation="passed")
            out["diff_plan"] = plan
            return out
        return services.update_adset_with_smart_diff(
            aid,
            token,
            explicit_request,
            merged_targeting=merged_targeting,
            dry_run=False,
            force_update=force_up,
        )
    if name == "facebook_pause_adset":
        aid = _strip(args.get("adset_id"))
        if not aid:
            raise ValueError("facebook_pause_adset requires adset_id")
        if dry_run:
            return _dry_run_response("pause_adset", f"POST /{aid}", {"status": "PAUSED"})
        return services.pause_adset(aid, token)
    if name == "facebook_resume_adset":
        aid = _strip(args.get("adset_id"))
        if not aid:
            raise ValueError("facebook_resume_adset requires adset_id")
        if dry_run:
            return _dry_run_response("resume_adset", f"POST /{aid}", {"status": "ACTIVE"})
        return services.resume_adset(aid, token)
    if name == "facebook_delete_adset":
        aid = _strip(args.get("adset_id"))
        if not aid:
            raise ValueError("facebook_delete_adset requires adset_id")
        if dry_run:
            return _dry_run_response("delete_adset", f"DELETE /{aid}", {})
        return services.delete_adset(aid, token)
    if name == "facebook_update_budget":
        aid = _strip(args.get("adset_id"))
        if not aid:
            raise ValueError("facebook_update_budget requires adset_id")
        if dry_run:
            return _dry_run_response("update_budget", f"POST /{aid}", {
                "daily_budget": args.get("daily_budget"), "lifetime_budget": args.get("lifetime_budget"),
            })
        return services.update_budget(aid, token, daily_budget=args.get("daily_budget"), lifetime_budget=args.get("lifetime_budget"))

    # Ads CRUD
    if name == "facebook_create_ad":
        if not account_id:
            raise ValueError("facebook_create_ad requires account_id")
        if dry_run:
            return _dry_run_response("create_ad", f"POST /act_{account_id}/ads", {
                "adset_id": args.get("adset_id") or "",
                "name": args.get("name") or "",
                "creative_id": args.get("creative_id"),
                "status": args.get("status") or "PAUSED",
            })
        # tracking_specs is JSON-stringified on the wire (the LLM sends a
        # string for any object-shaped property in our schema). Parse leniently
        # — if it's already a list, pass through.
        ts_raw = args.get("tracking_specs")
        ts: Any = None
        if isinstance(ts_raw, str) and ts_raw.strip():
            try:
                ts = json.loads(ts_raw)
            except (TypeError, ValueError):
                ts = None
        elif isinstance(ts_raw, (list, dict)):
            ts = ts_raw
        return services.create_ad(
            account_id,
            token,
            adset_id=args.get("adset_id") or "",
            name=args.get("name") or "",
            creative_id=args.get("creative_id"),
            status=args.get("status") or "PAUSED",
            tracking_specs=ts,
        )
    if name == "facebook_update_ad":
        adid = _strip(args.get("ad_id"))
        if not adid:
            raise ValueError("facebook_update_ad requires ad_id")
        explicit_ad = {}
        for k in ("name", "status", "adset_id", "creative_id", "tracking_specs"):
            if k in args:
                explicit_ad[k] = args[k]
        if "tracking_specs" in explicit_ad and isinstance(explicit_ad["tracking_specs"], str) and explicit_ad["tracking_specs"].strip():
            try:
                explicit_ad["tracking_specs"] = json.loads(explicit_ad["tracking_specs"])
            except (TypeError, ValueError):
                pass
        force_up_ad = _truthy_arg(args.get("force_update"))
        if dry_run:
            plan = services.update_ad_with_smart_diff(
                adid, token, explicit_ad, dry_run=True, force_update=force_up_ad,
            )
            out = _dry_run_response(
                "update_ad", f"POST /{adid}", {"explicit": explicit_ad, "force_update": force_up_ad}, validation="passed",
            )
            out["diff_plan"] = plan
            return out
        if explicit_ad:
            return services.update_ad_with_smart_diff(
                adid, token, explicit_ad, dry_run=False, force_update=force_up_ad,
            )
        return {"success": True, "message": "No changes needed", "changes_applied": [], "skipped": []}
    if name == "facebook_pause_ad":
        adid = _strip(args.get("ad_id"))
        if not adid:
            raise ValueError("facebook_pause_ad requires ad_id")
        if dry_run:
            return _dry_run_response("pause_ad", f"POST /{adid}", {"status": "PAUSED"})
        return services.pause_ad(adid, token)
    if name == "facebook_resume_ad":
        adid = _strip(args.get("ad_id"))
        if not adid:
            raise ValueError("facebook_resume_ad requires ad_id")
        if dry_run:
            return _dry_run_response("resume_ad", f"POST /{adid}", {"status": "ACTIVE"})
        return services.resume_ad(adid, token)
    if name == "facebook_delete_ad":
        adid = _strip(args.get("ad_id"))
        if not adid:
            raise ValueError("facebook_delete_ad requires ad_id")
        if dry_run:
            return _dry_run_response("delete_ad", f"DELETE /{adid}", {})
        return services.delete_ad(adid, token)
    if name == "facebook_get_ad_creatives":
        if not account_id:
            raise ValueError("facebook_get_ad_creatives requires account_id")
        return services.get_ad_creatives(account_id, token, fields=args.get("fields"), limit=args.get("limit"))

    # Reporting
    if name == "facebook_get_campaign_performance":
        if not account_id:
            raise ValueError("facebook_get_campaign_performance requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_campaign_performance(account_id, token, date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"), breakdowns=args.get("breakdowns"))
    if name == "facebook_get_adset_performance":
        if not account_id:
            raise ValueError("facebook_get_adset_performance requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_adset_performance(account_id, token, date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"), breakdowns=args.get("breakdowns"))
    if name == "facebook_get_ad_performance":
        if not account_id:
            raise ValueError("facebook_get_ad_performance requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_ad_performance(account_id, token, date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"), breakdowns=args.get("breakdowns"))
    if name == "facebook_get_ad_level_data_from_all_active_accounts":
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_ad_level_data_from_all_active_accounts(
            token,
            date_preset=args.get("date_preset") or "last_30d",
            time_start=ds if ds and de else None,
            time_end=de if ds and de else None,
            limit_per_account=args.get("limit_per_account") or 100,
        )
    if name == "facebook_get_clicks_impressions":
        if not account_id:
            raise ValueError("facebook_get_clicks_impressions requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_clicks_impressions(account_id, token, date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None)

    # Comparison & analysis
    if name == "facebook_compare_campaigns":
        if not account_id:
            raise ValueError("facebook_compare_campaigns requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.compare_campaigns(account_id, token, campaign_ids=args.get("campaign_ids"), date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"))
    if name == "facebook_compare_adsets":
        if not account_id:
            raise ValueError("facebook_compare_adsets requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.compare_adsets(account_id, token, adset_ids=args.get("adset_ids"), date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"))
    if name == "facebook_compare_ads":
        if not account_id:
            raise ValueError("facebook_compare_ads requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.compare_ads(account_id, token, ad_ids=args.get("ad_ids"), date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"))
    if name == "facebook_roas_analysis":
        if not account_id:
            raise ValueError("facebook_roas_analysis requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.roas_analysis(account_id, token, level=args.get("level") or "campaign", date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"))
    if name == "facebook_budget_recommendation":
        if not account_id:
            raise ValueError("facebook_budget_recommendation requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.budget_recommendation(account_id, token, level=args.get("level") or "campaign", date_preset=args.get("date_preset") or "last_30d", time_start=ds if ds and de else None, time_end=de if ds and de else None, limit=args.get("limit"))

    # Creative & media
    if name == "facebook_upload_image":
        if not account_id:
            raise ValueError("facebook_upload_image requires account_id")
        b64 = args.get("image_base64")
        if isinstance(b64, str) and b64.strip():
            return services.upload_image(
                account_id, token, image_base64=b64.strip(), filename=args.get("filename"),
            )
        url_arg = _strip(args.get("url"))
        if not url_arg:
            raise ValueError("facebook_upload_image requires url (public image URL) or image_base64")
        return services.upload_image(account_id, token, url_or_bytes=url_arg, filename=args.get("filename"))
    if name == "facebook_post_page_photo":
        page_id = _strip(args.get("page_id"))
        if not page_id:
            raise ValueError("facebook_post_page_photo requires page_id")
        link_arg = _strip(args.get("link"))
        url_arg = _strip(args.get("url"))
        picture_arg = _strip(args.get("picture"))
        message_arg = _strip(args.get("message"))
        b64_arg = (args.get("image_base64") or "").strip()
        fname_arg = _strip(args.get("filename"))
        # The chat-side attachment injector fills `url` from `x-image-url`, but
        # `/feed` (link-preview mode) reads `picture` instead. If the caller is
        # in feed-mode and didn't supply a separate `picture`, fall back to
        # whatever `url` was injected with so the image isn't silently dropped.
        if link_arg and not picture_arg and url_arg:
            picture_arg = url_arg
        # Symmetric: if photo-mode was intended but only `picture` got
        # injected, treat it as the photo url.
        if not link_arg and not url_arg and picture_arg:
            url_arg = picture_arg
        if link_arg:
            if not message_arg:
                raise ValueError("facebook_post_page_photo with link requires message")
            return services.post_page_feed(page_id, token, message=message_arg, link=link_arg, picture=picture_arg or None, name=args.get("name"), description=args.get("description"))
        # Photo-mode: bytes path wins over URL path when both are injected,
        # because the URL may point at a host (e.g. localhost) Facebook can't
        # reach. The bytes path uses multipart upload and always works.
        if b64_arg or url_arg:
            return services.post_page_photo(
                page_id, token,
                url=url_arg or None,
                caption=args.get("caption"),
                image_base64=b64_arg or None,
                filename=fname_arg,
            )
        raise ValueError("facebook_post_page_photo requires either url/image_base64 (for photo post) or message+link (for feed post with link preview)")
    if name == "facebook_post_page_video":
        page_id = _strip(args.get("page_id"))
        if not page_id:
            raise ValueError("facebook_post_page_video requires page_id")
        video_url = _strip(args.get("video_url"))
        if not video_url:
            raise ValueError("facebook_post_page_video requires video_url")
        return services.post_page_video(page_id, token, video_url=video_url, description=args.get("description"))
    # ---------- Instagram Content Publishing ----------
    if name in (
        "facebook_post_instagram_photo",
        "facebook_post_instagram_video",
        "facebook_post_instagram_reel",
        "facebook_post_instagram_story",
        "facebook_post_instagram_carousel",
    ):
        ig_user_id_arg = _strip(args.get("instagram_user_id"))
        page_id_arg = _strip(args.get("page_id"))
        if not ig_user_id_arg and not page_id_arg:
            raise ValueError(f"{name} requires instagram_user_id or page_id")
        ig_user_id, ig_err = services._resolve_ig_user_id(page_id_arg, ig_user_id_arg, token)
        if ig_err is not None:
            return ig_err
        if name == "facebook_post_instagram_photo":
            image_url = _strip(args.get("image_url"))
            if not image_url:
                raise ValueError("facebook_post_instagram_photo requires image_url")
            return services.post_instagram_photo(ig_user_id, token, image_url=image_url, caption=args.get("caption"))
        if name == "facebook_post_instagram_video":
            video_url = _strip(args.get("video_url"))
            if not video_url:
                raise ValueError("facebook_post_instagram_video requires video_url")
            return services.post_instagram_video(ig_user_id, token, video_url=video_url, caption=args.get("caption"), thumb_offset=args.get("thumb_offset"))
        if name == "facebook_post_instagram_reel":
            video_url = _strip(args.get("video_url"))
            if not video_url:
                raise ValueError("facebook_post_instagram_reel requires video_url")
            share_to_feed = args.get("share_to_feed")
            share_to_feed = True if share_to_feed is None else bool(share_to_feed)
            return services.post_instagram_reel(
                ig_user_id, token,
                video_url=video_url,
                caption=args.get("caption"),
                share_to_feed=share_to_feed,
                cover_url=_strip(args.get("cover_url")) or None,
                audio_name=_strip(args.get("audio_name")) or None,
            )
        if name == "facebook_post_instagram_story":
            image_url = _strip(args.get("image_url")) or None
            video_url = _strip(args.get("video_url")) or None
            if not image_url and not video_url:
                raise ValueError("facebook_post_instagram_story requires image_url or video_url")
            return services.post_instagram_story(ig_user_id, token, image_url=image_url, video_url=video_url)
        if name == "facebook_post_instagram_carousel":
            items = args.get("items")
            if not items:
                raise ValueError("facebook_post_instagram_carousel requires items (2-10 entries)")
            return services.post_instagram_carousel(ig_user_id, token, items=items, caption=args.get("caption"))
    if name == "facebook_upload_video":
        if not account_id:
            raise ValueError("facebook_upload_video requires account_id")
        video_url = _strip(args.get("video_url"))
        if not video_url:
            raise ValueError("facebook_upload_video requires video_url")
        return services.upload_video(account_id, token, video_url=video_url)
    if name == "facebook_create_creative":
        if not account_id:
            raise ValueError("facebook_create_creative requires account_id")
        if dry_run:
            return _dry_run_response("create_creative", f"POST /act_{account_id}/adcreatives", {
                "name": args.get("name") or "",
                "page_id": args.get("page_id"),
                "link_url": args.get("link_url"),
                "image_hash": args.get("image_hash"),
                "source_instagram_media_id": args.get("source_instagram_media_id"),
            })
        # PRE-FLIGHT (2026-09-22): a boosted post must actually exist.
        # An agent once supplied FABRICATED object_story_id values; Meta
        # answered 100/1487790 "promoted object is invalid" and the agent
        # reported that to the user as "these posts are not eligible" — the
        # opposite of the truth (the posts were fine, the ids pointed at
        # nothing). Verify existence here and fail with the REAL cause.
        # Only EXISTENCE is enforced: eligibility stays with the caller, which
        # has a video soft-path + allow_ineligible_post escape hatch.
        _oid_pf = _strip(args.get("object_story_id"))
        if _oid_pf and not args.get("source_instagram_media_id"):
            _verdict = services.verify_promotable_post(_oid_pf, token)
            if _verdict.get("exists") is False:
                _page_pf = _verdict.get("page_id") or ""
                _examples = []
                if _page_pf:
                    try:
                        _pp = services.get_page_posts(_page_pf, token)
                        _examples = [
                            str(p.get("id"))
                            for p in ((_pp or {}).get("data") or [])[:5]
                            if isinstance(p, dict) and p.get("id")
                        ]
                    except Exception:  # noqa: BLE001 — examples are advisory
                        _examples = []
                return {
                    "error": (
                        f"object_story_id {_oid_pf} does not exist on this Page. "
                        "This is NOT an eligibility problem — the post id is wrong. "
                        "Do not guess or construct post ids: call facebook_get_page_posts "
                        "and use an 'id' value from its response verbatim."
                    ),
                    "step": "preflight_creative",
                    "object_story_id": _oid_pf,
                    "page_id": _page_pf,
                    "exists": False,
                    "eligible_post_examples": _examples,
                }
        # Always go through the Instagram fallback when a page is in play: the
        # fallback function tries explicit IG identity first, then auto-discovers
        # via /{page_id}/instagram_business_accounts and falls back to FB-only
        # as a last attempt. Net effect: an LLM call without instagram_user_id
        # still gets the page's connected IG when one exists.
        has_page_or_story = bool(
            args.get("page_id")
            or args.get("source_instagram_media_id")
            or args.get("object_story_id")
            or args.get("instagram_actor_id")
            or args.get("instagram_user_id")
        )
        if has_page_or_story:
            return services.create_creative_with_instagram_fallback(
                account_id,
                token,
                name=args.get("name") or "",
                object_story_spec=args.get("object_story_spec"),
                page_id=args.get("page_id"),
                link_url=args.get("link_url"),
                message=args.get("message"),
                image_hash=args.get("image_hash"),
                object_story_id=args.get("object_story_id"),
                instagram_actor_id=args.get("instagram_actor_id"),
                instagram_user_id=args.get("instagram_user_id"),
                authorization_category=_strip(args.get("authorization_category")),
                source_instagram_media_id=args.get("source_instagram_media_id"),
                headline=args.get("headline"),
                description=args.get("description"),
                carousel_cards=args.get("carousel_cards"),
                instant_experience_id=_strip(args.get("instant_experience_id")) or None,
                branded_content_sponsor_page_id=_strip(args.get("branded_content_sponsor_page_id")) or None,
                asset_feed_spec=args.get("asset_feed_spec"),
                bylines=_strip(args.get("bylines")) or None,
                destination_spec=args.get("destination_spec"),
            )
        return services.create_creative(
            account_id,
            token,
            name=args.get("name") or "",
            object_story_spec=args.get("object_story_spec"),
            page_id=args.get("page_id"),
            link_url=args.get("link_url"),
            message=args.get("message"),
            image_hash=args.get("image_hash"),
            object_story_id=args.get("object_story_id"),
            instagram_actor_id=args.get("instagram_actor_id"),
            instagram_user_id=args.get("instagram_user_id"),
            authorization_category=_strip(args.get("authorization_category")),
            source_instagram_media_id=args.get("source_instagram_media_id"),
            instagram_permalink_url=_strip(args.get("instagram_permalink_url")) or None,
            headline=args.get("headline"),
            description=args.get("description"),
            carousel_cards=args.get("carousel_cards"),
            instant_experience_id=_strip(args.get("instant_experience_id")) or None,
            branded_content_sponsor_page_id=_strip(args.get("branded_content_sponsor_page_id")) or None,
            asset_feed_spec=args.get("asset_feed_spec"),
            bylines=_strip(args.get("bylines")) or None,
            destination_spec=args.get("destination_spec"),
        )
    if name == "facebook_delete_creative":
        cid = _strip(args.get("creative_id"))
        if not cid:
            raise ValueError("facebook_delete_creative requires creative_id")
        if dry_run:
            return _dry_run_response("delete_creative", f"DELETE /{cid}", {})
        return services.delete_creative(cid, token)

    # Conversions API
    if name == "facebook_send_conversion_event":
        if not token:
            raise ValueError("Token required for facebook_send_conversion_event")
        pid = _strip(args.get("pixel_id"))
        if not pid:
            raise ValueError("facebook_send_conversion_event requires pixel_id")
        en, et, asc = _strip(args.get("event_name")), args.get("event_time"), _strip(args.get("action_source"))
        if not en or et is None or not asc:
            raise ValueError("event_name, event_time (unix), and action_source are required")
        return services.send_conversion_event(pid, token, en, et, asc, user_data=args.get("user_data"), custom_data=args.get("custom_data"), event_id=args.get("event_id"), event_source_url=args.get("event_source_url"))

    # Catalog
    if name == "facebook_get_product_catalogs":
        return services.get_product_catalogs(token, business_id=args.get("business_id"), limit=args.get("limit"))
    if name == "facebook_get_catalog":
        cid = _strip(args.get("catalog_id"))
        if not cid:
            raise ValueError("facebook_get_catalog requires catalog_id")
        return services.get_catalog(cid, token, fields=args.get("fields"))
    if name == "facebook_get_catalog_products":
        cid = _strip(args.get("catalog_id"))
        if not cid:
            raise ValueError("facebook_get_catalog_products requires catalog_id")
        return services.get_catalog_products(cid, token, limit=args.get("limit"), filter_spec=args.get("filter"), fields=args.get("fields"))

    # Business Management
    if name == "facebook_get_business_owned_ad_accounts":
        bid = _strip(args.get("business_id"))
        if not bid:
            raise ValueError("facebook_get_business_owned_ad_accounts requires business_id")
        return services.get_business_owned_ad_accounts(bid, token, limit=args.get("limit"))
    if name == "facebook_get_business_owned_pages":
        bid = _strip(args.get("business_id"))
        if not bid:
            raise ValueError("facebook_get_business_owned_pages requires business_id")
        return services.get_business_owned_pages(bid, token, limit=args.get("limit"))
    if name == "facebook_get_business_client_ad_accounts":
        bid = _strip(args.get("business_id"))
        if not bid:
            raise ValueError("facebook_get_business_client_ad_accounts requires business_id")
        return services.get_business_client_ad_accounts(bid, token, limit=args.get("limit"))
    if name == "facebook_get_page_extension_info":
        pid = _strip(args.get("page_id"))
        if not pid:
            raise ValueError("facebook_get_page_extension_info requires page_id")
        return services.get_page_extension_info(pid, token, fields=args.get("fields"))
    if name == "facebook_get_catalog_product_sets":
        cid = _strip(args.get("catalog_id"))
        if not cid:
            raise ValueError("facebook_get_catalog_product_sets requires catalog_id")
        return services.get_catalog_product_sets(cid, token, limit=args.get("limit"))
    if name == "facebook_get_catalog_product_feeds":
        cid = _strip(args.get("catalog_id"))
        if not cid:
            raise ValueError("facebook_get_catalog_product_feeds requires catalog_id")
        return services.get_catalog_product_feeds(cid, token, limit=args.get("limit"))

    # ========== NEW PRODUCTION TOOLS ==========

    # Performance Summary (aggregated dashboard)
    if name == "facebook_get_performance_summary":
        if not account_id:
            raise ValueError("facebook_get_performance_summary requires account_id")
        ds, de = _strip(args.get("date_start")), _strip(args.get("date_end"))
        return services.get_performance_summary(
            account_id, token,
            date_preset=args.get("date_preset") or "last_7d",
            time_start=ds if ds and de else None,
            time_end=de if ds and de else None,
        )

    # Auto-Optimization
    if name == "facebook_auto_optimize":
        if not account_id:
            raise ValueError("facebook_auto_optimize requires account_id")
        return services.auto_optimize_account(
            account_id, token,
            min_impressions=int(args.get("min_impressions") or 1000),
            min_spend=float(args.get("min_spend") or 5.0),
            dry_run=dry_run,
        )

    if name == "facebook_validate_creative":
        account_id = _strip(args.get("account_id"))
        if not account_id:
            raise ValueError("facebook_validate_creative requires account_id")
        return services.validate_creative_spec(
            account_id, token,
            source_instagram_media_id=_strip(args.get("source_instagram_media_id")) or None,
            object_story_id=_strip(args.get("object_story_id")) or None,
            instagram_user_id=_strip(args.get("instagram_user_id")) or None,
            page_id=_strip(args.get("page_id")) or None,
            adset_id=_strip(args.get("adset_id")) or None,
            name=_strip(args.get("name")) or "__validate_only__",
        )

    # Token management
    if name == "facebook_check_token":
        from .auth_tokens import check_token_validity
        return check_token_validity(token)

    if name == "facebook_exchange_long_lived_token":
        from .auth_tokens import exchange_for_long_lived_token
        app_id = _strip(args.get("app_id"))
        app_secret = _strip(args.get("app_secret"))
        if not app_id or not app_secret:
            raise ValueError("facebook_exchange_long_lived_token requires app_id and app_secret")
        data = exchange_for_long_lived_token(token, app_id, app_secret)
        # Optionally save to DB
        user_id = _strip(args.get("user_id"))
        if user_id and data.get("access_token"):
            from .models import FacebookAdsCredential
            from django.utils import timezone
            from datetime import timedelta
            expires_in = data.get("expires_in", 0)
            expires_at = timezone.now() + timedelta(seconds=int(expires_in)) if expires_in else None
            FacebookAdsCredential.objects.update_or_create(
                user_id=user_id,
                defaults={
                    "access_token": data["access_token"],
                    "token_type": "long_lived",
                    "expires_at": expires_at,
                    "app_id": app_id,
                    "app_secret": app_secret,
                },
            )
            data["saved_to_db"] = True
            data["user_id"] = user_id
        return data

    # Async tools (Celery)
    if name == "facebook_create_campaign_async":
        if not account_id:
            raise ValueError("facebook_create_campaign_async requires account_id")
        from .tasks import task_create_full_campaign
        from .models import FacebookAdsMcpTaskResult
        user_id = _strip(args.get("user_id")) or ""
        if not user_id:
            raise ValueError("user_id is required for facebook_create_campaign_async")
        campaign_config = args.get("document") or args.get("campaign_config")
        if not campaign_config:
            raise ValueError("facebook_create_campaign_async requires document or campaign_config (JSON)")
        task = task_create_full_campaign.delay(
            user_id=user_id,
            account_id=account_id,
            token=token,
            campaign_config=campaign_config,
            user_confirmed=_coerce_bool_arg(args.get("user_confirmed")),
        )
        FacebookAdsMcpTaskResult.objects.create(
            task_id=task.id, user_id=user_id, task_type="create_full_campaign", status="pending",
        )
        return {
            "accepted": True,
            "task_id": task.id,
            "poll_after_ms": 2000,
            "status": "pending",
            "message": "Campaign creation queued. Polling task/status; you may also call facebook_get_task_status manually.",
        }

    if name == "facebook_get_task_status":
        task_id = _strip(args.get("task_id"))
        if not task_id:
            raise ValueError("facebook_get_task_status requires task_id")
        from .models import FacebookAdsMcpTaskResult
        try:
            tr = FacebookAdsMcpTaskResult.objects.get(task_id=task_id, user_id=_strip(args.get("user_id")))
            result = {
                "task_id": tr.task_id,
                "task_type": tr.task_type,
                "status": tr.status,
                "created_at": str(tr.created_at),
                "completed_at": str(tr.completed_at) if tr.completed_at else None,
            }
            if tr.status == "success" and tr.result_json:
                try:
                    result["result"] = json.loads(tr.result_json)
                except json.JSONDecodeError:
                    result["result"] = tr.result_json
            if tr.status == "failed" and tr.error_message:
                result["error"] = tr.error_message
            return result
        except FacebookAdsMcpTaskResult.DoesNotExist:
            return {"error": {"message": f"Task {task_id} not found"}}

    if name == "facebook_sync_performance":
        if not account_id:
            raise ValueError("facebook_sync_performance requires account_id")
        from .tasks import task_sync_performance_snapshots
        from .models import FacebookAdsMcpTaskResult
        user_id = _strip(args.get("user_id")) or ""
        if not user_id:
            raise ValueError("user_id is required for facebook_sync_performance")
        task = task_sync_performance_snapshots.delay(user_id=user_id, account_id=account_id, token=token)
        FacebookAdsMcpTaskResult.objects.create(
            task_id=task.id, user_id=user_id, task_type="sync_performance", status="pending",
        )
        return {
            "accepted": True,
            "task_id": task.id,
            "poll_after_ms": 2000,
            "status": "pending",
            "message": "Performance sync queued. Polling task/status; you may also call facebook_get_task_status manually.",
        }

    raise ValueError(f"Unknown tool: {name}")
