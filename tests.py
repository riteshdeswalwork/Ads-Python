"""Tests for the Facebook Ads connector — focused on the targeting re-resolve branch.

The retry loop in :func:`create_adset_with_targeting_retries` used to silently
drop the failing ``flexible_spec`` slice when Meta returned subcode 1487079.
The new behaviour is: harvest names from the popped slice, re-query
``batch_interest_search`` for fresh IDs, and rebuild the slice. These tests
cover the helper and the loop integration without hitting the real Meta API.
"""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from facebook_ads_mcp import production_flow, services


class SavedAudienceMergePreservesCustomAudienceTests(SimpleTestCase):
    """A brief combining a saved audience WITH a lookalike/custom audience must
    keep the custom audience after the saved-audience pre-merge (else it was
    silently dropped — the saved audience's geo-only targeting replaced all)."""

    def test_custom_audience_preserved_when_saved_lacks_it(self):
        saved = {"id": "SAV", "name": "Delhi LA",
                 "targeting": {"geo_locations": {"places": [{"name": "Delhi LA"}]}}}
        with mock.patch.object(services, "get_marketing_object", return_value=saved):
            merged = services.merge_saved_audience_targeting_with_placements(
                "tok", "SAV",
                {"custom_audiences": [{"id": "LAL123"}],
                 "publisher_platforms": ["facebook", "instagram"]},
                advantage_audience=0,
            )
        self.assertEqual(merged["custom_audiences"], [{"id": "LAL123"}])
        # placement overlay still works
        self.assertEqual(merged["publisher_platforms"], ["facebook", "instagram"])
        # saved geo retained
        self.assertIn("geo_locations", merged)

    def test_saved_audience_own_custom_audience_wins(self):
        saved = {"id": "SAV", "name": "x",
                 "targeting": {"custom_audiences": [{"id": "SAVED_CA"}],
                               "geo_locations": {"countries": ["IN"]}}}
        with mock.patch.object(services, "get_marketing_object", return_value=saved):
            merged = services.merge_saved_audience_targeting_with_placements(
                "tok", "SAV", {"custom_audiences": [{"id": "DOC_CA"}]}, 0)
        # saved audience already defines custom_audiences -> it wins
        self.assertEqual(merged["custom_audiences"], [{"id": "SAVED_CA"}])


class ReresolveDroppedInterestsTests(SimpleTestCase):
    def test_returns_none_for_non_dict(self):
        self.assertIsNone(
            production_flow._reresolve_dropped_interests("act_1", "tok", None)
        )
        self.assertIsNone(
            production_flow._reresolve_dropped_interests("act_1", "tok", "not a dict")
        )

    def test_rebuilds_slice_with_fresh_ids(self):
        popped = {
            "interests": [
                {"id": "60030000000", "name": "Lead Generation"},
                {"id": "60040000000", "name": "B2B Marketing"},
            ],
        }

        def fake_search(account_id, token, names):
            self.assertEqual(account_id, "act_1")
            self.assertEqual(token, "tok")
            self.assertEqual(list(names), ["Lead Generation", "B2B Marketing"])
            return {
                "found": [
                    {"id": "6003111122223", "name": "Lead Generation", "query": "Lead Generation"},
                    {"id": "6003999988887", "name": "B2B Marketing", "query": "B2B Marketing"},
                ],
                "missing": [],
            }

        with mock.patch.object(production_flow.services, "batch_interest_search", side_effect=fake_search):
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                rebuilt = production_flow._reresolve_dropped_interests("act_1", "tok", popped)

        self.assertIsNotNone(rebuilt)
        self.assertEqual(
            [it["id"] for it in rebuilt["interests"]],
            ["6003111122223", "6003999988887"],
        )

    def test_filters_out_known_bad_ids_from_search_results(self):
        popped = {"interests": [{"id": "60030000000", "name": "Sales"}]}

        def fake_search(*_a, **_kw):
            return {"found": [{"id": "6297846662583", "name": "Sales", "query": "Sales"}], "missing": []}

        with mock.patch.object(production_flow.services, "batch_interest_search", side_effect=fake_search):
            with mock.patch.object(
                production_flow, "_learned_bad_interest_ids", return_value={"6297846662583"}
            ):
                rebuilt = production_flow._reresolve_dropped_interests("act_1", "tok", popped)

        # Search only returned a known-bad ID → nothing to rebuild → None.
        self.assertIsNone(rebuilt)

    def test_preserves_non_interest_axes(self):
        popped = {
            "interests": [{"id": "60030000000", "name": "Sales"}],
            "behaviors": [{"id": "6002714898572", "name": "Engaged Shoppers"}],
        }

        def fake_search(*_a, **_kw):
            return {
                "found": [{"id": "6003111122223", "name": "Sales", "query": "Sales"}],
                "missing": [],
            }

        with mock.patch.object(production_flow.services, "batch_interest_search", side_effect=fake_search):
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                rebuilt = production_flow._reresolve_dropped_interests("act_1", "tok", popped)

        self.assertIsNotNone(rebuilt)
        self.assertEqual(rebuilt["interests"][0]["id"], "6003111122223")
        # Behaviors are preserved as-is — re-resolve only handles interests.
        self.assertEqual(rebuilt["behaviors"][0]["id"], "6002714898572")

    def test_no_names_means_no_op(self):
        popped = {"interests": [{"id": "60030000000"}]}  # no name → nothing to search
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                rebuilt = production_flow._reresolve_dropped_interests("act_1", "tok", popped)
        self.assertIsNone(rebuilt)
        m.assert_not_called()

    def test_search_exception_returns_none(self):
        popped = {"interests": [{"id": "60030000000", "name": "Sales"}]}
        with mock.patch.object(
            production_flow.services, "batch_interest_search", side_effect=RuntimeError("network down")
        ):
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                rebuilt = production_flow._reresolve_dropped_interests("act_1", "tok", popped)
        self.assertIsNone(rebuilt)


class CreateAdsetRetryLoopTests(SimpleTestCase):
    """Test the retry loop calls re-resolve before falling back to drop."""

    def _meta_deprecated_error(self, msg="category no longer available"):
        return {"error": {"message": msg, "error_subcode": 1487079, "last_response": {}}}

    def test_retry_uses_reresolved_interest_on_second_attempt(self):
        """First create_adset call fails with 1487079; loop re-resolves and retries."""
        targeting = {
            "geo_locations": {"countries": ["IN"]},
            "flexible_spec": [
                {"interests": [{"id": "60030000000", "name": "Lead Generation"}]},
            ],
        }
        fixes: list[str] = []

        responses = [
            self._meta_deprecated_error(),
            {"id": "act_adset_123"},  # success on second attempt
        ]
        captured_targeting: list[dict] = []

        def fake_create_adset(*_a, **kwargs):
            captured_targeting.append(dict(kwargs.get("targeting") or {}))
            return responses.pop(0)

        def fake_search(*_a, **_kw):
            return {
                "found": [
                    {"id": "6003111122223", "name": "Lead Generation", "query": "Lead Generation"},
                ],
                "missing": [],
            }

        with mock.patch.object(production_flow.services, "create_adset", side_effect=fake_create_adset):
            with mock.patch.object(
                production_flow.services, "batch_interest_search", side_effect=fake_search
            ):
                with mock.patch.object(
                    production_flow, "_learned_bad_interest_ids", return_value=set()
                ):
                    with mock.patch.object(production_flow, "_record_bad_interest_ids"):
                        result = production_flow.create_adset_with_targeting_retries(
                            account_id="act_1",
                            token="tok",
                            campaign_id="c1",
                            name="adset",
                            status="PAUSED",
                            billing_event="IMPRESSIONS",
                            optimization_goal="LANDING_PAGE_VIEWS",
                            targeting=targeting,
                            daily_budget=10000,
                            lifetime_budget=None,
                            bid_strategy=None,
                            promoted_object=None,
                            page_id=None,
                            destination_type=None,
                            start_time=None,
                            end_time=None,
                            fixes=fixes,
                        )

        self.assertEqual(result, {"id": "act_adset_123"})
        # The second create_adset call should have the rebuilt slice with the fresh ID.
        self.assertEqual(len(captured_targeting), 2)
        retry_flex = captured_targeting[1].get("flexible_spec") or []
        self.assertTrue(retry_flex, "retry should still have a flexible_spec")
        retry_ids = [it.get("id") for it in (retry_flex[-1].get("interests") or [])]
        self.assertIn("6003111122223", retry_ids)
        self.assertNotIn("60030000000", retry_ids)
        # The re-resolve event should be reported in fixes.
        self.assertTrue(any("re-resolved" in f for f in fixes), fixes)

    def test_retry_drops_when_search_returns_nothing(self):
        """If batch_interest_search returns no usable IDs, fall back to dropping the slice."""
        targeting = {
            "flexible_spec": [
                {"interests": [{"id": "60030000000", "name": "Sales"}]},
            ],
        }
        fixes: list[str] = []

        # Two failures so the loop has to drop after re-resolve fails.
        responses = [self._meta_deprecated_error(), self._meta_deprecated_error()]
        captured_targeting: list[dict] = []

        def fake_create_adset(*_a, **kwargs):
            captured_targeting.append(dict(kwargs.get("targeting") or {}))
            return responses.pop(0) if responses else {"error": {"message": "exhausted"}}

        def fake_search(*_a, **_kw):
            # Returns nothing usable.
            return {"found": [], "missing": ["Sales"]}

        with mock.patch.object(production_flow.services, "create_adset", side_effect=fake_create_adset):
            with mock.patch.object(
                production_flow.services, "batch_interest_search", side_effect=fake_search
            ):
                with mock.patch.object(
                    production_flow, "_learned_bad_interest_ids", return_value=set()
                ):
                    with mock.patch.object(production_flow, "_record_bad_interest_ids"):
                        result = production_flow.create_adset_with_targeting_retries(
                            account_id="act_1",
                            token="tok",
                            campaign_id="c1",
                            name="adset",
                            status="PAUSED",
                            billing_event="IMPRESSIONS",
                            optimization_goal="LANDING_PAGE_VIEWS",
                            targeting=targeting,
                            daily_budget=10000,
                            lifetime_budget=None,
                            bid_strategy=None,
                            promoted_object=None,
                            page_id=None,
                            destination_type=None,
                            start_time=None,
                            end_time=None,
                            fixes=fixes,
                            max_retries=2,
                        )

        # Loop exhausted both attempts (re-resolve returned None → slice dropped).
        self.assertIn("error", result)
        # Second call should have flexible_spec stripped.
        self.assertNotIn("flexible_spec", captured_targeting[1])
        self.assertTrue(any("dropped last flexible_spec" in f for f in fixes), fixes)


# ---------------------------------------------------------------------------
# Cross-account image_hash guard
# ---------------------------------------------------------------------------


class CheckImageHashInAccountTests(SimpleTestCase):
    """services.check_image_hash_in_account converts the Graph response into
    a tri-state (found / not-found / unknown) so the sync flow can fail
    closed only on a clean empty-data response from Meta."""

    def test_found_when_meta_returns_matching_hash(self):
        with mock.patch.object(
            services, "_get",
            return_value={"data": [{"hash": "abc123"}]},
        ):
            result = services.check_image_hash_in_account("act_1", "tok", "abc123")
        self.assertTrue(result["known"])
        self.assertTrue(result["found"])

    def test_not_found_when_meta_returns_empty_data(self):
        with mock.patch.object(services, "_get", return_value={"data": []}):
            result = services.check_image_hash_in_account("act_1", "tok", "abc123")
        self.assertTrue(result["known"])
        self.assertFalse(result["found"])

    def test_unknown_when_meta_returns_error(self):
        """Token without ads_read on this account → don't block creative."""
        with mock.patch.object(
            services, "_get",
            return_value={"error": {"message": "(#200) Permissions error"}},
        ):
            result = services.check_image_hash_in_account("act_1", "tok", "abc123")
        self.assertFalse(result["known"])
        self.assertFalse(result["found"])

    def test_missing_account_id_returns_unknown(self):
        result = services.check_image_hash_in_account("", "tok", "abc123")
        self.assertFalse(result["known"])

    def test_missing_image_hash_returns_unknown(self):
        result = services.check_image_hash_in_account("act_1", "tok", "")
        self.assertFalse(result["known"])

    def test_passes_hashes_param_as_json_array(self):
        captured = {}

        def fake_get(url, params, **_kw):
            captured["url"] = url
            captured["params"] = params
            return {"data": [{"hash": "abc123"}]}

        with mock.patch.object(services, "_get", side_effect=fake_get):
            services.check_image_hash_in_account("1377547692510260", "tok", "abc123")

        self.assertIn("act_1377547692510260/adimages", captured["url"])
        # Meta's hashes param is a JSON-encoded array string, not CSV.
        self.assertEqual(captured["params"]["hashes"], '["abc123"]')


# ---------------------------------------------------------------------------
# Page→account guard
# ---------------------------------------------------------------------------


class CheckPageInAccountTests(SimpleTestCase):
    """services.check_page_in_account converts the /promote_pages response
    into a tri-state so the sync flow can fail closed only when Meta
    confirms the page is not connected to the target ad account."""

    def test_found_when_page_is_in_promote_pages(self):
        with mock.patch.object(
            services, "_get",
            return_value={"data": [
                {"id": "999", "name": "Other"},
                {"id": "105263695507393", "name": "BOL7"},
            ]},
        ):
            result = services.check_page_in_account("act_1", "tok", "105263695507393")
        self.assertTrue(result["known"])
        self.assertTrue(result["found"])
        ids = [p["id"] for p in result["available_pages"]]
        self.assertIn("105263695507393", ids)

    def test_not_found_when_page_absent(self):
        with mock.patch.object(
            services, "_get",
            return_value={"data": [{"id": "999", "name": "Other"}]},
        ):
            result = services.check_page_in_account("act_1", "tok", "105263695507393")
        self.assertTrue(result["known"])
        self.assertFalse(result["found"])
        self.assertEqual(result["available_pages"], [{"id": "999", "name": "Other"}])

    def test_unknown_when_meta_returns_error(self):
        """Token without pages_show_list → don't block adset create."""
        with mock.patch.object(
            services, "_get",
            return_value={"error": {"message": "(#200) Permissions error"}},
        ):
            result = services.check_page_in_account("act_1", "tok", "105263695507393")
        self.assertFalse(result["known"])
        self.assertFalse(result["found"])

    def test_missing_page_id_returns_unknown(self):
        result = services.check_page_in_account("act_1", "tok", "")
        self.assertFalse(result["known"])

    def test_hits_promote_pages_endpoint(self):
        captured = {}

        def fake_get(url, params, **_kw):
            captured["url"] = url
            return {"data": [{"id": "1", "name": "X"}]}

        with mock.patch.object(services, "_get", side_effect=fake_get):
            services.check_page_in_account("1377547692510260", "tok", "1")

        self.assertIn("act_1377547692510260/promote_pages", captured["url"])


# ---------------------------------------------------------------------------
# Headline + description plumbing through build_link_object_story_spec
# ---------------------------------------------------------------------------

class BuildLinkObjectStorySpecTests(SimpleTestCase):
    def test_includes_headline_and_description_in_link_data(self):
        spec = production_flow.build_link_object_story_spec(
            page_id="123",
            link="https://example.com",
            message="body copy",
            cta_type="LEARN_MORE",
            image_hash="abc",
            name="My Headline",
            description="My description",
        )
        self.assertEqual(spec["link_data"]["name"], "My Headline")
        self.assertEqual(spec["link_data"]["description"], "My description")
        self.assertEqual(spec["link_data"]["link"], "https://example.com")
        self.assertEqual(spec["link_data"]["message"], "body copy")
        self.assertEqual(spec["link_data"]["image_hash"], "abc")

    def test_omits_blank_headline_and_description(self):
        spec = production_flow.build_link_object_story_spec(
            page_id="123",
            link="https://example.com",
            message="body",
            cta_type="LEARN_MORE",
            image_hash=None,
            name="",
            description=None,
        )
        self.assertNotIn("name", spec["link_data"])
        self.assertNotIn("description", spec["link_data"])

    def test_strips_whitespace_only_values(self):
        spec = production_flow.build_link_object_story_spec(
            page_id="123",
            link="https://example.com",
            message="body",
            cta_type="LEARN_MORE",
            image_hash=None,
            name="   ",
            description="\t\n",
        )
        self.assertNotIn("name", spec["link_data"])
        self.assertNotIn("description", spec["link_data"])


# ---------------------------------------------------------------------------
# Multi-axis flexible_spec retention
# ---------------------------------------------------------------------------

class SliceHasAnyTargetingTests(SimpleTestCase):
    def test_returns_true_for_work_employers_only(self):
        self.assertTrue(
            production_flow._slice_has_any_targeting({"work_employers": [{"id": "1", "name": "Acme"}]})
        )

    def test_returns_true_for_income_only(self):
        self.assertTrue(
            production_flow._slice_has_any_targeting({"income": [{"id": "6017253486583", "name": "Top 25%"}]})
        )

    def test_returns_true_for_industries_only(self):
        self.assertTrue(
            production_flow._slice_has_any_targeting({"industries": [{"id": "1", "name": "Tech"}]})
        )

    def test_returns_false_for_empty_dict(self):
        self.assertFalse(production_flow._slice_has_any_targeting({}))

    def test_returns_false_for_empty_lists(self):
        self.assertFalse(
            production_flow._slice_has_any_targeting({"interests": [], "behaviors": []})
        )

    def test_returns_false_for_non_dict(self):
        self.assertFalse(production_flow._slice_has_any_targeting(None))
        self.assertFalse(production_flow._slice_has_any_targeting("not a dict"))

    def test_axis_list_covers_known_meta_targeting_axes(self):
        # Sanity: the axes the retry loop already iterates for ID-learning
        # MUST all be in _TARGETING_AXES_FOR_KEEP, otherwise pruning + keep
        # can disagree.
        retry_axes = (
            "interests", "behaviors", "demographics",
            "work_employers", "work_positions",
            "education_majors", "education_schools",
        )
        for axis in retry_axes:
            self.assertIn(axis, production_flow._TARGETING_AXES_FOR_KEEP, f"missing axis: {axis}")


# ---------------------------------------------------------------------------
# Auto-resolution phase tests
# ---------------------------------------------------------------------------

