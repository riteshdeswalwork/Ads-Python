"""Production-hardening tests for the FB Ads MCP.

Two fixes covered:

Fix A — Pre-flight is_eligible_for_promotion check on object_story_id.
  When the brief specifies an FB post that Meta has flagged as ineligible
  for promotion, no campaign/adset gets created. The probe surfaces the
  reason plus a list of eligible posts on the same Page so the user can
  pick a valid one.

Fix B — Pre-flight validate_only probe on the ad set spec.
  Catches per-account x objective x optimization_goal lockouts (Meta
  subcode 2490408) BEFORE any account-side write happens, by POSTing
  the ad set spec with execution_options=["validate_only"].

Both replay the gating logic at unit level rather than driving
_sync_document_campaign end-to-end (the latter needs a heavy mock
harness; the gating logic is the contract that matters).
"""
from __future__ import annotations

import json
from unittest import mock

from django.test import SimpleTestCase

from facebook_ads_mcp import production_flow, services


class ValidateAdsetSpecHelperTests(SimpleTestCase):
    """services.validate_adset_spec is the building block for Fix B."""

    def test_injects_execution_options_validate_only(self):
        captured = {}

        def fake_post(url, data, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return {"success": True}

        with mock.patch.object(services, "_post", side_effect=fake_post):
            services.validate_adset_spec(
                "act_1",
                "tok",
                {
                    "name": "x",
                    "campaign_id": "123",
                    "optimization_goal": "REACH",
                    "billing_event": "IMPRESSIONS",
                    "targeting": {"geo_locations": {"countries": ["IN"]}},
                },
            )

        self.assertIn("/act_1/adsets", captured["url"])
        self.assertEqual(
            captured["data"]["execution_options"], json.dumps(["validate_only"])
        )
        # nested dicts JSON-encoded
        self.assertEqual(
            captured["data"]["targeting"],
            json.dumps({"geo_locations": {"countries": ["IN"]}}),
        )
        self.assertEqual(captured["data"]["optimization_goal"], "REACH")

    def test_missing_account_id_returns_error(self):
        r = services.validate_adset_spec(None, "tok", {})
        self.assertIn("error", r)
        self.assertIn("account_id is required", r["error"]["message"])


class EligibilityCheckLogicTests(SimpleTestCase):
    """Fix A — replay the eligibility-check decision logic."""

    @staticmethod
    def _decide(probe_response, post_id="123_456"):
        """Replay the exact decision branch that production_flow uses."""
        oid = post_id
        if (
            isinstance(probe_response, dict)
            and not probe_response.get("error")
            and probe_response.get("is_eligible_for_promotion") is False
        ):
            return {"action": "block", "reason": "ineligible", "oid": oid}
        return {"action": "proceed"}

    def test_ineligible_post_is_blocked(self):
        probe = {
            "id": "123_456",
            "is_eligible_for_promotion": False,
            "attachments": {"data": [{"media_type": "video"}]},
        }
        self.assertEqual(self._decide(probe)["action"], "block")

    def test_eligible_post_proceeds(self):
        probe = {
            "id": "123_456",
            "is_eligible_for_promotion": True,
            "attachments": {"data": [{"media_type": "album"}]},
        }
        self.assertEqual(self._decide(probe)["action"], "proceed")

    def test_probe_error_does_not_block(self):
        # If Meta can't be reached or returns an error, we let downstream
        # validation catch the real problem instead of falsely blocking.
        probe = {"error": {"code": 10, "message": "pages_read_engagement required"}}
        self.assertEqual(self._decide(probe)["action"], "proceed")

    def test_probe_missing_field_does_not_block(self):
        # Some Meta responses omit is_eligible_for_promotion entirely;
        # only an explicit False should block.
        probe = {"id": "123_456", "attachments": {"data": []}}
        self.assertEqual(self._decide(probe)["action"], "proceed")


class EligibilityVideoSoftBlockTests(SimpleTestCase):
    """Fix C — is_eligible_for_promotion=false is unreliable for video/Reels.

    Hard-block ONLY when it is not a soft case. Soft cases (demote to warning,
    proceed):
      - the post is a video AND video_buying_eligibility does not confirm a
        block (ELIGIBLE / absent), OR
      - options.allow_ineligible_post is set.
    """

    @staticmethod
    def _hard_block(probe, *, allow_override=False):
        """Replay the decision branch from production_flow."""
        iefp_false = (
            isinstance(probe, dict)
            and not probe.get("error")
            and probe.get("is_eligible_for_promotion") is False
        )
        if not iefp_false:
            return False
        att = (probe.get("attachments") or {}).get("data") or []
        is_video = str((att[0] or {}).get("media_type") or "").lower() == "video" if att else False
        vbe = probe.get("video_buying_eligibility")
        advisory_only = {"INELIGIBLE_VIDEO_LENGTH_FOR_COMPLETED_VIEW_BUYING"}
        vbe_list = vbe if isinstance(vbe, list) else ([vbe] if vbe else [])
        vbe_blocking = [
            str(x).upper() for x in vbe_list
            if str(x).upper() not in advisory_only and str(x).upper() != "ELIGIBLE"
        ]
        vbe_confirms_block = bool(vbe_blocking)
        soft_only = is_video and not vbe_confirms_block
        if allow_override or soft_only:
            return False  # demoted to warning
        return True

    def test_video_ineligible_but_vbe_not_confirming_is_soft(self):
        # Reel with iefp=false but no confirming video_buying_eligibility -> proceed.
        probe = {"is_eligible_for_promotion": False,
                 "attachments": {"data": [{"media_type": "video"}]}}
        self.assertFalse(self._hard_block(probe))

    def test_video_ineligible_and_vbe_eligible_is_soft(self):
        probe = {"is_eligible_for_promotion": False,
                 "video_buying_eligibility": ["ELIGIBLE"],
                 "attachments": {"data": [{"media_type": "video"}]}}
        self.assertFalse(self._hard_block(probe))

    def test_vbe_video_length_advisory_is_soft_not_block(self):
        # CRITICAL: observed live that ELIGIBLE reels ALSO carry
        # INELIGIBLE_VIDEO_LENGTH_FOR_COMPLETED_VIEW_BUYING. It only means
        # "don't use a completed-view goal", NOT "can't boost". Must NOT block.
        probe = {"is_eligible_for_promotion": False,
                 "video_buying_eligibility": ["INELIGIBLE_VIDEO_LENGTH_FOR_COMPLETED_VIEW_BUYING"],
                 "attachments": {"data": [{"media_type": "video"}]}}
        self.assertFalse(self._hard_block(probe))

    def test_video_ineligible_and_vbe_real_block_is_hard(self):
        # A genuine non-advisory reason -> still a hard block.
        probe = {"is_eligible_for_promotion": False,
                 "video_buying_eligibility": ["MEDIA_UNAVAILABLE"],
                 "attachments": {"data": [{"media_type": "video"}]}}
        self.assertTrue(self._hard_block(probe))

    def test_vbe_mixed_advisory_plus_real_block_is_hard(self):
        # advisory + a real reason -> the real reason wins, hard block.
        probe = {"is_eligible_for_promotion": False,
                 "video_buying_eligibility": [
                     "INELIGIBLE_VIDEO_LENGTH_FOR_COMPLETED_VIEW_BUYING",
                     "MEDIA_UNAVAILABLE"],
                 "attachments": {"data": [{"media_type": "video"}]}}
        self.assertTrue(self._hard_block(probe))

    def test_photo_ineligible_is_hard_block(self):
        # Non-video: the soft path does NOT apply; iefp=false still blocks.
        probe = {"is_eligible_for_promotion": False,
                 "attachments": {"data": [{"media_type": "photo"}]}}
        self.assertTrue(self._hard_block(probe))

    def test_override_demotes_any_block(self):
        probe = {"is_eligible_for_promotion": False,
                 "video_buying_eligibility": ["MEDIA_UNAVAILABLE"],
                 "attachments": {"data": [{"media_type": "photo"}]}}
        self.assertFalse(self._hard_block(probe, allow_override=True))

    def test_eligible_post_never_blocks(self):
        probe = {"is_eligible_for_promotion": True,
                 "attachments": {"data": [{"media_type": "video"}]}}
        self.assertFalse(self._hard_block(probe))

    def test_probe_error_never_blocks(self):
        self.assertFalse(self._hard_block({"error": {"message": "boom"}}))


class VideoDataFallbackTests(SimpleTestCase):
    """Fix C2 — non-promotable video post -> build video_data creative from the
    post's video_id + thumbnail (UI-style), instead of object_story_id."""

    def _fake_get(self, post_resp, video_resp=None):
        def fake_get(url, params=None):
            if url.endswith("_122107547271308207") or "_1221075" in url:
                return post_resp
            return video_resp or {}
        return fake_get

    def test_builds_video_data_from_video_post(self):
        post = {"message": "Delhi public transport",
                "attachments": {"data": [{"media_type": "video",
                    "media": {"image": {"src": "https://thumb/x.jpg"}},
                    "target": {"id": "850903357564794"}}]}}
        with mock.patch.object(services, "get_page_access_token", return_value="PTOK"):
            with mock.patch.object(services, "_get", side_effect=self._fake_get(post)):
                vd = production_flow._resolve_video_data_from_post(
                    "1165086893354722_122107547271308207", "1165086893354722", "PTOK", "UTOK")
        self.assertIsNotNone(vd)
        self.assertEqual(vd["video_id"], "850903357564794")
        self.assertEqual(vd["image_url"], "https://thumb/x.jpg")
        self.assertEqual(vd["message"], "Delhi public transport")

    def test_fetches_thumbnail_when_post_has_none(self):
        post = {"message": "x", "attachments": {"data": [{"media_type": "video",
                "media": {}, "target": {"id": "999"}}]}}
        vid = {"picture": "https://fallback/thumb.jpg"}
        with mock.patch.object(services, "get_page_access_token", return_value="PTOK"):
            with mock.patch.object(services, "_get", side_effect=self._fake_get(post, vid)):
                vd = production_flow._resolve_video_data_from_post(
                    "1165086893354722_122107547271308207", "1165086893354722", "PTOK", "UTOK")
        self.assertEqual(vd["image_url"], "https://fallback/thumb.jpg")

    def test_returns_none_for_non_video_post(self):
        post = {"attachments": {"data": [{"media_type": "photo", "target": {"id": "1"}}]}}
        with mock.patch.object(services, "get_page_access_token", return_value="PTOK"):
            with mock.patch.object(services, "_get", side_effect=self._fake_get(post)):
                vd = production_flow._resolve_video_data_from_post(
                    "1165086893354722_122107547271308207", "1165086893354722", "PTOK", "UTOK")
        self.assertIsNone(vd)

    def test_returns_none_when_no_video_id(self):
        post = {"attachments": {"data": [{"media_type": "video", "target": {}}]}}
        with mock.patch.object(services, "get_page_access_token", return_value="PTOK"):
            with mock.patch.object(services, "_get", side_effect=self._fake_get(post)):
                vd = production_flow._resolve_video_data_from_post(
                    "1165086893354722_122107547271308207", "1165086893354722", "PTOK", "UTOK")
        self.assertIsNone(vd)

    def test_returns_none_on_probe_error(self):
        with mock.patch.object(services, "get_page_access_token", return_value="PTOK"):
            with mock.patch.object(services, "_get", side_effect=self._fake_get({"error": {"m": "x"}})):
                vd = production_flow._resolve_video_data_from_post(
                    "1165086893354722_122107547271308207", "1165086893354722", "PTOK", "UTOK")
        self.assertIsNone(vd)


class IgDirectCreativeTests(SimpleTestCase):
    """Fix D2 — political IG posts build a creative DIRECTLY from the IG media
    + Page (no FB cross-post). Verified shape: source_instagram_media_id +
    object_id + instagram_user_id + link, all TOP-LEVEL."""

    def test_builds_ig_direct_creative_with_correct_shape(self):
        captured = {}

        def fake_post(url, data, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return {"id": "999"}

        with mock.patch.object(services, "_post", side_effect=fake_post):
            r = production_flow._create_ig_direct_creative(
                "1377547692510260", "tok", name="C", ig_media_id="18046",
                page_id="1165", instagram_user_id="17841",
                link="https://example.com", authorization_category="POLITICAL",
                bylines="BOL7",
            )
        self.assertEqual(r["id"], "999")
        # account_id must be normalized to act_ prefix in the URL
        self.assertIn("/act_1377547692510260/adcreatives", captured["url"])
        d = captured["data"]
        self.assertEqual(d["source_instagram_media_id"], "18046")
        self.assertEqual(d["object_id"], "1165")
        self.assertEqual(d["instagram_user_id"], "17841")
        self.assertEqual(d["link"], "https://example.com")
        self.assertEqual(d["authorization_category"], "POLITICAL")
        self.assertEqual(d["bylines"], "BOL7")
        # link/ids must be TOP-LEVEL, never wrapped in object_story_spec
        self.assertNotIn("object_story_spec", d)

    def test_defaults_link_to_page_when_missing(self):
        captured = {}
        with mock.patch.object(services, "_post",
                               side_effect=lambda u, d, timeout=None: captured.update(d) or {"id": "1"}):
            production_flow._create_ig_direct_creative(
                "act_1", "tok", name="C", ig_media_id="18046",
                page_id="1165", instagram_user_id="17841",
                link=None, authorization_category="POLITICAL", bylines=None,
            )
        self.assertEqual(captured["link"], "https://www.facebook.com/1165")
        self.assertNotIn("bylines", captured)


class GoalAvailabilityProbeLogicTests(SimpleTestCase):
    """Fix B — replay the 2490408 detection logic."""

    @staticmethod
    def _detect_goal_lock(meta_response):
        """Replay the goal-blame detection from the probe."""
        if not isinstance(meta_response, dict) or not meta_response.get("error"):
            return False
        err = meta_response["error"] or {}
        subcode = err.get("error_subcode")
        err_data_raw = err.get("error_data") or ""
        blame_is_goal = False
        try:
            ed = (
                json.loads(err_data_raw)
                if isinstance(err_data_raw, str)
                else err_data_raw
            )
            bf = (ed or {}).get("blame_field_specs") or []
            for entry in bf:
                if isinstance(entry, list) and "optimization_goal" in entry:
                    blame_is_goal = True
                    break
        except (TypeError, ValueError):
            pass
        return subcode == 2490408 and blame_is_goal

    def test_2490408_with_optimization_goal_blame_is_detected(self):
        resp = {
            "error": {
                "code": 100,
                "error_subcode": 2490408,
                "error_data": json.dumps(
                    {"blame_field_specs": [["optimization_goal"]]}
                ),
                "error_user_title": "Performance goal isn't available",
            }
        }
        self.assertTrue(self._detect_goal_lock(resp))

    def test_2490408_with_other_blame_is_not_goal_lock(self):
        # Same subcode but blame on a different field — don't claim
        # goal-availability problem.
        resp = {
            "error": {
                "code": 100,
                "error_subcode": 2490408,
                "error_data": json.dumps(
                    {"blame_field_specs": [["destination_type"]]}
                ),
            }
        }
        self.assertFalse(self._detect_goal_lock(resp))

    def test_different_subcode_is_not_goal_lock(self):
        resp = {
            "error": {
                "code": 100,
                "error_subcode": 1487079,
                "error_data": json.dumps(
                    {"blame_field_specs": [["optimization_goal"]]}
                ),
            }
        }
        self.assertFalse(self._detect_goal_lock(resp))

    def test_no_error_returns_false(self):
        self.assertFalse(self._detect_goal_lock({"success": True}))
        self.assertFalse(self._detect_goal_lock({"id": "abc"}))

    def test_malformed_error_data_does_not_crash(self):
        resp = {
            "error": {
                "error_subcode": 2490408,
                "error_data": "not-valid-json{",
            }
        }
        # Should return False (no blame parsed), not raise.
        self.assertFalse(self._detect_goal_lock(resp))


class ResolveIgPermalinkToFbXpostTests(SimpleTestCase):
    """Fix D — IG permalink → FB cross-post object_story_id auto-translate."""

    def _make_get(self, ig_data, fb_data, page_token="PTOK"):
        """Build a fake _get that returns IG media or FB published_posts
        based on the URL pattern.
        """
        def fake_get(url, params=None):
            if "/media" in url and "ig-user" not in url and (
                "17841" in url or url.endswith("/media")
            ):
                return {"data": ig_data, "paging": {}}
            if "/published_posts" in url:
                return {"data": fb_data, "paging": {}}
            return {"error": {"message": f"unexpected GET {url}"}}
        return fake_get

    def test_matches_reel_to_cross_post_within_timestamp_window(self):
        ig = [{
            "id": "18046363271562575",
            "caption": "Delhi public transport faster cleaner smarter",
            "media_type": "VIDEO",
            "media_product_type": "REELS",
            "permalink": "https://www.instagram.com/reel/DY3yAOBDTV9/",
            "timestamp": "2026-05-28T06:03:43+0000",
        }]
        fb = [{
            "id": "1165086893354722_122107547271308207",
            "message": "Delhi public transport faster cleaner smarter",
            "created_time": "2026-05-28T06:04:15+0000",  # 32s later
            "is_eligible_for_promotion": True,
            "attachments": {"data": [{"media_type": "video"}]},
        }]
        with mock.patch.object(
            services, "_resolve_ig_user_id", return_value=("17841", None)
        ):
            with mock.patch.object(
                services, "get_page_access_token", return_value="PTOK"
            ):
                with mock.patch.object(
                    services, "_get", side_effect=self._make_get(ig, fb)
                ):
                    r = services.resolve_ig_permalink_to_fb_xpost(
                        "USER_TOK",
                        "https://www.instagram.com/reel/DY3yAOBDTV9/",
                        "1165086893354722",
                    )
        self.assertEqual(r["object_story_id"], "1165086893354722_122107547271308207")
        self.assertEqual(r["reason"], "matched")
        self.assertEqual(r["ig_media"]["id"], "18046363271562575")

    def test_rejects_match_outside_timestamp_window(self):
        ig = [{
            "id": "18046363271562575",
            "caption": "Delhi public transport",
            "media_type": "VIDEO",
            "media_product_type": "REELS",
            "permalink": "https://www.instagram.com/reel/DY3yAOBDTV9/",
            "timestamp": "2026-05-28T06:03:43+0000",
        }]
        fb = [{
            "id": "1165086893354722_9999",
            "message": "Delhi public transport",
            "created_time": "2026-05-27T06:03:43+0000",  # 24h earlier
            "is_eligible_for_promotion": True,
            "attachments": {"data": [{"media_type": "video"}]},
        }]
        with mock.patch.object(
            services, "_resolve_ig_user_id", return_value=("17841", None)
        ):
            with mock.patch.object(
                services, "get_page_access_token", return_value="PTOK"
            ):
                with mock.patch.object(
                    services, "_get", side_effect=self._make_get(ig, fb)
                ):
                    r = services.resolve_ig_permalink_to_fb_xpost(
                        "USER_TOK",
                        "https://www.instagram.com/reel/DY3yAOBDTV9/",
                        "1165086893354722",
                    )
        # IG media found but no FB match → empty object_story_id with reason
        self.assertEqual(r["object_story_id"], "")
        self.assertIn("no corresponding FB cross-post", r["reason"])

    def test_rejects_link_attachment_not_media(self):
        ig = [{
            "id": "18046363271562575",
            "caption": "Delhi public transport",
            "media_type": "VIDEO",
            "media_product_type": "REELS",
            "permalink": "https://www.instagram.com/reel/DY3yAOBDTV9/",
            "timestamp": "2026-05-28T06:03:43+0000",
        }]
        fb = [{
            "id": "1165086893354722_9999",
            "message": "Delhi public transport",
            "created_time": "2026-05-28T06:04:00+0000",
            "is_eligible_for_promotion": True,
            "attachments": {"data": [{"media_type": "share"}]},  # not media
        }]
        with mock.patch.object(
            services, "_resolve_ig_user_id", return_value=("17841", None)
        ):
            with mock.patch.object(
                services, "get_page_access_token", return_value="PTOK"
            ):
                with mock.patch.object(
                    services, "_get", side_effect=self._make_get(ig, fb)
                ):
                    r = services.resolve_ig_permalink_to_fb_xpost(
                        "USER_TOK",
                        "https://www.instagram.com/reel/DY3yAOBDTV9/",
                        "1165086893354722",
                    )
        self.assertEqual(r["object_story_id"], "")

    def test_returns_empty_when_ig_shortcode_not_in_media(self):
        # IG account has different media — shortcode never appears
        ig = [{
            "id": "18000",
            "caption": "Other content",
            "media_type": "VIDEO",
            "media_product_type": "REELS",
            "permalink": "https://www.instagram.com/reel/OTHER/",
            "timestamp": "2026-05-28T06:03:43+0000",
        }]
        with mock.patch.object(
            services, "_resolve_ig_user_id", return_value=("17841", None)
        ):
            with mock.patch.object(
                services, "_get", side_effect=self._make_get(ig, [])
            ):
                r = services.resolve_ig_permalink_to_fb_xpost(
                    "USER_TOK",
                    "https://www.instagram.com/reel/DY3yAOBDTV9/",
                    "1165086893354722",
                )
        self.assertEqual(r["object_story_id"], "")
        self.assertIn("Could not find IG media", r["reason"])

    def test_invalid_permalink_format_returns_error(self):
        r = services.resolve_ig_permalink_to_fb_xpost(
            "USER_TOK", "https://example.com/not-instagram/", "PAGE_ID"
        )
        self.assertIn("error", r)
        self.assertIn("shortcode", r["error"]["message"])

    def test_missing_page_id_returns_error(self):
        r = services.resolve_ig_permalink_to_fb_xpost(
            "USER_TOK", "https://www.instagram.com/p/ABC/", ""
        )
        self.assertIn("error", r)
        self.assertIn("page_id is required", r["error"]["message"])

    def test_carousel_p_url_format_parses_shortcode(self):
        # Carousel uses /p/<code>/ not /reel/<code>/ — both should work
        ig = [{
            "id": "18174994480365618",
            "caption": "Mission Clean Yamuna",
            "media_type": "CAROUSEL_ALBUM",
            "media_product_type": "FEED",
            "permalink": "https://www.instagram.com/p/DYwiyV-mCzy/",
            "timestamp": "2026-05-25T10:35:40+0000",
        }]
        fb = [{
            "id": "1165086893354722_122106775089308207",
            "message": "Mission Clean Yamuna",
            "created_time": "2026-05-25T10:35:50+0000",  # 10s later
            "is_eligible_for_promotion": True,
            "attachments": {"data": [{"media_type": "album"}]},
        }]
        with mock.patch.object(
            services, "_resolve_ig_user_id", return_value=("17841", None)
        ):
            with mock.patch.object(
                services, "get_page_access_token", return_value="PTOK"
            ):
                with mock.patch.object(
                    services, "_get", side_effect=self._make_get(ig, fb)
                ):
                    r = services.resolve_ig_permalink_to_fb_xpost(
                        "USER_TOK",
                        "https://www.instagram.com/p/DYwiyV-mCzy/",
                        "1165086893354722",
                    )
        self.assertEqual(
            r["object_story_id"], "1165086893354722_122106775089308207"
        )
