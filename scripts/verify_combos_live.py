"""
Live combo verifier — hits Meta Graph API with validate_only to test which
(objective, optimization_goal, destination_type) triples Meta actually accepts.

USAGE (from MCP_SERVICEs root):
    export META_USER_TOKEN='paste-your-user-token-here'   # do NOT paste in chat
    export META_AD_ACCOUNT_ID='act_XXXXXXXXX'             # ad account to test against
    export META_PAGE_ID='1234567890'                      # a page you own
    uv run python -m facebook_ads_mcp.scripts.verify_combos_live

Outputs:
  - per-combo PASS / FAIL (with Meta error code+message on FAIL)
  - mismatches: combos our matrix says VALID but Meta rejects, or vice versa
  - JSON dump at /tmp/combo_verify_<timestamp>.json
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Bootstrap Django BEFORE importing anything from django.conf.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mcp.settings")
import django  # noqa: E402
django.setup()

from facebook_ads_mcp import services  # noqa: E402
from facebook_ads_mcp import meta_validation as mv  # noqa: E402


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"ERROR: environment variable {name} is required.", file=sys.stderr)
        sys.exit(2)
    return val


def _all_combos() -> List[Tuple[str, str, str]]:
    """Cartesian: every (objective, goal, destination) from our matrix."""
    out: List[Tuple[str, str, str]] = []
    for (obj, goal), dests in mv._OBJECTIVE_GOAL_DESTINATIONS.items():
        for dest in dests:
            out.append((obj, goal, dest))
    return out


def _our_says_valid(obj: str, goal: str, dest: str) -> bool:
    """What our offline matrix says."""
    if (obj, goal) in mv._FORBIDDEN_OBJECTIVE_OPTIMIZATION:
        return False
    dests = mv._OBJECTIVE_GOAL_DESTINATIONS.get((obj, goal))
    if dests is None:
        return False
    return dest in dests


def _validate_one(account_id: str, page_id: str, token: str,
                  obj: str, goal: str, dest: str) -> Dict[str, Any]:
    """Issue one validate_only POST. Returns {ok, meta_response, http_status}."""
    base = f"https://graph.facebook.com/v25.0/{account_id}"

    # Step 1: validate_only campaign with this objective. Cheap.
    camp_url = f"{base}/campaigns"
    camp_params = {
        "access_token": token,
        "name": f"combo_test_{obj}_{goal}_{dest}_{int(time.time())}",
        "objective": obj,
        "status": "PAUSED",
        "special_ad_categories": json.dumps([]),
        "execution_options": json.dumps(["validate_only"]),
    }
    camp_res = services._post(camp_url, camp_params) if hasattr(services, "_post") else None
    if not isinstance(camp_res, dict):
        return {"ok": False, "stage": "campaign", "meta": {"error": "no response"}}
    if camp_res.get("error"):
        return {"ok": False, "stage": "campaign", "meta": camp_res}

    # Step 2: validate_only adset with this goal + destination.
    aset_url = f"{base}/adsets"
    aset_params = {
        "access_token": token,
        "name": f"combo_aset_{goal}_{dest}",
        "campaign_id": "0",
        "optimization_goal": goal,
        "billing_event": "IMPRESSIONS",
        "daily_budget": 50000,
        "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        "targeting": json.dumps({"geo_locations": {"countries": ["IN"]},
                                 "age_min": 18, "age_max": 65}),
        "status": "PAUSED",
        "destination_type": dest,
        "promoted_object": json.dumps({"page_id": page_id}) if dest in {"ON_PAGE", "ON_AD"} else None,
        "execution_options": json.dumps(["validate_only"]),
    }
    aset_params = {k: v for k, v in aset_params.items() if v is not None}
    aset_res = services._post(aset_url, aset_params)
    if not isinstance(aset_res, dict):
        return {"ok": False, "stage": "adset", "meta": {"error": "no response"}}
    if aset_res.get("error"):
        return {"ok": False, "stage": "adset", "meta": aset_res}
    return {"ok": True, "stage": "adset", "meta": aset_res}


def main() -> int:
    token = _require_env("META_USER_TOKEN")
    account = _require_env("META_AD_ACCOUNT_ID")
    page = _require_env("META_PAGE_ID")
    if not account.startswith("act_"):
        account = f"act_{account}"

    combos = _all_combos()
    print(f"Total combos to test: {len(combos)}")
    print(f"Ad account: {account}")
    print(f"Page: {page}")
    print("-" * 80)

    results: List[Dict[str, Any]] = []
    mismatches: List[Dict[str, Any]] = []

    for i, (obj, goal, dest) in enumerate(combos, 1):
        ours = _our_says_valid(obj, goal, dest)
        meta_res = _validate_one(account, page, token, obj, goal, dest)
        meta_ok = bool(meta_res.get("ok"))
        agree = ours == meta_ok
        tag = "OK" if agree else "MISMATCH"
        marker = "✓" if meta_ok else "✗"
        err = ""
        if not meta_ok:
            e = meta_res.get("meta", {}).get("error", {}) if isinstance(meta_res.get("meta"), dict) else {}
            err = f" [{e.get('code')}/{e.get('error_subcode') or '-'}] {e.get('message', '')[:80]}"
        print(f"[{i:3d}/{len(combos)}] {marker} {tag:9s} {obj:25s} {goal:25s} {dest:20s}{err}")

        rec = {
            "objective": obj, "goal": goal, "destination": dest,
            "ours_says_valid": ours, "meta_says_valid": meta_ok,
            "agree": agree,
            "meta_stage": meta_res.get("stage"),
            "meta_error": meta_res.get("meta", {}).get("error") if not meta_ok else None,
        }
        results.append(rec)
        if not agree:
            mismatches.append(rec)
        # Be polite to Meta — 4 calls/sec is fine for validate_only.
        time.sleep(0.25)

    print("-" * 80)
    print(f"Total: {len(results)}   Agree: {len(results) - len(mismatches)}   "
          f"Mismatches: {len(mismatches)}")

    out_path = f"/tmp/combo_verify_{int(time.time())}.json"
    with open(out_path, "w") as f:
        json.dump({"results": results, "mismatches": mismatches}, f, indent=2)
    print(f"Full JSON: {out_path}")

    if mismatches:
        print("\n=== MISMATCHES (our matrix vs Meta reality) ===")
        for m in mismatches:
            arrow = "OURS=VALID Meta=REJECT" if m["ours_says_valid"] else "OURS=INVALID Meta=ACCEPT"
            print(f"  {arrow}  {m['objective']} / {m['goal']} / {m['destination']}")
            if m.get("meta_error"):
                e = m["meta_error"]
                print(f"      Meta: code={e.get('code')} subcode={e.get('error_subcode')} "
                      f"msg={e.get('message', '')[:120]}")
    return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