class AutoresolvePixelTests(SimpleTestCase):
    def test_picks_most_recently_fired(self):
        doc = {
            "adset": {"optimization_goal": "LANDING_PAGE_VIEWS"},
            "tracking": {},
        }
        fixes: list = []
        pixels = {"data": [
            {"id": "p_old", "name": "Old", "last_fired_time": "2025-01-01T00:00:00+0000"},
            {"id": "p_new", "name": "New", "last_fired_time": "2026-04-01T12:00:00+0000"},
            {"id": "p_mid", "name": "Mid", "last_fired_time": "2026-02-15T08:00:00+0000"},
        ]}
        with mock.patch.object(production_flow.services, "get_pixels", return_value=pixels):
            production_flow._autoresolve_pixel(doc, "act_1", "tok", fixes)
        self.assertEqual(doc["tracking"]["pixel_id"], "p_new")
        self.assertEqual(doc["tracking"]["custom_event_type"], "CONTENT_VIEW")
        self.assertTrue(any('"New"' in f and "p_new" in f for f in fixes), fixes)

    def test_skipped_when_brief_provides_pixel(self):
        doc = {
            "adset": {"optimization_goal": "LANDING_PAGE_VIEWS"},
            "tracking": {"pixel_id": "user_supplied"},
        }
        fixes: list = []
        with mock.patch.object(production_flow.services, "get_pixels") as m:
            production_flow._autoresolve_pixel(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        self.assertEqual(doc["tracking"]["pixel_id"], "user_supplied")

    def test_skipped_when_goal_not_in_tracking_set(self):
        doc = {"adset": {"optimization_goal": "REACH"}, "tracking": {}}
        with mock.patch.object(production_flow.services, "get_pixels") as m:
            production_flow._autoresolve_pixel(doc, "act_1", "tok", [])
        m.assert_not_called()

    def test_no_pixels_appends_warning(self):
        doc = {"adset": {"optimization_goal": "LANDING_PAGE_VIEWS"}, "tracking": {}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "get_pixels", return_value={"data": []}):
            production_flow._autoresolve_pixel(doc, "act_1", "tok", fixes)
        self.assertNotIn("pixel_id", doc["tracking"])
        self.assertTrue(any("no Meta Pixels" in f.lower() or "no meta pixels" in f.lower() for f in fixes))


class AutoresolvePageTests(SimpleTestCase):
    def test_raises_when_multiple_pages_and_no_name(self):
        """When multiple Pages are accessible and the brief gives neither
        page_id nor page_name, refuse to silently pick the first — raise
        ValueError listing the accessible Pages so the caller can pick."""
        doc = {"creative": {}, "adset": {}}
        fixes: list = []
        pages = {"data": [{"id": "page_1", "name": "BOL7"}, {"id": "page_2", "name": "Other"}]}
        with mock.patch.object(production_flow.services, "get_pages", return_value=pages):
            with self.assertRaises(ValueError) as ctx:
                production_flow._autoresolve_page(doc, "tok", fixes)
        msg = str(ctx.exception)
        self.assertIn("multiple Pages are accessible", msg)
        self.assertIn("BOL7", msg)
        self.assertIn("Other", msg)

    def test_picks_only_page_when_exactly_one(self):
        """When exactly ONE page is accessible, pick it (no ambiguity)."""
        doc = {"creative": {}, "adset": {}}
        fixes: list = []
        pages = {"data": [{"id": "page_only", "name": "OnlyPage"}]}
        with mock.patch.object(production_flow.services, "get_pages", return_value=pages):
            production_flow._autoresolve_page(doc, "tok", fixes)
        self.assertEqual(doc["creative"]["page_id"], "page_only")
        self.assertTrue(any("OnlyPage" in f for f in fixes))

    def test_resolves_page_name_exact_match(self):
        """creative.page_name lets the brief lookup by name; exact case-
        insensitive match wins."""
        doc = {"creative": {"page_name": "Rekha Gupta Fans Club"}, "adset": {}}
        fixes: list = []
        pages = {"data": [
            {"id": "page_1", "name": "BOL7"},
            {"id": "page_2", "name": "Rekha Gupta Fans Club"},
        ]}
        with mock.patch.object(production_flow.services, "get_pages", return_value=pages):
            production_flow._autoresolve_page(doc, "tok", fixes)
        self.assertEqual(doc["creative"]["page_id"], "page_2")

    def test_resolves_page_name_substring_match(self):
        """When no exact match exists but exactly one Page contains the
        requested name as a substring, accept that one and log the fix."""
        doc = {"creative": {"page_name": "Rekha Gupta"}, "adset": {}}
        fixes: list = []
        pages = {"data": [
            {"id": "page_1", "name": "BOL7"},
            {"id": "page_2", "name": "Rekha Gupta Fans Club"},
        ]}
        with mock.patch.object(production_flow.services, "get_pages", return_value=pages):
            production_flow._autoresolve_page(doc, "tok", fixes)
        self.assertEqual(doc["creative"]["page_id"], "page_2")
        self.assertTrue(any("substring match" in f for f in fixes))

    def test_raises_when_page_name_not_accessible(self):
        """When the requested Page name is not in the get_pages result, raise
        ValueError listing accessible Pages — never silently pick another."""
        doc = {"creative": {"page_name": "Nonexistent Page"}, "adset": {}}
        fixes: list = []
        pages = {"data": [{"id": "page_1", "name": "BOL7"}]}
        with mock.patch.object(production_flow.services, "get_pages", return_value=pages):
            with self.assertRaises(ValueError) as ctx:
                production_flow._autoresolve_page(doc, "tok", fixes)
        msg = str(ctx.exception)
        self.assertIn("not accessible", msg)
        self.assertIn("BOL7", msg)

    def test_skipped_when_creative_has_page(self):
        doc = {"creative": {"page_id": "explicit"}, "adset": {}}
        with mock.patch.object(production_flow.services, "get_pages") as m:
            production_flow._autoresolve_page(doc, "tok", [])
        m.assert_not_called()

    def test_skipped_when_adset_has_page(self):
        doc = {"creative": {}, "adset": {"page_id": "from_adset"}}
        with mock.patch.object(production_flow.services, "get_pages") as m:
            production_flow._autoresolve_page(doc, "tok", [])
        m.assert_not_called()


class AutoresolveInterestsTests(SimpleTestCase):
    def test_replaces_name_only_with_search_id(self):
        doc = {"targeting": {"flexible_spec": [{"interests": [{"name": "Lead Generation"}]}]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {"found": [{"id": "6003111122223", "name": "Lead Generation", "query": "Lead Generation"}], "missing": []}
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._autoresolve_interests(doc, "act_1", "tok", fixes)
        ids = [it["id"] for it in doc["targeting"]["flexible_spec"][0]["interests"]]
        self.assertEqual(ids, ["6003111122223"])
        self.assertTrue(any("6003111122223" in f for f in fixes))

    def test_filters_search_results_against_known_bad(self):
        doc = {"targeting": {"flexible_spec": [{"interests": [{"name": "Sales"}]}]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {"found": [{"id": "6297846662583", "name": "Sales", "query": "Sales"}], "missing": []}
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value={"6297846662583"}):
                production_flow._autoresolve_interests(doc, "act_1", "tok", fixes)
        # Search returned a known-bad id → interests becomes empty (popped).
        self.assertNotIn("interests", doc["targeting"]["flexible_spec"][0])

    def test_keeps_existing_valid_id_unchanged(self):
        doc = {"targeting": {"flexible_spec": [{"interests": [{"id": "6003147000111", "name": "Tech"}]}]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._autoresolve_interests(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        self.assertEqual(doc["targeting"]["flexible_spec"][0]["interests"][0]["id"], "6003147000111")


class AutoresolveCitiesTests(SimpleTestCase):
    def test_replaces_string_with_dict(self):
        doc = {"targeting": {"geo_cities": ["Mumbai"]}}
        fixes: list = []
        match = {"data": [{"key": "1011073", "name": "Mumbai", "type": "city", "country_code": "IN"}]}
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=match):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        c = doc["targeting"]["geo_cities"][0]
        self.assertEqual(c["key"], "1011073")
        self.assertEqual(c["name"], "Mumbai")
        self.assertEqual(c["distance_unit"], "kilometer")

    def test_city_state_upgrades_to_region_when_no_radius(self):
        """Delhi city (key 1023040) is the namesake of its own region (1728).
        With no explicit radius, target the REGION exact boundary instead of
        city+default-25km (which spills into NCR — the '+25 km' bug)."""
        doc = {"targeting": {"geo_cities": ["Delhi"]}}
        fixes: list = []
        match = {"data": [{
            "key": "1023040", "name": "Delhi", "type": "city",
            "country_code": "IN", "region": "Delhi", "region_id": 1728,
        }]}
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=match):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        self.assertNotIn("geo_cities", doc["targeting"])
        regions = (doc["targeting"].get("geo_locations") or {}).get("regions") or []
        self.assertEqual([r["key"] for r in regions], ["1728"])
        self.assertTrue(any("city-state" in f for f in fixes), fixes)

    def test_city_state_with_explicit_radius_stays_city(self):
        """An explicit radius means the caller wanted the circle — no upgrade."""
        doc = {"targeting": {"geo_cities": [{"name": "Delhi", "radius": 40}]}}
        fixes: list = []
        match = {"data": [{
            "key": "1023040", "name": "Delhi", "type": "city",
            "country_code": "IN", "region": "Delhi", "region_id": 1728,
        }]}
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=match):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        c = doc["targeting"]["geo_cities"][0]
        self.assertEqual(c["key"], "1023040")
        self.assertEqual(c["radius"], 40)

    def test_default_radius_injection_is_announced(self):
        """A normal city (region name differs) keeps the 25 km default but the
        injection must be LOUD in auto_fixes so agent/user know delivery
        includes surrounding areas."""
        doc = {"targeting": {"geo_cities": ["Mumbai"]}}
        fixes: list = []
        match = {"data": [{
            "key": "1011073", "name": "Mumbai", "type": "city",
            "country_code": "IN", "region": "Maharashtra", "region_id": 1745,
        }]}
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=match):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        c = doc["targeting"]["geo_cities"][0]
        self.assertEqual(c["radius"], 25)
        self.assertTrue(any("DEFAULT radius 25 km" in f for f in fixes), fixes)

    def test_drops_when_no_match_at_all(self):
        """When the Meta search returns no result of ANY location type
        (city, neighborhood, region, zip, country), drop the entry with a
        clear message that explains all location types were tried."""
        doc = {"targeting": {"geo_cities": ["NonexistentTown"]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value={"data": []}):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        self.assertNotIn("geo_cities", doc["targeting"])
        self.assertTrue(
            any("no Meta match found" in f for f in fixes),
            f"Expected drop message; got: {fixes}",
        )

    def test_routes_neighborhood_to_neighborhoods_key(self):
        """When the input is a neighborhood (e.g. 'Connaught Place'), Meta
        targeting_search returns type=neighborhood. The autoresolver should
        route it into geo_locations.neighborhoods, not silently drop it."""
        doc = {"targeting": {"geo_locations": {"cities": ["Connaught Place"]}}}
        fixes: list = []
        search_result = {"data": [{
            "key": "2806239",
            "name": "Connaught Place",
            "type": "neighborhood",
            "country_code": "IN",
        }]}
        with mock.patch.object(
            production_flow.services, "targeting_search_with_fallback",
            return_value=search_result,
        ):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        geo = doc["targeting"].get("geo_locations") or {}
        # The cities array should be empty/absent (no city match), but
        # neighborhoods should have the rerouted entry.
        self.assertNotIn("cities", geo)
        self.assertIn("neighborhoods", geo)
        self.assertEqual(geo["neighborhoods"][0]["key"], "2806239")
        self.assertTrue(
            any("geo_locations.neighborhoods" in f for f in fixes),
            f"Expected rerouting message; got: {fixes}",
        )

    def test_routes_zip_to_zips_key(self):
        """When input resolves to a zip code, route to geo_locations.zips."""
        doc = {"targeting": {"geo_locations": {"cities": ["110001"]}}}
        fixes: list = []
        search_result = {"data": [{
            "key": "IN:110001",
            "name": "110001",
            "type": "zip",
            "country_code": "IN",
        }]}
        with mock.patch.object(
            production_flow.services, "targeting_search_with_fallback",
            return_value=search_result,
        ):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        geo = doc["targeting"].get("geo_locations") or {}
        self.assertIn("zips", geo)
        self.assertEqual(geo["zips"][0]["key"], "IN:110001")

    def test_validates_existing_key_against_canonical(self):
        """When entry has both key and name, MCP searches by name and verifies
        the key matches Meta's canonical. If it does, the entry is kept (with
        canonical name). If the key differs, MCP swaps to the canonical key
        and reports the correction."""
        doc = {"targeting": {"geo_cities": [{"key": "1840030", "name": "Delhi", "radius": 50}]}}
        match = {"data": [{"key": "1840030", "name": "Delhi", "type": "city",
                           "country_code": "IN"}]}
        fixes: list = []
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=match):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        # Same canonical key → entry kept with that key.
        self.assertEqual(doc["targeting"]["geo_cities"][0]["key"], "1840030")
        # No "swap" auto_fix because key matched.
        self.assertFalse(any("Corrected fake city keys" in f for f in fixes), fixes)

    def test_corrects_fake_key_when_name_resolves_differently(self):
        """LLM emitted a fake key (resolves to a small village) for "Delhi"; MCP
        swaps to the canonical key from Meta /search."""
        doc = {"targeting": {"geo_cities": [{"key": "1021259", "name": "Delhi"}]}}
        match = {"data": [{"key": "1023040", "name": "Delhi", "type": "city",
                           "country_code": "IN"}]}
        fixes: list = []
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=match):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        # Key swapped to canonical.
        self.assertEqual(doc["targeting"]["geo_cities"][0]["key"], "1023040")
        # Reports the correction.
        self.assertTrue(any("Corrected fake city keys" in f and "1021259" in f for f in fixes), fixes)

    def test_drops_key_only_entry_with_no_name(self):
        """Entries with key but no name can't be validated — drop them."""
        doc = {"targeting": {"geo_cities": [{"key": "1021259"}]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback") as m:
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        # Search was NOT called (no name to query).
        m.assert_not_called()
        # geo_cities axis empty / removed.
        self.assertNotIn("geo_cities", doc["targeting"])
        # auto_fixes mentions the drop.
        self.assertTrue(any("with no name" in f and "1021259" in f for f in fixes), fixes)

    def test_drops_multiple_key_only_entries_with_aggregate_message(self):
        doc = {"targeting": {"geo_cities": [
            {"key": "1021259"},
            {"key": "1026692"},
            {"key": "1030557"},
        ]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback") as m:
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        self.assertNotIn("geo_cities", doc["targeting"])
        self.assertTrue(any("3 geo_cities entry" in f for f in fixes), fixes)

    def test_validates_geo_locations_cities_meta_shape(self):
        """Production case: LLM emits cities under targeting.geo_locations.cities
        (Meta API shape) with a name+wrong-key combo. Validator must fire on
        this shape too, not just the geo_cities shorthand."""
        doc = {
            "targeting": {
                "geo_locations": {
                    "cities": [
                        # name + wrong key (1040000 → "Purasavakkam" not "Hyderabad")
                        {"key": "1040000", "name": "Hyderabad", "radius": 25, "distance_unit": "kilometer"},
                    ]
                }
            }
        }
        # Meta /search returns the canonical key for "Hyderabad".
        match = {"data": [{"key": "1010946", "name": "Hyderabad", "type": "city",
                           "country_code": "IN"}]}
        fixes: list = []
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=match):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)
        # Key was swapped under the Meta-shape path.
        cities_after = doc["targeting"]["geo_locations"]["cities"]
        self.assertEqual(len(cities_after), 1)
        self.assertEqual(cities_after[0]["key"], "1010946")
        # auto_fix logged the correction.
        self.assertTrue(any("Corrected fake city keys" in f and "1040000" in f for f in fixes), fixes)
        # geo_cities shorthand was NOT created (we only write back to the shape used).
        self.assertNotIn("geo_cities", doc["targeting"])


    def test_parallel_fan_out_preserves_input_order_and_revalidates_each(self):
        """The 10-city loop now fans out targeting_search via a thread pool.
        Output must still be in input order, AND every entry — even ones with
        an existing key — must hit Meta (the fake-key check is the whole
        point of this code path)."""
        doc = {"targeting": {"geo_locations": {"cities": [
            {"key": "OLD_DEL", "name": "Delhi"},
            {"key": "OLD_MUM", "name": "Mumbai"},
            {"key": "OLD_BLR", "name": "Bangalore"},
            {"key": "OLD_HYD", "name": "Hyderabad"},
        ]}}}
        canonical_by_name = {
            "Delhi":     {"key": "1023040", "name": "Delhi",     "type": "city", "country_code": "IN"},
            "Mumbai":    {"key": "1035921", "name": "Mumbai",    "type": "city", "country_code": "IN"},
            "Bangalore": {"key": "1017930", "name": "Bangalore", "type": "city", "country_code": "IN"},
            "Hyderabad": {"key": "1026282", "name": "Hyderabad", "type": "city", "country_code": "IN"},
        }
        call_log: list = []

        def fake_search(_acct, _tok, _kind, q=None, **_kw):
            call_log.append(q)
            return {"data": [canonical_by_name[q]]}

        fixes: list = []
        with mock.patch.object(
            production_flow.services, "targeting_search_with_fallback", side_effect=fake_search,
        ):
            production_flow._autoresolve_cities(doc, "act_1", "tok", fixes)

        out = doc["targeting"]["geo_locations"]["cities"]
        # Output preserves input order.
        self.assertEqual([c["name"] for c in out], ["Delhi", "Mumbai", "Bangalore", "Hyderabad"])
        # Every name was revalidated (no skip-when-key-present optimization).
        self.assertEqual(sorted(call_log), ["Bangalore", "Delhi", "Hyderabad", "Mumbai"])
        # All four fake keys were swapped to canonical.
        self.assertEqual([c["key"] for c in out],
                         ["1023040", "1035921", "1017930", "1026282"])


class ApplyUtmParamsTests(SimpleTestCase):
    def test_appends_when_no_utm_source(self):
        doc = {
            "campaign": {"name": "Audience Hub Traffic"},
            "ad": {"name": "Audience Hub Creative"},
            "creative": {"link_url": "https://www.bol7.com/audience-hub/", "mode": "LINK_AD"},
        }
        production_flow._apply_utm_params(doc, [])
        url = doc["creative"]["link_url"]
        self.assertIn("utm_source=facebook", url)
        self.assertIn("utm_medium=paid", url)
        self.assertIn("utm_campaign=audience_hub_traffic", url)
        self.assertIn("utm_content=audience_hub_creative", url)

    def test_noop_when_utm_source_already_set(self):
        original = "https://www.bol7.com/audience-hub/?utm_source=existing&utm_medium=email"
        doc = {
            "campaign": {"name": "X"}, "ad": {"name": "Y"},
            "creative": {"link_url": original, "mode": "LINK_AD"},
        }
        production_flow._apply_utm_params(doc, [])
        self.assertEqual(doc["creative"]["link_url"], original)

    def test_user_overrides_win(self):
        doc = {
            "campaign": {"name": "C"}, "ad": {"name": "A"},
            "creative": {"link_url": "https://example.com/", "mode": "LINK_AD"},
            "tracking": {"utm_overrides": {"utm_source": "custom_source"}},
        }
        production_flow._apply_utm_params(doc, [])
        self.assertIn("utm_source=custom_source", doc["creative"]["link_url"])
        self.assertNotIn("utm_source=facebook", doc["creative"]["link_url"])

    def test_skips_for_existing_post_mode(self):
        doc = {
            "campaign": {"name": "C"}, "ad": {"name": "A"},
            "creative": {"link_url": "https://example.com/", "mode": "EXISTING_POST"},
        }
        production_flow._apply_utm_params(doc, [])
        self.assertEqual(doc["creative"]["link_url"], "https://example.com/")


class AdTrackingSpecsTests(SimpleTestCase):
    def test_landing_page_view_for_landing_page_views_goal(self):
        spec = production_flow._build_ad_tracking_specs("12345", "LANDING_PAGE_VIEWS")
        self.assertEqual(spec, [{"action.type": ["landing_page_view"], "fb_pixel": ["12345"]}])

    def test_offsite_conversion_for_offsite_conversions_goal(self):
        spec = production_flow._build_ad_tracking_specs("12345", "OFFSITE_CONVERSIONS")
        self.assertEqual(spec, [{"action.type": ["offsite_conversion"], "fb_pixel": ["12345"]}])

    def test_none_when_pixel_empty(self):
        self.assertIsNone(production_flow._build_ad_tracking_specs("", "LANDING_PAGE_VIEWS"))
        self.assertIsNone(production_flow._build_ad_tracking_specs(None, "LANDING_PAGE_VIEWS"))

    def test_none_when_goal_unmapped(self):
        self.assertIsNone(production_flow._build_ad_tracking_specs("12345", "REACH"))


class CreateAdWithTrackingSpecsTests(SimpleTestCase):
    def test_posts_tracking_specs_as_json(self):
        from facebook_ads_mcp import services
        with mock.patch.object(services, "_post") as m_post:
            m_post.return_value = {"id": "ad_1"}
            services.create_ad(
                "act_1", "tok", "as_1", "Ad",
                creative_id="cr_1",
                tracking_specs=[{"action.type": ["landing_page_view"], "fb_pixel": ["12345"]}],
            )
        # Body posted should have a JSON-stringified tracking_specs.
        sent_body = m_post.call_args[0][1]
        self.assertIn("tracking_specs", sent_body)
        import json as _json
        parsed = _json.loads(sent_body["tracking_specs"])
        self.assertEqual(parsed[0]["fb_pixel"], ["12345"])

    def test_omits_tracking_specs_when_empty(self):
        from facebook_ads_mcp import services
        with mock.patch.object(services, "_post") as m_post:
            m_post.return_value = {"id": "ad_1"}
            services.create_ad("act_1", "tok", "as_1", "Ad", creative_id="cr_1")
        sent_body = m_post.call_args[0][1]
        self.assertNotIn("tracking_specs", sent_body)


# ---------------------------------------------------------------------------
# meta_validation tests for the five new validators
# ---------------------------------------------------------------------------

class MetaValidationLengthTests(SimpleTestCase):
    def test_warns_long_headline(self):
        from facebook_ads_mcp import meta_validation
        notes = meta_validation.validate_creative_lengths({"headline": "A" * 60})
        self.assertTrue(any("60 chars" in n for n in notes))

    def test_warns_long_description(self):
        from facebook_ads_mcp import meta_validation
        notes = meta_validation.validate_creative_lengths({"description": "B" * 50})
        self.assertTrue(any("50 chars" in n for n in notes))

    def test_no_warnings_for_reasonable_lengths(self):
        from facebook_ads_mcp import meta_validation
        notes = meta_validation.validate_creative_lengths({"headline": "Short", "description": "Tight", "message": "Body"})
        self.assertEqual(notes, [])


class MetaValidationCtaDestinationTests(SimpleTestCase):
    def test_blocks_call_now_with_website_destination_only_when_invalid(self):
        from facebook_ads_mcp import meta_validation
        # CALL_NOW with WEBSITE is allowed.
        block, notes = meta_validation.validate_cta_destination_compatibility(
            {"cta_type": "CALL_NOW"}, "OUTCOME_TRAFFIC", "WEBSITE",
        )
        self.assertIsNone(block)

    def test_blocks_call_now_with_messenger(self):
        from facebook_ads_mcp import meta_validation
        block, notes = meta_validation.validate_cta_destination_compatibility(
            {"cta_type": "CALL_NOW"}, "OUTCOME_TRAFFIC", "MESSENGER",
        )
        self.assertIsNotNone(block)
        self.assertIn("CALL_NOW", block["message"])

    def test_blocks_whatsapp_message_with_website(self):
        from facebook_ads_mcp import meta_validation
        block, _ = meta_validation.validate_cta_destination_compatibility(
            {"cta_type": "WHATSAPP_MESSAGE"}, "OUTCOME_ENGAGEMENT", "WEBSITE",
        )
        self.assertIsNotNone(block)


class MetaValidationObjectiveTripleTests(SimpleTestCase):
    def test_traffic_landing_page_views_website_ok(self):
        from facebook_ads_mcp import meta_validation
        self.assertIsNone(
            meta_validation.validate_objective_goal_destination_triple(
                "OUTCOME_TRAFFIC", "LANDING_PAGE_VIEWS", "WEBSITE",
            )
        )

    def test_awareness_on_ad_blocked(self):
        """OUTCOME_AWARENESS rejects ON_AD per Meta docs — destination_type
        for awareness must be UNDEFINED/WEBSITE/MESSENGER/WHATSAPP/messaging
        variants/INSTAGRAM_DIRECT. Triggers subcode 1815715 on Meta."""
        from facebook_ads_mcp import meta_validation
        block = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_AWARENESS", "REACH", "ON_AD",
        )
        self.assertIsNotNone(block)
        self.assertIn("destination_type", block["message"])

    def test_app_promotion_website_blocked(self):
        """OUTCOME_APP_PROMOTION only accepts UNDEFINED or APP per Meta docs."""
        from facebook_ads_mcp import meta_validation
        block = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_APP_PROMOTION", "APP_INSTALLS", "WEBSITE",
        )
        self.assertIsNotNone(block)

    def test_traffic_messenger_ok(self):
        """OUTCOME_TRAFFIC accepts MESSENGER per Meta docs. Goal-level
        mismatch (e.g. LANDING_PAGE_VIEWS + MESSENGER) is left to Meta to
        reject via its own error response — the agent retries from
        structuredError.what_to_try."""
        from facebook_ads_mcp import meta_validation
        self.assertIsNone(
            meta_validation.validate_objective_goal_destination_triple(
                "OUTCOME_TRAFFIC", "LINK_CLICKS", "MESSENGER",
            )
        )

    def test_unknown_destination_under_known_objective_blocked(self):
        """Under a known objective (OUTCOME_TRAFFIC), an unknown destination
        is rejected — Meta's matrix is exhaustive per objective. The error
        message lists all valid destinations so the agent can correct."""
        from facebook_ads_mcp import meta_validation
        block = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_TRAFFIC", "LINK_CLICKS", "BRAND_NEW_DESTINATION_TYPE",
        )
        self.assertIsNotNone(block)
        # Error message includes the valid list so the agent can self-correct.
        self.assertIn("Valid destinations", block["message"])

    def test_unknown_objective_passes_through(self):
        """Unknown objective values pass through to Meta."""
        from facebook_ads_mcp import meta_validation
        self.assertIsNone(
            meta_validation.validate_objective_goal_destination_triple(
                "OUTCOME_FUTURE_OBJECTIVE", "LINK_CLICKS", "WEBSITE",
            )
        )

    def test_empty_destination_passes(self):
        """Empty destination_type (== omitted, UNDEFINED) is universally
        accepted by Meta and should not be blocked."""
        from facebook_ads_mcp import meta_validation
        self.assertIsNone(
            meta_validation.validate_objective_goal_destination_triple(
                "OUTCOME_AWARENESS", "REACH", "",
            )
        )


class MetaValidationAgeDateTests(SimpleTestCase):
    def test_age_min_below_13_blocked(self):
        from facebook_ads_mcp import meta_validation
        block = meta_validation.validate_age_range_and_dates({"age_min": 10, "age_max": 30})
        self.assertIsNotNone(block)

    def test_age_min_greater_than_max_blocked(self):
        from facebook_ads_mcp import meta_validation
        block = meta_validation.validate_age_range_and_dates({"age_min": 40, "age_max": 30})
        self.assertIsNotNone(block)

    def test_end_in_past_blocked(self):
        from facebook_ads_mcp import meta_validation
        block = meta_validation.validate_age_range_and_dates({
            "start_time": "2020-01-01T00:00:00+0000",
            "end_time": "2020-01-02T00:00:00+0000",
        })
        self.assertIsNotNone(block)

    def test_valid_dates_pass(self):
        from facebook_ads_mcp import meta_validation
        block = meta_validation.validate_age_range_and_dates({
            "age_min": 23, "age_max": 45,
            "start_time": "2030-01-01T00:00:00+0000",
            "end_time": "2030-12-31T00:00:00+0000",
        })
        self.assertIsNone(block)


class MetaValidationBudgetTests(SimpleTestCase):
    def test_blocks_both_daily_and_lifetime_at_same_level(self):
        from facebook_ads_mcp import meta_validation
        block, _ = meta_validation.validate_budgets_and_specials(
            {"daily_budget": 1000, "lifetime_budget": 50000}, {},
        )
        self.assertIsNotNone(block)
        self.assertIn("mutually exclusive", block["message"])

    def test_warns_low_budget_likely_unit_mistake(self):
        from facebook_ads_mcp import meta_validation
        _, notes = meta_validation.validate_budgets_and_specials({}, {"daily_budget": 50})
        self.assertTrue(any("minor units" in n for n in notes))

    def test_passes_typical_budget(self):
        from facebook_ads_mcp import meta_validation
        block, _ = meta_validation.validate_budgets_and_specials(
            {"objective": "OUTCOME_AWARENESS", "special_ad_categories": []},
            {"daily_budget": 10000},
        )
        self.assertIsNone(block)


# ---------------------------------------------------------------------------
# /search-based revalidation of flexible_spec
# ---------------------------------------------------------------------------

class RevalidateFlexViaSearchTests(SimpleTestCase):
    def _doc(self, items):
        return {"targeting": {"flexible_spec": [{"interests": items}]}}

    def test_keeps_canonical_id_unchanged(self):
        doc = self._doc([{"id": "6003147000111", "name": "Lead Generation"}])
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {
                "found": [{"id": "6003147000111", "name": "Lead Generation", "query": "Lead Generation"}],
                "missing": [],
            }
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        self.assertEqual(doc["targeting"]["flexible_spec"][0]["interests"][0]["id"], "6003147000111")
        # No swap/drop messages.
        self.assertFalse(any("Swapped" in f or "Dropped" in f for f in fixes), fixes)

    def test_swaps_to_canonical_id(self):
        doc = self._doc([{"id": "6003127206524", "name": "Lead Generation"}])  # stale
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {
                "found": [{"id": "6003147000111", "name": "Lead Generation", "query": "Lead Generation"}],
                "missing": [],
            }
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        self.assertEqual(doc["targeting"]["flexible_spec"][0]["interests"][0]["id"], "6003147000111")
        self.assertTrue(any("Swapped" in f and "6003127206524" in f for f in fixes), fixes)

    def test_drops_when_search_returns_missing(self):
        doc = self._doc([{"id": "6003127206524", "name": "Defunct Term"}])
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {"found": [], "missing": ["Defunct Term"]}
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        # interests axis got popped because nothing remained.
        self.assertNotIn("interests", doc["targeting"]["flexible_spec"][0])
        self.assertTrue(any("Dropped" in f and "Defunct Term" in f for f in fixes), fixes)

    def test_filters_known_bad_canonical_id(self):
        doc = self._doc([{"id": "6003127206524", "name": "Sales"}])
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {
                "found": [{"id": "6297846662583", "name": "Sales", "query": "Sales"}],
                "missing": [],
            }
            with mock.patch.object(
                production_flow, "_learned_bad_interest_ids", return_value={"6297846662583"}
            ):
                production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        self.assertNotIn("interests", doc["targeting"]["flexible_spec"][0])
        self.assertTrue(any("Dropped" in f for f in fixes), fixes)

    def test_skips_name_only_entries(self):
        doc = self._doc([{"name": "Lead Generation"}])  # no id — auto-resolve handles
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        self.assertEqual(doc["targeting"]["flexible_spec"][0]["interests"][0], {"name": "Lead Generation"})
        self.assertEqual(fixes, [])

    def test_skips_id_only_entries(self):
        # id-only entries won't get past the rule_body, but if one slips in,
        # revalidate has nothing to look up by — skip them safely.
        doc = self._doc([{"id": "6003127206524"}])
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        self.assertEqual(fixes, [])

    def test_no_op_when_no_flexible_spec(self):
        doc = {"targeting": {"geo_locations": {"countries": ["IN"]}}}
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            production_flow._revalidate_flex_via_search(doc, "act_1", "tok", [])
        m.assert_not_called()


# ---------------------------------------------------------------------------
# create_campaign smart_promotion_type plumbing
# ---------------------------------------------------------------------------

class CreateCampaignSmartPromotionTypeTests(SimpleTestCase):
    """Regression: ``smart_promotion_type`` must never be sent.

    Meta v22 only accepts ``GUIDED_CREATION`` or ``SMART_APP_PROMOTION`` for
    this field — both UI-flow markers, not ODAX hints. Sending the campaign
    objective (e.g. ``OUTCOME_TRAFFIC``) triggers error #100 and blocks
    every campaign create. These tests lock that down.
    """

    def test_omits_smart_promotion_type_for_outcome_traffic(self):
        from facebook_ads_mcp import services
        with mock.patch.object(services, "_post") as m_post:
            m_post.return_value = {"id": "c1"}
            services.create_campaign("act_1", "tok", "Camp", "OUTCOME_TRAFFIC")
        body = m_post.call_args[0][1]
        self.assertNotIn("smart_promotion_type", body)

    def test_omits_for_all_outcome_objectives(self):
        from facebook_ads_mcp import services
        for obj in ("OUTCOME_AWARENESS", "OUTCOME_LEADS", "OUTCOME_SALES",
                    "OUTCOME_APP_PROMOTION", "OUTCOME_ENGAGEMENT"):
            with mock.patch.object(services, "_post") as m_post:
                m_post.return_value = {"id": "c1"}
                services.create_campaign("act_1", "tok", "Camp", obj)
            body = m_post.call_args[0][1]
            self.assertNotIn("smart_promotion_type", body, f"failed for {obj}")

    def test_omits_for_legacy_objective(self):
        from facebook_ads_mcp import services
        with mock.patch.object(services, "_post") as m_post:
            m_post.return_value = {"id": "c1"}
            services.create_campaign("act_1", "tok", "Camp", "LINK_CLICKS")
        body = m_post.call_args[0][1]
        self.assertNotIn("smart_promotion_type", body)


# ---------------------------------------------------------------------------
# Regression: batch_interest_search must NOT return cross-type bad rows
# (the "id 4 / High school leaver" failure mode).
# ---------------------------------------------------------------------------

class BatchInterestSearchFiltersTests(SimpleTestCase):
    def test_filters_short_id_cross_type_match(self):
        """When Meta returns id=4 (cross-type leak), it must be filtered out."""
        from facebook_ads_mcp import services
        fake_response = {
            "data": [
                {"id": "4", "name": "High school leaver", "type": "education_school"},
                {"id": "6003147000111", "name": "Business school", "type": "interests"},
            ],
        }
        with mock.patch.object(services, "targeting_search_with_fallback", return_value=fake_response):
            r = services.batch_interest_search("act_1", "tok", ["Business school"])
        # Should pick the 13-digit id, never the cross-type id=4.
        self.assertEqual(len(r["found"]), 1)
        self.assertEqual(r["found"][0]["id"], "6003147000111")
        self.assertEqual(r["missing"], [])

    def test_returns_missing_when_only_cross_type_results(self):
        """If every row is < 11 digits, the query is reported as missing."""
        from facebook_ads_mcp import services
        fake_response = {
            "data": [
                {"id": "4", "name": "High school leaver"},
                {"id": "12", "name": "Some other thing"},
            ],
        }
        with mock.patch.object(services, "targeting_search_with_fallback", return_value=fake_response):
            r = services.batch_interest_search("act_1", "tok", ["Defunct Term"])
        self.assertEqual(r["found"], [])
        self.assertEqual(r["missing"], ["Defunct Term"])

    def test_prefers_exact_name_match_over_first_result(self):
        """If multiple plausible rows exist, exact name match wins."""
        from facebook_ads_mcp import services
        fake_response = {
            "data": [
                {"id": "6003111111111", "name": "Lead Generation marketing", "type": "interests"},
                {"id": "6003147000111", "name": "Lead Generation", "type": "interests"},
            ],
        }
        with mock.patch.object(services, "targeting_search_with_fallback", return_value=fake_response):
            r = services.batch_interest_search("act_1", "tok", ["Lead Generation"])
        self.assertEqual(r["found"][0]["id"], "6003147000111")

    def test_rejects_type_education_school(self):
        """type='education_school' must never appear in a result for adinterest."""
        from facebook_ads_mcp import services
        fake_response = {
            "data": [
                {"id": "6003147000111", "name": "Business school", "type": "education_school"},
                {"id": "6003147000222", "name": "Business school (interest)", "type": "interests"},
            ],
        }
        with mock.patch.object(services, "targeting_search_with_fallback", return_value=fake_response):
            r = services.batch_interest_search("act_1", "tok", ["Business school"])
        self.assertEqual(r["found"][0]["id"], "6003147000222")

    def test_revalidate_drops_when_search_returns_only_short_id(self):
        """End-to-end: a flexible_spec entry whose name only matches a cross-type
        id=4 row should be dropped, never swapped to id=4."""
        doc = {"targeting": {"flexible_spec": [{"interests": [{"id": "6003127206524", "name": "Business school"}]}]}}
        fixes: list = []
        # Simulate the unfixed Meta response shape (top hit is the bad id=4 row).
        fake_response = {"data": [{"id": "4", "name": "High school leaver"}]}
        with mock.patch.object(production_flow.services, "targeting_search_with_fallback", return_value=fake_response):
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        # interests axis is empty → popped.
        self.assertNotIn("interests", doc["targeting"]["flexible_spec"][0])
        # Auto-fix message mentions drop, NOT swap.
        self.assertTrue(any("Dropped interest" in f for f in fixes), fixes)
        self.assertFalse(any("Swapped interest" in f for f in fixes), fixes)
        # Critically: no entry references id=4.
        self.assertFalse(any(' id 4 ' in f or "→ 4" in f for f in fixes), fixes)


# ---------------------------------------------------------------------------
# batch_targeting_category_search (behaviors / demographics resolver)
# ---------------------------------------------------------------------------

class BatchTargetingCategorySearchTests(SimpleTestCase):
    def test_filters_short_id_cross_type_match(self):
        """When Meta's adTargetingCategory leaks a row with id < 11 digits, drop it."""
        from facebook_ads_mcp import services
        fake_response = {
            "data": [
                {"id": "9", "name": "Junk row", "path": ["Behaviors"]},
                {"id": "6002714898572", "name": "Engaged Shoppers",
                 "path": ["Behaviors", "Digital activities"]},
            ],
        }
        with mock.patch.object(services, "targeting_search_with_fallback", return_value=fake_response):
            r = services.batch_targeting_category_search("act_1", "tok", ["Engaged Shoppers"])
        self.assertEqual(len(r["found"]), 1)
        self.assertEqual(r["found"][0]["id"], "6002714898572")
        self.assertEqual(r["missing"], [])

    def test_returns_path_field(self):
        """The path array must be propagated so callers can route to behaviors/demographics."""
        from facebook_ads_mcp import services
        fake_response = {
            "data": [
                {"id": "6017253486583", "name": "Marketing Professionals",
                 "path": ["Demographics", "Work", "Industry"]},
            ],
        }
        with mock.patch.object(services, "targeting_search_with_fallback", return_value=fake_response):
            r = services.batch_targeting_category_search("act_1", "tok", ["Marketing Professionals"])
        self.assertEqual(r["found"][0]["path"], ["Demographics", "Work", "Industry"])

    def test_filters_path_at_category_level_then_falls_back_to_adinterest(self):
        """Category search rejects rows with path=['Interests', ...] (correct
        per-axis filter), then the cross-type fallback tries adinterest as
        last resort and accepts an exact-name match if one exists.

        This mirrors the production case: brief categorizes "Entrepreneurs"
        as a behavior; adTargetingCategory returns nothing usable; adinterest
        has an exact match — we accept it tagged as 'Behaviors' in the synthesized path."""
        from facebook_ads_mcp import services
        call_log = []

        def fake_search(account_id, token, search_type, **kwargs):
            call_log.append(search_type)
            if search_type == "adTargetingCategory":
                # Cross-type leak: row has path=['Interests', ...] — filtered out.
                return {"data": [
                    {"id": "6003147000111", "name": "Some Interest",
                     "path": ["Interests", "Marketing"]},
                ]}
            if search_type == "adinterest":
                # Adinterest has the same row and accepts it (fallback path).
                return {"data": [
                    {"id": "6003147000111", "name": "Some Interest",
                     "type": "interests"},
                ]}
            return {"data": []}

        with mock.patch.object(services, "targeting_search_with_fallback", side_effect=fake_search):
            r = services.batch_targeting_category_search("act_1", "tok", ["Some Interest"])
        # Category step DID try (and rejected the row via path filter).
        self.assertIn("adTargetingCategory", call_log)
        # Then fallback to adinterest fired and accepted the exact-name match.
        self.assertIn("adinterest", call_log)
        self.assertEqual(len(r["found"]), 1)
        self.assertEqual(r["found"][0]["id"], "6003147000111")
        # Synthesized path so caller can route correctly.
        self.assertEqual(r["found"][0]["path"], ["Behaviors"])

    def test_prefers_exact_name_match_over_first_result(self):
        from facebook_ads_mcp import services
        fake_response = {
            "data": [
                {"id": "6017253486583", "name": "Engaged Shoppers (high spend)",
                 "path": ["Behaviors"]},
                {"id": "6002714898572", "name": "Engaged Shoppers",
                 "path": ["Behaviors", "Digital activities"]},
            ],
        }
        with mock.patch.object(services, "targeting_search_with_fallback", return_value=fake_response):
            r = services.batch_targeting_category_search("act_1", "tok", ["Engaged Shoppers"])
        self.assertEqual(r["found"][0]["id"], "6002714898572")


# ---------------------------------------------------------------------------
# _autoresolve_behaviors_and_demographics
# ---------------------------------------------------------------------------

class AutoresolveBehaviorsDemographicsTests(SimpleTestCase):
    def test_resolves_behavior_name_to_id(self):
        doc = {"targeting": {"flexible_spec": [{"behaviors": [{"name": "Engaged Shoppers"}]}]}}
        fixes: list = []
        fake_search = {
            "found": [{"id": "6002714898570", "name": "Engaged Shoppers",
                       "path": ["Behaviors", "Digital activities"], "query": "Engaged Shoppers"}],
            "missing": [],
        }
        with mock.patch.object(production_flow.services, "batch_targeting_category_search", return_value=fake_search):
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._autoresolve_behaviors_and_demographics(doc, "act_1", "tok", fixes)
        beh = doc["targeting"]["flexible_spec"][0].get("behaviors") or []
        self.assertEqual(len(beh), 1)
        self.assertEqual(beh[0]["id"], "6002714898570")
        # Demographics axis must be removed.
        self.assertNotIn("demographics", doc["targeting"]["flexible_spec"][0])

    def test_demographics_routed_to_behaviors_axis(self):
        doc = {"targeting": {"flexible_spec": [{"demographics": [{"name": "Marketing Professionals"}]}]}}
        fixes: list = []
        fake_search = {
            "found": [{"id": "6017253486583", "name": "Marketing Professionals",
                       "path": ["Demographics", "Work"], "query": "Marketing Professionals"}],
            "missing": [],
        }
        with mock.patch.object(production_flow.services, "batch_targeting_category_search", return_value=fake_search):
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._autoresolve_behaviors_and_demographics(doc, "act_1", "tok", fixes)
        slc = doc["targeting"]["flexible_spec"][0]
        # Resolved item lands on behaviors, not demographics.
        self.assertEqual(slc.get("behaviors", [])[0]["id"], "6017253486583")
        self.assertNotIn("demographics", slc)

    def test_drops_deprecated_canonical_id_for_behavior(self):
        doc = {"targeting": {"flexible_spec": [{"behaviors": [{"name": "Small business owners"}]}]}}
        fixes: list = []
        fake_search = {
            "found": [{"id": "6002714898572", "name": "Small business owners",
                       "path": ["Behaviors"], "query": "Small business owners"}],
            "missing": [],
        }
        with mock.patch.object(production_flow.services, "batch_targeting_category_search", return_value=fake_search):
            with mock.patch.object(
                production_flow, "_learned_bad_interest_ids", return_value={"6002714898572"}
            ):
                production_flow._autoresolve_behaviors_and_demographics(doc, "act_1", "tok", fixes)
        # Deprecated canonical filtered → behaviors axis empty / popped.
        self.assertNotIn("behaviors", doc["targeting"]["flexible_spec"][0])

    def test_reports_missing_with_per_name_drop_message(self):
        doc = {"targeting": {"flexible_spec": [{"behaviors": [{"name": "Frequent Buyers"}]}]}}
        fixes: list = []
        fake_search = {"found": [], "missing": ["Frequent Buyers"]}
        with mock.patch.object(production_flow.services, "batch_targeting_category_search", return_value=fake_search):
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._autoresolve_behaviors_and_demographics(doc, "act_1", "tok", fixes)
        self.assertTrue(
            any("Frequent Buyers" in f and "no Meta adTargetingCategory match" in f for f in fixes),
            fixes,
        )

    def test_keeps_pre_validated_behavior_with_real_id(self):
        """An entry that already has a 13-digit id and passes the bad-cache filter is preserved."""
        doc = {"targeting": {"flexible_spec": [
            {"behaviors": [{"id": "6002714898570", "name": "Already Resolved"}]}
        ]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_targeting_category_search") as m:
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._autoresolve_behaviors_and_demographics(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        self.assertEqual(
            doc["targeting"]["flexible_spec"][0]["behaviors"][0]["id"],
            "6002714898570",
        )

    def test_no_op_when_no_flexible_spec(self):
        doc = {"targeting": {"geo_locations": {"countries": ["IN"]}}}
        with mock.patch.object(production_flow.services, "batch_targeting_category_search") as m:
            production_flow._autoresolve_behaviors_and_demographics(doc, "act_1", "tok", [])
        m.assert_not_called()


# ---------------------------------------------------------------------------
# Call-tracking transparency
# ---------------------------------------------------------------------------

class CallTrackingTransparencyTests(SimpleTestCase):
    def test_quality_call_announces_tracking_in_auto_fixes(self):
        doc = {"adset": {"optimization_goal": "QUALITY_CALL"}, "tracking": {}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "get_pixels") as m:
            production_flow._autoresolve_pixel(doc, "act_1", "tok", fixes)
        # Did NOT call get_pixels (call campaigns don't use a pixel).
        m.assert_not_called()
        # DID surface the call-tracking explainer.
        self.assertTrue(
            any("QUALITY_CALL" in f and "promoted_object.page_id" in f for f in fixes),
            fixes,
        )

    def test_call_volume_announces_tracking_in_auto_fixes(self):
        doc = {"adset": {"optimization_goal": "CALL_VOLUME"}, "tracking": {}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "get_pixels") as m:
            production_flow._autoresolve_pixel(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        self.assertTrue(any("CALL_VOLUME" in f for f in fixes), fixes)

    def test_reach_goal_does_not_announce_call_tracking(self):
        """Non-call non-tracking goals (REACH, IMPRESSIONS) stay silent — no false note."""
        doc = {"adset": {"optimization_goal": "REACH"}, "tracking": {}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "get_pixels") as m:
            production_flow._autoresolve_pixel(doc, "act_1", "tok", fixes)
        m.assert_not_called()
        # No call-tracking explainer (goal isn't a call goal).
        self.assertFalse(
            any("QUALITY_CALL" in f or "CALL_VOLUME" in f for f in fixes),
            fixes,
        )


# ---------------------------------------------------------------------------
# Graph API version centralization. Default version is bumped over time as
# Meta retires older versions — keep this test in sync with services.py.
# Current default: v25.0 (released Feb 2026). Earlier versions remain
# usable via the META_GRAPH_API_VERSION env var.
# ---------------------------------------------------------------------------

class GetPagesUnionTests(SimpleTestCase):
    """services.get_pages should union pages across /me/accounts and every
    Business Manager the user belongs to. This is what prevents the
    'silent BOL7 substitution' bug when the user's actual brief refers to
    a page owned by a different Business.

    Universal across all Business Manager setups — single-business users
    are unaffected (the /me/businesses step finds nothing or duplicates).
    """

    def _patch_get(self, responses):
        """Build a mock for services._get that dispatches by URL substring.

        ``responses`` is a list of ``(url_substring, response_dict)`` tuples;
        the first matching url wins. Anything unmatched returns an empty
        ``{"data": []}`` so the test fails open, not closed.
        """
        from facebook_ads_mcp import services

        def fake_get(url, params=None):
            for needle, resp in responses:
                if needle in url:
                    return resp
            return {"data": []}

        return mock.patch.object(services, "_get", side_effect=fake_get)

    def test_unions_owned_pages_across_businesses(self):
        """Page in a separate Business (not on /me/accounts) must appear
        in the returned union. This is the exact production case that
        caused the silent-BOL7-substitution bug."""
        from facebook_ads_mcp import services
        responses = [
            ("/me/accounts", {"data": [
                {"id": "page_bol7", "name": "BOL7", "access_token": "tok_bol7", "category": "Advertising/marketing"},
            ]}),
            ("/me/businesses", {"data": [
                {"id": "biz_bol7", "name": "Bol7 Technologies"},
                {"id": "biz_rekha", "name": "Rekha Gupta Fans Club"},
            ]}),
            ("/biz_bol7/owned_pages", {"data": [
                {"id": "page_bol7", "name": "BOL7", "access_token": "tok_bol7"},
            ]}),
            ("/biz_bol7/client_pages", {"data": []}),
            ("/biz_rekha/owned_pages", {"data": [
                {"id": "page_rekha", "name": "Rekha Gupta Fans Club", "access_token": "tok_rekha", "category": "Public figure"},
            ]}),
            ("/biz_rekha/client_pages", {"data": []}),
        ]
        with self._patch_get(responses):
            result = services.get_pages("dummy_user_token")
        ids = [p["id"] for p in result.get("data", [])]
        self.assertIn("page_bol7", ids)
        self.assertIn("page_rekha", ids)

    def test_dedupes_pages_seen_multiple_times(self):
        """A page returned by both /me/accounts and an owned_pages call
        should appear ONCE; the /me/accounts entry wins (it carries the
        page-scoped access token reliably)."""
        from facebook_ads_mcp import services
        responses = [
            ("/me/accounts", {"data": [
                {"id": "page_a", "name": "PageA", "access_token": "from_me_accounts"},
            ]}),
            ("/me/businesses", {"data": [{"id": "biz1", "name": "Biz1"}]}),
            ("/biz1/owned_pages", {"data": [
                {"id": "page_a", "name": "PageA", "access_token": "from_owned_pages"},
            ]}),
            ("/biz1/client_pages", {"data": []}),
        ]
        with self._patch_get(responses):
            result = services.get_pages("dummy_user_token")
        ids = [p["id"] for p in result.get("data", [])]
        self.assertEqual(ids.count("page_a"), 1)
        # /me/accounts entry wins (first occurrence).
        page_a = next(p for p in result["data"] if p["id"] == "page_a")
        self.assertEqual(page_a["access_token"], "from_me_accounts")

    def test_falls_back_gracefully_when_businesses_endpoint_fails(self):
        """If /me/businesses returns an error (missing scope etc.), still
        return whatever /me/accounts gave us. Never raise — this is a
        discovery endpoint, not a critical-path call."""
        from facebook_ads_mcp import services
        responses = [
            ("/me/accounts", {"data": [
                {"id": "page_bol7", "name": "BOL7", "access_token": "tok"},
            ]}),
            ("/me/businesses", {"error": {"message": "missing scope: business_management"}}),
        ]
        with self._patch_get(responses):
            result = services.get_pages("dummy_user_token")
        ids = [p["id"] for p in result.get("data", [])]
        self.assertEqual(ids, ["page_bol7"])

    def test_includes_client_pages_too(self):
        """Pages added as 'client' (not owned by the business) must also
        be discoverable — covers agency / multi-tenant setups."""
        from facebook_ads_mcp import services
        responses = [
            ("/me/accounts", {"data": []}),
            ("/me/businesses", {"data": [{"id": "biz_agency", "name": "AgencyBM"}]}),
            ("/biz_agency/owned_pages", {"data": []}),
            ("/biz_agency/client_pages", {"data": [
                {"id": "client_page_1", "name": "ClientPage1", "access_token": "tok_client"},
            ]}),
        ]
        with self._patch_get(responses):
            result = services.get_pages("dummy_user_token")
        ids = [p["id"] for p in result.get("data", [])]
        self.assertIn("client_page_1", ids)

    def test_respects_limit_cap(self):
        """The final union honours the caller-supplied limit."""
        from facebook_ads_mcp import services
        responses = [
            ("/me/accounts", {"data": [
                {"id": f"p{i}", "name": f"Page{i}"} for i in range(50)
            ]}),
            ("/me/businesses", {"data": []}),
        ]
        with self._patch_get(responses):
            result = services.get_pages("dummy_user_token", limit=10)
        self.assertEqual(len(result["data"]), 10)


class APIVersionCentralizationTests(SimpleTestCase):
    def test_base_url_default_is_v26(self):
        import os
        if os.environ.get("META_GRAPH_API_VERSION"):
            self.skipTest("META_GRAPH_API_VERSION overridden in env")
        from facebook_ads_mcp import services
        self.assertEqual(services.BASE_URL, "https://graph.facebook.com/v26.0")
        self.assertEqual(services.META_GRAPH_API_VERSION, "v26.0")

    def test_auth_tokens_and_connect_views_share_base_url(self):
        from facebook_ads_mcp import services, auth_tokens, connect_views
        self.assertEqual(auth_tokens.FB_GRAPH_URL, services.BASE_URL)
        self.assertEqual(connect_views.FB_GRAPH_URL, services.BASE_URL)

    def test_create_adset_url_uses_base_url(self):
        from facebook_ads_mcp import services
        with mock.patch.object(services, "_post") as m:
            m.return_value = {"id": "as_1"}
            services.create_adset(
                "act_1", "tok", "c1", "Adset",
                billing_event="IMPRESSIONS",
                optimization_goal="LINK_CLICKS",
            )
        url = m.call_args[0][0]
        self.assertTrue(url.startswith(services.BASE_URL), f"url={url}")


class InstagramActorIdDeprecationTests(SimpleTestCase):
    def _attempts_for(self, **kwargs):
        """Run the fallback chain in dry-run mode by intercepting create_creative."""
        from facebook_ads_mcp import services
        captured = []

        def fake_create_creative(*args, **inner):
            captured.append({
                "instagram_user_id": inner.get("instagram_user_id"),
                "instagram_actor_id": inner.get("instagram_actor_id"),
            })
            return {"id": "cr_1"}  # success on first attempt

        with mock.patch.object(services, "create_creative", side_effect=fake_create_creative):
            with mock.patch.object(services, "get_instagram_accounts", return_value={}):
                with mock.patch.object(services, "get_page_backed_instagram_accounts", return_value={}):
                    services.create_creative_with_instagram_fallback(
                        "act_1", "tok", "name",
                        object_story_spec='{"page_id":"p1","link_data":{"link":"x"}}',
                        page_id="p1",
                        **kwargs,
                    )
        return captured

    def test_user_id_only_attempt_first(self):
        """When only instagram_user_id is given, first attempt uses it as user_id."""
        attempts = self._attempts_for(instagram_user_id="17841000000000001")
        self.assertEqual(attempts[0]["instagram_user_id"], "17841000000000001")
        self.assertIsNone(attempts[0]["instagram_actor_id"])

    def test_actor_id_promoted_to_user_id(self):
        """Caller-supplied instagram_actor_id should hit user_id slot first."""
        attempts = self._attempts_for(instagram_actor_id="17841000000000002")
        # First attempt must use the actor_id value as user_id (v22 semantics).
        self.assertEqual(attempts[0]["instagram_user_id"], "17841000000000002")
        self.assertIsNone(attempts[0]["instagram_actor_id"])


# ---------------------------------------------------------------------------
# Strict same-name swap guard (Part 3)
# ---------------------------------------------------------------------------

class NamesMatchForSwapTests(SimpleTestCase):
    def test_exact_match_case_insensitive(self):
        self.assertTrue(production_flow._names_match_for_swap("Lead Generation", "lead generation"))
        self.assertTrue(production_flow._names_match_for_swap("LEAD GENERATION", "Lead Generation"))

    def test_parenthetical_suffix_tolerated(self):
        self.assertTrue(production_flow._names_match_for_swap(
            "Lead Generation", "Lead Generation (marketing)"
        ))
        self.assertTrue(production_flow._names_match_for_swap(
            "Entrepreneurship (business and finance)", "Entrepreneurship"
        ))

    def test_different_concept_rejected(self):
        self.assertFalse(production_flow._names_match_for_swap(
            "Business school", "Information technology consulting"
        ))
        self.assertFalse(production_flow._names_match_for_swap(
            "Engaged shoppers", "Newly engaged (1 year)"
        ))

    def test_empty_strings(self):
        self.assertTrue(production_flow._names_match_for_swap("", ""))
        self.assertFalse(production_flow._names_match_for_swap("Lead Generation", ""))


class RevalidateStrictNameSwapTests(SimpleTestCase):
    def test_drops_when_canonical_name_mismatches(self):
        doc = {"targeting": {"flexible_spec": [{"interests": [
            {"id": "6002964301046", "name": "Business school"}
        ]}]}}
        fixes: list = []
        # Search returns a different-concept canonical (the corruption case).
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {
                "found": [{"id": "6003142970561", "name": "Information technology consulting",
                           "query": "Business school"}],
                "missing": [],
            }
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        # Drop, not swap.
        self.assertNotIn("interests", doc["targeting"]["flexible_spec"][0])
        self.assertTrue(any("different-concept" in f and "refusing to corrupt" in f for f in fixes), fixes)

    def test_swaps_when_canonical_name_matches_with_paren(self):
        doc = {"targeting": {"flexible_spec": [{"interests": [
            {"id": "6003111111111", "name": "Lead Generation"}
        ]}]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {
                "found": [{"id": "6003222222222", "name": "Lead Generation (marketing)",
                           "query": "Lead Generation"}],
                "missing": [],
            }
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)
        # Swap accepted (paren-suffix tolerated).
        ids = [it["id"] for it in doc["targeting"]["flexible_spec"][0]["interests"]]
        self.assertEqual(ids, ["6003222222222"])
        self.assertTrue(any("Swapped" in f for f in fixes), fixes)


class AutoresolveInterestsStrictNameTests(SimpleTestCase):
    def test_drops_different_concept_match_at_resolve_layer(self):
        doc = {"targeting": {"flexible_spec": [{"interests": [{"name": "Business school"}]}]}}
        fixes: list = []
        with mock.patch.object(production_flow.services, "batch_interest_search") as m:
            m.return_value = {
                "found": [{"id": "6003142970561", "name": "Information technology consulting",
                           "query": "Business school"}],
                "missing": [],
            }
            with mock.patch.object(production_flow, "_learned_bad_interest_ids", return_value=set()):
                production_flow._autoresolve_interests(doc, "act_1", "tok", fixes)
        # Empty interests axis (popped because nothing usable).
        self.assertNotIn("interests", doc["targeting"]["flexible_spec"][0])
        self.assertTrue(
            any("different concept" in f and "to avoid brief corruption" in f for f in fixes),
            fixes,
        )


# ---------------------------------------------------------------------------
# Per-axis revalidation (Part 4)
# ---------------------------------------------------------------------------

class RevalidatePerAxisSearchTests(SimpleTestCase):
    def test_behaviors_use_adtargeting_category_not_adinterest(self):
        doc = {"targeting": {"flexible_spec": [
            {"interests": [{"id": "6003111111111", "name": "Digital marketing"}]},
            {"behaviors": [{"id": "6002000000001", "name": "Engaged shoppers"}]},
        ]}}
        fixes: list = []
        interest_called_with = []
        category_called_with = []

        def fake_interest_search(account_id, token, names):
            interest_called_with.extend(names)
            return {
                "found": [{"id": "6003111111111", "name": "Digital marketing",
                           "query": "Digital marketing"}],
                "missing": [],
            }

        def fake_category_search(account_id, token, names):
            category_called_with.extend(names)
            return {
                "found": [{"id": "6002000000001", "name": "Engaged shoppers",
                           "path": ["Behaviors"], "query": "Engaged shoppers"}],
                "missing": [],
            }

        with mock.patch.object(
            production_flow.services, "batch_interest_search", side_effect=fake_interest_search
        ), mock.patch.object(
            production_flow.services, "batch_targeting_category_search", side_effect=fake_category_search
        ), mock.patch.object(
            production_flow, "_learned_bad_interest_ids", return_value=set()
        ):
            production_flow._revalidate_flex_via_search(doc, "act_1", "tok", fixes)

        # Each helper called with ONLY its axis's names — no cross-contamination.
        self.assertEqual(interest_called_with, ["Digital marketing"])
        self.assertEqual(category_called_with, ["Engaged shoppers"])
        # Both axes survive.
        slc0 = doc["targeting"]["flexible_spec"][0]
        slc1 = doc["targeting"]["flexible_spec"][1]
        self.assertEqual(slc0["interests"][0]["id"], "6003111111111")
        self.assertEqual(slc1["behaviors"][0]["id"], "6002000000001")


# ---------------------------------------------------------------------------
# Pixel attach skipped for CALL goals (Part 5)
# ---------------------------------------------------------------------------

class PixelAttachSkipTests(SimpleTestCase):
    def test_no_pixel_attach_goals_constant_includes_call_goals(self):
        # CALL goals must always be in the no-pixel-attach set (Meta error 1815704).
        self.assertIn("QUALITY_CALL", production_flow._NO_PIXEL_ATTACH_GOALS)
        self.assertIn("CALL_VOLUME", production_flow._NO_PIXEL_ATTACH_GOALS)

    def test_no_pixel_attach_goals_includes_video_view_goals(self):
        # Video-view goals don't have a VIDEO_VIEW custom_event_type on
        # promoted_object; Meta rejects with #100. The connector strips
        # pixel_id pre-emptively for these goals.
        for goal in ("THRUPLAY", "VIDEO_VIEWS", "TWO_SECOND_CONTINUOUS_VIDEO_VIEWS"):
            self.assertIn(goal, production_flow._NO_PIXEL_ATTACH_GOALS, goal)

    def test_no_pixel_attach_goals_includes_awareness_goals(self):
        # Awareness goals optimize against Meta's own models, not website events.
        for goal in ("REACH", "IMPRESSIONS", "AD_RECALL_LIFT"):
            self.assertIn(goal, production_flow._NO_PIXEL_ATTACH_GOALS, goal)

    def test_no_pixel_attach_goals_includes_on_meta_engagement_goals(self):
        # Engagement-on-Meta goals never trigger a website pixel.
        for goal in ("POST_ENGAGEMENT", "PAGE_LIKES", "EVENT_RESPONSES"):
            self.assertIn(goal, production_flow._NO_PIXEL_ATTACH_GOALS, goal)

    def test_strip_pixel_removes_pixel_id_and_custom_event_type(self):
        """For any goal in _NO_PIXEL_ATTACH_GOALS, _strip_pixel_for_call_goals
        must remove both pixel_id and custom_event_type from tracking."""
        for goal in ("THRUPLAY", "REACH", "POST_ENGAGEMENT", "QUALITY_CALL"):
            with self.subTest(goal=goal):
                doc = {
                    "adset": {"optimization_goal": goal},
                    "tracking": {"pixel_id": "12345", "custom_event_type": "VIDEO_VIEW"},
                }
                fixes: list = []
                production_flow._strip_pixel_for_call_goals(doc, fixes)
                self.assertNotIn("pixel_id", doc.get("tracking", {}))
                self.assertNotIn("custom_event_type", doc.get("tracking", {}))
                self.assertTrue(any("Stripped tracking.pixel_id" in f for f in fixes))


class PixelEventTypeMapTests(SimpleTestCase):
    """Custom_event_type aliasing: brief-style names → valid Meta enums."""

    def test_video_view_aliases_to_content_view(self):
        from facebook_ads_mcp import services
        for alias in ("VIDEO_VIEW", "VIDEO_VIEWS", "VideoView", "video_view", "THRUPLAY"):
            with self.subTest(alias=alias):
                self.assertEqual(services.PIXEL_EVENT_TYPE_MAP[alias], "CONTENT_VIEW")

    def test_page_view_aliases_still_work(self):
        from facebook_ads_mcp import services
        for alias in ("PAGE_VIEW", "PAGEVIEW", "PageView", "page_view", "VIEW_CONTENT", "VIEW"):
            with self.subTest(alias=alias):
                self.assertEqual(services.PIXEL_EVENT_TYPE_MAP[alias], "CONTENT_VIEW")

    def test_signup_aliases_to_complete_registration(self):
        from facebook_ads_mcp import services
        for alias in ("SIGN_UP", "SIGNUP", "REGISTRATION"):
            with self.subTest(alias=alias):
                self.assertEqual(services.PIXEL_EVENT_TYPE_MAP[alias], "COMPLETE_REGISTRATION")

    def test_meta_accepted_enums_pass_through(self):
        from facebook_ads_mcp import services
        # Each Meta-accepted enum maps to itself.
        for enum in ("CONTENT_VIEW", "PURCHASE", "LEAD", "ADD_TO_CART", "OTHER", "D2_RETENTION"):
            with self.subTest(enum=enum):
                self.assertEqual(services.PIXEL_EVENT_TYPE_MAP[enum], enum)


class ValidateCreativeRequiredFieldsTests(SimpleTestCase):
    """Pre-flight per-mode creative validator — fails fast BEFORE Meta object create."""

    def test_link_ad_missing_page_id_blocks(self):
        block = production_flow._validate_creative_required_fields(
            {"link_url": "https://example.com", "image_hash": "abc"}, "LINK_AD"
        )
        self.assertIsNotNone(block)
        self.assertIn("page_id", block["message"])

    def test_link_ad_missing_link_url_blocks(self):
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123", "image_hash": "abc"}, "LINK_AD"
        )
        self.assertIsNotNone(block)
        self.assertIn("link_url", block["message"])

    def test_link_ad_missing_asset_blocks(self):
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123", "link_url": "https://example.com"}, "LINK_AD"
        )
        self.assertIsNotNone(block)
        self.assertIn("asset", block["message"].lower())

    def test_link_ad_full_passes(self):
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123", "link_url": "https://example.com", "image_hash": "abc"},
            "LINK_AD",
        )
        self.assertIsNone(block)

    def test_existing_post_requires_object_story_id_or_ig_media(self):
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123"}, "EXISTING_POST"
        )
        self.assertIsNotNone(block)
        self.assertIn("object_story_id", block["message"])

    def test_existing_post_with_object_story_id_passes(self):
        block = production_flow._validate_creative_required_fields(
            {"object_story_id": "123_456"}, "EXISTING_POST"
        )
        self.assertIsNone(block)

    def test_existing_post_with_source_instagram_media_id_passes(self):
        block = production_flow._validate_creative_required_fields(
            {"source_instagram_media_id": "17841_xxx"}, "EXISTING_POST"
        )
        self.assertIsNone(block)

    def test_carousel_requires_cards(self):
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123"}, "CAROUSEL"
        )
        self.assertIsNotNone(block)
        self.assertIn("carousel_cards", block["message"])

    def test_lead_form_requires_form_id(self):
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123"}, "LEAD_FORM"
        )
        self.assertIsNotNone(block)
        self.assertIn("lead_gen_form_id", block["message"])

    def test_unknown_mode_passes_through(self):
        # Don't block on modes we don't know — let Meta surface the real error.
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123"}, "SOMETHING_NEW"
        )
        self.assertIsNone(block)

    # --- Video-goal asset-required validation ---
    def test_thruplay_with_image_only_blocks(self):
        """THRUPLAY + LINK_AD image-only is hard-rejected by Meta with subcode
        1815503. Catch upfront — universal across every video-views brief."""
        block = production_flow._validate_creative_required_fields(
            {
                "page_id": "123",
                "link_url": "https://example.com",
                "image_hash": "img_abc",
            },
            "LINK_AD",
            optimization_goal="THRUPLAY",
        )
        self.assertIsNotNone(block)
        self.assertIn("video", block["message"].lower())
        self.assertIn("1815503", block["message"])

    def test_video_views_with_image_only_blocks(self):
        """Same check, different video-family goal name."""
        block = production_flow._validate_creative_required_fields(
            {"page_id": "123", "link_url": "https://example.com", "image_hash": "h"},
            "LINK_AD",
            optimization_goal="VIDEO_VIEWS",
        )
        self.assertIsNotNone(block)

    def test_thruplay_with_video_id_passes(self):
        """Direct video_id from facebook_upload_video → ad creation allowed."""
        block = production_flow._validate_creative_required_fields(
            {
                "page_id": "123",
                "link_url": "https://example.com",
                "video_id": "vid_999",
            },
            "LINK_AD",
            optimization_goal="THRUPLAY",
        )
        self.assertIsNone(block)

    def test_thruplay_with_asset_feed_video_passes(self):
        """DCO mode with videos[] in asset_feed_spec satisfies the video req."""
        block = production_flow._validate_creative_required_fields(
            {
                "page_id": "123",
                "asset_feed_spec": {"videos": [{"video_id": "v1"}]},
            },
            "DCO",
            optimization_goal="THRUPLAY",
        )
        self.assertIsNone(block)

    def test_thruplay_with_existing_post_passes_through(self):
        """EXISTING_POST mode: we can't introspect the post type without a
        Meta call, so let Meta surface 1815503 if the post is image-only.
        The mode-level check already requires object_story_id or
        source_instagram_media_id, which is the contract."""
        block = production_flow._validate_creative_required_fields(
            {"object_story_id": "page_post_id"},
            "EXISTING_POST",
            optimization_goal="THRUPLAY",
        )
        self.assertIsNone(block)

    def test_non_video_goal_with_image_passes(self):
        """REACH / IMPRESSIONS / POST_ENGAGEMENT / LINK_CLICKS on image is
        perfectly fine — only video-family goals enforce video assets."""
        for goal in ("REACH", "IMPRESSIONS", "POST_ENGAGEMENT", "LINK_CLICKS"):
            with self.subTest(goal=goal):
                block = production_flow._validate_creative_required_fields(
                    {
                        "page_id": "123",
                        "link_url": "https://example.com",
                        "image_hash": "img",
                    },
                    "LINK_AD",
                    optimization_goal=goal,
                )
                self.assertIsNone(block, f"goal={goal} should pass with image")

    def test_carousel_with_video_card_passes_video_goal(self):
        """CAROUSEL with at least one video card satisfies a video goal."""
        block = production_flow._validate_creative_required_fields(
            {
                "page_id": "123",
                "carousel_cards": [
                    {"image_hash": "img1", "link": "https://a"},
                    {"video_id": "vid1", "link": "https://b"},
                ],
            },
            "CAROUSEL",
            optimization_goal="THRUPLAY",
        )
        self.assertIsNone(block)


