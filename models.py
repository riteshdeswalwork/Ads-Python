from django.db import models


class FacebookAdsCredential(models.Model):
    """
    Facebook access token storage per user/tenant with expiry tracking.
    MCP tools can now auto-resolve tokens via user_id when arguments.token is not provided.
    """
    user_id = models.CharField(max_length=255, unique=True, db_index=True)
    # Email of the connected Facebook account/user (captured at connect time). Shown in admin.
    email = models.CharField(max_length=255, blank=True, default="", db_index=True)
    access_token = models.TextField()
    token_type = models.CharField(max_length=20, default="short_lived")  # short_lived / long_lived
    expires_at = models.DateTimeField(null=True, blank=True)
    app_id = models.CharField(max_length=100, blank=True, default="")
    app_secret = models.CharField(max_length=255, blank=True, default="")
    # Convenience hint for the UI: the ad account to default to. The token is
    # user-level and grants access to ALL ad accounts the user manages; tools
    # still accept an explicit ad account_id per call.
    default_ad_account_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "facebook_ads_mcp"
        verbose_name = "Facebook Ads credential"
        verbose_name_plural = "Facebook Ads credentials"


class FacebookAdsOAuthState(models.Model):
    """
    Short-lived OAuth2 handshake state for "Connect with Facebook".

    Facebook's redirect back to oauth_callback only carries ?code & ?state — it
    cannot carry the X-User-Id header or a session cookie reliably. So user_id
    (resolved at oauth_start) and the exact redirect_uri are persisted here and
    recovered via the opaque `state` token in the callback. Rows are deleted on
    successful callback. app_secret is intentionally NOT stored (read from
    settings.FACEBOOK_APP_SECRET in the callback).
    """
    state = models.CharField(max_length=128, unique=True, db_index=True)
    user_id = models.CharField(max_length=255, db_index=True)
    redirect_uri = models.CharField(max_length=512, blank=True, default="")
    success_url = models.TextField(blank=True, default="")
    connected_email = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "facebook_ads_mcp"
        verbose_name = "Facebook Ads OAuth state"
        verbose_name_plural = "Facebook Ads OAuth states"


class FacebookAdsMcpIdempotency(models.Model):
    """Cache successful tool results by idempotency key to prevent duplicate creates."""
    idempotency_key = models.CharField(max_length=64, unique=True, db_index=True)
    tool_name = models.CharField(max_length=120, db_index=True)
    response_json = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "facebook_ads_mcp"
        verbose_name = "Facebook Ads MCP idempotency cache"
        verbose_name_plural = "Facebook Ads MCP idempotency cache"


class FacebookAdsPerformanceSnapshot(models.Model):
    """Daily performance snapshot for historical tracking of campaigns/adsets/ads."""
    user_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    account_id = models.CharField(max_length=50, db_index=True)
    entity_type = models.CharField(max_length=20, db_index=True)  # campaign / adset / ad
    entity_id = models.CharField(max_length=50)
    date = models.DateField(db_index=True)
    impressions = models.IntegerField(default=0)
    clicks = models.IntegerField(default=0)
    spend = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    conversions = models.IntegerField(default=0)
    ctr = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    cpc = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    roas = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    raw_json = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "facebook_ads_mcp"
        unique_together = ("account_id", "entity_type", "entity_id", "date")
        verbose_name = "Facebook Ads performance snapshot"
        verbose_name_plural = "Facebook Ads performance snapshots"


class FacebookAdsMcpAuditLog(models.Model):
    """Structured audit log for all MCP JSON-RPC requests."""
    request_id = models.CharField(max_length=36, db_index=True)
    user_id = models.CharField(max_length=255, blank=True, default="")
    method = models.CharField(max_length=50)  # tools/call, tools/list, initialize, etc.
    tool_name = models.CharField(max_length=120, blank=True, default="")
    request_payload = models.TextField(blank=True, default="")  # redacted
    response_summary = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20)  # success / error
    duration_ms = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "facebook_ads_mcp"
        verbose_name = "Facebook Ads MCP audit log"
        verbose_name_plural = "Facebook Ads MCP audit logs"


class FacebookAdsDocMapping(models.Model):
    """Maps a caller's doc_id to the latest Facebook entity IDs after each sync (campaign always created fresh per run)."""
    doc_id = models.CharField(max_length=255, db_index=True)
    user_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    account_id = models.CharField(max_length=50, db_index=True)
    campaign_id = models.CharField(max_length=50, blank=True, default="")
    adset_id = models.CharField(max_length=50, blank=True, default="")
    ad_id = models.CharField(max_length=50, blank=True, default="")
    creative_id = models.CharField(max_length=50, blank=True, default="")
    document_hash = models.CharField(max_length=64, blank=True, default="")
    document_json = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        app_label = "facebook_ads_mcp"
        unique_together = ("doc_id", "user_id", "account_id")
        verbose_name = "Facebook Ads doc mapping"
        verbose_name_plural = "Facebook Ads doc mappings"


class FacebookAdsMcpTaskResult(models.Model):
    """Track Celery background task status and results."""
    task_id = models.CharField(max_length=255, unique=True, db_index=True)
    user_id = models.CharField(max_length=255, db_index=True, blank=True, default="")
    task_type = models.CharField(max_length=100)  # create_full_campaign, sync_performance, auto_optimize
    status = models.CharField(max_length=20, default="pending")  # pending / running / success / failed
    result_json = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        app_label = "facebook_ads_mcp"
        verbose_name = "Facebook Ads MCP task result"
        verbose_name_plural = "Facebook Ads MCP task results"
