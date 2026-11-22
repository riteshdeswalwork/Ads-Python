"""Tests for facebook_sync_document_campaigns_batch (batch document sync)."""
from unittest import mock

from django.test import SimpleTestCase

from facebook_ads_mcp import production_flow as pf


class ApplyPostNumberToNameTests(SimpleTestCase):
    def test_placeholder_substitution(self):
        self.assertEqual(
            pf._apply_post_number_to_name("Reel {post_number} | Delhi", 3),
            "Reel 3 | Delhi",
        )
        self.assertEqual(pf._apply_post_number_to_name("{n} | X", 7), "7 | X")

    def test_existing_post_token_renumbered(self):
        self.assertEqual(
            pf._apply_post_number_to_name("Post 1 | Delhi Video Views | ThruPlay", 4),
            "Post 4 | Delhi Video Views | ThruPlay",
        )
        # case-insensitive, only the first token renumbered
        self.assertEqual(
            pf._apply_post_number_to_name("post 12 | again post 1", 2),
            "Post 2 | again post 1",
        )

    def test_append_fallback(self):
        self.assertEqual(
            pf._apply_post_number_to_name("Delhi ThruPlay", 5),
            "Delhi ThruPlay | Post 5",
        )
        self.assertEqual(pf._apply_post_number_to_name("", 2), "Post 2")


class PostItemToDocumentTests(SimpleTestCase):
    TEMPLATE = {
        "campaign": {"name": "Post 1 | C", "objective": "OUTCOME_ENGAGEMENT"},
        "adset": {"name": "Post 1 | AS", "optimization_goal": "THRUPLAY"},
        "ad": {"name": "Post 1 | Ad"},
        "creative": {"authorization_category": "POLITICAL"},
    }

    def test_creative_shorthand_and_numbering(self):
        doc = pf._post_item_to_document(
            self.TEMPLATE, {"source_instagram_media_id": "123"}, 2)
        self.assertEqual(doc["creative"]["source_instagram_media_id"], "123")
        self.assertEqual(doc["creative"]["authorization_category"], "POLITICAL")
        self.assertEqual(doc["campaign"]["name"], "Post 2 | C")
        self.assertEqual(doc["adset"]["name"], "Post 2 | AS")
        self.assertEqual(doc["ad"]["name"], "Post 2 | Ad")
        # template must not be mutated
        self.assertEqual(self.TEMPLATE["campaign"]["name"], "Post 1 | C")
        self.assertNotIn("source_instagram_media_id", self.TEMPLATE["creative"])

    def test_url_aliases_map_to_permalink(self):
        for alias in ("instagram_permalink_url", "instagram_url", "url"):
            doc = pf._post_item_to_document(
                self.TEMPLATE, {alias: "https://www.instagram.com/reel/AbC/"}, 1)
            self.assertEqual(
                doc["creative"]["instagram_permalink_url"],
                "https://www.instagram.com/reel/AbC/",
            )

    def test_name_overrides_win_then_get_numbered(self):
        doc = pf._post_item_to_document(
            self.TEMPLATE, {"campaign_name": "Custom {post_number}"}, 9)
        self.assertEqual(doc["campaign"]["name"], "Custom 9")

    def test_section_deep_merge(self):
        doc = pf._post_item_to_document(
            self.TEMPLATE, {"adset": {"daily_budget": 5000}}, 1)
        self.assertEqual(doc["adset"]["daily_budget"], 5000)
        self.assertEqual(doc["adset"]["optimization_goal"], "THRUPLAY")