# ---------------------------------------------------------------------------
# Two-pass search retry with paren-stripping (Fix #2)
# ---------------------------------------------------------------------------

class TwoPassSearchTests(SimpleTestCase):
    def test_strip_paren_suffix_basic(self):
        from facebook_ads_mcp import services
        self.assertEqual(services._strip_paren_suffix("Lead generation (marketing)"), "Lead generation")
        self.assertEqual(services._strip_paren_suffix("Digital marketing"), "Digital marketing")
        self.assertEqual(services._strip_paren_suffix(""), "")

    def test_batch_interest_search_retries_with_stripped_paren(self):
        """First pass with the qualifier returns empty; second pass without it succeeds."""
        from facebook_ads_mcp import services
        call_log = []

        def fake_search(account_id, token, search_type, **kwargs):
            q = kwargs.get("q")
            call_log.append(q)
            if q == "Lead generation (marketing)":
                return {"data": []}  # first pass misses
            if q == "Lead generation":
                return {"data": [{"id": "6003147000111", "name": "Lead generation",
                                  "type": "interests"}]}
            return {"data": []}

        with mock.patch.object(services, "targeting_search_with_fallback", side_effect=fake_search):
            r = services.batch_interest_search("act_1", "tok", ["Lead generation (marketing)"])

        self.assertEqual(call_log, ["Lead generation (marketing)", "Lead generation"])
        self.assertEqual(len(r["found"]), 1)
        self.assertEqual(r["found"][0]["id"], "6003147000111")
        # query field preserves the brief's original (paren-included) form so callers can match.
        self.assertEqual(r["found"][0]["query"], "Lead generation (marketing)")

    def test_batch_category_search_retries_with_stripped_paren(self):
        from facebook_ads_mcp import services
        call_log = []

        def fake_search(account_id, token, search_type, **kwargs):
            q = kwargs.get("q")
            call_log.append(q)
            if q == "Engaged shoppers (digital activities)":
                return {"data": []}
            if q == "Engaged shoppers":
                return {"data": [{"id": "6002714898570", "name": "Engaged shoppers",
                                  "path": ["Behaviors", "Digital activities"]}]}
            return {"data": []}

        with mock.patch.object(services, "targeting_search_with_fallback", side_effect=fake_search):
            r = services.batch_targeting_category_search("act_1", "tok", ["Engaged shoppers (digital activities)"])

        self.assertEqual(call_log[0], "Engaged shoppers (digital activities)")
        self.assertEqual(call_log[1], "Engaged shoppers")
        self.assertEqual(r["found"][0]["id"], "6002714898570")

    def test_no_paren_no_retry(self):
        """When the query has no parens, no second pass is made."""
        from facebook_ads_mcp import services
        call_log = []

        def fake_search(account_id, token, search_type, **kwargs):
            call_log.append(kwargs.get("q"))
            return {"data": []}

        with mock.patch.object(services, "targeting_search_with_fallback", side_effect=fake_search):
            r = services.batch_interest_search("act_1", "tok", ["Defunct Term"])

        self.assertEqual(len(call_log), 1)
        self.assertEqual(r["missing"], ["Defunct Term"])


