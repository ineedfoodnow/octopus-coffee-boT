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
API_BACKEND = "https://api.octopus.energy/v1/graphql/"
UK_TZ       = ZoneInfo("Europe/London")

POLL_WINDOWS = {
    0: 20 * 60,  # Monday    — primary,  20 min
    1:  5 * 60,  # Tuesday   — fallback,  5 min
    2:  5 * 60,  # Wednesday — fallback,  5 min
    3:  5 * 60,  # Thursday  — fallback,  5 min
}

MAX_PRE_SLEEP = 600
FORCE_RUN     = os.environ.get("FORCE_RUN", "false").lower() == "true"

ACCOUNTS = [
    {
        "label":   "Account 1",
        "api_key": os.environ.get("OCTO_APIKEY_1", "").strip(),
        "account": os.environ.get("OCTO_ACC_1", "").strip(),
    },
    {
        "label":   "Account 2",
        "api_key": os.environ.get("OCTO_APIKEY_2", "").strip(),
        "account": os.environ.get("OCTO_ACC_2", "").strip(),
    },
    {
        "label":   "Account 3",
        "api_key": os.environ.get("OCTO_APIKEY_3", "").strip(),
        "account": os.environ.get("OCTO_ACC_3", "").strip(),
    },
]

OBTAIN_TOKEN_MUTATION = """
mutation ObtainToken($apiKey: String!){
  obtainKrakenToken(input: { APIKey: $apiKey }){
    token
  }
}
"""

