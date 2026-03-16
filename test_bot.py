import requests
import time
import threading
import os
import sys
from zoneinfo import ZoneInfo
from datetime import datetime

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
API_BACKEND  = "https://api.backend.octopus.energy/v1/graphql/"
UK_TZ        = ZoneInfo("Europe/London")
POLL_WINDOW  = 2 * 60   # 2 minutes — enough for a test without burning minutes
FORCE_RUN    = os.environ.get("FORCE_RUN", "true").lower() == "true"

ACCOUNTS = [
    {
        "label":   "Account 1",
        "api_key": os.environ.get("OCTO_APIKEY_1"),
        "account": os.environ.get("OCTO_ACC_1"),
    },
    {
        "label":   "Account 2",
        "api_key": os.environ.get("OCTO_APIKEY_2"),
        "account": os.environ.get("OCTO_ACC_2"),
    },
    {
        "label":   "Account 3",
        "api_key": os.environ.get("OCTO_APIKEY_3"),
        "account": os.environ.get("OCTO_ACC_3"),
    },
]

CHECK_QUERY = """
query Offers($account:String!){
  octoplusOfferGroups(accountNumber:$account){
    edges{
      node{
        octoplusOffers{
          slug
          claimAbility{
            canClaimOffer
          }
        }
      }
    }
  }
}
"""

CLAIM_MUTATION = """
mutation Claim($account:String!,$slug:String!){
  claimOctoplusReward(accountNumber:$account,offerSlug:$slug){
    success
  }
}
"""

# ─────────────────────────────────────────────
#  LOGGING HELPERS
# ─────────────────────────────────────────────
def divider(char="─", width=56):
    print(char * width)

def header(title):
    divider("═")
    pad = (56 - len(title) - 2) // 2
    print(f"{'═' * pad} {title} {'═' * (56 - pad - len(title) - 2)}")
    divider("═")

def section(title):
    print()
    print(f"  ┌─ {title}")

def log(icon, label, msg, indent=2):
    prefix = "  │  " + "  " * indent
    print(f"{prefix}{icon} {label}: {msg}")

def result(icon, msg, indent=2):
    prefix = "  │  " + "  " * indent
    print(f"{prefix}{icon} {msg}")

def close_section():
    print("  └" + "─" * 54)

def mask(val):
    """Show first 4 chars only — confirms the value loaded without exposing it."""
    return (val[:4] + "••••••") if val else "── NOT SET ──"

# ─────────────────────────────────────────────
#  PHASE 1 — SECRETS
# ─────────────────────────────────────────────
def check_secrets():
    section("PHASE 1 — Secrets")
    all_ok = True
    for acc in ACCOUNTS:
        key_ok = bool(acc["api_key"])
        acc_ok = bool(acc["account"])
        icon   = "✅" if (key_ok and acc_ok) else "❌"
        log(icon, acc["label"],
            f"api_key={mask(acc['api_key'])}  "
            f"account={mask(acc['account'])}")
        if not (key_ok and acc_ok):
            all_ok = False
    if not all_ok:
        result("⛔", "One or more secrets missing — check repo Settings → Secrets")
    close_section()
    return all_ok

# ─────────────────────────────────────────────
#  PHASE 2 — TIMEZONE / SCHEDULE LOGIC
# ─────────────────────────────────────────────
def check_timezone():
    section("PHASE 2 — Timezone & Schedule Logic")
    now      = datetime.now(UK_TZ)
    weekday  = now.weekday()
    day_name = now.strftime("%A")
    today_5am = now.replace(hour=5, minute=0, second=0, microsecond=0)
    secs_until = (today_5am - now).total_seconds()

    log("🌍", "Current UK time", now.strftime("%A %d %b %Y  %H:%M:%S %Z"))
    log("📅", "Valid run day",
        f"{'✅ Yes' if weekday in range(4) else '⚠️  No (bot skips Fri–Sun in production)'}"
        f"  ({day_name})")

    if secs_until > 0:
        log("⏳", "Time until 5am UK", f"{secs_until:.0f}s  ({secs_until/60:.1f} min)")
    else:
        log("⏰", "Time past 5am UK",  f"{abs(secs_until):.0f}s  ({abs(secs_until)/60:.1f} min ago)")

    if FORCE_RUN:
        result("⚡", "FORCE_RUN=true — time guard bypassed for this test")

    close_section()

# ─────────────────────────────────────────────
#  PHASE 3 — API CONNECTIVITY
# ─────────────────────────────────────────────
def check_api_connectivity():
    section("PHASE 3 — API Connectivity")
    try:
        r = requests.get("https://api.backend.octopus.energy/", timeout=8)
        log("🌐", "Octopus API reachable", f"HTTP {r.status_code}")
    except Exception as e:
        log("❌", "Octopus API unreachable", str(e))
        result("⛔", "Cannot proceed — network issue")
        close_section()
        return False
    close_section()
    return True