# ---------------------------------------------------------------------------
# Advantage+ Audience auto-enable for sparse calls targeting (Fix #3)
# ---------------------------------------------------------------------------

class AdvantageAudienceWhenSparseTests(SimpleTestCase):
    def test_enabled_for_quality_call_with_thin_targeting(self):
        doc = {
            "adset": {"optimization_goal": "QUALITY_CALL"},
            "targeting": {"flexible_spec": [{"interests": [
                {"id": "6003111111111", "name": "X"},
                {"id": "6003222222222", "name": "Y"},
            ]}]},
        }
        fixes: list = []
        production_flow._enable_advantage_audience_when_sparse(doc, fixes)
        self.assertEqual(
            doc["targeting"]["targeting_automation"]["advantage_audience"], 1
        )
        self.assertTrue(any("Advantage+ Audience" in f for f in fixes), fixes)

    def test_disabled_for_quality_call_with_dense_targeting(self):
        items = [{"id": f"6003{i:011d}", "name": f"X{i}"} for i in range(6)]
        doc = {
            "adset": {"optimization_goal": "QUALITY_CALL"},
            "targeting": {"flexible_spec": [{"interests": items}]},
        }
        fixes: list = []
        production_flow._enable_advantage_audience_when_sparse(doc, fixes)
        ta = doc["targeting"].get("targeting_automation") or {}
        self.assertNotEqual(ta.get("advantage_audience"), 1)
        self.assertFalse(any("Advantage+ Audience" in f for f in fixes), fixes)

    def test_no_op_for_non_call_goal(self):
        doc = {
            "adset": {"optimization_goal": "LANDING_PAGE_VIEWS"},
            "targeting": {"flexible_spec": [{"interests": [
                {"id": "6003111111111", "name": "X"},
            ]}]},
        }
        fixes: list = []
        production_flow._enable_advantage_audience_when_sparse(doc, fixes)
        self.assertNotIn("targeting_automation", doc["targeting"])
        self.assertEqual(fixes, [])

    def test_respects_explicit_opt_out(self):
        doc = {
            "adset": {"optimization_goal": "QUALITY_CALL"},
            "targeting": {
                "flexible_spec": [{"interests": [{"id": "6003111111111", "name": "X"}]}],
                "targeting_automation": {"advantage_audience": 0},
            },
        }
        fixes: list = []
        production_flow._enable_advantage_audience_when_sparse(doc, fixes)
        # Caller's explicit 0 must not be overwritten.
        self.assertEqual(doc["targeting"]["targeting_automation"]["advantage_audience"], 0)

    def test_no_op_when_already_enabled(self):
        doc = {
            "adset": {"optimization_goal": "QUALITY_CALL"},
            "targeting": {
                "flexible_spec": [{"interests": [{"id": "6003111111111", "name": "X"}]}],
                "targeting_automation": {"advantage_audience": 1},
            },
        }
        fixes: list = []
        production_flow._enable_advantage_audience_when_sparse(doc, fixes)
        # Already 1; no change, no auto_fix.
        self.assertEqual(doc["targeting"]["targeting_automation"]["advantage_audience"], 1)
        self.assertEqual(fixes, [])


# ---------------------------------------------------------------------------
# Word-form tolerance in _names_match_for_swap
# ---------------------------------------------------------------------------

class NamesMatchWordFormTests(SimpleTestCase):
    def test_plural_singular(self):
        self.assertTrue(production_flow._names_match_for_swap("Owners", "Owner"))
        self.assertTrue(production_flow._names_match_for_swap("Shopper", "Shoppers"))
        self.assertTrue(production_flow._names_match_for_swap("Engaged Shoppers", "Engaged shopper"))

    def test_ship_to_er_variants(self):
        self.assertTrue(production_flow._names_match_for_swap("Entrepreneurs", "Entrepreneurship"))
        self.assertTrue(production_flow._names_match_for_swap("Entrepreneurship", "Entrepreneur"))
        self.assertTrue(production_flow._names_match_for_swap("Business Owners", "Business Ownership"))

    def test_punctuation_tolerated(self):
        self.assertTrue(production_flow._names_match_for_swap("Small business owner", "Small-business owner"))

    def test_genuine_different_concept_still_rejected(self):
        # Critical regression — must NOT accept these.
        self.assertFalse(production_flow._names_match_for_swap(
            "Business school", "Information technology consulting"
        ))
        self.assertFalse(production_flow._names_match_for_swap(
            "Engaged shoppers", "Newly engaged"
        ))
        self.assertFalse(production_flow._names_match_for_swap(
            "Marketing Professionals", "Friends of people with birthdays in a month"
        ))


# ---------------------------------------------------------------------------
# Cross-type search fallback in batch_targeting_category_search
# ---------------------------------------------------------------------------

class CrossTypeSearchFallbackTests(SimpleTestCase):
    def test_falls_back_to_adinterest_when_category_misses(self):
        """When adTargetingCategory returns nothing, adinterest is tried as fallback."""
        from facebook_ads_mcp import services
        call_log = []

        def fake_search(account_id, token, search_type, **kwargs):
            call_log.append((search_type, kwargs.get("q")))
            if search_type == "adTargetingCategory":
                return {"data": []}  # both passes miss
            if search_type == "adinterest":
                return {"data": [{"id": "6003147000111", "name": "Entrepreneurs",
                                  "type": "interests"}]}
            return {"data": []}

        with mock.patch.object(services, "targeting_search_with_fallback", side_effect=fake_search):
            r = services.batch_targeting_category_search("act_1", "tok", ["Entrepreneurs"])

        # Should have tried adTargetingCategory first, then fallen back to adinterest.
        types_tried = [t for t, _ in call_log]
        self.assertIn("adTargetingCategory", types_tried)
        self.assertIn("adinterest", types_tried)
        self.assertEqual(len(r["found"]), 1)
        self.assertEqual(r["found"][0]["id"], "6003147000111")
        # Synthetic path so caller's path[0] routing still works.
        self.assertEqual(r["found"][0]["path"], ["Behaviors"])

    def test_no_fallback_when_category_search_finds_match(self):
        from facebook_ads_mcp import services
        call_log = []

        def fake_search(account_id, token, search_type, **kwargs):
            call_log.append(search_type)
            return {"data": [{"id": "6002714898570", "name": "Engaged shoppers",
                              "path": ["Behaviors"]}]}

        with mock.patch.object(services, "targeting_search_with_fallback", side_effect=fake_search):
            r = services.batch_targeting_category_search("act_1", "tok", ["Engaged shoppers"])

        # Only adTargetingCategory called; no fallback to adinterest.
        self.assertEqual(call_log, ["adTargetingCategory"])
        self.assertEqual(r["found"][0]["id"], "6002714898570")

    def test_fallback_requires_exact_name_match(self):
        """The cross-type adinterest fallback must NOT accept fuzzy matches —
        only exact case-insensitive name matches. Different-concept fuzzy
        results stay dropped (production_flow's strict-name guard would catch
        them later anyway, but rejecting upfront keeps the response clean)."""
        from facebook_ads_mcp import services

        def fake_search(account_id, token, search_type, **kwargs):
            if search_type == "adTargetingCategory":
                return {"data": []}
            if search_type == "adinterest":
                # adinterest returns a fuzzy near-match, NOT exact.
                return {"data": [{"id": "6003147000111",
                                  "name": "Friends of people with birthdays in a month",
                                  "type": "interests"}]}
            return {"data": []}

        with mock.patch.object(services, "targeting_search_with_fallback", side_effect=fake_search):
            r = services.batch_targeting_category_search("act_1", "tok", ["Marketing Professionals"])
        # Fuzzy match must not be accepted via the cross-type path.
        self.assertEqual(r["found"], [])
        self.assertEqual(r["missing"], ["Marketing Professionals"])


# ---------------------------------------------------------------------------
# Strip pixel_id for CALL goals (prevents Meta subcode 1815704)
# ---------------------------------------------------------------------------

class StripPixelForCallGoalsTests(SimpleTestCase):
    def test_strips_pixel_id_for_quality_call(self):
        doc = {
            "adset": {"optimization_goal": "QUALITY_CALL"},
            "tracking": {"pixel_id": "1387427865790619", "custom_event_type": "CONTENT_VIEW"},
        }
        fixes: list = []
        production_flow._strip_pixel_for_call_goals(doc, fixes)
        self.assertNotIn("pixel_id", doc["tracking"])
        self.assertNotIn("custom_event_type", doc["tracking"])
        self.assertTrue(any("1815704" in f for f in fixes), fixes)

    def test_strips_pixel_id_for_call_volume(self):
        doc = {
            "adset": {"optimization_goal": "CALL_VOLUME"},
            "tracking": {"pixel_id": "1387427865790619"},
        }
        fixes: list = []
        production_flow._strip_pixel_for_call_goals(doc, fixes)
        self.assertNotIn("pixel_id", doc["tracking"])

    def test_no_op_for_landing_page_views(self):
        doc = {
            "adset": {"optimization_goal": "LANDING_PAGE_VIEWS"},
            "tracking": {"pixel_id": "1387427865790619"},
        }
        fixes: list = []
        production_flow._strip_pixel_for_call_goals(doc, fixes)
        # Pixel preserved for non-call goals.
        self.assertEqual(doc["tracking"]["pixel_id"], "1387427865790619")
        self.assertEqual(fixes, [])

    def test_no_op_when_pixel_id_absent(self):
        doc = {
            "adset": {"optimization_goal": "QUALITY_CALL"},
            "tracking": {},
        }
        fixes: list = []
        production_flow._strip_pixel_for_call_goals(doc, fixes)
        self.assertEqual(fixes, [])

    def test_no_op_when_no_tracking(self):
        doc = {"adset": {"optimization_goal": "QUALITY_CALL"}}
        fixes: list = []
        production_flow._strip_pixel_for_call_goals(doc, fixes)
        self.assertEqual(fixes, [])


# ---------------------------------------------------------------------------
# Instagram-account / page-backed-instagram retry: error codes 190 AND 200
# both trigger a page-scoped-token retry. Verified May 2026 against
# cross-business pages where the user-token's per-page grant returns code
# 200 but the page-scoped token from /me/accounts returns the data fine.
# ---------------------------------------------------------------------------

class InstagramAccountRetryTests(SimpleTestCase):
    """services.get_instagram_accounts + get_page_backed_instagram_accounts
    must retry with the page-scoped access token on BOTH Meta error 190 and
    error 200. Anything else surfaces through unchanged."""

    # ----- get_instagram_accounts -----

    def test_get_instagram_accounts_retries_on_code_190(self):
        """Classical Meta 190: first call rejects user token, retry with
        page-scoped token succeeds and returns the IG account payload."""
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 190, "message": "Page Access Token required"}},
            {"id": "page_1", "instagram_business_account": {"id": "ig_99"}},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token", return_value="page_tok"):
                result = services.get_instagram_accounts("page_1", "user_tok")
        self.assertEqual(m_get.call_count, 2)
        self.assertEqual(result.get("instagram_business_account", {}).get("id"), "ig_99")

    def test_get_instagram_accounts_retries_on_code_200(self):
        """NEW: Meta 200 (cross-business 'pages_read_engagement' / page-role
        nuance) also triggers the page-scoped-token retry. Production case
        observed May 2026 — direct curl confirmed page-scoped token reads
        the same /{page_id} fields successfully."""
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 200, "message": "pages_read_engagement missing"}},
            {"id": "page_2", "instagram_business_account": {"id": "ig_42"}},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token", return_value="page_tok"):
                result = services.get_instagram_accounts("page_2", "user_tok")
        self.assertEqual(m_get.call_count, 2)
        self.assertEqual(result.get("instagram_business_account", {}).get("id"), "ig_42")

    def test_get_instagram_accounts_does_not_retry_on_other_errors(self):
        """Codes outside (190, 200) must NOT trigger a retry — the original
        error surfaces unchanged, preserving Meta's diagnostic for the agent
        to read via structuredError."""
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 100, "message": "Invalid parameter"}},
            # Would only be consumed if retry incorrectly fired.
            {"id": "page_x"},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token", return_value="page_tok") as m_tok:
                result = services.get_instagram_accounts("page_x", "user_tok")
        self.assertEqual(m_get.call_count, 1)
        m_tok.assert_not_called()
        self.assertEqual(result.get("error", {}).get("code"), 100)

    def test_get_instagram_accounts_returns_error_when_page_token_lookup_fails(self):
        """If get_page_access_token returns None (e.g. user token can't read
        the page at all), the original error surfaces — no infinite recurse,
        no swallowed failure."""
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 200, "message": "pages_read_engagement missing"}},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token", return_value=None):
                result = services.get_instagram_accounts("page_y", "user_tok")
        self.assertEqual(m_get.call_count, 1)
        self.assertEqual(result.get("error", {}).get("code"), 200)

    # ----- get_page_backed_instagram_accounts (parallel coverage) -----

    def test_get_pbia_retries_on_code_190(self):
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 190, "message": "Page Access Token required"}},
            {"data": [{"id": "ig_pbia_1", "username": "rekha_bba"}]},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token", return_value="page_tok"):
                result = services.get_page_backed_instagram_accounts("page_1", "user_tok")
        self.assertEqual(m_get.call_count, 2)
        self.assertEqual(result.get("data", [{}])[0].get("id"), "ig_pbia_1")

    def test_get_pbia_retries_on_code_200(self):
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 200, "message": "pages_read_engagement missing"}},
            {"data": [{"id": "ig_pbia_2", "username": "rekha_bba"}]},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token", return_value="page_tok"):
                result = services.get_page_backed_instagram_accounts("page_2", "user_tok")
        self.assertEqual(m_get.call_count, 2)
        self.assertEqual(result.get("data", [{}])[0].get("id"), "ig_pbia_2")

    def test_get_pbia_does_not_retry_on_other_errors(self):
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 17, "message": "Rate limit"}},
            {"data": [{"id": "should_not_appear"}]},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token") as m_tok:
                result = services.get_page_backed_instagram_accounts("page_z", "user_tok")
        self.assertEqual(m_get.call_count, 1)
        m_tok.assert_not_called()
        self.assertEqual(result.get("error", {}).get("code"), 17)

    def test_get_pbia_surfaces_original_error_when_page_token_lookup_fails(self):
        from facebook_ads_mcp import services
        responses = iter([
            {"error": {"code": 200, "message": "pages_read_engagement missing"}},
        ])
        with mock.patch.object(services, "_get", side_effect=lambda *a, **k: next(responses)) as m_get:
            with mock.patch.object(services, "get_page_access_token", return_value=None):
                result = services.get_page_backed_instagram_accounts("page_q", "user_tok")
        self.assertEqual(m_get.call_count, 1)
        self.assertEqual(result.get("error", {}).get("code"), 200)


# ---------------------------------------------------------------------------
# Page-token cache: get_pages harvests page-scoped tokens; get_page_access_token
# prefers the cache so retries don't re-issue a Meta call that itself 200-fails.
# ---------------------------------------------------------------------------

class PageTokenCacheTests(SimpleTestCase):
    """services._PAGE_TOKEN_CACHE is populated by get_pages and read by
    get_page_access_token. This pair is what makes the in-function retries
    in get_instagram_accounts / get_page_backed_instagram_accounts actually
    succeed for cross-business pages."""

    def setUp(self):
        # Isolate the module-level cache between tests so order doesn't matter.
        from facebook_ads_mcp import services
        self._cache_snapshot = dict(services._PAGE_TOKEN_CACHE)
        services._PAGE_TOKEN_CACHE.clear()

    def tearDown(self):
        from facebook_ads_mcp import services
        services._PAGE_TOKEN_CACHE.clear()
        services._PAGE_TOKEN_CACHE.update(self._cache_snapshot)

    def test_get_pages_populates_token_cache(self):
        """Every page returned by get_pages that carries access_token
        should land in _PAGE_TOKEN_CACHE keyed by page id."""
        from facebook_ads_mcp import services
        responses = {
            "/me/accounts": {"data": [
                {"id": "page_a", "name": "PageA", "access_token": "tok_a"},
            ]},
            "/me/businesses": {"data": [{"id": "biz1", "name": "Biz1"}]},
            "/biz1/owned_pages": {"data": [
                {"id": "page_b", "name": "PageB", "access_token": "tok_b"},
            ]},
            "/biz1/client_pages": {"data": [
                {"id": "page_c", "name": "PageC", "access_token": "tok_c"},
            ]},
        }

        def fake_get(url, params=None):
            for needle, resp in responses.items():
                if needle in url:
                    return resp
            return {"data": []}

        with mock.patch.object(services, "_get", side_effect=fake_get):
            services.get_pages("user_tok")
        self.assertEqual(services._page_cache_get("user_tok", "page_a"), "tok_a")
        self.assertEqual(services._page_cache_get("user_tok", "page_b"), "tok_b")
        self.assertEqual(services._page_cache_get("user_tok", "page_c"), "tok_c")

    def test_get_page_access_token_uses_cache_first(self):
        """When the cache already has the page token, no Meta call should
        happen at all."""
        from facebook_ads_mcp import services
        services._page_cache_set("user_tok", "page_x", "cached_tok")
        with mock.patch.object(services, "_get") as m_get:
            result = services.get_page_access_token("page_x", "user_tok")
        self.assertEqual(result, "cached_tok")
        m_get.assert_not_called()

    def test_get_instagram_accounts_retry_uses_cached_token_when_meta_lookup_would_fail(self):
        """End-to-end: the exact production scenario. get_pages already
        populated the cache for Rekha Gupta. get_instagram_accounts hits
        Meta 200 with the user token. The retry calls get_page_access_token
        which reads the cache (no extra Meta call that would also fail)
        and re-issues the IG-account read with the page-scoped token,
        which now succeeds."""
        from facebook_ads_mcp import services
        services._page_cache_set("user_tok", "1165086893354722", "rekha_page_tok")

        calls = []

        def fake_get(url, params=None):
            calls.append((url, params.get("access_token") if params else None))
            tok = params.get("access_token") if params else None
            if tok == "rekha_page_tok":
                return {"id": "1165086893354722",
                        "instagram_business_account": {"id": "17841417760968301"}}
            return {"error": {"code": 200, "message": "pages_read_engagement missing"}}

        with mock.patch.object(services, "_get", side_effect=fake_get):
            result = services.get_instagram_accounts("1165086893354722", "user_tok")
        # First call with user token → 200; second call with cached page tok → success.
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], "user_tok")
        self.assertEqual(calls[1][1], "rekha_page_tok")
        self.assertEqual(
            result.get("instagram_business_account", {}).get("id"),
            "17841417760968301",
        )

    def test_get_page_access_token_falls_back_to_get_pages_when_user_lookup_fails(self):
        """Multi-worker scenario: cache is empty in THIS worker (another
        worker handled facebook_get_pages). The direct /{pid}?fields=
        access_token lookup ALSO fails (cross-business 200). The function
        should then call get_pages internally, harvest page tokens into
        the cache, and return the target page's token.

        This is the bug that broke the May 20 18:04 IST test: 3 gunicorn
        workers + a process-local cache that was only populated in worker
        A meant the agent's subsequent IG-account request landed on a
        cold worker."""
        from facebook_ads_mcp import services
        # Ensure cache is empty for this test (setUp already cleared it).

        def fake_get(url, params=None):
            tok = (params or {}).get("access_token")
            # Step 2 (direct /{pid}?fields=access_token with user token): 200.
            if "/1165086893354722" in url and (params or {}).get("fields") == "access_token":
                return {"error": {"code": 200, "message": "pages_read_engagement missing"}}
            # Step 3 (get_pages → /me/accounts): returns BOL7 (primary).
            if "/me/accounts" in url:
                return {"data": [
                    {"id": "1876788849233669", "name": "BOL7", "access_token": "bol7_tok"},
                ]}
            # Step 3 (get_pages → /me/businesses): the user's businesses.
            if "/me/businesses" in url:
                return {"data": [{"id": "biz_rekha", "name": "Rekha Gupta Fans Club"}]}
            # Step 3 (Rekha business → owned_pages): the page WITH its token.
            if "/biz_rekha/owned_pages" in url:
                return {"data": [
                    {"id": "1165086893354722", "name": "Rekha Gupta Fans Club",
                     "access_token": "rekha_page_tok"},
                ]}
            return {"data": []}

        with mock.patch.object(services, "_get", side_effect=fake_get):
            result = services.get_page_access_token("1165086893354722", "user_tok")
        self.assertEqual(result, "rekha_page_tok")
        # Cache should now contain the harvested token for this worker.
        self.assertEqual(
            services._page_cache_get("user_tok", "1165086893354722"),
            "rekha_page_tok",
        )