CHECK_QUERY = """
query Offers($account:String!){
  octoplusOfferGroups(accountNumber:$account, first:10){
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
_print_lock = threading.Lock()

def safe_print(*args, **kwargs):
    with _print_lock:
        print(*args, **kwargs)

def divider(char="─", width=56):
    safe_print(char * width)

def header(title):
    divider("═")
    pad = (56 - len(title) - 2) // 2
    safe_print(f"{'═' * pad} {title} {'═' * (56 - pad - len(title) - 2)}")
    divider("═")

def section(title):
    safe_print()
    safe_print(f"  ┌─ {title}")

def log(icon, label, msg, indent=2):
    prefix = "  │  " + "  " * indent
    safe_print(f"{prefix}{icon} {label}: {msg}")

def result(icon, msg, indent=2):
    prefix = "  │  " + "  " * indent
    safe_print(f"{prefix}{icon} {msg}")

def close_section():
    safe_print("  └" + "─" * 54)

def account_header(label, masked_account):
    safe_print(f"  │")
    safe_print(f"  │  ▸ {label}  ({masked_account})")

def mask(val):
    return (val[:4] + "••••••") if val else "── NOT SET ──"

# ─────────────────────────────────────────────
#  TOKEN EXCHANGE
# ─────────────────────────────────────────────
def get_auth_token(api_key, label):
    try:
        r = requests.post(
            API_BACKEND,
            json={
                "query": OBTAIN_TOKEN_MUTATION,
                "variables": {"apiKey": api_key},
            },
            timeout=10,
        )
        data  = r.json()
        token = (
            data.get("data", {})
                .get("obtainKrakenToken", {})
                .get("token")
        )
        if not token:
            log("⛔", "Token exchange failed",
                str(data.get("errors", data)), indent=3)
        return token
    except Exception as e:
        log("⛔", "Token exchange error", str(e), indent=3)
        return None

# ─────────────────────────────────────────────
#  TIME GUARD
# ─────────────────────────────────────────────
def wait_for_5am():
    section("Schedule Check")
    now      = datetime.now(UK_TZ)
    weekday  = now.weekday()
    day_name = now.strftime("%A")

    log("🌍", "Current UK time", now.strftime("%A %d %b %Y  %H:%M:%S %Z"))

    if FORCE_RUN:
        log("⚡", "FORCE_RUN", "time/day guard bypassed")
        close_section()
        return True, 2 * 60

    if weekday not in POLL_WINDOWS:
        log("📅", "Run day check", f"✗ {day_name} — bot only runs Mon–Thu")
        close_section()
        return False, 0

    poll_window = POLL_WINDOWS[weekday]
    today_5am   = now.replace(hour=5, minute=0, second=0, microsecond=0)
    secs        = (today_5am - now).total_seconds()

    log("📅", "Run day check", f"✅ {day_name} — valid")
    log("⏱ ", "Poll window",   f"{poll_window // 60} minutes")

    if secs > MAX_PRE_SLEEP:
        log("⏳", "Time check",
            f"✗ {secs:.0f}s before 5am — wrong-season cron fired, aborting")
        close_section()
        return False, 0

    if secs < -poll_window:
        log("⏰", "Time check",
            f"✗ {abs(secs):.0f}s past 5am — stale trigger, aborting")
        close_section()
        return False, 0

    if secs > 0:
        log("⏳", "Sleeping",
            f"{secs:.1f}s until exactly 5am UK...")
        close_section()
        time.sleep(secs)
    else:
        log("⏰", "Time check",
            f"✅ {abs(secs):.0f}s past 5am — polling immediately")
        close_section()

    return True, poll_window


def get_poll_end_time(poll_window):
    today_5am = datetime.now(UK_TZ).replace(
        hour=5, minute=0, second=0, microsecond=0
    )
    return today_5am.timestamp() + poll_window

# ─────────────────────────────────────────────
#  CHECK
# ─────────────────────────────────────────────
def check_reward(token, account, label):
    try:
        r = requests.post(
            API_BACKEND,
            headers={"Authorization": token},
            json={"query": CHECK_QUERY, "variables": {"account": account}},
            timeout=10,
        )
        data   = r.json()
        errors = data.get("errors")

        if errors:
            code = errors[0].get("extensions", {}).get("errorCode", "")
            msg  = errors[0].get("message", str(errors))

            if code == "KT-GB-9319":
                return None         # Offers not available yet — keep polling
            elif code == "KT-GB-9316":
                log("❌", "Octoplus", "Account not enrolled — stopping", indent=3)
                return "NOT_ENROLLED"
            else:
                log("⚠️ ", "API error", f"{msg} ({code})", indent=3)
                return None

        groups = data.get("data", {}).get("octoplusOfferGroups", {})
        if not groups:
            return None

        for group in groups.get("edges", []):
            for offer in group["node"].get("octoplusOffers", []):
                if offer.get("claimAbility", {}).get("canClaimOffer"):
                    return offer.get("slug")

        return None

    except Exception as e:
        log("⚠️ ", "Check error", str(e), indent=3)
        return None

# ─────────────────────────────────────────────
#  CLAIM
# ─────────────────────────────────────────────
def claim_reward(token, account, slug, label):
    log("🎯", "Claiming", slug, indent=3)
    try:
        r = requests.post(
            API_BACKEND,
            headers={"Authorization": token},
            json={
                "query": CLAIM_MUTATION,
                "variables": {"account": account, "slug": slug},
            },
            timeout=10,
        )
        resp    = r.json()
        success = (
            resp.get("data", {})
                .get("claimOctoplusReward", {})
                .get("success", False)
        )
        if not success:
            errors = resp.get("errors")
            msg    = errors[0].get("message", str(resp)) if errors else str(resp)
            code   = errors[0].get("extensions", {}).get("errorCode", "") if errors else ""
            log("❌", "Claim failed", f"{msg} ({code})", indent=3)
        return success
    except Exception as e:
        log("❌", "Claim error", str(e), indent=3)
        return False

# ─────────────────────────────────────────────
#  WORKER
# ─────────────────────────────────────────────
def worker(acc, end_time, on_done, summary):
    api_key = acc["api_key"]
    account = acc["account"]
    label   = acc["label"]

    account_header(label, mask(account))

    if not api_key or not account:
        log("❌", "Credentials", "Missing — check secrets", indent=3)
        summary[label] = "❌  Missing credentials"
        on_done()
        return

    log("🔑", "Token", "Exchanging API key...", indent=3)
    token = get_auth_token(api_key, label)
    if not token:
        log("❌", "Token", "Exchange failed — skipping", indent=3)
        summary[label] = "❌  Token exchange failed"
        on_done()
        return

    log("✅", "Token", "Obtained — polling...", indent=3)

    try:
        while time.time() < end_time:
            slug = check_reward(token, account, label)

            if slug == "NOT_ENROLLED":
                summary[label] = "❌  Account not enrolled in Octoplus"
                return

            if slug:
                log("🎟 ", "Offer found", slug, indent=3)
                for attempt in range(3):
                    if claim_reward(token, account, slug, label):
                        log("✅", "Claimed", "Successfully! 🎉", indent=3)
                        summary[label] = "✅  Claimed successfully"
                        return
                    log("⚠️ ", f"Attempt {attempt + 1}", "Failed — retrying in 2s", indent=3)
                    time.sleep(2)
                log("❌", "Claim", "All 3 attempts failed", indent=3)
                summary[label] = "❌  All claim attempts failed"
                return

            time.sleep(0.25 if (end_time - time.time()) < 60 else 1)

        now = datetime.now(UK_TZ)
        if now.weekday() == 0:
            log("⏰", "Window", "Monday expired — Tue–Thu fallback will retry", indent=3)
            summary[label] = "⏸   Window expired — fallback will retry"
        else:
            log("⏰", "Window", "Expired — retry next Monday", indent=3)
            summary[label] = "⏸   Window expired — retry next Monday"

    finally:
        on_done()

# ─────────────────────────────────────────────
#  SUMMARY
# ─────────────────────────────────────────────
def print_summary(summary):
    section("Summary")
    for acc in ACCOUNTS:
        log("", acc["label"], summary.get(acc["label"], "⏸  No result recorded"), indent=1)
    close_section()

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    header("OCTOPUS COFFEE BOT  ·  PRODUCTION")
    print(f"  Started : {datetime.now(UK_TZ).strftime('%A %d %b %Y  %H:%M:%S %Z')}")
    print(f"  Mode    : {'⚡ FORCE_RUN (manual)' if FORCE_RUN else '🕐 Scheduled run'}")

    should_run, poll_window = wait_for_5am()
    if not should_run:
        sys.exit(0)

    section("Workers")
    end_time   = get_poll_end_time(poll_window)
    done_count = [0]
    done_lock  = threading.Lock()
    all_done   = threading.Event()
    summary    = {}

    def on_done():
        with done_lock:
            done_count[0] += 1
            if done_count[0] >= len(ACCOUNTS):
                all_done.set()

    threads = [
        threading.Thread(target=worker, args=(acc, end_time, on_done, summary))
        for acc in ACCOUNTS
    ]
    for t in threads:
        t.start()

    all_done.wait(timeout=poll_window + 30)
    for t in threads:
        t.join(timeout=5)

    close_section()
    print_summary(summary)
    header(f"FINISHED  ·  {datetime.now(UK_TZ).strftime('%H:%M:%S %Z')}")


if __name__ == "__main__":
    main()