class BatchSyncTests(SimpleTestCase):
    TEMPLATE = {"campaign": {"name": "Post 1 | C", "objective": "OUTCOME_ENGAGEMENT"}}

    def setUp(self):
        # Keep the dedup guard's campaign prefetch off the network.
        patcher = mock.patch.object(
            pf.services, "get_campaigns", return_value={"data": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_batch_continues_after_failures(self):
        calls = []

        def fake_sync(account_id, token, doc, user_id="", user_confirmed=False):
            calls.append(doc["campaign"]["name"])
            n = len(calls)
            if n == 2:
                raise RuntimeError("boom")
            if n == 3:
                return {"error": {"message": "meta said no"}, "auto_fixes": ["af"]}
            return {
                "ok": True, "campaign_id": f"c{n}", "adset_id": f"as{n}",
                "creative_id": f"cr{n}", "ad_id": f"ad{n}", "auto_fixes": [],
            }

        posts = [
            {"source_instagram_media_id": "m1"},
            {"source_instagram_media_id": "m2"},
            {"source_instagram_media_id": "m3"},
            {"source_instagram_media_id": "m4"},
        ]
        with mock.patch.object(pf, "sync_document_campaign", side_effect=fake_sync):
            res = pf.sync_document_campaigns_batch("act_1", "tok", self.TEMPLATE, posts)

        self.assertEqual(res["total"], 4)
        self.assertEqual(res["succeeded"], 2)
        self.assertEqual(res["failed"], 2)
        self.assertFalse(res["ok"])
        # all four attempted despite two failures
        self.assertEqual(calls, ["Post 1 | C", "Post 2 | C", "Post 3 | C", "Post 4 | C"])
        by_n = {r["post_number"]: r for r in res["results"]}
        self.assertEqual(by_n[1]["campaign_id"], "c1")
        self.assertIn("boom", by_n[2]["error"]["message"])
        self.assertEqual(by_n[3]["error"]["message"], "meta said no")
        self.assertEqual(by_n[4]["ad_id"], "ad4")

    def test_all_success_and_json_string_inputs(self):
        import json as _json

        def fake_sync(account_id, token, doc, user_id="", user_confirmed=False):
            return {"ok": True, "campaign_id": "c", "adset_id": "a",
                    "creative_id": "cr", "ad_id": "ad", "auto_fixes": []}

        with mock.patch.object(pf, "sync_document_campaign", side_effect=fake_sync):
            res = pf.sync_document_campaigns_batch(
                "act_1", "tok", _json.dumps(self.TEMPLATE),
                _json.dumps([{"url": "https://www.instagram.com/reel/X/"}]))
        self.assertTrue(res["ok"])
        self.assertEqual(res["succeeded"], 1)

    def test_rejects_empty_posts(self):
        with self.assertRaises(ValueError):
            pf.sync_document_campaigns_batch("act_1", "tok", self.TEMPLATE, [])

    def test_explicit_post_number_respected(self):
        def fake_sync(account_id, token, doc, user_id="", user_confirmed=False):
            return {"ok": True, "campaign_id": doc["campaign"]["name"],
                    "adset_id": "a", "creative_id": "cr", "ad_id": "ad"}

        with mock.patch.object(pf, "sync_document_campaign", side_effect=fake_sync):
            res = pf.sync_document_campaigns_batch(
                "act_1", "tok", self.TEMPLATE, [{"post_number": 7, "url": "u"}])
        self.assertEqual(res["results"][0]["post_number"], 7)
        self.assertEqual(res["results"][0]["campaign_id"], "Post 7 | C")


class ToolRegistrationTests(SimpleTestCase):
    def test_tool_listed_and_dispatchable(self):
        from facebook_ads_mcp.views import TOOLS
        names = {t["name"] for t in TOOLS}
        self.assertIn("facebook_sync_document_campaigns_batch", names)

        from facebook_ads_mcp import tool_dispatch
        self.assertIn(
            "facebook_sync_document_campaigns_batch", tool_dispatch._IDEMPOTENT_TOOLS)


class BatchTimeBudgetTests(SimpleTestCase):
    """Time-budget continuation: large batches stop before the chat-side tool
    timeout and hand untouched posts back as continuation.remaining_posts."""

    def setUp(self):
        # Keep the dedup guard's campaign prefetch off the network.
        patcher = mock.patch.object(
            pf.services, "get_campaigns", return_value={"data": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _ok_sync(*a, **k):
        return {"ok": True, "campaign_id": "c1", "adset_id": "s1",
                "creative_id": "cr1", "ad_id": "a1"}

    def test_budget_exhaustion_returns_continuation(self):
        posts = [{"instagram_permalink_url": f"https://ig/reel/{i}"} for i in range(5)]
        clock = iter([0.0,        # started
                      1.0, 1.0,   # post 1 check ok (results empty → also runs)
                      100.0,      # post 2 check: over budget? budget=50 → yes
                      100.0, 100.0, 100.0])
        with mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync), \
             mock.patch.object(pf.time, "monotonic", side_effect=lambda: next(clock)):
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", {"campaign": {"name": "X | Post {post_number}"}},
                posts, time_budget_seconds=50)
        self.assertEqual(r["total"], 5)
        self.assertEqual(r["processed"], 2)
        self.assertFalse(r["ok"])
        cont = r["continuation"]
        self.assertEqual(len(cont["remaining_posts"]), 3)
        # untouched items keep their ORIGINAL numbering (3, 4, 5)
        self.assertEqual([p["post_number"] for p in cont["remaining_posts"]], [3, 4, 5])
        self.assertIn("remaining_posts", cont["message"])

    def test_no_budget_hit_no_continuation(self):
        posts = [{"instagram_permalink_url": "https://ig/reel/1"},
                 {"instagram_permalink_url": "https://ig/reel/2"}]
        with mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync):
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", {"campaign": {"name": "X"}}, posts)
        self.assertTrue(r["ok"])
        self.assertEqual(r["processed"], 2)
        self.assertNotIn("continuation", r)

    def test_first_post_always_runs_even_with_tiny_budget(self):
        # The budget check requires results non-empty — a pathological budget
        # can never return an all-remaining batch that made zero progress.
        posts = [{"instagram_permalink_url": f"https://ig/reel/{i}"} for i in range(3)]
        with mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync), \
             mock.patch.object(pf.time, "monotonic", side_effect=[0.0] + [999.0] * 10):
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", {"campaign": {"name": "X"}}, posts,
                time_budget_seconds=1)
        self.assertGreaterEqual(r["processed"], 1)
        self.assertEqual(r["processed"] + len(r["continuation"]["remaining_posts"]), 3)

    def test_explicit_post_number_preserved_in_remaining(self):
        posts = [{"instagram_permalink_url": "https://ig/1"},
                 {"instagram_permalink_url": "https://ig/2", "post_number": 99}]
        with mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync), \
             mock.patch.object(pf.time, "monotonic", side_effect=[0.0, 0.0, 999.0]):
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", {"campaign": {"name": "X"}}, posts,
                time_budget_seconds=10)
        self.assertEqual(r["continuation"]["remaining_posts"][0]["post_number"], 99)