# ---------------------------------------------------------------------------
# Migration 0054 — 3-tier page check, soft-warn validate_page, cascades,
# destination_type strip, placement aliases, hash recursion, resolve account.
# ---------------------------------------------------------------------------


class CheckPageInAccountTieredTests(SimpleTestCase):
    """3-tier check: promote_pages -> client_pages -> page-side ad_accounts.

    Catches partner-shared pages (Business A shares page with Business B)
    that the old single-endpoint check would have false-positive-rejected.
    """

    def _route_get(self, *, promote=None, client=None, page=None):
        """Build a fake_get that routes by URL substring."""

        def fake_get(url, params, **_kw):
            if "/promote_pages" in url:
                return promote if promote is not None else {"data": []}
            if "/client_pages" in url:
                return client if client is not None else {"data": []}
            # Page-side GET /{pid}?fields=ad_accounts{...}
            return page if page is not None else {}
        return fake_get

    def test_partner_shared_page_found_via_client_pages_tier2(self):
        fake = self._route_get(
            promote={"data": [{"id": "999", "name": "Other"}]},
            client={"data": [{"id": "1165086893354722", "name": "Partner Page"}]},
        )
        with mock.patch.object(services, "_get", side_effect=fake):
            r = services.check_page_in_account("act_1", "tok", "1165086893354722")
        self.assertTrue(r["known"])
        self.assertTrue(r["found"])
        self.assertEqual(r["via"], "client_pages")
        ids = [p["id"] for p in r["available_pages"]]
        self.assertIn("1165086893354722", ids)

    def test_page_found_via_page_assigned_tier3(self):
        """Page-side lookup returns the page's ad_accounts; account_id matches."""
        fake = self._route_get(
            promote={"data": []},
            client={"data": []},
            page={"ad_accounts": {"data": [
                {"id": "act_1", "account_id": "1"},
            ]}},
        )
        with mock.patch.object(services, "_get", side_effect=fake):
            r = services.check_page_in_account("act_1", "tok", "55512")
        self.assertTrue(r["found"])
        self.assertEqual(r["via"], "page_assigned")

    def test_page_assigned_matches_bare_id_too(self):
        """Tier 3 handles act_X and bare numeric ids interchangeably."""
        fake = self._route_get(
            promote={"data": []},
            client={"data": []},
            page={"ad_accounts": {"data": [{"account_id": "1"}]}},
        )
        with mock.patch.object(services, "_get", side_effect=fake):
            r = services.check_page_in_account("act_1", "tok", "55512")
        self.assertTrue(r["found"])

    def test_not_found_all_three_tiers_clean(self):
        """All three calls succeed but no match -> known=True, found=False."""
        fake = self._route_get(
            promote={"data": [{"id": "999", "name": "Other"}]},
            client={"data": []},
            page={"ad_accounts": {"data": []}},
        )
        with mock.patch.object(services, "_get", side_effect=fake):
            r = services.check_page_in_account("act_1", "tok", "55512")
        self.assertTrue(r["known"])
        self.assertFalse(r["found"])
        self.assertIsNone(r["via"])
        self.assertEqual(r["tier_errors"], {})

    def test_tier_error_tolerance_marks_unknown(self):
        """One tier errors -> known=False so caller does not hard-block."""
        fake = self._route_get(
            promote={"data": [{"id": "999", "name": "Other"}]},
            client={"error": {"message": "(#200) Permissions error"}},
            page={"ad_accounts": {"data": []}},
        )
        with mock.patch.object(services, "_get", side_effect=fake):
            r = services.check_page_in_account("act_1", "tok", "55512")
        self.assertFalse(r["known"])
        self.assertIn("client_pages", r["tier_errors"])

    def test_tier_errors_dict_carries_diagnostic_messages(self):
        fake = self._route_get(
            promote={"error": {"message": "scope missing"}},
            client={"error": {"message": "scope missing"}},
            page={"error": {"message": "page not visible"}},
        )
        with mock.patch.object(services, "_get", side_effect=fake):
            r = services.check_page_in_account("act_1", "tok", "55512")
        self.assertFalse(r["known"])
        self.assertEqual(set(r["tier_errors"]), {"promote_pages", "client_pages", "page_assigned"})

    def test_tier1_short_circuits_when_match_found(self):
        """Owner-assigned (Tier 1) match returns immediately; Tier 2/3 never called."""
        call_count = {"n": 0}

        def fake(url, params, **_kw):
            call_count["n"] += 1
            return {"data": [{"id": "55512", "name": "Owner Page"}]}

        with mock.patch.object(services, "_get", side_effect=fake):
            r = services.check_page_in_account("act_1", "tok", "55512")
        self.assertEqual(r["via"], "promote_pages")
        self.assertEqual(call_count["n"], 1)


class ValidatePageSoftWarnTests(SimpleTestCase):
    """Sync proceeds with a soft-warn when the 3-tier check says known=True,
    found=False — unless options.strict_page_account_check=true (then hard-block).
    """

    def _minimal_doc(self, *, strict_check=False):
        doc = {
            "campaign": {"name": "C", "objective": "OUTCOME_AWARENESS"},
            "adset": {
                "name": "A",
                "optimization_goal": "REACH",
                "destination_type": None,
                "promoted_object": {"page_id": "555"},
            },
            "creative": {"name": "Cr", "mode": "LINK_AD",
                         "page_id": "555", "link_url": "https://x",
                         "message": "hi", "image_hash": "deadbeef"},
            "ad": {"name": "Ad"},
        }
        if strict_check:
            doc["options"] = {"strict_page_account_check": True}
        return doc

    def test_soft_warn_default_proceeds_past_validate_page(self):
        """Soft-warn: validate_page must NOT short-circuit with sync_step_error."""
        doc = self._minimal_doc(strict_check=False)
        page_check = {
            "found": False, "known": True, "via": None,
            "available_pages": [{"id": "999", "name": "Other"}],
            "tier_errors": {},
        }
        with mock.patch.object(services, "check_page_in_account", return_value=page_check):
            # Mock build_targeting + downstream so we don't go all the way to Meta.
            with mock.patch.object(production_flow, "build_targeting_from_document",
                                   side_effect=RuntimeError("STOP_PAST_VALIDATE_PAGE")):
                with mock.patch.object(production_flow, "_autoresolve_phase"):
                    with mock.patch.object(production_flow, "_revalidate_flex_via_search"):
                        try:
                            production_flow.sync_document_campaign("act_1", "tok", doc, user_confirmed=True)
                        except RuntimeError as exc:
                            # Expected: we made it past validate_page.
                            self.assertEqual(str(exc), "STOP_PAST_VALIDATE_PAGE")

    def test_strict_page_check_opt_in_hard_blocks(self):
        """options.strict_page_account_check=true -> the old hard-block fires."""
        doc = self._minimal_doc(strict_check=True)
        page_check = {
            "found": False, "known": True, "via": None,
            "available_pages": [{"id": "999", "name": "Other"}],
            "tier_errors": {},
        }
        with mock.patch.object(services, "check_page_in_account", return_value=page_check):
            with mock.patch.object(production_flow, "_autoresolve_phase"):
                with mock.patch.object(production_flow, "_revalidate_flex_via_search"):
                    result = production_flow.sync_document_campaign(
                        "act_1", "tok", doc, user_confirmed=True,
                    )
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("step"), "validate_page")
        self.assertIn("page_account_mismatch", str(result))


class AuthorizationCategoryCascadeTests(SimpleTestCase):
    """campaign.special_ad_categories ISSUES_ELECTIONS_POLITICS auto-sets
    creative.authorization_category=POLITICAL — fixes Meta subcode 2446466.
    """

    def test_politics_cascades_to_creative_authorization_category(self):
        camp = {
            "name": "C", "objective": "OUTCOME_AWARENESS",
            "special_ad_categories": ["ISSUES_ELECTIONS_POLITICS"],
        }
        creative = {"name": "Cr", "mode": "LINK_AD"}
        fixes = []
        production_flow._cascade_special_ad_category_to_creative(camp, creative, fixes)
        self.assertEqual(creative.get("authorization_category"), "POLITICAL")
        self.assertTrue(any("authorization_category=POLITICAL" in f for f in fixes))

    def test_existing_authorization_category_not_overwritten(self):
        camp = {"special_ad_categories": ["ISSUES_ELECTIONS_POLITICS"]}
        creative = {"authorization_category": "POLITICAL_LOCAL_LEVEL"}
        fixes = []
        production_flow._cascade_special_ad_category_to_creative(camp, creative, fixes)
        self.assertEqual(creative["authorization_category"], "POLITICAL_LOCAL_LEVEL")
        self.assertFalse(fixes)

    def test_housing_does_not_set_authorization_category(self):
        camp = {"special_ad_categories": ["HOUSING"]}
        creative = {}
        fixes = []
        production_flow._cascade_special_ad_category_to_creative(camp, creative, fixes)
        self.assertNotIn("authorization_category", creative)
        self.assertFalse(fixes)

    def test_employment_credit_no_cascade(self):
        for cat in ("EMPLOYMENT", "CREDIT"):
            creative = {}
            production_flow._cascade_special_ad_category_to_creative(
                {"special_ad_categories": [cat]}, creative, []
            )
            self.assertNotIn("authorization_category", creative)

    def test_cascade_runs_via_validate_and_normalize_document(self):
        """Full integration: cascade fires when running through the document
        normalizer (the path sync uses on every brief)."""
        doc = {
            "campaign": {
                "name": "Rekha campaign", "objective": "OUTCOME_AWARENESS",
                "special_ad_categories": ["ISSUES_ELECTIONS_POLITICS"],
            },
            "adset": {"name": "A", "optimization_goal": "REACH"},
            "creative": {"name": "Cr", "mode": "LINK_AD",
                         "page_id": "1", "link_url": "https://x", "message": "hi"},
            "ad": {"name": "Ad"},
        }
        normalized, fixes = production_flow.validate_and_normalize_document(doc)
        self.assertEqual(normalized["creative"].get("authorization_category"), "POLITICAL")


class BidStrategyAmountFallbackTests(SimpleTestCase):
    """A bid-cap strategy with NO bid_amount anywhere -> auto-fallback to
    LOWEST_COST_WITHOUT_CAP (else Meta fails with subcode 1815857)."""

    def _doc(self, bid_strategy_on="campaign", strategy="LOWEST_COST_WITH_BID_CAP",
             bid_amount=None):
        camp = {"name": "C", "objective": "OUTCOME_AWARENESS", "daily_budget": 10000}
        ads = {"name": "A", "optimization_goal": "REACH"}
        if bid_strategy_on == "campaign":
            camp["bid_strategy"] = strategy
        else:
            ads["bid_strategy"] = strategy
        if bid_amount is not None:
            ads["bid_amount"] = bid_amount
        return {"campaign": camp, "adset": ads,
                "creative": {"name": "Cr", "mode": "LINK_AD", "page_id": "1",
                             "link_url": "https://x", "message": "hi"},
                "ad": {"name": "Ad"}}

    def test_bidcap_no_amount_falls_back_to_lowest_cost(self):
        norm, fixes = production_flow.validate_and_normalize_document(self._doc())
        # bid_strategy must no longer be a bid-cap variant on either object
        self.assertNotEqual(norm["campaign"].get("bid_strategy"), "LOWEST_COST_WITH_BID_CAP")
        self.assertNotEqual(norm["adset"].get("bid_strategy"), "LOWEST_COST_WITH_BID_CAP")
        self.assertTrue(any("1815857" in f or "LOWEST_COST_WITHOUT_CAP" in f for f in fixes))

    def test_bidcap_with_amount_is_kept(self):
        # bid_amount present -> strategy preserved, no fallback.
        norm, fixes = production_flow.validate_and_normalize_document(
            self._doc(bid_amount=2000))
        self.assertEqual(norm["adset"].get("bid_amount"), 2000)

    def test_default_strategy_untouched(self):
        norm, fixes = production_flow.validate_and_normalize_document(
            self._doc(strategy="LOWEST_COST_WITHOUT_CAP"))
        # already automatic — no bid fallback message
        self.assertFalse(any("Switched to LOWEST_COST_WITHOUT_CAP" in f for f in fixes))


class DestinationTypeStripTests(SimpleTestCase):
    """OUTCOME_AWARENESS + ON_AD is invalid; auto-strip before strict validator."""

    def test_awareness_on_ad_stripped(self):
        camp = {"objective": "OUTCOME_AWARENESS"}
        ads = {"destination_type": "ON_AD"}
        fixes = []
        production_flow._strip_invalid_destination_type(camp, ads, fixes)
        self.assertNotIn("destination_type", ads)
        self.assertTrue(any("Dropped destination_type=ON_AD" in f for f in fixes))

    def test_valid_combo_not_stripped(self):
        camp = {"objective": "OUTCOME_TRAFFIC"}
        ads = {"destination_type": "WEBSITE"}
        fixes = []
        production_flow._strip_invalid_destination_type(camp, ads, fixes)
        self.assertEqual(ads.get("destination_type"), "WEBSITE")
        self.assertFalse(fixes)

    def test_unknown_objective_passes_through(self):
        camp = {"objective": "OUTCOME_FOOBAR"}
        ads = {"destination_type": "ON_AD"}
        fixes = []
        production_flow._strip_invalid_destination_type(camp, ads, fixes)
        # Unknown objective -> leave Meta to surface the real error.
        self.assertEqual(ads.get("destination_type"), "ON_AD")

    def test_missing_objective_or_dest_is_noop(self):
        production_flow._strip_invalid_destination_type({}, {}, [])
        production_flow._strip_invalid_destination_type({"objective": "OUTCOME_AWARENESS"}, {}, [])


class PlacementAliasTests(SimpleTestCase):
    """facebook_positions=reels -> facebook_reels (Meta v25 token);
    instagram_positions=reels stays as 'reels' (canonical there)."""

    def test_facebook_reels_aliased(self):
        holder = {"facebook_positions": ["reels"]}
        fixes = []
        production_flow._alias_placement_tokens(holder, fixes)
        self.assertEqual(holder["facebook_positions"], ["facebook_reels"])
        self.assertTrue(any("facebook_reels" in f for f in fixes))

    def test_instagram_reels_canonical(self):
        holder = {"instagram_positions": ["reels"]}
        fixes = []
        production_flow._alias_placement_tokens(holder, fixes)
        self.assertEqual(holder["instagram_positions"], ["reels"])
        self.assertFalse(fixes)

    def test_facebook_story_typos(self):
        for raw in ("stories", "story"):
            holder = {"facebook_positions": [raw]}
            production_flow._alias_placement_tokens(holder, [])
            self.assertEqual(holder["facebook_positions"], ["story"])

    def test_mixed_valid_and_aliased(self):
        holder = {"facebook_positions": ["feed", "reels", "marketplace"]}
        production_flow._alias_placement_tokens(holder, [])
        self.assertEqual(
            holder["facebook_positions"],
            ["feed", "facebook_reels", "marketplace"],
        )

    def test_instagram_feed_to_stream(self):
        """IG 'feed' is canonically 'stream' on the API side."""
        holder = {"instagram_positions": ["feed"]}
        production_flow._alias_placement_tokens(holder, [])
        self.assertEqual(holder["instagram_positions"], ["stream"])

    def test_empty_or_missing_lists_safe(self):
        # No throw on missing keys / non-list / empty.
        production_flow._alias_placement_tokens({}, [])
        production_flow._alias_placement_tokens({"facebook_positions": None}, [])
        production_flow._alias_placement_tokens({"facebook_positions": []}, [])


class HashGateRecursionTests(SimpleTestCase):
    """Hash excludes strict_preview_document_hash + strict_validator_version
    from the hashed body so the preview->sync round trip is stable.
    """

    def test_hash_stable_when_only_strict_fields_added(self):
        base = {
            "campaign": {"name": "C", "objective": "OUTCOME_AWARENESS"},
            "adset": {"name": "A"},
        }
        h1 = production_flow._compute_document_hash(base)
        with_hash = dict(base, options={"strict_preview_document_hash": h1})
        h2 = production_flow._compute_document_hash(with_hash)
        self.assertEqual(h1, h2)

    def test_strict_validator_version_also_excluded(self):
        base = {"campaign": {"name": "C"}}
        h1 = production_flow._compute_document_hash(base)
        h2 = production_flow._compute_document_hash(dict(
            base, options={"strict_validator_version": 7,
                           "strict_preview_document_hash": "abc"},
        ))
        self.assertEqual(h1, h2)

    def test_other_options_changes_change_hash(self):
        """Real document changes still flip the hash."""
        a = {"campaign": {"name": "C"}, "options": {"strict_mode": True}}
        b = {"campaign": {"name": "C"}, "options": {"strict_mode": False}}
        self.assertNotEqual(
            production_flow._compute_document_hash(a),
            production_flow._compute_document_hash(b),
        )

    def test_body_mutation_flips_hash(self):
        a = {"campaign": {"name": "C", "objective": "OUTCOME_AWARENESS"}}
        b = {"campaign": {"name": "C", "objective": "OUTCOME_TRAFFIC"}}
        self.assertNotEqual(
            production_flow._compute_document_hash(a),
            production_flow._compute_document_hash(b),
        )


class ResolveAccountForPageTests(SimpleTestCase):
    """services.resolve_account_for_page tries each accessible ad account,
    returns the first match (with `via` from the 3-tier check), stops at
    first match.
    """

    def test_returns_match_on_second_account(self):
        accounts = {"data": [
            {"id": "act_111", "name": "Brand", "account_status": 1},
            {"id": "act_222", "name": "Politics", "account_status": 1},
        ]}
        # Page found ONLY on account 222.
        page_checks = {
            "act_111": {"found": False, "known": True, "via": None,
                        "available_pages": [], "tier_errors": {}},
            "act_222": {"found": True, "known": True, "via": "client_pages",
                        "available_pages": [{"id": "55512", "name": "X"}],
                        "tier_errors": {}},
        }
        with mock.patch.object(services, "get_ad_accounts", return_value=accounts):
            with mock.patch.object(
                services, "check_page_in_account",
                side_effect=lambda aid, tok, pid: page_checks[aid],
            ):
                r = services.resolve_account_for_page("55512", "tok")
        self.assertTrue(r["ok"])
        self.assertEqual(r["account_id"], "act_222")
        self.assertEqual(r["via"], "client_pages")
        self.assertEqual(len(r["tried_accounts"]), 2)

    def test_stops_at_first_match(self):
        """First match short-circuits — subsequent check_page_in_account
        calls should NOT happen."""
        accounts = {"data": [
            {"id": "act_111", "name": "First", "account_status": 1},
            {"id": "act_222", "name": "Second", "account_status": 1},
            {"id": "act_333", "name": "Third", "account_status": 1},
        ]}
        call_count = {"n": 0}

        def fake_check(aid, tok, pid):
            call_count["n"] += 1
            return {"found": True, "known": True, "via": "promote_pages",
                    "available_pages": [], "tier_errors": {}}

        with mock.patch.object(services, "get_ad_accounts", return_value=accounts):
            with mock.patch.object(services, "check_page_in_account", side_effect=fake_check):
                r = services.resolve_account_for_page("55512", "tok")
        self.assertTrue(r["ok"])
        self.assertEqual(r["account_id"], "act_111")
        self.assertEqual(call_count["n"], 1)

    def test_not_bound_to_any_active_account(self):
        accounts = {"data": [
            {"id": "act_111", "name": "A", "account_status": 1},
            {"id": "act_222", "name": "B", "account_status": 1},
        ]}
        with mock.patch.object(services, "get_ad_accounts", return_value=accounts):
            with mock.patch.object(
                services, "check_page_in_account",
                return_value={"found": False, "known": True, "via": None,
                              "available_pages": [], "tier_errors": {}},
            ):
                r = services.resolve_account_for_page("55512", "tok")
        self.assertFalse(r["ok"])
        self.assertIn("not bound", r["error"])
        self.assertEqual(len(r["tried_accounts"]), 2)

    def test_skips_inactive_accounts(self):
        """account_status != 1 should not be probed."""
        accounts = {"data": [
            {"id": "act_111", "name": "Inactive", "account_status": 2},
            {"id": "act_222", "name": "Active", "account_status": 1},
        ]}
        probed = []

        def fake_check(aid, tok, pid):
            probed.append(aid)
            return {"found": False, "known": True, "via": None,
                    "available_pages": [], "tier_errors": {}}

        with mock.patch.object(services, "get_ad_accounts", return_value=accounts):
            with mock.patch.object(services, "check_page_in_account", side_effect=fake_check):
                services.resolve_account_for_page("55512", "tok")
        self.assertEqual(probed, ["act_222"])

    def test_missing_page_id_returns_error(self):
        r = services.resolve_account_for_page("", "tok")
        self.assertFalse(r["ok"])
        self.assertIn("page_id", r["error"])

    def test_get_ad_accounts_error_returns_friendly_error(self):
        with mock.patch.object(
            services, "get_ad_accounts",
            return_value={"error": {"message": "(#190) token expired"}},
        ):
            r = services.resolve_account_for_page("55512", "tok")
        self.assertFalse(r["ok"])
        self.assertIn("token expired", r["error"])


class GetInstagramMediaShortcodeQueryTests(SimpleTestCase):
    """get_instagram_media: a shortcode-shaped caption_query (e.g. DY3yAOBDTV9)
    must match the PERMALINK, not the caption (where it never appears)."""

    def setUp(self):
        self._patch = mock.patch.object(services, "_resolve_ig_user_id",
                                        return_value=("17841", None))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def test_shortcode_matches_permalink_not_caption(self):
        page = {"data": [
            {"id": "1", "caption": "unrelated text", "permalink": "https://www.instagram.com/reel/AAAAA/"},
            {"id": "2", "caption": "Delhi EV buses", "permalink": "https://www.instagram.com/reel/DY3yAOBDTV9/"},
        ], "paging": {}}
        with mock.patch.object(services, "_get", return_value=page):
            r = services.get_instagram_media("tok", instagram_user_id="17841",
                                             caption_query="DY3yAOBDTV9")
        self.assertEqual(r["search"]["lookup"], "shortcode_permalink")
        self.assertTrue(r["search"]["matched"])
        self.assertEqual([m["id"] for m in r["data"]], ["2"])

    def test_shortcode_not_found_returns_empty(self):
        page = {"data": [{"id": "1", "caption": "x", "permalink": "https://www.instagram.com/reel/ZZZ/"}],
                "paging": {}}
        with mock.patch.object(services, "_get", return_value=page):
            r = services.get_instagram_media("tok", instagram_user_id="17841",
                                             caption_query="DY3yAOBDTV9")
        self.assertEqual(r["search"]["lookup"], "shortcode_permalink")
        self.assertFalse(r["search"]["matched"])

    def test_plain_text_query_still_matches_caption(self):
        page = {"data": [{"id": "9", "caption": "Delhi EV buses are great",
                          "permalink": "https://www.instagram.com/reel/QQQ/"}], "paging": {}}
        with mock.patch.object(services, "_get", return_value=page):
            r = services.get_instagram_media("tok", instagram_user_id="17841",
                                             caption_query="EV buses")
        self.assertEqual(r["search"]["lookup"], "caption_text")
        self.assertTrue(r["search"]["matched"])

    def test_partial_shortcode_matches_permalink(self):
        # An agent may send a TRUNCATED shortcode (e.g. "DY3y", 4 chars).
        # It must still match against the permalink, not be treated as caption.
        page = {"data": [
            {"id": "1", "caption": "x", "permalink": "https://www.instagram.com/reel/AAAAA/"},
            {"id": "2", "caption": "y", "permalink": "https://www.instagram.com/reel/DY3yAOBDTV9/"},
        ], "paging": {}}
        with mock.patch.object(services, "_get", return_value=page):
            r = services.get_instagram_media("tok", instagram_user_id="17841",
                                             caption_query="DY3y")
        self.assertEqual(r["search"]["lookup"], "shortcode_permalink")
        self.assertTrue(r["search"]["matched"])
        self.assertEqual([m["id"] for m in r["data"]], ["2"])


class GetPagePostsCaptionQueryTests(SimpleTestCase):
    """Server-side substring search + auto-pagination for get_page_posts.

    Eliminates a class of agent bugs where the caller passes too small a
    limit and gives up before finding a post that exists further back.
    """

    def test_no_caption_query_returns_all_posts(self):
        meta_resp = {"data": [{"id": "1", "message": "hello"}],
                     "paging": {"cursors": {}}}
        with mock.patch.object(services, "_get", return_value=meta_resp):
            r = services.get_page_posts("page1", "tok")
        self.assertEqual(r["data"], meta_resp["data"])
        self.assertNotIn("search", r)

    def test_caption_query_matches_on_first_page(self):
        page1 = {
            "data": [
                {"id": "1", "message": "random news today"},
                {"id": "2", "message": "Today is a day to salute India's scientific achievements"},
            ],
            "paging": {},
        }
        with mock.patch.object(services, "_get", return_value=page1):
            r = services.get_page_posts("page1", "tok", caption_query="scientific achievements")
        self.assertTrue(r["search"]["matched"])
        self.assertEqual(r["search"]["pages_scanned"], 1)
        self.assertEqual(len(r["data"]), 1)
        self.assertEqual(r["data"][0]["id"], "2")

    def test_caption_query_walks_pagination(self):
        page1 = {
            "data": [{"id": "a", "message": "first page only"}],
            "paging": {"next": "https://example/next1"},
        }
        page2 = {
            "data": [{"id": "b", "message": "still nothing here"}],
            "paging": {"next": "https://example/next2"},
        }
        page3 = {
            "data": [{"id": "c", "message": "Scientific achievements salute India"}],
            "paging": {},
        }
        calls = [page1, page2, page3]
        def fake_get(url, params, timeout=None):
            return calls.pop(0)
        with mock.patch.object(services, "_get", side_effect=fake_get):
            r = services.get_page_posts(
                "page1", "tok", caption_query="scientific",
            )
        self.assertTrue(r["search"]["matched"])
        self.assertEqual(r["search"]["pages_scanned"], 3)
        self.assertEqual(r["data"][0]["id"], "c")

    def test_caption_query_case_insensitive(self):
        page1 = {
            "data": [{"id": "x", "message": "TODAY IS A DAY TO SALUTE"}],
            "paging": {},
        }
        with mock.patch.object(services, "_get", return_value=page1):
            r = services.get_page_posts("page1", "tok", caption_query="today is")
        self.assertTrue(r["search"]["matched"])
        self.assertEqual(r["data"][0]["id"], "x")

    def test_caption_query_stops_when_paging_next_missing(self):
        page = {"data": [{"id": "a", "message": "nope"}], "paging": {}}
        with mock.patch.object(services, "_get", return_value=page):
            r = services.get_page_posts(
                "page1", "tok", caption_query="needle",
            )
        self.assertFalse(r["search"]["matched"])
        self.assertEqual(r["search"]["pages_scanned"], 1)

    def test_caption_query_returns_meta_error_unchanged(self):
        err = {"error": {"message": "boom", "code": 12}}
        with mock.patch.object(services, "_get", return_value=err):
            r = services.get_page_posts("page1", "tok", caption_query="x")
        self.assertIn("error", r)
        self.assertNotIn("search", r)

    def test_caption_query_token_swap_then_search(self):
        """190 → swap to page-scoped token → then search. Both happy paths
        must compose: token retry happens before caption_query walks pages."""
        first = {"error": {"code": 190, "message": "user token rejected"}}
        retry_page = {
            "data": [{"id": "match", "message": "Scientific achievements salute"}],
            "paging": {},
        }
        responses = [first, retry_page]
        def fake_get(url, params, timeout=None):
            return responses.pop(0)
        with mock.patch.object(services, "_get", side_effect=fake_get):
            with mock.patch.object(services, "get_page_access_token",
                                   return_value="page-tok"):
                r = services.get_page_posts(
                    "page1", "user-tok", caption_query="scientific",
                )
        self.assertTrue(r["search"]["matched"])
        self.assertEqual(r["data"][0]["id"], "match")

    def test_empty_caption_query_treated_as_unset(self):
        meta_resp = {"data": [{"id": "1"}], "paging": {}}
        with mock.patch.object(services, "_get", return_value=meta_resp):
            r = services.get_page_posts("page1", "tok", caption_query="   ")
        self.assertEqual(r["data"], meta_resp["data"])
        self.assertNotIn("search", r)




