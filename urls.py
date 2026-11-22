from django.urls import path
from facebook_ads_mcp.views import (
    mcp_endpoint,
    mcp_info_endpoint,
)
from facebook_ads_mcp.connect_views import (
    adaccounts_view,
    connect_view,
    oauth_callback,
    oauth_start,
    status_view,
    test_view,
)

# Mounted at api/mcp/facebook-ads/ (and api/mcp/facebook_ads/) in mcp/urls.py.
# Subpaths must live here so they are not lost to the api/ → app.urls catch-all (which
# historically surfaced WhatsApp tools for unknown mcp/<provider>/ children).
urlpatterns = [
    path("connect/", connect_view, name="facebook-ads-connect"),
    # "Connect with Facebook" OAuth (server-side single-app). Callback is
    # registered with and without a trailing slash because FB app dashboards
    # commonly store it without one.
    path("oauth/start/", oauth_start, name="facebook-ads-oauth-start"),
    path("oauth/callback", oauth_callback, name="facebook-ads-oauth-callback-noslash"),
    path("oauth/callback/", oauth_callback, name="facebook-ads-oauth-callback"),
    path("adaccounts/", adaccounts_view, name="facebook-ads-adaccounts"),
    path("status/", status_view, name="facebook-ads-status"),
    path("test/", test_view, name="facebook-ads-test"),
    path("", mcp_endpoint, name="facebook-ads-mcp"),
    path("info/", mcp_info_endpoint, name="facebook-ads-mcp-info"),
]