# The dedup guard only looks back FACEBOOK_BATCH_DEDUP_WINDOW_HOURS (48h), so a
# hardcoded fixture date silently ages out — stamp "now" at import instead.
from datetime import datetime as _dt, timezone as _tz
_RECENT_CREATED_TIME = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


class BatchDedupGuardTests(SimpleTestCase):
    """Duplicate guard: posts whose campaign already exists in the account's
    recent campaigns are returned as skipped=already_exists instead of being
    re-created (the 2026-08-18 empty-response duplicate incident)."""

    TEMPLATE = {"campaign": {"name": "Post 1 | C", "objective": "OUTCOME_ENGAGEMENT"}}

    @staticmethod
    def _ok_sync(*a, **k):
        return {"ok": True, "campaign_id": "new1", "adset_id": "s1",
                "creative_id": "cr1", "ad_id": "a1"}

    def _with_existing(self, campaigns):
        return mock.patch.object(
            pf.services, "get_campaigns",
            return_value={"data": campaigns})

    def test_exact_name_match_skips(self):
        existing = [{"id": "old9", "name": "Post 1 | C",
                     "created_time": _RECENT_CREATED_TIME,
                     "effective_status": "ACTIVE"}]
        with self._with_existing(existing), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync) as sync:
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", self.TEMPLATE,
                [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"}])
        sync.assert_not_called()
        self.assertEqual(r["skipped_already_exists"], 1)
        self.assertEqual(r["results"][0]["skipped"], "already_exists")
        self.assertEqual(r["results"][0]["campaign_id"], "old9")
        self.assertTrue(r["ok"])

    def test_shortcode_in_existing_name_skips_renamed_variant(self):
        # Same reel, DIFFERENT campaign name (the real incident pattern).
        existing = [{"id": "old7", "name": "Fans Club Reel AbCd123",
                     "created_time": _RECENT_CREATED_TIME,
                     "effective_status": "ACTIVE"}]
        with self._with_existing(existing), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync) as sync:
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", self.TEMPLATE,
                [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"}])
        sync.assert_not_called()
        self.assertEqual(r["results"][0]["campaign_id"], "old7")

    def test_old_campaigns_outside_window_do_not_block(self):
        existing = [{"id": "ancient", "name": "Post 1 | C",
                     "created_time": "2026-01-01T10:00:00+0000",
                     "effective_status": "ACTIVE"}]
        with self._with_existing(existing), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync) as sync:
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", self.TEMPLATE,
                [{"instagram_permalink_url": "https://www.instagram.com/reel/Zz9/"}])
        sync.assert_called_once()
        self.assertEqual(r["succeeded"], 1)
        self.assertEqual(r["skipped_already_exists"], 0)

    def test_allow_duplicate_overrides(self):
        existing = [{"id": "old9", "name": "Post 1 | C",
                     "created_time": _RECENT_CREATED_TIME,
                     "effective_status": "ACTIVE"}]
        doc = {"campaign": {"name": "Post 1 | C"},
               "options": {"allow_duplicate_campaigns": True}}
        with self._with_existing(existing), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync) as sync:
            pf.sync_document_campaigns_batch(
                "act_1", "tok", doc,
                [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"}])
        sync.assert_called_once()

    def test_prefetch_failure_disables_guard_not_batch(self):
        with mock.patch.object(pf.services, "get_campaigns",
                               side_effect=RuntimeError("graph down")), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync) as sync:
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", self.TEMPLATE,
                [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"}])
        sync.assert_called_once()
        self.assertTrue(r["ok"])

    def test_same_post_twice_in_one_batch_second_is_skipped(self):
        with mock.patch.object(pf.services, "get_campaigns", return_value={"data": []}), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync) as sync:
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", self.TEMPLATE,
                [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/",
                  "campaign_name": "Same Name"},
                 {"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/",
                  "campaign_name": "Same Name"}])
        sync.assert_called_once()
        self.assertEqual(r["skipped_already_exists"], 1)

    def test_default_budget_is_45s(self):
        import os as _os
        with mock.patch.dict(_os.environ, {}, clear=False):
            _os.environ.pop("FACEBOOK_BATCH_TIME_BUDGET_SECONDS", None)
            # No direct accessor; assert via source to lock the default.
            import inspect
            src = inspect.getsource(pf.sync_document_campaigns_batch)
            self.assertIn('"45"', src)
            self.assertNotIn('"240"', src)


