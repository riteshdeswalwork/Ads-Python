# Add FacebookAdsDocMapping for doc_id → Facebook entity ID mapping

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("facebook_ads_mcp", "0002_production_readiness"),
    ]

    operations = [
        migrations.CreateModel(
            name="FacebookAdsDocMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("doc_id", models.CharField(db_index=True, max_length=255)),
                ("user_id", models.CharField(blank=True, db_index=True, default="", max_length=255)),
                ("account_id", models.CharField(db_index=True, max_length=50)),
                ("campaign_id", models.CharField(blank=True, default="", max_length=50)),
                ("adset_id", models.CharField(blank=True, default="", max_length=50)),
                ("ad_id", models.CharField(blank=True, default="", max_length=50)),
                ("creative_id", models.CharField(blank=True, default="", max_length=50)),
                ("document_hash", models.CharField(blank=True, default="", max_length=64)),
                ("document_json", models.TextField(blank=True, default="")),
                ("status", models.CharField(default="active", max_length=20)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Facebook Ads doc mapping",
                "verbose_name_plural": "Facebook Ads doc mappings",
                "app_label": "facebook_ads_mcp",
                "unique_together": {("doc_id", "user_id", "account_id")},
            },
        ),
    ]
