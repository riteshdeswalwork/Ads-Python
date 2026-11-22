"""
Facebook Ads MCP — stdio transport.
Run: python -m facebook_ads_mcp.stdio_server (from project root with PYTHONPATH set)
or: python -m MCP_SERVICEs.facebook_ads_mcp.stdio_server from repo root.
Reads JSON-RPC from stdin (one JSON object per line), writes response to stdout.
Auth: arguments.token and/or arguments.user_id. For auto long-lived refresh without server env, pass facebook_app_id+facebook_app_secret (or client_id+client_secret) on each tools/call. Optional credential_user_id to persist refreshed token to DB.
"""
import json
import sys


def _read_message():
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return _read_message()
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _write_message(msg):
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    # Import here so stdio server can run without Django when used as subprocess
    import os
    import logging
    import django

    # Package dir is .../facebook_ads_mcp; parent is MCP_SERVICEs (Django project root).
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    services_root = os.path.dirname(pkg_dir)
    if services_root not in sys.path:
        sys.path.insert(0, services_root)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcp.settings")
    django.setup()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )

    from facebook_ads_mcp.views import TOOLS, handle_mcp_request_from_body

    # Meta WhatsApp MCP uses send_message / create_template / get_All_templates — not Marketing API.
    _whatsapp_mcp_names = frozenset(
        {"send_message", "send_template", "send_voice", "create_template", "get_template", "get_All_templates"}
    )
    names = {t.get("name") for t in TOOLS if isinstance(t, dict)}
    if names and names <= _whatsapp_mcp_names:
        sys.stderr.write(
            "[facebook-ads-mcp] FATAL: Loaded TOOLS look like WhatsApp MCP, not Facebook Ads Marketing API. "
            "Use cwd=MCP_SERVICEs and `python -m facebook_ads_mcp` (see .cursor/mcp.json).\n"
        )
        sys.exit(1)
    first = (TOOLS[0].get("name") or "") if TOOLS else ""
    if first and not str(first).startswith("facebook_"):
        sys.stderr.write(
            "[facebook-ads-mcp] FATAL: First tool is %r; expected names prefixed with 'facebook_'. "
            "Another module may be shadowing package facebook_ads_mcp.\n" % (first,)
        )
        sys.exit(1)
    sys.stderr.write(
        "[facebook-ads-mcp] Marketing API MCP ready: %d tools (e.g. %s)\n"
        % (len(TOOLS), first or "(empty)"),
    )
    sys.stderr.flush()

    while True:
        msg = _read_message()
        if msg is None:
            break
        if "method" not in msg:
            continue
        method = msg.get("method")
        # JSON-RPC notifications have no id; MCP clients must not get a reply (Cursor breaks otherwise).
        if isinstance(method, str) and method.startswith("notifications/"):
            continue
        try:
            result = handle_mcp_request_from_body(msg)
            _write_message(result)
        except Exception as e:
            req_id = msg.get("id")
            _write_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32603, "message": str(e)}})


if __name__ == "__main__":
    main()
