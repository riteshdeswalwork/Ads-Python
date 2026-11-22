"""breakdowns param: platform/audience split on the 5 insights tools."""
from unittest import mock

from django.test import SimpleTestCase

from facebook_ads_mcp import services, tool_dispatch


class ApplyBreakdownsTests(SimpleTestCase):
    def test_string_and_list_and_empty(self):
        p = {}
        services._apply_breakdowns(p, "publisher_platform")
        self.assertEqual(p["breakdowns"], "publisher_platform")
        p = {}
        services._apply_breakdowns(p, ["age", "gender"])
        self.assertEqual(p["breakdowns"], "age,gender")
        p = {}
        services._apply_breakdowns(p, None)
        services._apply_breakdowns(p, "  ")
        self.assertNotIn("breakdowns", p)


class BreakdownsReachMetaTests(SimpleTestCase):
    """Each service function must forward breakdowns into the Graph params."""

    def _capture(self, fn, *a, **k):
        with mock.patch.object(services, "_get",
                               side_effect=lambda url, params: {"url": url, "params": params}) as g:
            out = fn(*a, **k)
        return out["params"]

    def test_account_insights(self):
        p = self._capture(services.get_insights, "act_1", "tok",
                          breakdowns="publisher_platform")
        self.assertEqual(p["breakdowns"], "publisher_platform")

    def test_campaign_insights(self):
        p = self._capture(services.get_campaign_insights, "123", "tok",
                          breakdowns="publisher_platform,platform_position")
        self.assertEqual(p["breakdowns"], "publisher_platform,platform_position")

    def test_performance_levels(self):
        for fn in (services.get_campaign_performance,
                   services.get_adset_performance, services.get_ad_performance):
            p = self._capture(fn, "act_1", "tok", breakdowns="publisher_platform")
            self.assertEqual(p["breakdowns"], "publisher_platform")

    def test_no_breakdowns_param_absent(self):
        p = self._capture(services.get_insights, "act_1", "tok")
        self.assertNotIn("breakdowns", p)


class DispatchPassesBreakdownsTests(SimpleTestCase):
    def test_dispatch_forwards_breakdowns(self):
        captured = {}

        def fake(url, params):
            captured.update(params)
            return {"data": []}

        with mock.patch.object(services, "_get", side_effect=fake):
            tool_dispatch._dispatch_tool(
                "facebook_get_insights",
                {"account_id": "act_1", "breakdowns": "publisher_platform"},
                "tok", "act_1", False,
            )
        self.assertEqual(captured.get("breakdowns"), "publisher_platform")


class SchemaExposesBreakdownsTests(SimpleTestCase):
    def test_all_five_tools_document_breakdowns(self):
        from facebook_ads_mcp.views import TOOLS
        want = {"facebook_get_insights", "facebook_get_campaign_insights",
                "facebook_get_campaign_performance", "facebook_get_adset_performance",
                "facebook_get_ad_performance"}
        for t in TOOLS:
            if t["name"] in want:
                self.assertIn("breakdowns", t["inputSchema"]["properties"], t["name"])
                want.discard(t["name"])
        self.assertFalse(want, f"tools missing breakdowns: {want}")