# ─────────────────────────────────────────────
#  PHASE 4 — AUTH & OFFER CHECK
# ─────────────────────────────────────────────
def check_offers():
    section("PHASE 4 — Auth & Offer Status per Account")
    results = {}

    for acc in ACCOUNTS:
        label   = acc["label"]
        api_key = acc["api_key"]
        account = acc["account"]
        print(f"  │")
        print(f"  │  ▸ {label}  ({mask(account)})")

        if not api_key or not account:
            result("⚠️ ", "Skipped — missing credentials", indent=3)
            results[label] = {"skip": True}
            continue

        try:
            r = requests.post(
                API_BACKEND,
                auth=(api_key, ""),
                json={"query": CHECK_QUERY, "variables": {"account": account}},
                timeout=10,
            )

            log("📡", "HTTP", str(r.status_code), indent=3)

            if r.status_code == 401:
                result("❌", "Auth failed — api_key rejected by Octopus", indent=3)
                results[label] = {"auth_failed": True}
                continue

            data   = r.json()
            errors = data.get("errors")

            if errors:
                result(f"❌", f"API error — {errors[0].get('message', errors)}", indent=3)
                results[label] = {"api_error": True}
                continue

            groups = data.get("data", {}).get("octoplusOfferGroups", {})
            edges  = groups.get("edges", []) if groups else []

            if not edges:
                result("⚠️ ", "No offer groups returned — account may not be on Octoplus", indent=3)
                results[label] = {"no_offers": True}
                continue

            claimable_slug = None
            for group in edges:
                for offer in group["node"].get("octoplusOffers", []):
                    slug      = offer.get("slug", "unknown")
                    claimable = offer.get("claimAbility", {}).get("canClaimOffer", False)
                    icon      = "✅ CLAIMABLE NOW" if claimable else "⏸  Not claimable (already claimed or vouchers gone)"
                    log("🎟 ", f"{slug}", icon, indent=3)
                    if claimable:
                        claimable_slug = slug

            results[label] = {"slug": claimable_slug}

        except Exception as e:
            result(f"❌", f"Request failed — {e}", indent=3)
            results[label] = {"exception": True}

    close_section()
    return results

# ─────────────────────────────────────────────
#  PHASE 5 — CLAIM ATTEMPT
# ─────────────────────────────────────────────
def attempt_claims(offer_results):
    section("PHASE 5 — Claim Attempt")

    claimable = {
        label: info["slug"]
        for label, info in offer_results.items()
        if info.get("slug")
    }

    if not claimable:
        result("ℹ️ ", "No claimable offers found right now")
        result("💡", "This is expected if today's vouchers are gone — "
                     "the scheduled 5am run will catch them tomorrow")
        close_section()
        return

    results  = {}
    lock     = threading.Lock()
    all_done = threading.Event()
    counter  = [0]

    def on_done():
        with lock:
            counter[0] += 1
            if counter[0] >= len(claimable):
                all_done.set()

    def claim_worker(label, account_info, slug):
        api_key = account_info["api_key"]
        account = account_info["account"]
        print(f"  │")
        print(f"  │  ▸ {label} — attempting claim of {slug}")
        try:
            r = requests.post(
                API_BACKEND,
                auth=(api_key, ""),
                json={
                    "query": CLAIM_MUTATION,
                    "variables": {"account": account, "slug": slug}
                },
                timeout=10,
            )
            resp    = r.json()
            success = (
                resp.get("data", {})
                    .get("claimOctoplusReward", {})
                    .get("success", False)
            )
            errors = resp.get("errors")

            if success:
                result("✅", "Claimed successfully! 🎉", indent=3)
                results[label] = "claimed"
            elif errors:
                msg = errors[0].get("message", str(errors))
                result(f"❌", f"Claim rejected — {msg}", indent=3)
                results[label] = f"rejected: {msg}"
            else:
                result("⚠️ ", f"Unexpected response — {resp}", indent=3)
                results[label] = "unexpected"

        except Exception as e:
            result(f"❌", f"Claim request failed — {e}", indent=3)
            results[label] = f"exception: {e}"
        finally:
            on_done()

    acc_map = {acc["label"]: acc for acc in ACCOUNTS}

    threads = [
        threading.Thread(target=claim_worker, args=(label, acc_map[label], slug))
        for label, slug in claimable.items()
    ]
    for t in threads:
        t.start()
    all_done.wait(timeout=POLL_WINDOW + 10)
    for t in threads:
        t.join(timeout=5)

    close_section()
    return results

# ─────────────────────────────────────────────
#  SUMMARY
# ─────────────────────────────────────────────
def print_summary(offer_results, claim_results):
    section("SUMMARY")
    for acc in ACCOUNTS:
        label  = acc["label"]
        info   = offer_results.get(label, {})
        claim  = (claim_results or {}).get(label)

        if info.get("skip") or info.get("auth_failed") or info.get("api_error") or info.get("exception"):
            status = "❌  Error — see Phase 3/4 output above"
        elif info.get("no_offers"):
            status = "⚠️   Not on Octoplus"
        elif info.get("slug"):
            status = f"✅  Claimed" if claim == "claimed" else f"⚠️   Offer found but claim failed ({claim})"
        else:
            status = "⏸   No claimable offer — vouchers taken or already claimed this week"

        log("", label, status, indent=1)
    close_section()

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    header("OCTOPUS COFFEE BOT  ·  TEST RUN")
    print(f"  Started : {datetime.now(UK_TZ).strftime('%A %d %b %Y  %H:%M:%S %Z')}")
    print(f"  Mode    : {'⚡ FORCE_RUN (manual test)' if FORCE_RUN else '🕐 Scheduled run'}")

    if not check_secrets():
        sys.exit(1)

    check_timezone()

    if not check_api_connectivity():
        sys.exit(1)

    offer_results = check_offers()
    claim_results = attempt_claims(offer_results)
    print_summary(offer_results, claim_results)

    header("TEST COMPLETE")


if __name__ == "__main__":
    main()
