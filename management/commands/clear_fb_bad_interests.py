"""Wipe the learned-bad Meta interest IDs cache.

The Facebook Ads connector caches interest IDs Meta has rejected (subcode
1487079 / 1487694 / 2446395) under ``fb_ads:known_bad_interest_ids:v1`` in the
Django cache, with a 7-day TTL. When stale or misclassified entries accumulate,
this command clears the slot so the next agent run starts clean. The static
global blocklist (``_GLOBAL_BAD_INTEREST_IDS`` in production_flow.py) is
intentionally NOT touched — those IDs are deprecated globally, not learned.

Usage:
    python manage.py clear_fb_bad_interests          # delete the cached set
    python manage.py clear_fb_bad_interests --dry-run # preview without delete
"""
from __future__ import annotations

from django.core.cache import cache
from django.core.management.base import BaseCommand

from facebook_ads_mcp.production_flow import _KNOWN_BAD_INTERESTS_CACHE_KEY


class Command(BaseCommand):
    help = "Clear the learned-bad Meta interest IDs cache (fb_ads:known_bad_interest_ids:v1)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the cached IDs without deleting.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        raw = cache.get(_KNOWN_BAD_INTERESTS_CACHE_KEY)

        if raw is None:
            self.stdout.write(self.style.SUCCESS(
                f"Cache key {_KNOWN_BAD_INTERESTS_CACHE_KEY!r} is empty — nothing to clear."
            ))
            return

        if not isinstance(raw, set):
            try:
                raw = set(raw)
            except TypeError:
                raw = {str(raw)}

        count = len(raw)
        preview = sorted(raw)[:20]
        self.stdout.write(
            f"Cache key {_KNOWN_BAD_INTERESTS_CACHE_KEY!r} holds {count} learned ID(s)."
        )
        for entry in preview:
            self.stdout.write(f"  - {entry}")
        if count > len(preview):
            self.stdout.write(f"  ... and {count - len(preview)} more")

        if dry_run:
            self.stdout.write(self.style.WARNING("--dry-run: not deleted."))
            return

        cache.delete(_KNOWN_BAD_INTERESTS_CACHE_KEY)
        self.stdout.write(self.style.SUCCESS(
            f"Cleared {count} learned-bad interest ID(s)."
        ))
