# Production readiness: idempotency, performance snapshots, audit logs, task results, enhanced credentials

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("facebook_ads_mcp", "0001_add_facebook_ads_credential"),
    ]

    operations = [
        # Enhance FacebookAdsCredential with token_type, expires_at, app_id, app_secret
        migrations.AddField(
            model_name="facebookadscredential",
            name="token_type",
            field=models.CharField(default="short_lived", max_length=20),
        ),
        migrations.AddField(
            model_name="facebookadscredential",
            name="expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="facebookadscredential",
            name="app_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="facebookadscredential",
            name="app_secret",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        # Idempotency cache
        migrations.CreateModel(
            name="FacebookAdsMcpIdempotency",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("idempotency_key", models.CharField(db_index=True, max_length=64, unique=True)),
                ("tool_name", models.CharField(db_index=True, max_length=120)),
                ("response_json", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Facebook Ads MCP idempotency cache",
                "verbose_name_plural": "Facebook Ads MCP idempotency cache",
                "app_label": "facebook_ads_mcp",
            },
        ),
        # Performance snapshots
        migrations.CreateModel(
            name="FacebookAdsPerformanceSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("user_id", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("account_id", models.CharField(db_index=True, max_length=50)),
                ("entity_type", models.CharField(db_index=True, max_length=20)),
                ("entity_id", models.CharField(max_length=50)),
                ("date", models.DateField(db_index=True)),
                ("impressions", models.IntegerField(default=0)),
                ("clicks", models.IntegerField(default=0)),
                ("spend", models.DecimalField(decimal_places=2, default=0, max_digits=12)),
                ("conversions", models.IntegerField(default=0)),
                ("ctr", models.DecimalField(decimal_places=4, default=0, max_digits=6)),
                ("cpc", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("roas", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("raw_json", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Facebook Ads performance snapshot",
                "verbose_name_plural": "Facebook Ads performance snapshots",
                "app_label": "facebook_ads_mcp",
                "unique_together": {("account_id", "entity_type", "entity_id", "date")},
            },
        ),
        # Audit logs
        migrations.CreateModel(
            name="FacebookAdsMcpAuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("request_id", models.CharField(db_index=True, max_length=36)),
                ("user_id", models.CharField(blank=True, default="", max_length=255)),
                ("method", models.CharField(max_length=50)),
                ("tool_name", models.CharField(blank=True, default="", max_length=120)),
                ("request_payload", models.TextField(blank=True, default="")),
                ("response_summary", models.TextField(blank=True, default="")),
                ("status", models.CharField(max_length=20)),
                ("duration_ms", models.IntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Facebook Ads MCP audit log",
                "verbose_name_plural": "Facebook Ads MCP audit logs",
                "app_label": "facebook_ads_mcp",
            },
        ),
        # Task results (Celery)
        migrations.CreateModel(
            name="FacebookAdsMcpTaskResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("task_id", models.CharField(db_index=True, max_length=255, unique=True)),
                ("user_id", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("task_type", models.CharField(max_length=100)),
                ("status", models.CharField(default="pending", max_length=20)),
                ("result_json", models.TextField(blank=True, default="")),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "verbose_name": "Facebook Ads MCP task result",
                "verbose_name_plural": "Facebook Ads MCP task results",
                "app_label": "facebook_ads_mcp",
            },
        ),
    ]
