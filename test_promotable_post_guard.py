"""
Guard against FABRICATED object_story_id values (incident 2026-09-22).

An agent was told to boost 7 existing Delhi Vibes reels. It invented the
``{page}_{post}`` ids (and the captions), Meta refused them with 100/1487790
and 100/2446187, and the agent reported "these posts are not eligible" — the
opposite of the truth: every real post WAS eligible, the ids pointed at
nothing. These tests lock in the two layers that now prevent that:

1. ``services.verify_promotable_post`` separates EXISTENCE from ELIGIBILITY.
2. ``tool_dispatch`` refuses a non-existent post BEFORE calling Meta.
3. ``api_errors`` offers the wrong-id cause for those two subcodes.
4. The tool spec carries ``_discovery`` so the loop_agent's existing
   hallucination guard rejects unseen ids in-loop.
"""
from __future__ import annotations

import json
from unittest import mock

from django.test import SimpleTestCase

from facebook_ads_mcp import api_errors, services, tool_dispatch, views

PAGE = "1119445944595873"
REAL = f"{PAGE}_122123298735397641"      # verified live: exists + eligible
FAKE = f"{PAGE}_122122789359397641"      # what the agent invented

_NOT_FOUND = {
    "error": {
        "message": "(#10) Object does not exist, cannot be loaded due to missing permissions",
        "type": "GraphMethodException",
        "code": 100,
        "error_subcode": 33,
    }
}


class VerifyPromotablePostTests(SimpleTestCase):
    def _verify(self, probe, oid=REAL):
        with mock.patch.object(services, "get_page_access_token", return_value="PTOK"), \
                mock.patch.object(services, "_get", return_value=probe):
            return services.verify_promotable_post(oid, "UTOK")

    def test_real_eligible_post(self):
        v = self._verify({
            "id": REAL, "message": "Delhi ke development ke liye drain…",
            "is_eligible_for_promotion": True,
        })
        self.assertIs(v["exists"], True)
        self.assertIs(v["eligible"], True)
        self.assertEqual(v["page_id"], PAGE)
        self.assertTrue(v["message"])

    def test_nonexistent_post_is_exists_false(self):
        v = self._verify(_NOT_FOUND, oid=FAKE)
        self.assertIs(v["exists"], False)
        self.assertIn("does not exist", v["error"])

    def test_real_but_ineligible_is_not_reported_as_missing(self):
        """The distinction that the incident conflated."""
        v = self._verify({"id": REAL, "is_eligible_for_promotion": False})
        self.assertIs(v["exists"], True)
        self.assertIs(v["eligible"], False)

    def test_permission_error_is_not_mistaken_for_a_wrong_id(self):
        v = self._verify({"error": {"message": "Rate limit reached", "code": 613}})
        self.assertIsNone(v["exists"], "a transient error must stay 'unknown', not False")

    def test_video_post_is_flagged(self):
        v = self._verify({
            "id": REAL, "is_eligible_for_promotion": True,
            "attachments": {"data": [{"media_type": "video"}]},
            "video_buying_eligibility": ["ELIGIBLE"],
        })
        self.assertTrue(v["is_video"])

    def test_empty_id_short_circuits_without_a_call(self):
        with mock.patch.object(services, "_get") as g:
            v = services.verify_promotable_post("", "UTOK")
        self.assertIs(v["exists"], False)
        g.assert_not_called()

    def test_probe_exception_degrades_to_unknown(self):
        with mock.patch.object(services, "get_page_access_token", return_value="PTOK"), \
                mock.patch.object(services, "_get", side_effect=RuntimeError("boom")):
            v = services.verify_promotable_post(REAL, "UTOK")
        self.assertIsNone(v["exists"])


class CreativePreflightTests(SimpleTestCase):
    """The creative tool must not hand an invented id to Meta."""

    def _dispatch(self, oid, verdict):
        with mock.patch.object(services, "verify_promotable_post", return_value=verdict), \
                mock.patch.object(services, "get_page_posts", return_value={"data": [{"id": REAL}]}), \
                mock.patch.object(services, "create_creative_with_instagram_fallback") as create:
            res = tool_dispatch._dispatch_tool(
                "facebook_create_creative",
                {"name": "Test", "object_story_id": oid},
                "UTOK",
                "3360180104207635",
                False,
            )
        return res, create

    def test_fabricated_id_is_refused_before_meta(self):
        res, create = self._dispatch(FAKE, {"exists": False, "page_id": PAGE, "error": "does not exist"})
        create.assert_not_called()
        self.assertEqual(res.get("step"), "preflight_creative")
        self.assertIn("does not exist", res["error"])
        self.assertIn("NOT an eligibility problem", res["error"])
        self.assertIn("facebook_get_page_posts", res["error"])
        self.assertEqual(res.get("eligible_post_examples"), [REAL])

    def test_real_post_passes_through(self):
        _res, create = self._dispatch(REAL, {"exists": True, "eligible": True, "page_id": PAGE})
        create.assert_called_once()

    def test_unknown_verdict_does_not_block(self):
        """A probe failure must never stop a legitimate create."""
        _res, create = self._dispatch(REAL, {"exists": None, "page_id": PAGE, "error": "rate limited"})
        create.assert_called_once()

    def test_ineligible_but_real_post_still_reaches_the_caller(self):
        """Eligibility stays the caller's decision (video soft-path exists)."""
        _res, create = self._dispatch(REAL, {"exists": True, "eligible": False, "page_id": PAGE})
        create.assert_called_once()


class ErrorHintTests(SimpleTestCase):
    def test_wrong_id_cause_is_offered_for_both_subcodes(self):
        for sub in (1487790, 2446187):
            out = api_errors.explain_graph_error(
                {"code": 100, "error_subcode": sub, "message": "The advert's promoted object is invalid"}
            )
            blob = json.dumps(out).lower()
            self.assertIn("may not exist", blob, f"subcode {sub}")
            self.assertIn("facebook_get_page_posts", blob, f"subcode {sub}")


class DiscoveryMetadataTests(SimpleTestCase):
    """Activates the loop_agent guard — without this the agent may invent ids."""

    def _spec(self, name):
        return next(t for t in views.TOOLS if t.get("name") == name)

    def test_page_posts_declares_object_story_id_discovery(self):
        self.assertEqual(
            self._spec("facebook_get_page_posts").get("_discovery"),
            [["object_story_id", ["data", "*", "id"]]],
        )

    def test_page_posts_returns_caption_and_eligibility_by_default(self):
        captured = {}

        def fake_get_shrinking(url, params):
            captured.update(params)
            return {"data": []}

        with mock.patch.object(services, "_get_shrinking", side_effect=fake_get_shrinking):
            services.get_page_posts(PAGE, "PTOK")
        self.assertIn("message", captured["fields"])
        self.assertIn("is_eligible_for_promotion", captured["fields"])
