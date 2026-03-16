import requests
import time
import threading
import os
import sys
from zoneinfo import ZoneInfo
from datetime import datetime

API_BACKEND = "https://api.backend.octopus.energy/v1/graphql/"
UK_TZ = ZoneInfo("Europe/London")

# How long to poll after 5am before giving up
POLL_WINDOW_SECONDS = 480   # 8 minutes
# Abort if we start more than this many seconds before 5am (too early = wrong trigger)
MAX_PRE_SLEEP = 600         # 10 minutes
# Abort if we start more than this many seconds after 5am (stale trigger)
MAX_POST_DELAY = POLL_WINDOW_SECONDS

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
    Sleep until 5am UK time if we started slightly early.
    Returns False (and exits) if we're too early or too late.
    """
    now = datetime.now(UK_TZ)
    today_5am = now.replace(hour=5, minute=0, second=0, microsecond=0)
    seconds_until = (today_5am - now).total_seconds()

    if seconds_until > MAX_PRE_SLEEP:
        print(f"Started {seconds_until:.0f}s before 5am UK — too early, aborting to save runner minutes.")
        return False

    if seconds_until < -MAX_POST_DELAY:
        print(f"Started {abs(seconds_until):.0f}s after 5am UK — stale trigger, aborting.")
        return False

    if seconds_until > 0:
        print(f"Sleeping {seconds_until:.1f}s until exactly 5am UK...")
        time.sleep(seconds_until)
    else:
        print(f"Started {abs(seconds_until):.0f}s past 5am UK — polling immediately.")

    return True


def get_poll_end_time():
    """Absolute timestamp 8 minutes after 5am UK today."""
    today_5am = datetime.now(UK_TZ).replace(hour=5, minute=0, second=0, microsecond=0)
    return today_5am.timestamp() + POLL_WINDOW_SECONDS


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

        return None  # Not claimable yet — keep polling

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


def worker(acc, end_time):
    api_key = acc["api_key"]
    account = acc["account"]

    if not api_key or not account:
        print("Missing credentials for an account — check your secrets.")
        return

    print(f"[{account}] Worker started.")
    session = make_session(api_key)

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
            print(f"[{account}] All claim attempts failed.")
            return

        # canClaimOffer=False just means not available yet — keep polling
        time.sleep(0.25 if (end_time - time.time()) < 60 else 1)

    print(f"[{account}] Poll window expired without claiming.")


def main():
    print(f"Octopus coffee bot started at {datetime.now(UK_TZ).strftime('%H:%M:%S %Z')}")

    if not wait_for_5am():
        sys.exit(0)

    print("5am reached — starting all workers.")
    end_time = get_poll_end_time()

    threads = [
        threading.Thread(target=worker, args=(acc, end_time))
        for acc in ACCOUNTS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    print("Bot finished.")


if __name__ == "__main__":
    main()
