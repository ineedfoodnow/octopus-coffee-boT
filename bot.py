import requests
import time
import threading
import os
import sys
from zoneinfo import ZoneInfo
from datetime import datetime

API_BACKEND = "https://api.backend.octopus.energy/v1/graphql/"
UK_TZ = ZoneInfo("Europe/London")  # Handles GMT/BST transitions automatically

# Monday gets a wide window to catch delayed releases.
# Tue–Thu fallback gets a short window (codes already released, just check quickly).
POLL_WINDOWS = {
    0: 20 * 60,  # Monday:    20 minutes
    1:  5 * 60,  # Tuesday:    5 minutes
    2:  5 * 60,  # Wednesday:  5 minutes
    3:  5 * 60,  # Thursday:   5 minutes
}

MAX_PRE_SLEEP = 600  # Abort if we're more than 10 minutes before 5am (wrong cron season fired)

ACCOUNTS = [
    {"api_key": os.environ.get("OCTO_APIKEY_1"), "account": os.environ.get("OCTO_ACC_1")},
    {"api_key": os.environ.get("OCTO_APIKEY_2"), "account": os.environ.get("OCTO_ACC_2")},
    {"api_key": os.environ.get("OCTO_APIKEY_3"), "account": os.environ.get("OCTO_ACC_3")},
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


def wait_for_5am():
    """
    Sleep until 5am UK time if slightly early.
    Aborts (returns False) if:
      - Today is not a valid run day (Mon–Thu)
      - Started too early (wrong cron season fired — aborts in seconds)
      - Started too late (stale trigger)
    Returns (True, poll_window_seconds) if we should proceed.
    """
    now = datetime.now(UK_TZ)
    weekday = now.weekday()  # 0=Mon, 1=Tue, ..., 6=Sun
    day_name = now.strftime("%A")

    if weekday not in POLL_WINDOWS:
        print(f"Today is {day_name} — bot only runs Mon–Thu. Exiting.")
        return False, 0

    poll_window = POLL_WINDOWS[weekday]
    today_5am = now.replace(hour=5, minute=0, second=0, microsecond=0)
    seconds_until = (today_5am - now).total_seconds()

    if seconds_until > MAX_PRE_SLEEP:
        # The wrong-season GitHub cron fired (e.g. BST 3:55am UTC cron fired during GMT)
        # Abort immediately — costs < 1 runner minute
        print(
            f"Started {seconds_until:.0f}s before 5am UK ({now.strftime('%H:%M %Z')}) — "
            f"wrong-season cron, aborting."
        )
        return False, 0

    if seconds_until < -poll_window:
        print(
            f"Poll window already expired ({abs(seconds_until):.0f}s past 5am UK) — "
            f"stale trigger, aborting."
        )
        return False, 0

    if seconds_until > 0:
        print(f"[{day_name}] Sleeping {seconds_until:.1f}s until exactly 5am UK...")
        time.sleep(seconds_until)
    else:
        print(f"[{day_name}] Started {abs(seconds_until):.0f}s past 5am UK — polling immediately.")

    print(f"[{day_name}] Poll window: {poll_window // 60} minutes.")
    return True, poll_window


def get_poll_end_time(poll_window):
    today_5am = datetime.now(UK_TZ).replace(hour=5, minute=0, second=0, microsecond=0)
    return today_5am.timestamp() + poll_window


def make_session(api_key):
    s = requests.Session()
    s.auth = (api_key, "")
    return s


def check_reward(session, account):
    try:
        r = session.post(
            API_BACKEND,
            json={"query": CHECK_QUERY, "variables": {"account": account}},
            timeout=10,
        )
        data = r.json()

        if not data or "data" not in data or not data["data"]:
            print(f"[{account}] Invalid API response:", data)
            return None

        groups = data["data"].get("octoplusOfferGroups")
        if not groups:
            return None

        for group in groups.get("edges", []):
            for offer in group["node"].get("octoplusOffers", []):
                if offer.get("claimAbility", {}).get("canClaimOffer"):
                    return offer.get("slug")

        return None  # canClaimOffer=False — not available yet, keep polling

    except Exception as e:
        print(f"[{account}] Check error:", e)
        return None


def claim_reward(session, account, slug):
    print(f"[{account}] Attempting claim: {slug}")
    try:
        r = session.post(
            API_BACKEND,
            json={"query": CLAIM_MUTATION, "variables": {"account": account, "slug": slug}},
            timeout=10,
        )
        resp = r.json()
        print(f"[{account}] Claim response:", resp)
        return (
            resp.get("data", {})
            .get("claimOctoplusReward", {})
            .get("success", False)
        )
    except Exception as e:
        print(f"[{account}] Claim error:", e)
        return False


def worker(acc, end_time, on_done):
    api_key = acc["api_key"]
    account = acc["account"]

    if not api_key or not account:
        print("Missing credentials for an account — check your secrets.")
        on_done()
        return

    print(f"[{account}] Worker started.")
    session = make_session(api_key)

    try:
        while time.time() < end_time:
            slug = check_reward(session, account)

            if slug:
                print(f"[{account}] Reward available: {slug}")
                for attempt in range(3):
                    if claim_reward(session, account, slug):
                        print(f"[{account}] ✓ Claimed successfully.")
                        return
                    print(f"[{account}] Attempt {attempt + 1} failed, retrying in 2s...")
                    time.sleep(2)
                print(f"[{account}] All 3 claim attempts failed.")
                return

            # Poll faster as we approach the end of the window
            time.sleep(0.25 if (end_time - time.time()) < 60 else 1)

        # Window expired without a claimable offer
        now = datetime.now(UK_TZ)
        if now.weekday() == 0:
            print(
                f"[{account}] Monday window expired — voucher may be delayed. "
                f"Fallback runs will retry Tue–Thu."
            )
        else:
            print(
                f"[{account}] Fallback window expired. Voucher likely already claimed "
                f"this week or unavailable — will retry next Monday."
            )

    finally:
        on_done()  # Always fires, even on early return


def main():
    now_str = datetime.now(UK_TZ).strftime("%A %d %b %Y %H:%M:%S %Z")
    print(f"Octopus coffee bot started at {now_str}")

    should_run, poll_window = wait_for_5am()
    if not should_run:
        sys.exit(0)

    print("5am UK reached — launching all workers.")
    end_time = get_poll_end_time(poll_window)

    done_counter = [0]
    done_lock = threading.Lock()
    all_done = threading.Event()

    def on_done():
        with done_lock:
            done_counter[0] += 1
            if done_counter[0] >= len(ACCOUNTS):
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

    print(f"Bot finished at {datetime.now(UK_TZ).strftime('%H:%M:%S %Z')}.")


if __name__ == "__main__":
    main()