# ====================================================================
# Phase 1+2: forbidden combo suggestions + user_options propagation
# ====================================================================


class ForbiddenComboSuggestionsTests(SimpleTestCase):
    """Verify expanded _FORBIDDEN_OBJECTIVE_OPTIMIZATION + suggestions map."""

    def test_forbidden_pairs_count_matches_docs(self):
        """Docs §3 lists 16 forbidden (objective, goal) pairs."""
        from facebook_ads_mcp import meta_validation
        self.assertEqual(len(meta_validation._FORBIDDEN_OBJECTIVE_OPTIMIZATION), 16)

    def test_every_forbidden_pair_has_suggestions(self):
        """Every forbidden pair must have a suggestions entry."""
        from facebook_ads_mcp import meta_validation
        for pair in meta_validation._FORBIDDEN_OBJECTIVE_OPTIMIZATION:
            self.assertIn(
                pair, meta_validation._SUGGESTIONS_FOR_FORBIDDEN,
                f"Missing suggestions for forbidden pair {pair}",
            )
            opts = meta_validation._SUGGESTIONS_FOR_FORBIDDEN[pair]
            self.assertGreaterEqual(len(opts), 2,
                f"{pair} should offer at least 2 alternatives")
            for opt in opts:
                self.assertIn("label", opt)
                self.assertIn("patch", opt)
                self.assertIn("explanation", opt)

    def test_traffic_thruplay_blocked_with_options(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_and_optimization(
            "OUTCOME_TRAFFIC", "THRUPLAY"
        )
        self.assertIsNotNone(result)
        self.assertIn("message", result)
        self.assertIn("user_options", result)
        self.assertGreaterEqual(len(result["user_options"]), 2)

    def test_sales_post_engagement_blocked_with_options(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_and_optimization(
            "OUTCOME_SALES", "POST_ENGAGEMENT"
        )
        self.assertIsNotNone(result)
        # Sales+POST_ENGAGEMENT is in expanded forbidden list
        self.assertIn("user_options", result)

    def test_engagement_thruplay_still_valid(self):
        """Pichli session ke fix mein ENGAGEMENT+THRUPLAY ko allow kiya tha.
        Yeh regression-safety test hai."""
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_and_optimization(
            "OUTCOME_ENGAGEMENT", "THRUPLAY"
        )
        self.assertIsNone(result, "ENGAGEMENT+THRUPLAY must remain valid (UI-verified)")

    def test_app_promotion_reach_blocked(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_and_optimization(
            "OUTCOME_APP_PROMOTION", "REACH"
        )
        self.assertIsNotNone(result)
        self.assertIn("user_options", result)

    def test_unknown_combo_returns_none(self):
        """Combos not in forbidden list pass through (let Meta decide)."""
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_and_optimization(
            "OUTCOME_SALES", "OFFSITE_CONVERSIONS"
        )
        self.assertIsNone(result)

    def test_empty_inputs_return_none(self):
        from facebook_ads_mcp import meta_validation
        self.assertIsNone(meta_validation.validate_objective_and_optimization("", ""))
        self.assertIsNone(meta_validation.validate_objective_and_optimization(None, "REACH"))
        self.assertIsNone(meta_validation.validate_objective_and_optimization("OUTCOME_X", None))


# ====================================================================
# Special-ad-category combo restriction (political 2490408)
# ====================================================================


class SpecialAdCategoryComboTests(SimpleTestCase):
    """validate_special_ad_category_combo blocks combos rejected only under a
    restricted category (political), and stays silent otherwise."""

    def setUp(self):
        from facebook_ads_mcp import meta_validation
        self.mv = meta_validation

    def test_political_engagement_post_engagement_is_allowed(self):
        # Live-verified 2026-07: Meta ACCEPTS OUTCOME_ENGAGEMENT+POST_ENGAGEMENT
        # under ISSUES_ELECTIONS_POLITICS; the old hardcoded block was wrong.
        self.assertIsNone(self.mv.validate_special_ad_category_combo(
            ["ISSUES_ELECTIONS_POLITICS"], "OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT"))

    def test_political_engagement_page_likes_is_blocked(self):
        r = self.mv.validate_special_ad_category_combo(
            ["ISSUES_ELECTIONS_POLITICS"], "OUTCOME_ENGAGEMENT", "PAGE_LIKES"
        )
        self.assertIsNotNone(r)
        self.assertEqual(r["error_subcode_hint"], 2490408)
        patched = {o["patch"]["adset.optimization_goal"] for o in r["user_options"]}
        self.assertEqual(patched, {"IMPRESSIONS", "POST_ENGAGEMENT", "REACH", "THRUPLAY"})

    def test_political_awareness_reach_is_allowed(self):
        self.assertIsNone(self.mv.validate_special_ad_category_combo(
            ["ISSUES_ELECTIONS_POLITICS"], "OUTCOME_AWARENESS", "REACH"))

    def test_political_awareness_thruplay_is_allowed(self):
        self.assertIsNone(self.mv.validate_special_ad_category_combo(
            ["ISSUES_ELECTIONS_POLITICS"], "OUTCOME_AWARENESS", "THRUPLAY"))

    def test_no_special_category_is_not_restricted(self):
        self.assertIsNone(self.mv.validate_special_ad_category_combo(
            [], "OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT"))

    def test_string_category_is_accepted(self):
        # A bare string (not list) category must still be parsed and enforced —
        # proven via a combo that IS still blocked (PAGE_LIKES).
        self.assertIsNotNone(self.mv.validate_special_ad_category_combo(
            "ISSUES_ELECTIONS_POLITICS", "OUTCOME_ENGAGEMENT", "PAGE_LIKES"))

    def test_unconstrained_objective_under_category_falls_through(self):
        self.assertIsNone(self.mv.validate_special_ad_category_combo(
            ["ISSUES_ELECTIONS_POLITICS"], "OUTCOME_SALES", "OFFSITE_CONVERSIONS"))

    def test_document_for_sync_blocks_political_bad_combo(self):
        doc = {
            "campaign": {"objective": "OUTCOME_ENGAGEMENT",
                         "special_ad_categories": ["ISSUES_ELECTIONS_POLITICS"]},
            "adset": {"optimization_goal": "PAGE_LIKES"},
        }
        res = self.mv.validate_document_for_sync(
            doc, strict=False, creative_mode="EXISTING_POST", destination_type=None)
        self.assertIsNotNone(res["blocking_error"])
        self.assertEqual(res["blocking_error"]["error_subcode_hint"], 2490408)


# ====================================================================
# Phase 2: _sync_step_error surfaces user_options
# ====================================================================


class SyncStepErrorUserOptionsTests(SimpleTestCase):
    """Verify _sync_step_error extracts user_options from error dict."""

    def test_user_options_surfaced_when_in_error(self):
        from facebook_ads_mcp import production_flow
        err = {
            "message": "Bad combo",
            "user_options": [
                {"label": "Option A", "patch": {}, "explanation": "..."},
                {"label": "Option B", "patch": {}, "explanation": "..."},
            ],
        }
        result = production_flow._sync_step_error([], err, "validate_document")
        self.assertEqual(result["ok"], False)
        self.assertIn("user_options", result)
        self.assertEqual(len(result["user_options"]), 2)

    def test_no_user_options_when_error_lacks_them(self):
        from facebook_ads_mcp import production_flow
        err = {"message": "Some error"}
        result = production_flow._sync_step_error([], err, "create_campaign")
        self.assertEqual(result["ok"], False)
        self.assertNotIn("user_options", result)

    def test_user_options_preserved_in_error_dict(self):
        from facebook_ads_mcp import production_flow
        err = {
            "message": "Bad combo",
            "user_options": [{"label": "X", "patch": {}, "explanation": "y"}],
        }
        result = production_flow._sync_step_error([], err, "validate_document")
        # Also accessible nested under 'error'
        self.assertIn("user_options", result["error"])


# ====================================================================
# Phase 4: Granular (objective, goal) -> destinations matrix
# ====================================================================


class GranularDestinationMatrixTests(SimpleTestCase):
    """Verify _OBJECTIVE_GOAL_DESTINATIONS catches finer-grained issues."""

    def test_traffic_landing_page_views_messenger_blocked(self):
        """TRAFFIC + LANDING_PAGE_VIEWS only allows WEBSITE, not MESSENGER —
        even though TRAFFIC objective alone allows MESSENGER for other goals."""
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_TRAFFIC", "LANDING_PAGE_VIEWS", "MESSENGER"
        )
        self.assertIsNotNone(result)
        self.assertIn("LANDING_PAGE_VIEWS", result["message"])

    def test_traffic_landing_page_views_website_passes(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_TRAFFIC", "LANDING_PAGE_VIEWS", "WEBSITE"
        )
        self.assertIsNone(result)

    def test_engagement_event_responses_on_event_passes(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_ENGAGEMENT", "EVENT_RESPONSES", "ON_EVENT"
        )
        self.assertIsNone(result)

    def test_engagement_event_responses_messenger_blocked(self):
        """EVENT_RESPONSES requires ON_EVENT, not MESSENGER."""
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_ENGAGEMENT", "EVENT_RESPONSES", "MESSENGER"
        )
        self.assertIsNotNone(result)

    def test_leads_quality_call_phone_call_passes(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_LEADS", "QUALITY_CALL", "PHONE_CALL"
        )
        self.assertIsNone(result)

    def test_leads_quality_call_messenger_blocked(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_LEADS", "QUALITY_CALL", "MESSENGER"
        )
        self.assertIsNotNone(result)

    def test_app_promotion_app_installs_app_passes(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_APP_PROMOTION", "APP_INSTALLS", "APP"
        )
        self.assertIsNone(result)

    def test_unmapped_combo_falls_back_to_objective_only(self):
        """If (objective, goal) not in granular matrix, fall back to broader matrix."""
        from facebook_ads_mcp import meta_validation
        # SALES + APP_INSTALLS_AND_OFFSITE_CONVERSIONS not in granular matrix
        # but APP destination is valid for SALES per objective-only matrix
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_SALES", "APP_INSTALLS_AND_OFFSITE_CONVERSIONS", "APP"
        )
        self.assertIsNone(result)

    def test_undefined_passes_when_granular_set_allows_it(self):
        """UNDEFINED passes only when the granular set lists it (AWARENESS+
        THRUPLAY does); it is NOT universally accepted anymore."""
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_AWARENESS", "THRUPLAY", "UNDEFINED"
        )
        self.assertIsNone(result)

    def test_unknown_objective_passes_through(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_FUTURE", "REACH", "WEBSITE"
        )
        self.assertIsNone(result)


class ThruplayRequiredDestinationTests(SimpleTestCase):
    """ENGAGEMENT+THRUPLAY requires destination_type=ON_VIDEO. Meta rejects an
    absent/UNDEFINED destination with subcode 2490408 ("Performance goal isn't
    available") — observed live in agent runs. Local matrix must agree."""

    def test_engagement_thruplay_on_video_passes(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_ENGAGEMENT", "THRUPLAY", "ON_VIDEO"
        )
        self.assertIsNone(result)

    def test_engagement_thruplay_undefined_blocked(self):
        from facebook_ads_mcp import meta_validation
        result = meta_validation.validate_objective_goal_destination_triple(
            "OUTCOME_ENGAGEMENT", "THRUPLAY", "UNDEFINED"
        )
        self.assertIsNotNone(result)
        self.assertIn("ON_VIDEO", result["message"])

    def test_required_destination_for_thruplay_engagement(self):
        from facebook_ads_mcp import meta_validation
        self.assertEqual(
            meta_validation.required_destination_for("OUTCOME_ENGAGEMENT", "THRUPLAY"),
            "ON_VIDEO",
        )

    def test_required_destination_none_when_undefined_allowed(self):
        from facebook_ads_mcp import meta_validation
        # AWARENESS+THRUPLAY allows UNDEFINED -> nothing to force
        self.assertIsNone(
            meta_validation.required_destination_for("OUTCOME_AWARENESS", "THRUPLAY")
        )
        # multi-destination sets have no safe single choice
        self.assertIsNone(
            meta_validation.required_destination_for("OUTCOME_LEADS", "LEAD_GENERATION")
        )
        self.assertIsNone(
            meta_validation.required_destination_for("OUTCOME_SALES", "UNKNOWN_GOAL")
        )

    def _doc(self, destination_type=None):
        ads = {"name": "A", "optimization_goal": "THRUPLAY", "daily_budget": 10000}
        if destination_type is not None:
            ads["destination_type"] = destination_type
        return {
            "campaign": {"name": "C", "objective": "OUTCOME_ENGAGEMENT"},
            "adset": ads,
            "creative": {"name": "Cr", "mode": "VIDEO_AD", "page_id": "1",
                         "video_id": "999", "message": "hi"},
            "ad": {"name": "Ad"},
        }

    def test_normalizer_injects_on_video_when_absent(self):
        norm, fixes = production_flow.validate_and_normalize_document(self._doc())
        self.assertEqual(norm["adset"].get("destination_type"), "ON_VIDEO")
        self.assertTrue(any("ON_VIDEO" in f and "2490408" in f for f in fixes))

    def test_normalizer_replaces_invalid_destination_with_on_video(self):
        # The exact live failure: agent sent ON_AD, old code dropped it and
        # Meta rejected the absent destination. Now it must end as ON_VIDEO.
        norm, fixes = production_flow.validate_and_normalize_document(
            self._doc(destination_type="ON_AD"))
        self.assertEqual(norm["adset"].get("destination_type"), "ON_VIDEO")

    def test_normalizer_keeps_explicit_on_video(self):
        norm, fixes = production_flow.validate_and_normalize_document(
            self._doc(destination_type="ON_VIDEO"))
        self.assertEqual(norm["adset"].get("destination_type"), "ON_VIDEO")


# ====================================================================
# Option 2: facebook_get_combo_quickref MCP tool
# ====================================================================


class ComboQuickRefTests(SimpleTestCase):
    """Verify get_combo_quickref returns a structurally valid response."""

    def test_has_all_top_level_keys(self):
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        for key in (
            "version", "source",
            "valid_combinations", "forbidden_combinations",
            "destination_requirements", "objective_destinations",
            "always_safe_destination", "usage_hint",
            "special_ad_categories_warning",
        ):
            self.assertIn(key, ref, f"Missing key: {key}")

    def test_valid_combinations_has_all_6_objectives(self):
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        for obj in (
            "OUTCOME_AWARENESS", "OUTCOME_TRAFFIC", "OUTCOME_ENGAGEMENT",
            "OUTCOME_LEADS", "OUTCOME_SALES", "OUTCOME_APP_PROMOTION",
        ):
            self.assertIn(obj, ref["valid_combinations"])
            self.assertIsInstance(ref["valid_combinations"][obj], list)

    def test_forbidden_combinations_count_matches_constant(self):
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        self.assertEqual(
            len(ref["forbidden_combinations"]),
            len(meta_validation._FORBIDDEN_OBJECTIVE_OPTIMIZATION),
        )

    def test_forbidden_combinations_format(self):
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        for pair in ref["forbidden_combinations"]:
            self.assertIsInstance(pair, list)
            self.assertEqual(len(pair), 2)

    def test_engagement_thruplay_not_in_forbidden(self):
        """Regression: ENGAGEMENT+THRUPLAY must remain valid."""
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        for pair in ref["forbidden_combinations"]:
            self.assertNotEqual(pair, ["OUTCOME_ENGAGEMENT", "THRUPLAY"])

    def test_thruplay_in_engagement_valid_combinations(self):
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        self.assertIn("THRUPLAY", ref["valid_combinations"]["OUTCOME_ENGAGEMENT"])

    def test_destination_requirements_has_critical_goals(self):
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        for goal in ("THRUPLAY", "LEAD_GENERATION", "LANDING_PAGE_VIEWS", "QUALITY_CALL"):
            self.assertIn(goal, ref["destination_requirements"])

    def test_always_safe_destination_is_undefined(self):
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        self.assertEqual(ref["always_safe_destination"], "UNDEFINED")

    def test_response_is_json_serializable(self):
        import json
        from facebook_ads_mcp import meta_validation
        ref = meta_validation.get_combo_quickref()
        # Must not raise
        encoded = json.dumps(ref)
        # Compact target: should be < 8 KB
        self.assertLess(len(encoded), 8000)
        # Should be > 1 KB (else it's empty)
        self.assertGreater(len(encoded), 1000)

    def test_dispatch_returns_quickref(self):
        """Verify the MCP tool name routes to get_combo_quickref."""
        from facebook_ads_mcp import tool_dispatch
        result = tool_dispatch._dispatch_tool(
            "facebook_get_combo_quickref", {}, token=None,
            account_id=None, dry_run=False,
        )
        self.assertIn("valid_combinations", result)
        self.assertIn("forbidden_combinations", result)