class BatchCaptionNamingTests(SimpleTestCase):
    """options.caption_names: server-side caption-derived names (first 50 words)."""

    CAP = " ".join(f"w{i}" for i in range(1, 61))  # 60-word caption

    def _doc(self):
        return {"campaign": {"name": "T | C"}, "adset": {"name": "T | AS"},
                "ad": {"name": "T | Ad"},
                "creative": {"page_id": "pg1", "authorization_category": "POLITICAL"},
                "options": {"caption_names": True}}

    def _media(self):
        return {"data": [
            {"id": "m1", "caption": self.CAP,
             "permalink": "https://www.instagram.com/reel/AbCd123/"},
            {"id": "m2", "caption": "",
             "permalink": "https://www.instagram.com/reel/NoCap99/"},
        ]}

    @staticmethod
    def _ok_sync(account_id, token, doc, user_id="", user_confirmed=False):
        return {"ok": True, "campaign_id": doc["campaign"]["name"],
                "adset_id": doc["adset"]["name"], "creative_id": "cr",
                "ad_id": doc["ad"]["name"]}

    def _run(self, posts, media=None):
        with mock.patch.object(pf.services, "get_campaigns", return_value={"data": []}), \
             mock.patch.object(pf.services, "get_instagram_media",
                               return_value=media or self._media()), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync):
            return pf.sync_document_campaigns_batch("act_1", "tok", self._doc(), posts)

    def test_caption_first_50_words_used_for_all_three_names(self):
        r = self._run([{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"}])
        expect = " ".join(f"w{i}" for i in range(1, 51))  # 50 of 60 words
        res = r["results"][0]
        self.assertEqual(res["campaign_id"], expect)
        self.assertEqual(res["adset_id"], expect)
        self.assertEqual(res["ad_id"], expect)

    def test_explicit_per_post_name_override_wins(self):
        r = self._run([{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/",
                        "campaign_name": "My Name"}])
        res = r["results"][0]
        # Explicit override kept (the batch's post-numbering still applies).
        self.assertEqual(res["campaign_id"], "My Name | Post 1")
        self.assertTrue(res["adset_id"].startswith("w1 w2"))  # caption applied

    def test_missing_caption_falls_back_with_note(self):
        r = self._run([{"instagram_permalink_url": "https://www.instagram.com/reel/NoCap99/"}])
        res = r["results"][0]
        self.assertEqual(res["campaign_id"], "T | C | Post 1")
        self.assertTrue(any("no caption found" in a for a in res["auto_fixes"]))

    def test_same_caption_two_posts_names_stay_unique(self):
        media = {"data": [
            {"id": "m1", "caption": self.CAP,
             "permalink": "https://www.instagram.com/reel/AbCd123/"},
            {"id": "m3", "caption": self.CAP,
             "permalink": "https://www.instagram.com/reel/XyZ789/"},
        ]}
        r = self._run(
            [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"},
             {"instagram_permalink_url": "https://www.instagram.com/reel/XyZ789/"}],
            media=media)
        self.assertEqual(r["succeeded"], 2)
        self.assertEqual(r["skipped_already_exists"], 0)
        names = [x["campaign_id"] for x in r["results"]]
        self.assertNotEqual(names[0], names[1])
        self.assertIn("XyZ789", names[1])

    def test_media_crawl_failure_keeps_template_names(self):
        with mock.patch.object(pf.services, "get_campaigns", return_value={"data": []}), \
             mock.patch.object(pf.services, "get_instagram_media",
                               side_effect=RuntimeError("ig down")), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync):
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", self._doc(),
                [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"}])
        self.assertEqual(r["results"][0]["campaign_id"], "T | C | Post 1")
        self.assertTrue(r["ok"])


class BatchCaptionNamingDefaultTests(SimpleTestCase):
    """Caption naming is ON BY DEFAULT (owner directive: captions always) when
    the template carries the IG identity; off only with explicit false or when
    no identity exists to crawl."""

    CAP = " ".join(f"w{i}" for i in range(1, 21))

    def _media(self):
        return {"data": [{"id": "m1", "caption": self.CAP,
                          "permalink": "https://www.instagram.com/reel/AbCd123/"}]}

    @staticmethod
    def _ok_sync(account_id, token, doc, user_id="", user_confirmed=False):
        return {"ok": True, "campaign_id": doc["campaign"]["name"],
                "adset_id": doc["adset"]["name"] if doc.get("adset") else "as",
                "creative_id": "cr", "ad_id": "ad"}

    def _run(self, doc, posts):
        with mock.patch.object(pf.services, "get_campaigns", return_value={"data": []}), \
             mock.patch.object(pf.services, "get_instagram_media",
                               return_value=self._media()), \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync):
            return pf.sync_document_campaigns_batch("act_1", "tok", doc, posts)

    def test_default_on_when_identity_present(self):
        doc = {"campaign": {"name": "T | C"}, "adset": {"name": "T | AS"},
               "creative": {"page_id": "pg1"}}  # no options at all
        r = self._run(doc, [{"instagram_permalink_url":
                             "https://www.instagram.com/reel/AbCd123/"}])
        self.assertTrue(r["results"][0]["campaign_id"].startswith("w1 w2"))

    def test_explicit_false_keeps_template_names(self):
        doc = {"campaign": {"name": "T | C"},
               "creative": {"page_id": "pg1"},
               "options": {"caption_names": False}}
        r = self._run(doc, [{"instagram_permalink_url":
                             "https://www.instagram.com/reel/AbCd123/"}])
        self.assertEqual(r["results"][0]["campaign_id"], "T | C | Post 1")

    def test_no_identity_skips_silently_without_network(self):
        doc = {"campaign": {"name": "T | C"}, "creative": {}}
        with mock.patch.object(pf.services, "get_campaigns", return_value={"data": []}), \
             mock.patch.object(pf.services, "get_instagram_media",
                               side_effect=AssertionError("must not be called")) as gim, \
             mock.patch.object(pf, "sync_document_campaign", side_effect=self._ok_sync):
            r = pf.sync_document_campaigns_batch(
                "act_1", "tok", doc,
                [{"instagram_permalink_url": "https://www.instagram.com/reel/AbCd123/"}])
        gim.assert_not_called()
        self.assertEqual(r["results"][0]["campaign_id"], "T | C | Post 1")
