from django.contrib import admin
from .models import (
    FacebookAdsCredential,
    FacebookAdsMcpIdempotency,
    FacebookAdsPerformanceSnapshot,
    FacebookAdsMcpAuditLog,
    FacebookAdsMcpTaskResult,
    FacebookAdsDocMapping,
)


@admin.register(FacebookAdsCredential)
class FacebookAdsCredentialAdmin(admin.ModelAdmin):
    list_display = (
        "user_id",
        "email",
        "app_id",
        "token_type",
        "expires_at",
        "created_at",
        "updated_at",
    )
    search_fields = ("user_id", "email", "app_id")
    readonly_fields = ("access_token", "app_secret", "created_at", "updated_at")
    list_filter = ("token_type",)


@admin.register(FacebookAdsMcpIdempotency)
class FacebookAdsMcpIdempotencyAdmin(admin.ModelAdmin):
    list_display = ("tool_name", "idempotency_key", "created_at")
    search_fields = ("tool_name", "idempotency_key")
    readonly_fields = ("created_at",)
    list_filter = ("tool_name",)


@admin.register(FacebookAdsPerformanceSnapshot)
class FacebookAdsPerformanceSnapshotAdmin(admin.ModelAdmin):
    list_display = ("account_id", "entity_type", "entity_id", "date", "impressions", "clicks", "spend", "ctr")
    search_fields = ("account_id", "entity_id", "user_id")
    list_filter = ("entity_type", "date")
    readonly_fields = ("created_at",)


@admin.register(FacebookAdsMcpAuditLog)
class FacebookAdsMcpAuditLogAdmin(admin.ModelAdmin):
    list_display = ("request_id", "method", "tool_name", "status", "duration_ms", "created_at")
    search_fields = ("request_id", "user_id", "tool_name")
    list_filter = ("method", "status")
    readonly_fields = ("created_at",)


@admin.register(FacebookAdsMcpTaskResult)
class FacebookAdsMcpTaskResultAdmin(admin.ModelAdmin):
    list_display = ("task_id", "task_type", "status", "user_id", "created_at", "completed_at")
    search_fields = ("task_id", "user_id")
    list_filter = ("task_type", "status")
    readonly_fields = ("created_at", "completed_at")


@admin.register(FacebookAdsDocMapping)
class FacebookAdsDocMappingAdmin(admin.ModelAdmin):
    list_display = ("doc_id", "user_id", "account_id", "campaign_id", "adset_id", "ad_id", "status", "updated_at")
    search_fields = ("doc_id", "user_id", "account_id", "campaign_id")
    list_filter = ("status",)
    readonly_fields = ("created_at", "updated_at")