class GetPostInsightsTests(SimpleTestCase):
    """facebook_get_post_insights: one-call per-post metrics for IG media and
    FB Page posts, with metric auto-adjust on #100 and server-side where filter."""

    @staticmethod
    def _ig_item(mid, views, reach=None, **extra):
        data = [{"name": "views", "values": [{"value": views}]}]
        if reach is not None:
            data.append({"name": "reach", "values": [{"value": reach}]})
        return {"id": mid, "permalink": f"https://ig/{mid}",
                "insights": {"data": data}, **extra}

    def test_ig_crawl_flattens_and_filters_where_lt(self):
        page = {"data": [self._ig_item("1", 100), self._ig_item("2", 9000),
                         self._ig_item("3", 4999)], "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", return_value=page) as g:
            r = services.get_post_insights(
                "tok", instagram_user_id="17841",
                where={"metric": "views", "lt": 5000}, sort="views")
        self.assertEqual([x["id"] for x in r["data"]], ["1", "3"])
        self.assertEqual(r["data"][0]["views"], 100)
        self.assertNotIn("insights", r["data"][0])
        self.assertEqual(r["summary"]["items_scanned"], 3)
        self.assertEqual(r["summary"]["matched"], 2)
        self.assertEqual(r["summary"]["where"], {"metric": "views", "lt": 5000})
        url, params = g.call_args_list[0][0]
        self.assertIn("/17841/media", url)
        self.assertIn("insights.metric(total_views,views,reach)", params["fields"])

    def test_facebook_platform_walks_published_posts_with_fb_defaults(self):
        page = {"data": [{"id": "p_1", "message": "hi",
                          "insights": {"data": [{"name": "post_video_views",
                                                 "values": [{"value": 42}]}]}}],
                "paging": {}}
        with mock.patch.object(services, "_get", return_value=page) as g:
            r = services.get_post_insights("tok", page_id="555", platform="facebook")
        url, params = g.call_args_list[0][0]
        self.assertIn("/555/published_posts", url)
        self.assertIn("insights.metric(post_video_views,post_clicks,post_reactions_like_total)",
                      params["fields"])
        self.assertEqual(r["data"][0]["post_video_views"], 42)

    def test_metric_rejection_auto_adjusts_from_server_list(self):
        err = {"error": {"code": 100, "message":
               "(#100) metric[0] must be one of the following values: views, reach, replies"}}
        ok = {"data": [self._ig_item("1", 7)], "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", side_effect=[err, ok]) as g:
            r = services.get_post_insights("tok", instagram_user_id="17841",
                                           metrics="plays,views,reach")
        self.assertEqual(r["summary"]["metrics_auto_adjusted"], ["views", "reach"])
        retry_fields = g.call_args_list[1][0][1]["fields"]
        self.assertIn("insights.metric(views,reach)", retry_fields)
        self.assertEqual(r["data"][0]["views"], 7)

    def test_no_surviving_metric_returns_valid_metrics(self):
        err = {"error": {"code": 100, "message":
               "(#100) metric[0] must be one of the following values: impressions, reach"}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", return_value=err):
            r = services.get_post_insights("tok", instagram_user_id="17841",
                                           metrics="plays")
        self.assertIn("error", r)
        self.assertEqual(r["valid_metrics"], ["impressions", "reach"])

    def test_where_skips_rows_missing_metric_and_counts_them(self):
        page = {"data": [self._ig_item("1", 10),
                         {"id": "2", "permalink": "https://ig/2"}], "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", return_value=page):
            r = services.get_post_insights("tok", instagram_user_id="17841",
                                           where={"metric": "views", "lt": 5000})
        self.assertEqual([x["id"] for x in r["data"]], ["1"])
        self.assertEqual(r["summary"]["skipped_no_metric"], 1)

    def test_sort_desc_and_limit(self):
        page = {"data": [self._ig_item("1", 10), self._ig_item("2", 300),
                         self._ig_item("3", 20)], "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", return_value=page):
            r = services.get_post_insights("tok", instagram_user_id="17841",
                                           sort="-views", limit=2)
        self.assertEqual([x["id"] for x in r["data"]], ["2", "3"])

    def test_post_ids_path_fetches_each_object(self):
        item = self._ig_item("777", 55)
        with mock.patch.object(services, "_get", return_value=item) as g:
            r = services.get_post_insights("tok", post_ids=["777"])
        url = g.call_args_list[0][0][0]
        self.assertTrue(url.endswith("/777"))
        self.assertEqual(r["data"][0]["views"], 55)
        self.assertEqual(r["summary"]["items_scanned"], 1)

    def test_where_as_json_string_is_parsed(self):
        page = {"data": [self._ig_item("1", 10), self._ig_item("2", 9999)], "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", return_value=page):
            r = services.get_post_insights("tok", instagram_user_id="17841",
                                           where='{"metric": "views", "lt": 5000}')
        self.assertEqual([x["id"] for x in r["data"]], ["1"])

    def test_fb_210_error_swaps_to_page_token(self):
        err = {"error": {"code": 210, "message":
               "(#210) A page access token is required to request this resource."}}
        ok = {"data": [{"id": "p_9", "insights": {"data": [
            {"name": "post_impressions", "values": [{"value": 5}]}]}}], "paging": {}}
        with mock.patch.object(services, "get_page_access_token",
                               return_value="PAGE_TOK") as swap, \
             mock.patch.object(services, "_get", side_effect=[err, ok]) as g:
            r = services.get_post_insights("user_tok", page_id="555",
                                           platform="facebook")
        swap.assert_called_once_with("555", "user_tok")
        self.assertEqual(g.call_args_list[1][0][1]["access_token"], "PAGE_TOK")
        self.assertEqual(r["data"][0]["post_impressions"], 5)

    def test_listless_100_probes_metrics_against_sample_post(self):
        # v25 Page-post rejection carries NO valid-values list — the tool must
        # fetch one post id and probe each metric via /{id}/insights.
        err = {"error": {"code": 100, "message":
               "(#100) The value must be a valid insights metric"}}
        sample = {"data": [{"id": "p_1"}], "paging": {}}
        probe_bad = {"error": {"code": 100, "message": "(#100) The value must be a valid insights metric"}}
        probe_ok = {"data": [{"name": "post_video_views", "values": [{"value": 3}]}]}
        ok = {"data": [{"id": "p_1", "insights": {"data": [
            {"name": "post_video_views", "values": [{"value": 3}]}]}}], "paging": {}}
        # call order: crawl(err) -> sample -> probe(post_impressions bad) ->
        # probe(post_video_views ok) -> crawl retry(ok)
        with mock.patch.object(services, "_get",
                               side_effect=[err, sample, probe_bad, probe_ok, ok]) as g:
            r = services.get_post_insights("tok", page_id="555", platform="facebook",
                                           metrics="post_impressions,post_video_views")
        self.assertEqual(r["summary"]["metrics_auto_adjusted"], ["post_video_views"])
        self.assertEqual(r["data"][0]["post_video_views"], 3)
        probe_urls = [c[0][0] for c in g.call_args_list[2:4]]
        self.assertTrue(all(u.endswith("/p_1/insights") for u in probe_urls))

    def test_crawl_dedupes_overlapping_pages_by_id(self):
        page1 = {"data": [self._ig_item("1", 10), self._ig_item("2", 20)],
                 "paging": {"next": "https://next"}}
        page2 = {"data": [self._ig_item("2", 20), self._ig_item("3", 30)],
                 "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", side_effect=[page1, page2]):
            r = services.get_post_insights("tok", instagram_user_id="17841")
        self.assertEqual([x["id"] for x in r["data"]], ["1", "2", "3"])
        self.assertEqual(r["summary"]["items_scanned"], 4)
        self.assertEqual(r["summary"]["matched"], 3)

    def test_matched_counts_filter_hits_not_limited_rows(self):
        page = {"data": [self._ig_item("1", 10), self._ig_item("2", 20),
                         self._ig_item("3", 30)], "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id",
                               return_value=("17841", None)), \
             mock.patch.object(services, "_get", return_value=page):
            r = services.get_post_insights("tok", instagram_user_id="17841",
                                           where={"metric": "views", "lt": 5000},
                                           limit=1)
        self.assertEqual(r["summary"]["matched"], 3)
        self.assertEqual(r["summary"]["returned"], 1)
        self.assertEqual(len(r["data"]), 1)


class Reel1815279MessageTests(SimpleTestCase):
    """The 1815279 (real Meta rejection) message for an existing IG reel must
    NOT tell the user cross-posting is required (Meta removed that rule
    2023-06-27) — it must name the real boost-ineligibility cause and surface
    Meta's own error_user_* reason. See _reel_boost_ineligible_message.
    """

    def test_message_does_not_claim_crosspost_required(self):
        msg = production_flow._reel_boost_ineligible_message(
            "17966", {"error_subcode": "1815279"},
            media_product_type="REELS", permalink="https://instagram.com/reel/Dbu78/",
            pages=["1119445944595873"],
        )
        # Accurate framing: crosspost NOT required, real cause named.
        self.assertIn("does NOT require cross-posting", msg)
        self.assertIn("2023-06-27", msg)
        self.assertIn("boost-INELIGIBLE", msg)
        self.assertIn("licensed", msg.lower())
        # Reel identity + permalink surfaced.
        self.assertIn("REELS 17966", msg)
        self.assertIn("https://instagram.com/reel/Dbu78/", msg)
        # Must NOT imply the OLD misleading "you must cross-post" requirement.
        self.assertNotIn("no promotable FB cross-post was found", msg)

    def test_message_surfaces_meta_error_user_reason(self):
        err = {
            "error_subcode": "1815279",
            "error_user_title": "This reel can't be boosted",
            "error_user_msg": "It contains music that isn't available for ads.",
        }
        msg = production_flow._reel_boost_ineligible_message(
            "17966", err, media_product_type="REELS",
        )
        self.assertIn('Meta says:', msg)
        self.assertIn("This reel can't be boosted", msg)
        self.assertIn("music that isn't available for ads", msg)

    def test_message_degrades_gracefully_without_user_reason(self):
        # No error_user_* → no fabricated 'Meta says' clause, but full guidance.
        msg = production_flow._reel_boost_ineligible_message(
            "17966", {"error_subcode": "1815279"}, media_type="VIDEO",
        )
        self.assertNotIn("Meta says:", msg)
        self.assertIn("VIDEO 17966", msg)
        self.assertIn("boost-INELIGIBLE", msg)

    def test_message_handles_non_dict_error(self):
        # Robust to a non-dict error payload (best-effort probe path).
        msg = production_flow._reel_boost_ineligible_message("17966", None)
        self.assertIn("IG media 17966", msg)
        self.assertNotIn("Meta says:", msg)


class ReduceDataShrinkTests(SimpleTestCase):
    """Meta code 1 "Please reduce the amount of data" on Page post queries
    (live: BOL7 Page 1876788849233669, 2026-09-08) must shrink the page size
    instead of aborting — otherwise the 1815279 cross-post rescue reports
    "FB Page posts unreachable" for a cross-post that exists.
    """

    _REDUCE = {"error": {"code": 1, "message": "Please reduce the amount of data you're asking for, then retry your request"}}

    def test_is_reduce_data_error(self):
        self.assertTrue(services._is_reduce_data_error(self._REDUCE))
        self.assertTrue(services._is_reduce_data_error({"error": {"code": 1, "message": "An unknown error occurred"}}))
        self.assertFalse(services._is_reduce_data_error({"error": {"code": 190, "message": "bad token"}}))
        self.assertFalse(services._is_reduce_data_error({"data": []}))
        self.assertFalse(services._is_reduce_data_error(None))

    def test_get_shrinking_steps_limit_down_until_success(self):
        seen = []
        def fake_get(url, params):
            seen.append(params["limit"])
            return self._REDUCE if params["limit"] > 25 else {"data": [{"id": "ok"}]}
        with mock.patch.object(services, "_get", side_effect=fake_get):
            r = services._get_shrinking("u", {"access_token": "t", "limit": 50})
        self.assertEqual(seen, [50, 25])
        self.assertEqual(r["data"][0]["id"], "ok")

    def test_get_shrinking_returns_last_error_when_never_ok(self):
        with mock.patch.object(services, "_get", return_value=self._REDUCE):
            r = services._get_shrinking("u", {"limit": 10})
        self.assertTrue(services._is_reduce_data_error(r))

    def test_get_shrinking_passes_through_other_errors(self):
        calls = []
        def fake_get(url, params):
            calls.append(1)
            return {"error": {"code": 190, "message": "expired"}}
        with mock.patch.object(services, "_get", side_effect=fake_get):
            r = services._get_shrinking("u", {"limit": 100})
        self.assertEqual(len(calls), 1)
        self.assertEqual(r["error"]["code"], 190)

    def test_resolver_finds_crosspost_after_shrink(self):
        # The exact live shape: IG reel 10:17:18Z, FB cross-post 10:15:20Z
        # (118s earlier, Business Suite dual publish); first published_posts
        # call at limit=50 gets code 1, the shrunk call succeeds.
        ig = [{
            "id": "18092067551449311",
            "caption": "Build your own AI Voice Bot in just 10 minutes!",
            "media_type": "VIDEO", "media_product_type": "REELS",
            "permalink": "https://www.instagram.com/reel/Dc-4BTwgY0Z/",
            "timestamp": "2026-09-07T10:17:18+0000",
        }]
        fb = [{
            "id": "1876788849233669_1656988173107789",
            "message": "Build your own AI Voice Bot in just 10 minutes!",
            "created_time": "2026-09-07T10:15:20+0000",
            "is_eligible_for_promotion": True,
            "attachments": {"data": [{"media_type": "video"}]},
        }]
        limits = []
        def fake_get(url, params=None):
            if "/published_posts" in url:
                limits.append(params["limit"])
                if params["limit"] >= 50:
                    return self._REDUCE
                return {"data": fb, "paging": {}}
            if "/media" in url:
                return {"data": ig, "paging": {}}
            return {"error": {"message": f"unexpected {url}"}}
        with mock.patch.object(services, "_resolve_ig_user_id", return_value=("17841405371873952", None)), \
             mock.patch.object(services, "get_page_access_token", return_value="PTOK"), \
             mock.patch.object(services, "_get", side_effect=fake_get):
            r = services.resolve_ig_permalink_to_fb_xpost(
                "TOK", "https://www.instagram.com/reel/Dc-4BTwgY0Z/", "1876788849233669",
            )
        self.assertEqual(limits, [50, 25])
        self.assertEqual(r["object_story_id"], "1876788849233669_1656988173107789")
        self.assertTrue(r["fb_post"]["is_eligible_for_promotion"])

    def test_resolver_pages_forward_until_window_exhausted(self):
        ig = [{
            "id": "1", "caption": "target caption here",
            "media_type": "VIDEO", "media_product_type": "REELS",
            "permalink": "https://www.instagram.com/reel/ABC/",
            "timestamp": "2026-09-07T10:00:00+0000",
        }]
        page1 = [{"id": "p_new", "message": "unrelated", "created_time": "2026-09-07T10:30:00+0000",
                  "attachments": {"data": [{"media_type": "video"}]}}]
        page2 = [{"id": "p_match", "message": "target caption here", "created_time": "2026-09-07T09:58:00+0000",
                  "is_eligible_for_promotion": True, "attachments": {"data": [{"media_type": "video"}]}}]
        def fake_get(url, params=None):
            if "/published_posts" in url:
                if params.get("after"):
                    return {"data": page2, "paging": {}}
                return {"data": page1, "paging": {"cursors": {"after": "CUR"}}}
            return {"data": ig, "paging": {}}
        with mock.patch.object(services, "_resolve_ig_user_id", return_value=("17841", None)), \
             mock.patch.object(services, "get_page_access_token", return_value="PTOK"), \
             mock.patch.object(services, "_get", side_effect=fake_get):
            r = services.resolve_ig_permalink_to_fb_xpost(
                "TOK", "https://www.instagram.com/reel/ABC/", "PG", posts_to_scan=50,
            )
        self.assertEqual(r["object_story_id"], "p_match")

    def test_get_page_posts_shrinks_on_reduce_data(self):
        limits = []
        def fake_get(url, params=None, timeout=None):
            limits.append((params or {}).get("limit"))
            if (params or {}).get("limit", 0) > 25:
                return self._REDUCE
            return {"data": [{"id": "x", "message": "hello world"}], "paging": {}}
        with mock.patch.object(services, "_get", side_effect=fake_get):
            r = services.get_page_posts("page1", "tok", caption_query="hello")
        self.assertEqual(limits, [100, 50, 25])
        self.assertTrue(r["search"]["matched"])


class Reel1815279EligibleMessageTests(SimpleTestCase):
    """When Meta's boost_eligibility_info says the reel IS eligible, the
    1815279 message must not blame music/stickers or tell the user to repost."""

    def test_eligible_true_does_not_blame_content(self):
        msg = production_flow._reel_boost_ineligible_message(
            "18092067551449311", {"error_subcode": "1815279"},
            media_product_type="REELS",
            permalink="https://www.instagram.com/reel/Dc-4BTwgY0Z/",
            eligible_to_boost=True,
            lookup_notes="page 1876788849233669: FB Page posts unreachable: code 1",
        )
        self.assertIn("eligible_to_boost=true", msg)
        self.assertIn("NOT a licensed-music", msg)
        self.assertIn("NOT a timing issue", msg)
        self.assertIn("Facebook copy", msg)
        self.assertIn("Do NOT pick another reel", msg)
        self.assertIn("Cross-post rescue: page 1876788849233669", msg)
        self.assertNotIn("boost-INELIGIBLE", msg)
        self.assertNotIn("retry the same brief", msg)

    def test_eligible_false_keeps_ineligible_guidance(self):
        msg = production_flow._reel_boost_ineligible_message(
            "1", {"error_subcode": "1815279"}, media_product_type="REELS", eligible_to_boost=False,
        )
        self.assertIn("eligible_to_boost=false", msg)
        self.assertIn("boost-INELIGIBLE", msg)

    def test_unknown_eligibility_unchanged(self):
        msg = production_flow._reel_boost_ineligible_message(
            "1", {"error_subcode": "1815279"}, media_product_type="REELS",
        )
        self.assertNotIn("eligible_to_boost", msg)
        self.assertIn("boost-INELIGIBLE", msg)


import json as _vjson


class ValidateCreativeSpecTests(SimpleTestCase):
    """facebook_validate_creative: validate_only probe must never omit
    execution_options (that would be a real write) and must report verdicts."""

    def test_creative_level_sends_validate_only_and_reports_rejection(self):
        seen = {}
        def fake_post(url, data):
            seen["url"] = url; seen["data"] = dict(data)
            return {"error": {"code": 100, "error_subcode": 1815279, "message": "must upload"}}
        with mock.patch.object(services, "_post", side_effect=fake_post):
            r = services.validate_creative_spec("123", "TOK", source_instagram_media_id="18015912299948778")
        self.assertTrue(seen["url"].endswith("/act_123/adcreatives"))
        self.assertEqual(_vjson.loads(seen["data"]["execution_options"]), ["validate_only"])
        self.assertEqual(seen["data"]["source_instagram_media_id"], "18015912299948778")
        self.assertEqual(r["verdict"], "REJECTED")
        self.assertEqual(r["level"], "creative")
        self.assertFalse(r["committed"])
        self.assertNotIn("access_token", r["request"])
        self.assertEqual(r["meta_response"]["error"]["error_subcode"], 1815279)

    def test_ad_level_wraps_creative_inline(self):
        seen = {}
        def fake_post(url, data):
            seen["url"] = url; seen["data"] = dict(data)
            return {"success": True}
        with mock.patch.object(services, "_post", side_effect=fake_post):
            r = services.validate_creative_spec("act_123", "TOK", source_instagram_media_id="1", adset_id="999")
        self.assertTrue(seen["url"].endswith("/act_123/ads"))
        self.assertEqual(seen["data"]["adset_id"], "999")
        self.assertEqual(_vjson.loads(seen["data"]["creative"])["source_instagram_media_id"], "1")
        self.assertEqual(_vjson.loads(seen["data"]["execution_options"]), ["validate_only"])
        self.assertEqual(r["verdict"], "ACCEPTED")
        self.assertEqual(r["level"], "ad")

    def test_requires_a_media_or_post(self):
        r = services.validate_creative_spec("act_1", "TOK")
        self.assertIn("error", r)


# ---------------------------------------------------------------------------
# Graph API v26 compliance (2026-09-16). Rules Meta enforces on EVERY version
# from 2026-10-27: Instagram Explore + Messenger Stories placements removed,
# explicit advantage_audience for HEC ad sets, Shop-ads destination opt-out.
# ---------------------------------------------------------------------------
class GraphV26PlacementRemovalTests(SimpleTestCase):
    def test_retired_placements_not_in_allowlists(self):
        self.assertNotIn("explore", production_flow.ALLOWED_IG_POSITIONS)
        self.assertNotIn("story", production_flow.ALLOWED_MESSENGER_POSITIONS)
        self.assertNotIn("explore", production_flow._IG_POSITION_ALIASES)
        # IG Stories is NOT retired — only Messenger Stories is.
        self.assertIn("story", production_flow.ALLOWED_IG_POSITIONS)

    def test_non_strict_strips_with_v26_reason(self):
        t = {
            "instagram_positions": ["stream", "explore", "reels"],
            "messenger_positions": ["messenger_home", "story"],
        }
        out, fixes = production_flow.sanitize_placements_in_targeting(t, strict=False)
        self.assertEqual(out["instagram_positions"], ["stream", "reels"])
        self.assertEqual(out["messenger_positions"], ["messenger_home"])
        joined = " ".join(fixes)
        self.assertIn("Instagram Explore feed placement was removed in Graph API v26", joined)
        self.assertIn("Messenger Stories placement was removed in Graph API v26", joined)

    def test_strict_errors_with_v26_reason(self):
        with self.assertRaises(ValueError) as ctx:
            production_flow.sanitize_placements_in_targeting(
                {"instagram_positions": ["explore"]}, strict=True
            )
        self.assertIn("Graph API v26", str(ctx.exception))

    def test_services_placement_spec_drops_messenger_story(self):
        spec = services.build_targeting_spec(
            geo_countries=["IN"],
            publisher_platforms=["messenger"],
            messenger_positions=["messenger_home", "story"],
        )
        self.assertEqual(spec.get("messenger_positions"), ["messenger_home"])


class GraphV26ErrorHintTests(SimpleTestCase):
    def test_hec_explicit_advantage_audience_hint(self):
        from facebook_ads_mcp.api_errors import explain_graph_error
        out = explain_graph_error({
            "code": 100, "error_subcode": 1870227,
            "message": "ADS_TARGETING__REQUIRE_EXPLICIT_ADVANTAGE_AUDIENCE_FLAG",
        })
        self.assertTrue(any("advantage_audience" in h for h in out["what_to_try"]))
        self.assertTrue(any("HOUSING" in c for c in out["likely_causes"]))

    def test_retired_placement_hint(self):
        from facebook_ads_mcp.api_errors import explain_graph_error
        out = explain_graph_error({
            "code": 100, "error_subcode": 1234,
            "error_user_msg": "Instagram explore placement is not available",
        })
        self.assertTrue(any("v26" in c for c in out["likely_causes"]))


class GraphV26ShopDestinationTests(SimpleTestCase):
    def test_normalize_destination_spec_shapes(self):
        n = services._normalize_destination_spec
        self.assertEqual(n(None), "")
        self.assertEqual(n(""), "")
        self.assertEqual(n("website_and_shop_opt_out"), '{"destination_type": "WEBSITE_AND_SHOP_OPT_OUT"}')
        self.assertEqual(n({"destination_type": "website"}), '{"destination_type": "WEBSITE"}')
        self.assertEqual(n('{"destination_type": "WEBSITE_AND_SHOP"}'), '{"destination_type": "WEBSITE_AND_SHOP"}')
        self.assertEqual(n("{not json"), "")

    def test_create_creative_forwards_destination_spec_on_every_shape(self):
        captured = []

        def fake_post(url, data, timeout=None):
            captured.append(dict(data))
            return {"id": "cr1"}

        with mock.patch.object(services, "_post", side_effect=fake_post):
            services.create_creative("act_1", "TOK", "n", object_story_id="1_2",
                                     destination_spec="WEBSITE_AND_SHOP_OPT_OUT")
            services.create_creative("act_1", "TOK", "n", source_instagram_media_id="99",
                                     destination_spec={"destination_type": "WEBSITE_AND_SHOP_OPT_OUT"})
            services.create_creative("act_1", "TOK", "n", page_id="p", link_url="https://x.y",
                                     message="m", image_hash="h")
        self.assertEqual(len(captured), 3)
        self.assertEqual(captured[0]["destination_spec"], '{"destination_type": "WEBSITE_AND_SHOP_OPT_OUT"}')
        self.assertEqual(captured[1]["destination_spec"], '{"destination_type": "WEBSITE_AND_SHOP_OPT_OUT"}')
        self.assertNotIn("destination_spec", captured[2])  # omitted → Meta default

    def test_fallback_wrapper_forwards_destination_spec(self):
        with mock.patch.object(services, "create_creative", return_value={"id": "x"}) as cc:
            services.create_creative_with_instagram_fallback(
                "act_1", "TOK", "n", source_instagram_media_id="9", destination_spec="WEBSITE_AND_SHOP_OPT_OUT"
            )
        self.assertEqual(cc.call_args.kwargs.get("destination_spec"), "WEBSITE_AND_SHOP_OPT_OUT")


class GraphV26VersionTests(SimpleTestCase):
    def test_default_version_is_v26(self):
        import os
        if os.environ.get("META_GRAPH_API_VERSION"):
            self.skipTest("META_GRAPH_API_VERSION overridden in env")
        self.assertEqual(services.META_GRAPH_API_VERSION, "v26.0")
        self.assertTrue(services.BASE_URL.endswith("/v26.0"))


# ---------------------------------------------------------------------------
# MCP protocol 2026-07-28 — step 2: version negotiation + server/discover
# (facebook_ads_mcp/mcp_protocol.py). Legacy no-header clients must keep working.
# ---------------------------------------------------------------------------
class McpProtocolNegotiationTests(SimpleTestCase):
    def setUp(self):
        from django.test import RequestFactory
        from facebook_ads_mcp import mcp_protocol, views
        self.rf = RequestFactory()
        self.mp = mcp_protocol
        self.views = views
        self.META_KEY = mcp_protocol.META_PROTOCOL_VERSION_KEY

    def _post(self, body, **headers):
        import json as _json
        return self.rf.post("/api/mcp/facebook-ads/", data=_json.dumps(body),
                            content_type="application/json", **headers)

    def _call(self, body, **headers):
        import json as _json
        resp = self.views.mcp_endpoint(self._post(body, **headers))
        return resp, _json.loads(resp.content)

    # -- legacy path -------------------------------------------------------
    def test_no_version_anywhere_is_served_as_legacy(self):
        resp, out = self._call({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("tools", out["result"])
        self.assertNotIn("_meta", out["result"])  # legacy response byte-compatible
        self.assertEqual(resp["MCP-Protocol-Version"], self.mp.LEGACY_DEFAULT_VERSION)

    def test_initialize_echoes_supported_requested_version(self):
        _, out = self._call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(out["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(out["result"]["serverInfo"]["name"], self.mp.SERVER_NAME)
        self.assertEqual(out["result"]["serverInfo"]["version"], self.mp.SERVER_VERSION)

    def test_initialize_unknown_version_falls_back(self):
        _, out = self._call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "1999-01-01"}})
        self.assertEqual(out["result"]["protocolVersion"], self.mp.INITIALIZE_FALLBACK_VERSION)

    # -- modern path -------------------------------------------------------
    def test_modern_request_stamps_meta_and_header(self):
        body = {"jsonrpc": "2.0", "id": 7, "method": "tools/list",
                "params": {"_meta": {self.META_KEY: "2026-07-28"}}}
        resp, out = self._call(body, HTTP_MCP_PROTOCOL_VERSION="2026-07-28", HTTP_MCP_METHOD="tools/list")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(out["result"]["_meta"][self.META_KEY], "2026-07-28")
        self.assertEqual(resp["MCP-Protocol-Version"], "2026-07-28")

    def test_server_discover(self):
        resp, out = self._call({"jsonrpc": "2.0", "id": 2, "method": "server/discover"})
        r = out["result"]
        self.assertEqual(r["preferredProtocolVersion"], "2026-07-28")
        self.assertIn("2024-11-05", r["protocolVersions"])
        self.assertEqual(r["protocolVersions"][0], "2026-07-28")
        self.assertIn("tools", r["capabilities"])
        self.assertEqual(r["serverInfo"]["name"], self.mp.SERVER_NAME)

    def test_header_body_mismatch_is_32020_http_400(self):
        body = {"jsonrpc": "2.0", "id": 3, "method": "tools/list",
                "params": {"_meta": {self.META_KEY: "2025-11-25"}}}
        resp, out = self._call(body, HTTP_MCP_PROTOCOL_VERSION="2026-07-28")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(out["error"]["code"], -32020)

    def test_unsupported_version_is_32022_with_supported_list(self):
        resp, out = self._call({"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                               HTTP_MCP_PROTOCOL_VERSION="2030-01-01")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(out["error"]["code"], -32022)
        self.assertEqual(out["error"]["data"]["supportedVersions"], list(self.mp.SUPPORTED_PROTOCOL_VERSIONS))
        self.assertEqual(out["error"]["data"]["preferredVersion"], "2026-07-28")

    def test_mcp_method_header_mismatch(self):
        resp, out = self._call({"jsonrpc": "2.0", "id": 5, "method": "tools/list"},
                               HTTP_MCP_PROTOCOL_VERSION="2026-07-28", HTTP_MCP_METHOD="tools/call")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(out["error"]["code"], -32020)

    def test_mcp_name_header_mismatch_on_tools_call(self):
        body = {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                "params": {"name": "facebook_get_combo_quickref", "arguments": {}}}
        resp, out = self._call(body, HTTP_MCP_PROTOCOL_VERSION="2026-07-28",
                               HTTP_MCP_METHOD="tools/call", HTTP_MCP_NAME="facebook_get_pages")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(out["error"]["code"], -32020)

    def test_matching_routing_headers_pass(self):
        body = {"jsonrpc": "2.0", "id": 8, "method": "tools/list"}
        resp, out = self._call(body, HTTP_MCP_PROTOCOL_VERSION="2026-07-28", HTTP_MCP_METHOD="tools/list")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("tools", out["result"])

    def test_negotiate_without_request_uses_body_only(self):
        v, err = self.mp.negotiate(None, {"method": "tools/list",
                                          "params": {"_meta": {self.META_KEY: "2026-07-28"}}})
        self.assertIsNone(err)
        self.assertEqual(v, "2026-07-28")
        v2, err2 = self.mp.negotiate(None, {"method": "tools/list"})
        self.assertIsNone(err2)
        self.assertEqual(v2, self.mp.LEGACY_DEFAULT_VERSION)

    def test_get_on_mcp_url_is_405_with_info_pointer(self):
        import json as _json
        resp = self.views.mcp_endpoint(self.rf.get("/api/mcp/facebook-ads/"))
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(resp["Allow"], "POST")
        out = _json.loads(resp.content)
        self.assertEqual(out["preferredProtocolVersion"], "2026-07-28")
        self.assertTrue(out["info"].endswith("/api/mcp/facebook-ads/info/"))
        resp = self.views.mcp_info_endpoint(self.rf.get("/api/mcp/facebook-ads/info/"))
        out = _json.loads(resp.content)
        self.assertEqual(out["version"], self.mp.SERVER_VERSION)
        self.assertNotIn("sse", out["transports"])
        self.assertIn(self.mp.TASKS_EXTENSION, out["extensions"])


# ---------------------------------------------------------------------------
# MCP protocol 2026-07-28 — step 3: tool definitions + result semantics.
# ---------------------------------------------------------------------------
class McpToolDefinitionTests(SimpleTestCase):
    def setUp(self):
        from facebook_ads_mcp import mcp_protocol, views
        self.mp = mcp_protocol
        self.views = views

    def test_title_and_annotations_derived_from_name(self):
        self.assertEqual(self.mp.tool_title("facebook_get_ad_sets"), "Get Ad Sets")
        self.assertEqual(self.mp.tool_title("facebook_roas_analysis"), "ROAS Analysis")
        a = self.mp.tool_annotations("facebook_get_campaigns")
        self.assertEqual((a["readOnlyHint"], a["destructiveHint"], a["idempotentHint"]), (True, False, True))
        a = self.mp.tool_annotations("facebook_delete_campaign")
        self.assertEqual((a["readOnlyHint"], a["destructiveHint"]), (False, True))
        a = self.mp.tool_annotations("facebook_create_campaign", idempotent_tools={"facebook_create_campaign"})
        self.assertEqual((a["readOnlyHint"], a["idempotentHint"]), (False, True))
        a = self.mp.tool_annotations("facebook_sync_document_campaigns_batch")
        self.assertFalse(a["readOnlyHint"])
        a = self.mp.tool_annotations("facebook_batch_interest_search")
        self.assertTrue(a["readOnlyHint"])
        self.assertTrue(a["openWorldHint"])
        self.assertEqual(self.mp.tool_annotations("facebook_x", explicit={"readOnlyHint": True}), {"readOnlyHint": True})

    def test_legacy_list_keeps_hints_top_level(self):
        tools = self.views.tools_list_dicts_for_mcp("")
        self.assertTrue(all("title" in t and "annotations" in t for t in tools))
        hinted = [t for t in tools if t.get("_discovery") or t.get("_remember")]
        self.assertTrue(hinted, "expected at least one tool with _discovery/_remember")
        self.assertTrue(all("_meta" not in t for t in tools))

    def test_modern_list_moves_hints_under_meta(self):
        tools = self.views.tools_list_dicts_for_mcp("2026-07-28")
        self.assertTrue(all("_discovery" not in t and "_remember" not in t for t in tools))
        hinted = [t for t in tools if isinstance(t.get("_meta"), dict) and t["_meta"]]
        self.assertTrue(hinted)
        legacy_names = [t["name"] for t in self.views.tools_list_dicts_for_mcp("")]
        self.assertEqual([t["name"] for t in tools], legacy_names)  # deterministic order

    def test_tools_list_result_cache_hints_only_modern(self):
        self.assertNotIn("ttlMs", self.mp.tools_list_result([], "2025-03-26"))
        r = self.mp.tools_list_result([], "2026-07-28")
        self.assertEqual((r["ttlMs"], r["cacheScope"]), (300_000, "public"))


class McpToolResultSemanticsTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("facebook_ads_mcp.auth_tokens.require_facebook_identity", return_value="protocol-user")
        patcher.start()
        self.addCleanup(patcher.stop)
        import json as _json
        from django.test import RequestFactory
        from facebook_ads_mcp import mcp_protocol, views
        self.mp, self.views, self.rf, self.json = mcp_protocol, views, RequestFactory(), _json

    def _call(self, body, **headers):
        req = self.rf.post("/api/mcp/facebook-ads/", data=self.json.dumps(body),
                           content_type="application/json", **headers)
        resp = self.views.mcp_endpoint(req)
        return resp, self.json.loads(resp.content)

    def test_legacy_result_shape_unchanged(self):
        r = self.mp.tool_call_result({"error": {"message": "x"}}, "2025-03-26", legacy_text="LEGACY")
        self.assertEqual(r, {"content": [{"type": "text", "text": "LEGACY"}]})
        self.assertIsNone(self.mp.tool_input_error_result("bad", "2025-03-26"))

    def test_modern_success_has_structured_content_no_iserror(self):
        r = self.mp.tool_call_result({"data": [1]}, "2026-07-28", legacy_text="ignored")
        self.assertEqual(r["structuredContent"], {"data": [1]})
        self.assertNotIn("isError", r)
        self.assertEqual(self.json.loads(r["content"][0]["text"]), {"data": [1]})

    def test_modern_meta_error_is_iserror(self):
        r = self.mp.tool_call_result({"error": {"code": 100, "message": "Invalid parameter"}}, "2026-07-28", legacy_text="")
        self.assertTrue(r["isError"])
        self.assertEqual(r["structuredContent"]["error"]["code"], 100)

    def test_modern_input_error_result(self):
        r = self.mp.tool_input_error_result("missing account_id", "2026-07-28", data={"hint": 1})
        self.assertTrue(r["isError"])
        self.assertEqual(r["structuredContent"]["error"]["code"], -32602)
        self.assertEqual(r["structuredContent"]["error"]["data"], {"hint": 1})

    @mock.patch("facebook_ads_mcp.auth_tokens.get_valid_token", return_value=None)
    def test_end_to_end_validation_error_legacy_vs_modern(self, _get):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "facebook_get_campaigns", "arguments": {}}}
        # legacy: JSON-RPC error (token missing → ValueError in auth resolve)
        resp, out = self._call(body)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error", out)
        self.assertEqual(out["error"]["code"], -32602)
        # modern: normal result with isError so the model can self-correct
        resp, out = self._call(body, HTTP_MCP_PROTOCOL_VERSION="2026-07-28",
                               HTTP_MCP_METHOD="tools/call", HTTP_MCP_NAME="facebook_get_campaigns")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("error", out)
        self.assertTrue(out["result"]["isError"])
        self.assertEqual(out["result"]["resultType"], "complete")
        self.assertEqual(out["result"]["_meta"][self.mp.META_PROTOCOL_VERSION_KEY], "2026-07-28")

    def test_modern_tools_list_end_to_end(self):
        resp, out = self._call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                               HTTP_MCP_PROTOCOL_VERSION="2026-07-28", HTTP_MCP_METHOD="tools/list")
        r = out["result"]
        self.assertEqual(r["ttlMs"], 300_000)
        self.assertEqual(r["resultType"], "complete")
        self.assertTrue(all("annotations" in t for t in r["tools"]))


# ---------------------------------------------------------------------------
# MCP protocol 2026-07-28 — step 4: transport hygiene + tasks extension.
# ---------------------------------------------------------------------------
class McpTransportHygieneTests(SimpleTestCase):
    def setUp(self):
        import json as _json
        from django.test import RequestFactory
        from facebook_ads_mcp import mcp_protocol, views
        self.mp, self.views, self.rf, self.json = mcp_protocol, views, RequestFactory(), _json

    def test_delete_is_405(self):
        resp = self.views.mcp_endpoint(self.rf.delete("/api/mcp/facebook-ads/"))
        self.assertEqual(resp.status_code, 405)

    def test_origin_rules(self):
        body = self.json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        # no Origin → allowed
        r = self.views.mcp_endpoint(self.rf.post("/api/mcp/facebook-ads/", body, content_type="application/json"))
        self.assertEqual(r.status_code, 200)
        # same host → allowed
        r = self.views.mcp_endpoint(self.rf.post("/api/mcp/facebook-ads/", body, content_type="application/json",
                                                 HTTP_HOST="mcp.local:8000", HTTP_ORIGIN="http://mcp.local:8000"))
        self.assertEqual(r.status_code, 200)
        # foreign origin → 403
        r = self.views.mcp_endpoint(self.rf.post("/api/mcp/facebook-ads/", body, content_type="application/json",
                                                 HTTP_HOST="mcp.local:8000", HTTP_ORIGIN="https://evil.example"))
        self.assertEqual(r.status_code, 403)
        # allow-listed foreign origin → allowed
        with mock.patch.dict("os.environ", {"MCP_ALLOWED_ORIGINS": "https://app.bol7.com, https://evil.example/"}):
            r = self.views.mcp_endpoint(self.rf.post("/api/mcp/facebook-ads/", body, content_type="application/json",
                                                     HTTP_HOST="mcp.local:8000", HTTP_ORIGIN="https://evil.example"))
        self.assertEqual(r.status_code, 200)
        # "null" origin → refused
        r = self.views.mcp_endpoint(self.rf.post("/api/mcp/facebook-ads/", body, content_type="application/json",
                                                 HTTP_HOST="mcp.local:8000", HTTP_ORIGIN="null"))
        self.assertEqual(r.status_code, 403)

    def test_sse_views_removed(self):
        self.assertFalse(hasattr(self.views, "mcp_sse_endpoint"))
        self.assertFalse(hasattr(self.views, "mcp_messages_endpoint"))
        from facebook_ads_mcp import urls as fb_urls
        names = [getattr(p, "name", "") for p in fb_urls.urlpatterns]
        self.assertFalse(any("sse" in n or "messages" in n for n in names))


class McpTasksExtensionTests(SimpleTestCase):
    def setUp(self):
        patcher = mock.patch("facebook_ads_mcp.auth_tokens.require_facebook_identity", return_value="protocol-user")
        patcher.start()
        self.addCleanup(patcher.stop)
        import json as _json
        from django.test import RequestFactory
        from facebook_ads_mcp import mcp_protocol, views
        self.mp, self.views, self.rf, self.json = mcp_protocol, views, RequestFactory(), _json

    def _row(self, status, result_json="", error=""):
        r = mock.Mock()
        r.status, r.result_json, r.error_message, r.created_at = status, result_json, error, None
        return r

    def _call(self, body, modern=True):
        hdrs = {"HTTP_MCP_PROTOCOL_VERSION": "2026-07-28", "HTTP_MCP_METHOD": body["method"]} if modern else {}
        req = self.rf.post("/api/mcp/facebook-ads/", self.json.dumps(body), content_type="application/json", **hdrs)
        resp = self.views.mcp_endpoint(req)
        return resp, self.json.loads(resp.content)

    def test_accepted_data_becomes_task_descriptor_on_modern_only(self):
        data = {"accepted": True, "task_id": "abc", "poll_after_ms": 1500, "status": "pending"}
        r = self.mp.tool_call_result(data, "2026-07-28", legacy_text="")
        self.assertEqual(r["task"], {"taskId": "abc", "status": "working", "pollInterval": 1500})
        self.assertNotIn("isError", r)
        self.assertNotIn("task", self.mp.tool_call_result(data, "2025-03-26", legacy_text="x"))
        self.assertIsNone(self.mp.accepted_to_task({"ok": True}))

    def test_tasks_get_states(self):
        from facebook_ads_mcp import models as m
        cases = {"pending": "working", "running": "working", "success": "completed", "failed": "failed"}
        for row_status, expect in cases.items():
            with mock.patch.object(m.FacebookAdsMcpTaskResult.objects, "get", return_value=self._row(row_status, error="boom")):
                _, out = self._call({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"taskId": "t1"}})
            self.assertEqual(out["result"]["status"], expect, row_status)
            self.assertEqual(out["result"]["taskId"], "t1")
            if expect == "working":
                self.assertEqual(out["result"]["pollInterval"], 2000)
            if expect == "failed":
                self.assertEqual(out["result"]["statusMessage"], "boom")

    def test_tasks_get_unknown_task(self):
        from facebook_ads_mcp import models as m
        with mock.patch.object(m.FacebookAdsMcpTaskResult.objects, "get", side_effect=m.FacebookAdsMcpTaskResult.DoesNotExist):
            resp, out = self._call({"jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"taskId": "nope"}})
        self.assertEqual(out["error"]["code"], -32602)

    def test_tasks_result_completed_and_failed_and_working(self):
        from facebook_ads_mcp import models as m
        with mock.patch.object(m.FacebookAdsMcpTaskResult.objects, "get", return_value=self._row("success", result_json='{"campaign_id": "c1"}')):
            _, out = self._call({"jsonrpc": "2.0", "id": 1, "method": "tasks/result", "params": {"taskId": "t1"}})
        self.assertEqual(out["result"]["structuredContent"], {"campaign_id": "c1"})
        self.assertNotIn("isError", out["result"])
        with mock.patch.object(m.FacebookAdsMcpTaskResult.objects, "get", return_value=self._row("failed", error="meta said no")):
            _, out = self._call({"jsonrpc": "2.0", "id": 2, "method": "tasks/result", "params": {"taskId": "t1"}})
        self.assertTrue(out["result"]["isError"])
        self.assertEqual(out["result"]["structuredContent"]["error"]["message"], "meta said no")
        with mock.patch.object(m.FacebookAdsMcpTaskResult.objects, "get", return_value=self._row("running")):
            _, out = self._call({"jsonrpc": "2.0", "id": 3, "method": "tasks/result", "params": {"taskId": "t1"}})
        self.assertEqual(out["error"]["code"], -32602)
        self.assertEqual(out["error"]["data"]["status"], "working")

    def test_legacy_task_status_unchanged(self):
        from facebook_ads_mcp import models as m
        with mock.patch.object(m.FacebookAdsMcpTaskResult.objects, "get", return_value=self._row("running")):
            _, out = self._call({"jsonrpc": "2.0", "id": 1, "method": "task/status", "params": {"task_id": "t1"}}, modern=False)
        self.assertEqual(out["result"], {"done": False, "poll_after_ms": 2000})


class AuditFollowupTests(SimpleTestCase):
    """2026-09-17 audit: consistency fixes."""

    def setUp(self):
        import json as _json
        from django.test import RequestFactory
        from facebook_ads_mcp import mcp_protocol, views
        self.mp, self.views, self.rf, self.json = mcp_protocol, views, RequestFactory(), _json

    def _call(self, body, **h):
        req = self.rf.post("/api/mcp/facebook-ads/", self.json.dumps(body), content_type="application/json", **h)
        resp = self.views.mcp_endpoint(req)
        return resp, self.json.loads(resp.content)

    @mock.patch("facebook_ads_mcp.auth_tokens.require_facebook_identity", return_value="protocol-user")
    def test_foreign_tool_name_is_iserror_on_modern_and_32602_on_legacy(self, _identity):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_message", "arguments": {}}}
        _, legacy = self._call(body)
        self.assertEqual(legacy["error"]["code"], -32602)
        _, modern = self._call(body, HTTP_MCP_PROTOCOL_VERSION="2026-07-28", HTTP_MCP_METHOD="tools/call", HTTP_MCP_NAME="send_message")
        self.assertTrue(modern["result"]["isError"])
        self.assertIn("Facebook Ads", modern["result"]["structuredContent"]["error"]["message"])

    def test_non_object_body_is_32600_not_500(self):
        req = self.rf.post("/api/mcp/facebook-ads/", "[1,2]", content_type="application/json")
        resp = self.views.mcp_endpoint(req)
        self.assertEqual(self.json.loads(resp.content)["error"]["code"], -32600)

    def test_origin_trusted_by_cors_allowlist_passes(self):
        body = self.json.dumps({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        with self.settings(CORS_ALLOWED_ORIGINS=["https://app.bol7.com"]):
            r = self.views.mcp_endpoint(self.rf.post("/api/mcp/facebook-ads/", body, content_type="application/json",
                                                     HTTP_HOST="mcp.local:8000", HTTP_ORIGIN="https://app.bol7.com"))
        self.assertEqual(r.status_code, 200)


class FacebookLoginIdentityTests(SimpleTestCase):
    """Exercise actual encrypted, signed JWTs without provider or auth-service calls."""

    def setUp(self):
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from rest_framework_simplejwt.backends import TokenBackend
        from django.test import RequestFactory
        self.rf = RequestFactory()
        self.fernet = Fernet(Fernet.generate_key())
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.key = private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        public = private.public_key().public_bytes(serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        for patcher in (
            mock.patch("app.authentication.fernet", self.fernet),
            mock.patch("app.authentication.check_token_revoked"),
            mock.patch("rest_framework_simplejwt.state.token_backend",
                       TokenBackend("RS256", verifying_key=public)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def bearer(self, **overrides):
        import jwt
        import time
        payload = {"user_id": "alice", "exp": int(time.time()) + 300,
                   "iat": int(time.time()), "jti": "test-login", "token_type": "access"}
        payload.update(overrides)
        raw = jwt.encode(payload, self.key, algorithm="RS256").encode()
        return "Bearer " + self.fernet.encrypt(raw).decode()

    def request(self, body=None, bearer=None, **headers):
        import json
        return self.rf.post("/api/mcp/facebook-ads/", json.dumps(body or {}),
            content_type="application/json",
            HTTP_AUTHORIZATION=bearer if bearer is not None else self.bearer(), **headers)

    def test_valid_jwt_resolves_only_own_provider_token(self):
        from .auth_tokens import resolve_access_token
        with mock.patch("facebook_ads_mcp.auth_tokens.get_valid_token", return_value="meta-token") as get:
            token, meta = resolve_access_token({}, self.request())
        get.assert_called_once_with("alice")
        self.assertEqual(token, "meta-token")
        self.assertEqual(meta["user_id"], "alice")

    def test_invalid_login_never_reads_credentials(self):
        from .auth_tokens import resolve_access_token
        for bearer in ("", "Bearer broken", self.bearer(exp=1),
                       self.bearer(token_type="refresh"),
                       self.bearer(user_id="", sub="")):
            with self.subTest(bearer_kind=bearer[:6]):
                with mock.patch("facebook_ads_mcp.auth_tokens.get_valid_token") as get:
                    with self.assertRaises(ValueError):
                        resolve_access_token({"user_id": "alice"}, self.request(bearer=bearer))
                    get.assert_not_called()

    def test_forged_signature_rejected(self):
        from .auth_tokens import require_facebook_identity
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        raw = jwt.encode({"user_id": "alice", "exp": 9999999999, "jti": "x", "token_type": "access"},
                         rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256")
        bearer = "Bearer " + self.fernet.encrypt(raw.encode()).decode()
        with self.assertRaises(ValueError):
            require_facebook_identity(self.request(bearer=bearer))

    def test_conflicting_selectors_rejected_in_all_locations(self):
        from .auth_tokens import require_facebook_identity
        for selector in ("user_id", "userId", "tenant_id", "credential_user_id"):
            for body in ({selector: "bob"}, {"params": {selector: "bob"}},
                         {"params": {"arguments": {selector: "bob"}}},
                         {"params": {"_meta": {selector: "bob"}}}):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    require_facebook_identity(self.request(body), body)
        for header in ("HTTP_X_USER_ID", "HTTP_X_TENANT_ID"):
            with self.assertRaises(ValueError):
                require_facebook_identity(self.request(**{header: "bob"}))
        req = self.request()
        from django.http import QueryDict
        req.GET = QueryDict("user_id=alice&user_id=bob")
        with self.assertRaises(ValueError):
            require_facebook_identity(req)

    def test_unsigned_and_stdio_tool_calls_fail_before_dispatch(self):
        from .views import handle_mcp_request_from_body
        body = {"id": 1, "method": "tools/call",
                "params": {"name": "facebook_get_campaigns", "arguments": {"token": "meta-token"}}}
        for req in (None, self.request(body, bearer="")):
            with mock.patch("facebook_ads_mcp.tool_dispatch.run_tool") as run:
                out = handle_mcp_request_from_body(body, req)
                self.assertEqual(out["error"]["code"], -32001)
                run.assert_not_called()

    def test_authenticated_dispatch_injects_owner_and_never_login_token(self):
        from .views import handle_mcp_request_from_body
        body = {"id": 1, "method": "tools/call",
                "params": {"name": "facebook_get_campaigns", "arguments": {"token": "meta-token", "skip_token_refresh": True}}}
        with mock.patch("facebook_ads_mcp.tool_dispatch.run_tool", return_value={"ok": True}) as run, \
             mock.patch("facebook_ads_mcp.views.save_audit_to_db"):
            out = handle_mcp_request_from_body(body, self.request(body))
        self.assertIn("result", out)
        self.assertEqual(run.call_args.args[1]["user_id"], "alice")
        self.assertEqual(run.call_args.args[2], "meta-token")

    def test_task_polling_scoped_to_verified_owner(self):
        from .views import handle_mcp_request_from_body
        from .models import FacebookAdsMcpTaskResult
        for method in ("tasks/get", "tasks/result", "task/status"):
            body = {"id": 1, "method": method, "params": {"task_id": "foreign", "taskId": "foreign"}}
            with mock.patch.object(FacebookAdsMcpTaskResult.objects, "get",
                                   side_effect=FacebookAdsMcpTaskResult.DoesNotExist) as get:
                out = handle_mcp_request_from_body(body, self.request(body))
                get.assert_called_once_with(task_id="foreign", user_id="alice")
                self.assertIn("error", out.get("result", out))
        from .tool_dispatch import run_tool
        with mock.patch.object(FacebookAdsMcpTaskResult.objects, "get",
                               side_effect=FacebookAdsMcpTaskResult.DoesNotExist) as get:
            out = run_tool("facebook_get_task_status", {"task_id": "foreign", "user_id": "alice"}, "meta-token")
            get.assert_called_once_with(task_id="foreign", user_id="alice")
            self.assertIn("error", out)

    def test_connection_endpoints_reject_unsigned_before_side_effects(self):
        from . import connect_views
        for name in ("connect_view", "status_view", "test_view", "oauth_start", "adaccounts_view"):
            view = getattr(connect_views, name)
            req = self.rf.post("/", "{}", content_type="application/json") if name in ("connect_view", "oauth_start") else self.rf.get("/")
            self.assertEqual(view(req).status_code, 401, name)


    def test_revoked_login_is_rejected(self):
        from rest_framework.exceptions import AuthenticationFailed
        from .auth_tokens import require_facebook_identity
        with mock.patch("app.authentication.check_token_revoked", side_effect=AuthenticationFailed("Revoked")):
            with self.assertRaises(ValueError):
                require_facebook_identity(self.request())

    def test_missing_login_is_http_401(self):
        from .views import mcp_endpoint
        body = {"id": 1, "method": "tools/call", "params": {"name": "facebook_get_accounts"}}
        self.assertEqual(mcp_endpoint(self.request(body, bearer="")).status_code, 401)

    def test_refresh_cannot_persist_under_another_user(self):
        from .auth_tokens import resolve_mcp_access_token
        with mock.patch("facebook_ads_mcp.auth_tokens.ensure_refreshed_user_token",
                        return_value=("meta-token", {})) as refresh:
            with self.assertRaises(ValueError):
                resolve_mcp_access_token({"token": "meta-token", "credential_user_id": "bob"}, self.request())
            refresh.assert_not_called()
            resolve_mcp_access_token({"token": "meta-token", "credential_user_id": "alice"}, self.request())
            self.assertEqual(refresh.call_args.kwargs["persist_user_id"], "alice")

    def test_shared_tools_status_requires_login(self):
        from app.mcp_tools_views import mcp_tools
        from .models import FacebookAdsCredential
        with mock.patch.object(FacebookAdsCredential.objects, "filter") as rows:
            req = self.rf.get("/?provider=facebook_ads&user_id=bob")
            self.assertEqual(mcp_tools(req).status_code, 401)
            rows.assert_not_called()
            req = self.rf.get("/?provider=facebook_ads&user_id=alice", HTTP_AUTHORIZATION=self.bearer())
            rows.return_value.exists.return_value = True
            self.assertEqual(mcp_tools(req).status_code, 200)
            rows.assert_called_once_with(user_id="alice")

    def test_registry_status_ignores_email_identity(self):
        from app.mcp_app_registry import check_app_connected
        from .models import FacebookAdsCredential
        with mock.patch.object(FacebookAdsCredential.objects, "filter") as rows:
            self.assertEqual(check_app_connected("facebook_ads", "victim@example.test"), (False, "", ""))
            rows.assert_not_called()
            rows.return_value.first.return_value = mock.Mock(user_id="alice")
            connected, _, uid = check_app_connected("facebook_ads", "victim@example.test", request=self.request())
            self.assertTrue(connected)
            self.assertEqual(uid, "alice")
            rows.assert_called_once_with(user_id="alice")

    def test_aggregate_status_never_reads_facebook_for_unsigned_caller(self):
        from app.integration_status_views import _integrations_status_response
        from .models import FacebookAdsCredential
        req = self.rf.get("/?user_id=bob")
        with mock.patch.object(FacebookAdsCredential.objects, "filter") as rows:
            out = _integrations_status_response(req)
            rows.assert_not_called()
            self.assertFalse(out.data["facebook_ads"]["connected"])
            self.assertEqual(out.data["facebook_ads"]["email"], "")

    def test_bot_flow_connect_rejects_foreign_user_before_lookup(self):
        from app.bot_flow_mcp_views import bot_flow_mcp_connect_app
        from .models import FacebookAdsCredential
        body = {"app_slug": "facebook_ads", "email": "other@example.test", "user_id": "bob"}
        with mock.patch.object(FacebookAdsCredential.objects, "filter") as rows:
            self.assertEqual(bot_flow_mcp_connect_app(self.request(body)).status_code, 401)
            rows.assert_not_called()

    def test_connect_autowire_cannot_update_another_users_email_match(self):
        from app import bot_flow_autowire as wiring
        row = mock.Mock(id=1, pk=1)
        with mock.patch.object(wiring, "fetch_tools_from_mcp_url", return_value=([], "integration")), \
             mock.patch.object(wiring.transaction, "atomic"), \
             mock.patch.object(wiring.BotFlowMCPConfig.objects, "update_or_create", return_value=(row, True)) as update, \
             mock.patch.object(wiring.BotFlowMCPConfig.objects, "filter") as rows:
            result = wiring.autowire_bot_flow_from_connection(app_slug="facebook_ads",
                email="other@example.test", internal_user_id="alice", request=self.request())
            self.assertTrue(result["bot_flow_wired"])
            self.assertEqual(update.call_args.kwargs["user_id"], "alice")
            self.assertNotIn("email__iexact", update.call_args.kwargs)
            rows.assert_called_once_with(email__iexact="other@example.test", app_slug="facebook_ads", user_id="alice")


# ---------------------------------------------------------------------------
# Audit regressions 2026-10-22: MCP-19 / MCP-31 / MCP-32 / MCP-35
# ---------------------------------------------------------------------------

class AuditServicesRegressionTests(SimpleTestCase):
    def setUp(self):
        self._snap = dict(services._PAGE_TOKEN_CACHE)
        services._PAGE_TOKEN_CACHE.clear()

    def tearDown(self):
        services._PAGE_TOKEN_CACHE.clear()
        services._PAGE_TOKEN_CACHE.update(self._snap)

    # MCP-19 -----------------------------------------------------------
    def test_page_token_primed_by_a_is_invisible_to_b(self):
        """Tenant A harvests a page token; tenant B selecting the same page id
        must not get A's token from the cache and must do its own lookup."""
        services._page_cache_set("tok_A", "page_1", "A_page_tok")
        self.assertEqual(services._page_cache_get("tok_A", "page_1"), "A_page_tok")
        self.assertIsNone(services._page_cache_get("tok_B", "page_1"))
        with mock.patch.object(services, "_get", return_value={"error": {"message": "(#200) no"}}) as m_get, \
             mock.patch.object(services, "get_pages", return_value={"data": []}):
            self.assertIsNone(services.get_page_access_token("page_1", "tok_B"))
        self.assertTrue(m_get.called)

    def test_page_token_cache_expires_and_invalidates(self):
        services._page_cache_set("tok_A", "page_1", "A_page_tok")
        with mock.patch.object(services, "_page_cache_ttl", return_value=-1):
            services._page_cache_set("tok_A", "page_2", "stale")
        self.assertIsNone(services._page_cache_get("tok_A", "page_2"))
        self.assertEqual(services.invalidate_page_token_cache("tok_A"), 1)
        self.assertIsNone(services._page_cache_get("tok_A", "page_1"))

    def test_get_page_access_token_without_user_token_never_uses_cache(self):
        services._page_cache_set("tok_A", "page_1", "A_page_tok")
        self.assertIsNone(services.get_page_access_token("page_1", ""))

    # MCP-31 -----------------------------------------------------------
    def test_idempotency_raw_bound_to_principal_and_grant(self):
        from facebook_ads_mcp import tool_dispatch as td
        args = {"account_id": "act_1", "name": "camp", "objective": "OUTCOME_TRAFFIC"}
        a = td._build_idempotency_raw("facebook_create_campaign", dict(args, user_id="u1"), "tokA")
        b = td._build_idempotency_raw("facebook_create_campaign", dict(args, user_id="u2"), "tokA")
        c = td._build_idempotency_raw("facebook_create_campaign", dict(args, user_id="u1"), "tokB")
        d = td._build_idempotency_raw("facebook_create_campaign", dict(args, user_id="u1", _run_id="x"), "tokA")
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(a, d)
        self.assertNotIn("tokA", a)

    def test_run_tool_passes_grant_to_idempotency(self):
        from facebook_ads_mcp import tool_dispatch as td
        name = next(iter(td._IDEMPOTENT_TOOLS))
        seen = {}
        def fake_get_cached(tool, raw):
            seen["raw"] = raw
            return {"cached": True}
        with mock.patch("facebook_ads_mcp.idempotency.get_cached_result", side_effect=fake_get_cached):
            out = td.run_tool(name, {"user_id": "u1", "account_id": "act_1", "name": "n"}, "tokA")
        self.assertTrue(out.get("_cached"))
        import hashlib, json
        self.assertEqual(json.loads(seen["raw"])["grant"], hashlib.sha256(b"tokA").hexdigest())
        self.assertEqual(json.loads(seen["raw"])["principal"], "u1")

    # MCP-32 -----------------------------------------------------------
    def test_fetch_public_bytes_rejects_private_and_redirects(self):
        from app import tenant_http as th
        for bad in ("http://127.0.0.1/x.png", "http://169.254.169.254/latest", "http://[::1]/x",
                    "http://10.0.0.5/a", "http://localhost/x", "ftp://example.com/x",
                    "http://user:pw@example.com/x", "http://example.com:25/x"):
            with self.assertRaises(ValueError, msg=bad):
                th.fetch_public_bytes(bad, max_bytes=10)
        # Hostname resolving to a private address
        with mock.patch.object(th.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("10.1.2.3", 80))]):
            with self.assertRaises(th.UnsafeURLError):
                th.fetch_public_bytes("http://evil.example/x", max_bytes=10)
        # Public host that redirects to a private address: hop must be rejected
        class _Resp:
            status = 302
            def getheader(self, k, d=None): return "http://127.0.0.1/secret" if k == "Location" else d
            def read(self, n=-1): return b""
        class _Conn:
            def __init__(self, *a, **k): pass
            def request(self, *a, **k): pass
            def getresponse(self): return _Resp()
            def close(self): pass
        with mock.patch.object(th.socket, "getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 80))]), \
             mock.patch.object(th, "_PinnedHTTPConnection", _Conn):
            with self.assertRaises(th.UnsafeURLError):
                th.fetch_public_bytes("http://public.example/x", max_bytes=10)

    def test_fetch_public_bytes_enforces_size_and_content_type(self):
        from app import tenant_http as th
        class _Resp:
            status = 200
            def __init__(self, ct, body): self._ct, self._body, self._i = ct, body, 0
            def getheader(self, k, d=None): return self._ct if k == "Content-Type" else d
            def read(self, n=-1):
                chunk = self._body[self._i:self._i + (n if n and n > 0 else len(self._body))]
                self._i += len(chunk); return chunk
        def conn_with(resp):
            class _Conn:
                def __init__(self, *a, **k): pass
                def request(self, *a, **k): pass
                def getresponse(self): return resp
                def close(self): pass
            return _Conn
        gai = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(th.socket, "getaddrinfo", return_value=gai), \
             mock.patch.object(th, "_PinnedHTTPSConnection", conn_with(_Resp("text/html", b"<html>"))):
            with self.assertRaises(ValueError):
                th.fetch_public_bytes("https://public.example/x", max_bytes=100, content_types=("image/",))
        with mock.patch.object(th.socket, "getaddrinfo", return_value=gai), \
             mock.patch.object(th, "_PinnedHTTPSConnection", conn_with(_Resp("image/png", b"x" * 200))):
            with self.assertRaises(ValueError):
                th.fetch_public_bytes("https://public.example/x", max_bytes=100, content_types=("image/",))
        with mock.patch.object(th.socket, "getaddrinfo", return_value=gai), \
             mock.patch.object(th, "_PinnedHTTPSConnection", conn_with(_Resp("image/png", b"\x89PNG"))):
            body, ct, final = th.fetch_public_bytes("https://public.example/x", max_bytes=100, content_types=("image/",))
        self.assertEqual((body, ct), (b"\x89PNG", "image/png"))

    def test_upload_image_uses_safe_fetcher_and_never_requests(self):
        from app.tenant_http import UnsafeURLError
        with mock.patch("app.tenant_http.fetch_public_bytes", side_effect=UnsafeURLError("nope")) as m_fetch, \
             mock.patch.object(services.requests, "get") as m_get, \
             mock.patch.object(services.requests, "head") as m_head:
            out = services.upload_image("act_1", "tok", url_or_bytes="http://10.0.0.1/x.png")
        self.assertEqual(out["error"]["type"], "image_fetch_error")
        self.assertTrue(m_fetch.called)
        m_get.assert_not_called(); m_head.assert_not_called()

    # MCP-35 -----------------------------------------------------------
    def _expired_adset(self):
        return {"id": "as1", "end_time": "2020-01-01T00:00:00+0000", "start_time": "2019-01-01T00:00:00+0000",
                "name": "old", "status": "PAUSED", "optimization_goal": "LINK_CLICKS", "targeting": {}}

    def test_expired_adset_preview_makes_zero_mutations(self):
        with mock.patch.object(services, "get_marketing_object", return_value=self._expired_adset()), \
             mock.patch.object(services, "_post") as m_post, \
             mock.patch.object(services, "update_adset_from_payload") as m_upd:
            out = services.update_adset_with_smart_diff("as1", "tok", {"name": "new"}, None, dry_run=True)
        m_post.assert_not_called(); m_upd.assert_not_called()
        self.assertTrue(out["schedule_expired"]); self.assertTrue(out["dry_run"])
        self.assertEqual(out["error"]["type"], "adset_expired")
        self.assertIn("proposed_end_time", out)

    def test_expired_adset_edit_without_approved_end_time_is_refused(self):
        with mock.patch.object(services, "get_marketing_object", return_value=self._expired_adset()), \
             mock.patch.object(services, "_post") as m_post, \
             mock.patch.object(services, "update_adset_from_payload") as m_upd:
            out = services.update_adset_with_smart_diff("as1", "tok", {"name": "new"}, None, dry_run=False)
        m_post.assert_not_called(); m_upd.assert_not_called()
        self.assertEqual(out["error"]["type"], "adset_expired")

    def test_expired_adset_edit_with_future_end_time_applies_only_that_first(self):
        cur = self._expired_adset()
        with mock.patch.object(services, "get_marketing_object", return_value=cur), \
             mock.patch.object(services, "_post", return_value={"success": True}) as m_post, \
             mock.patch.object(services, "update_adset_from_payload", return_value={"success": True}) as m_upd:
            out = services.update_adset_with_smart_diff(
                "as1", "tok", {"name": "new", "end_time": "2099-01-01T00:00:00+0000"}, None, dry_run=False)
        self.assertEqual(m_post.call_count, 1)
        self.assertEqual(m_post.call_args[0][1].get("end_time"), "2099-01-01T00:00:00+0000")
        self.assertTrue(out.get("success"))
        self.assertIn("end_time", out.get("changes_applied", []))
