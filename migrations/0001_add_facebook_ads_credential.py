# Generated manually for Facebook Ads token storage (DB only, no env)

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="FacebookAdsCredential",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("user_id", models.CharField(db_index=True, max_length=255, unique=True)),
                ("access_token", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Facebook Ads credential",
                "verbose_name_plural": "Facebook Ads credentials",
                "app_label": "facebook_ads_mcp",
            },
        ),
    ]
