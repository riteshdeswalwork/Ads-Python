"""
Celery background tasks for Facebook Ads MCP.
Heavy operations run async to avoid HTTP timeouts and improve scalability.
"""
from __future__ import annotations

import json
import logging
import traceback

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger("facebook_ads_mcp")


def _save_task_result(task_id: str, user_id: str, task_type: str, status: str, result=None, error=None):
    """Update or create task result record."""
    from .models import FacebookAdsMcpTaskResult

    defaults = {"status": status}
    if result is not None:
        defaults["result_json"] = json.dumps(result, default=str)
    if error is not None:
        defaults["error_message"] = str(error)[:5000]
    if status in ("success", "failed"):
        defaults["completed_at"] = timezone.now()

    FacebookAdsMcpTaskResult.objects.update_or_create(
        task_id=task_id,
        defaults={**defaults, "user_id": user_id or "", "task_type": task_type},
    )


@shared_task(bind=True, max_retries=3)
def task_create_full_campaign(self, *, user_id, account_id, token, campaign_config, user_confirmed=False):
    """
    Async campaign creation using production_flow.sync_document_campaign.
    Stores result in FacebookAdsMcpTaskResult.
    """
    task_id = self.request.id
    _save_task_result(task_id, user_id, "create_full_campaign", "running")
    try:
        from . import production_flow

        document = campaign_config if isinstance(campaign_config, dict) else json.loads(campaign_config)
        result = production_flow.sync_document_campaign(
            account_id, token, document, user_confirmed=bool(user_confirmed),
        )
        _save_task_result(task_id, user_id, "create_full_campaign", "success", result=result)
        return result
    except Exception as exc:
        logger.exception("task_create_full_campaign failed: %s", exc)
        _save_task_result(task_id, user_id, "create_full_campaign", "failed", error=traceback.format_exc())
        try:
            self.retry(countdown=2 ** self.request.retries, exc=exc)
        except self.MaxRetriesExceededError:
            _save_task_result(task_id, user_id, "create_full_campaign", "failed", error=f"Max retries exceeded: {exc}")


@shared_task(bind=True, max_retries=3)
def task_sync_performance_snapshots(self, *, user_id, account_id, token):
    """
    Pull latest insights from Facebook and store in FacebookAdsPerformanceSnapshot.
    Runs per account, fetches campaign/adset/ad level data for today.
    """
    task_id = self.request.id
    _save_task_result(task_id, user_id, "sync_performance", "running")
    try:
        from . import services
        from .models import FacebookAdsPerformanceSnapshot
        from decimal import Decimal

        today = timezone.now().date()
        saved_count = 0

        for level, fetch_fn in [
            ("campaign", lambda: services.get_campaign_performance(account_id, token, date_preset="today")),
            ("adset", lambda: services.get_adset_performance(account_id, token, date_preset="today")),
            ("ad", lambda: services.get_ad_performance(account_id, token, date_preset="today")),
        ]:
            data = fetch_fn()
            if isinstance(data, dict) and "error" in data:
                logger.warning("sync_performance %s error: %s", level, data["error"])
                continue
            rows = data.get("data") or (data if isinstance(data, list) else [])
            for row in rows:
                entity_id = row.get(f"{level}_id") or row.get("id") or ""
                if not entity_id:
                    continue
                FacebookAdsPerformanceSnapshot.objects.update_or_create(
                    account_id=account_id.replace("act_", ""),
                    entity_type=level,
                    entity_id=str(entity_id),
                    date=today,
                    defaults={
                        "user_id": user_id or "",
                        "impressions": int(row.get("impressions") or 0),
                        "clicks": int(row.get("clicks") or 0),
                        "spend": Decimal(str(row.get("spend") or 0)),
                        "conversions": int(row.get("conversions") or 0),
                        "ctr": Decimal(str(row.get("ctr") or 0)),
                        "cpc": Decimal(str(row.get("cpc") or 0)),
                        "roas": Decimal(str(row.get("roas") or 0)),
                        "raw_json": json.dumps(row, default=str)[:16000],
                    },
                )
                saved_count += 1

        result = {"saved_count": saved_count, "date": str(today), "account_id": account_id}
        _save_task_result(task_id, user_id, "sync_performance", "success", result=result)
        return result
    except Exception as exc:
        logger.exception("task_sync_performance_snapshots failed: %s", exc)
        _save_task_result(task_id, user_id, "sync_performance", "failed", error=traceback.format_exc())
        try:
            self.retry(countdown=2 ** self.request.retries, exc=exc)
        except self.MaxRetriesExceededError:
            _save_task_result(task_id, user_id, "sync_performance", "failed", error=f"Max retries exceeded: {exc}")


@shared_task(bind=True, max_retries=3)
def task_auto_optimize(self, *, user_id, account_id, token, rules=None):
    """
    Run auto-optimization rules in background.
    Uses services.auto_optimize_account() with the provided rules.
    """
    task_id = self.request.id
    _save_task_result(task_id, user_id, "auto_optimize", "running")
    try:
        from . import services

        result = services.auto_optimize_account(
            account_id, token,
            min_impressions=(rules or {}).get("min_impressions", 1000),
            min_spend=(rules or {}).get("min_spend", 5.0),
            dry_run=False,
        )
        _save_task_result(task_id, user_id, "auto_optimize", "success", result=result)
        return result
    except Exception as exc:
        logger.exception("task_auto_optimize failed: %s", exc)
        _save_task_result(task_id, user_id, "auto_optimize", "failed", error=traceback.format_exc())
        try:
            self.retry(countdown=2 ** self.request.retries, exc=exc)
        except self.MaxRetriesExceededError:
            _save_task_result(task_id, user_id, "auto_optimize", "failed", error=f"Max retries exceeded: {exc}")


@shared_task(bind=True, max_retries=2)
def task_refresh_tokens(self):
    """
    Scheduled: refresh all Facebook tokens expiring within 1 hour.
    Should be run via Celery beat every 30 minutes.
    """
    from .models import FacebookAdsCredential
    from .auth_tokens import exchange_for_long_lived_token
    from datetime import timedelta

    threshold = timezone.now() + timedelta(hours=1)
    expiring = FacebookAdsCredential.objects.filter(
        expires_at__isnull=False,
        expires_at__lte=threshold,
    ).exclude(access_token="")

    refreshed = 0
    failed = 0
    for cred in expiring:
        app_id = (cred.app_id or "").strip()
        app_secret = (cred.app_secret or "").strip()
        if not app_id or not app_secret:
            from django.conf import settings
            app_id = app_id or (getattr(settings, "FACEBOOK_APP_ID", None) or "").strip()
            app_secret = app_secret or (getattr(settings, "FACEBOOK_APP_SECRET", None) or "").strip()

        if not app_id or not app_secret:
            logger.warning("Cannot refresh token for user_id=%s: no app credentials", cred.user_id)
            failed += 1
            continue

        try:
            data = exchange_for_long_lived_token(cred.access_token, app_id, app_secret)
            from .services import invalidate_page_token_cache
            invalidate_page_token_cache(cred.access_token)
            cred.access_token = data["access_token"]
            cred.token_type = "long_lived"
            expires_in = data.get("expires_in", 0)
            if expires_in:
                cred.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
            cred.save()
            refreshed += 1
            logger.info("Refreshed token for user_id=%s", cred.user_id)
        except Exception as e:
            logger.warning("Failed to refresh token for user_id=%s: %s", cred.user_id, e)
            failed += 1

    return {"refreshed": refreshed, "failed": failed, "total_expiring": expiring.count()}
