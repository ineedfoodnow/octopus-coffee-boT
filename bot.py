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
            print(f"[{label}] Token exchange failed: {data.get('errors', data)}")
        return token
    except Exception as e:
        print(f"[{label}] Token exchange error: {e}")
        return None

# ─────────────────────────────────────────────
#  TIME GUARD
# ─────────────────────────────────────────────
def wait_for_5am():
    now      = datetime.now(UK_TZ)
    weekday  = now.weekday()
    day_name = now.strftime("%A")

    if FORCE_RUN:
        print(f"⚡ FORCE_RUN — bypassing time/day guard.")
        print(f"   Current UK time: {now.strftime('%A %d %b %Y %H:%M:%S %Z')}")
        return True, 2 * 60

    if weekday not in POLL_WINDOWS:
        print(f"Today is {day_name} — bot only runs Mon–Thu. Exiting.")
        return False, 0

    poll_window = POLL_WINDOWS[weekday]
    today_5am   = now.replace(hour=5, minute=0, second=0, microsecond=0)
    secs        = (today_5am - now).total_seconds()

    if secs > MAX_PRE_SLEEP:
        print(f"Started {secs:.0f}s before 5am UK — wrong-season cron fired, aborting.")
        return False, 0

    if secs < -poll_window:
        print(f"Poll window expired ({abs(secs):.0f}s past 5am UK) — stale trigger, aborting.")
        return False, 0

    if secs > 0:
        print(f"[{day_name}] Sleeping {secs:.1f}s until exactly 5am UK...")
        time.sleep(secs)
    else:
        print(f"[{day_name}] {abs(secs):.0f}s past 5am UK — polling immediately.")

    print(f"[{day_name}] Poll window: {poll_window // 60} minutes.")
    return True, poll_window

def get_poll_end_time(poll_window):
    today_5am = datetime.now(UK_TZ).replace(hour=5, minute=0, second=0, microsecond=0)
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
                return None       # Offers not available yet — keep polling
            elif code == "KT-GB-9316":
                print(f"[{label}] Account not enrolled in Octoplus — stopping.")
                return "NOT_ENROLLED"
            else:
                print(f"[{label}] API error: {msg} ({code})")
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
        print(f"[{label}] Check error: {e}")
        return None

# ─────────────────────────────────────────────
#  CLAIM
# ─────────────────────────────────────────────
def claim_reward(token, account, slug, label):
    print(f"[{label}] Attempting claim: {slug}")
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
            print(f"[{label}] Claim failed: {msg} ({code})")
        return success
    except Exception as e:
        print(f"[{label}] Claim error: {e}")
        return False

# ─────────────────────────────────────────────
#  WORKER
# ─────────────────────────────────────────────
def worker(acc, end_time, on_done):
    api_key = acc["api_key"]
    account = acc["account"]
    label   = acc["label"]

    if not api_key or not account:
        print(f"[{label}] Missing credentials — check secrets.")
        on_done()
        return

    token = get_auth_token(api_key, label)
    if not token:
        print(f"[{label}] Could not obtain token — skipping.")
        on_done()
        return

    print(f"[{label}] Token obtained. Polling...")

    try:
        while time.time() < end_time:
            slug = check_reward(token, account, label)

            if slug == "NOT_ENROLLED":
                return

            if slug:
                print(f"[{label}] Reward available: {slug}")
                for attempt in range(3):
                    if claim_reward(token, account, slug, label):
                        print(f"[{label}] ✓ Claimed successfully.")
                        return
                    print(f"[{label}] Attempt {attempt + 1} failed, retrying in 2s...")
                    time.sleep(2)
                print(f"[{label}] All 3 claim attempts failed.")
                return

            time.sleep(0.25 if (end_time - time.time()) < 60 else 1)

        now = datetime.now(UK_TZ)
        if now.weekday() == 0:
            print(f"[{label}] Monday window expired — fallback runs Tue–Thu will retry.")
        else:
            print(f"[{label}] Fallback window expired — retry next Monday.")

    finally:
        on_done()

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    print(f"Octopus coffee bot started: {datetime.now(UK_TZ).strftime('%A %d %b %Y %H:%M:%S %Z')}")

    should_run, poll_window = wait_for_5am()
    if not should_run:
        sys.exit(0)

    print("Launching workers...")
    end_time   = get_poll_end_time(poll_window)
    done_count = [0]
    done_lock  = threading.Lock()
    all_done   = threading.Event()

    def on_done():
        with done_lock:
            done_count[0] += 1
            if done_count[0] >= len(ACCOUNTS):
                all_done.set()

    threads = [
        threading.Thread(target=worker, args=(acc, end_time, on_done))
        for acc in ACCOUNTS
    ]
    for t in threads:
        t.start()

    all_done.wait(timeout=poll_window + 30)
    for t in threads:
        t.join(timeout=5)

    print(f"Bot finished: {datetime.now(UK_TZ).strftime('%H:%M:%S %Z')}")


if __name__ == "__main__":
    main()
