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

# Codes are released at 5am but claimable from ~5:02am
# Bot wakes at 4:55am (cron-job.org), sleeps until 5:01:50am,
# then polls hard until 5:20am or all accounts claimed
TARGET_HOUR    = 5
TARGET_MIN     = 2
TARGET_BUFFER  = 10     # seconds before 5:02 to wake and start polling
POLL_WINDOW    = 20 * 60  # hard cutoff: 20 min from 5:00am = 5:20am
MAX_PRE_SLEEP  = 600    # abort if >10 min before target (wrong cron season)
POLL_INTERVAL  = 0.5    # poll every 500ms — fast enough without hammering API
CLAIM_RETRIES  = 3
TOKEN_LIFETIME = 55 * 60  # refresh token at 55 min (expires at 60 min)

FORCE_RUN = os.environ.get("FORCE_RUN", "false").lower() == "true"

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

# ─────────────────────────────────────────────
#  GRAPHQL
# ─────────────────────────────────────────────
GQL_OBTAIN_TOKEN = """
mutation ObtainToken($apiKey: String!){
  obtainKrakenToken(input: { APIKey: $apiKey }){
    token
    refreshToken
  }
}
"""

GQL_REFRESH_TOKEN = """
mutation RefreshToken($refresh: String!){
  obtainKrakenToken(input: { refreshToken: $refresh }){
    token
    refreshToken
  }
}
"""

GQL_CHECK = """
query Offers($account:String!){
  octoplusOfferGroups(accountNumber:$account, first:10){
    edges{
      node{
        octoplusOffers{
          slug
          claimAbility{
            canClaimOffer
            reasonCantClaim
          }
        }
      }
    }
  }
}
"""

GQL_CLAIM = """
mutation Claim($account:String!,$slug:String!){
  claimOctoplusReward(accountNumber:$account,offerSlug:$slug){
    success
  }
}
"""

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
_print_lock = threading.Lock()

def safe_print(*a, **k):
    with _print_lock:
        print(*a, **k)

def header(title):
    w = 56
    safe_print("═" * w)
    pad = (w - len(title) - 2) // 2
    safe_print(f"{'═'*pad} {title} {'═'*(w-pad-len(title)-2)}")
    safe_print("═" * w)

def section(title):
    safe_print()
    safe_print(f"  ┌─ {title}")

def log(icon, label, msg, indent=2):
    safe_print(f"  │  {'  '*indent}{icon} {label}: {msg}")

def close_section():
    safe_print("  └" + "─" * 54)

def acc_header(label, masked):
    safe_print("  │")
    safe_print(f"  │  ▸ {label}  ({masked})")

def mask(val):
    return (val[:4] + "••••••") if val else "── NOT SET ──"

def ts():
    return datetime.now(UK_TZ).strftime("%H:%M:%S")

# ─────────────────────────────────────────────
#  API CLIENT
#  Handles auth, token refresh, and all requests
#  for a single account
# ─────────────────────────────────────────────
class OctopusClient:

    def __init__(self, api_key, account, label):
        self.api_key       = api_key
        self.account       = account
        self.label         = label
        self._session      = requests.Session()
        self._token        = None
        self._refresh      = None
        self._token_issued = 0.0

    def _gql(self, query, variables=None, auth=True):
        headers = {}
        if auth and self._token:
            headers["Authorization"] = self._token
        r = self._session.post(
            API_BACKEND,
            headers=headers,
            json={"query": query, "variables": variables or {}},
            timeout=10,
        )
        return r.json()

    def _parse_token_response(self, data):
        tok = (data.get("data") or {}).get("obtainKrakenToken") or {}
        self._token        = tok.get("token")
        self._refresh      = tok.get("refreshToken")
        self._token_issued = time.time()
        return bool(self._token)

    def authenticate(self):
        """Exchange API key for token. Returns True on success."""
        try:
            data = self._gql(
                GQL_OBTAIN_TOKEN,
                {"apiKey": self.api_key},
                auth=False,
            )
            if self._parse_token_response(data):
                log("✅", "Auth", f"Token obtained at {ts()}", indent=3)
                return True
            log("❌", "Auth", f"No token in response: {data.get('errors')}", indent=3)
            return False
        except Exception as e:
            log("❌", "Auth", str(e), indent=3)
            return False

    def refresh_token(self):
        """Use refresh token to get a new access token silently."""
        if not self._refresh:
            return self.authenticate()
        try:
            data = self._gql(
                GQL_REFRESH_TOKEN,
                {"refresh": self._refresh},
                auth=False,
            )
            if self._parse_token_response(data):
                log("🔄", "Token", f"Refreshed at {ts()}", indent=3)
                return True
            return self.authenticate()
        except Exception:
            return self.authenticate()

    def ensure_token(self):
        """Refresh if token is approaching expiry."""
        if time.time() - self._token_issued > TOKEN_LIFETIME:
            return self.refresh_token()
        return True

    def get_claimable_slug(self, verbose=False):
        """
        Returns:
          str   — slug ready to claim
          None  — no claimable offer yet (keep polling)
          False — permanent stop (already claimed / not enrolled)
        """
        try:
            data   = self._gql(GQL_CHECK, {"account": self.account})
            errors = data.get("errors")

            if errors:
                code = errors[0].get("extensions", {}).get("errorCode", "")
                msg  = errors[0].get("message", "")

                if code == "KT-GB-9319":
                    if verbose:
                        log("⏳", "API", f"Offers not available yet ({code})", indent=3)
                    return None

                if code == "KT-GB-9316":
                    log("❌", "API", "Account not enrolled in Octoplus", indent=3)
                    return False

                log("⚠️ ", "API error", f"{msg} ({code})", indent=3)
                return None

            groups = (data.get("data") or {}).get("octoplusOfferGroups") or {}
            edges  = groups.get("edges") or []

            if verbose:
                log("🔍", "Offers", f"{len(edges)} group(s) returned at {ts()}", indent=3)

            if not edges:
                log("⚠️ ", "Offers", "No offer groups — not on Octoplus?", indent=3)
                return False

            for group in edges:
                for offer in group["node"].get("octoplusOffers") or []:
                    slug      = offer.get("slug", "unknown")
                    claimable = offer.get("claimAbility", {}).get("canClaimOffer", False)
                    reason    = offer.get("claimAbility", {}).get("reasonCantClaim") or ""

                    if verbose:
                        if claimable:
                            log("🎟 ", slug, "✅ CLAIMABLE", indent=3)
                        else:
                            log("🎟 ", slug,
                                f"⏸  Not claimable — {reason or 'no reason given'}",
                                indent=3)

                    # Exit early if already claimed — no point polling further
                    if not claimable and reason and "claim" in reason.lower():
                        log("ℹ️ ", "Already claimed", reason, indent=3)
                        return False

                    if claimable:
                        return slug

            return None  # Offers exist but none claimable yet

        except Exception as e:
            log("⚠️ ", "Check error", str(e), indent=3)
            return None

    def claim(self, slug):
        """Returns True on success."""
        try:
            data    = self._gql(GQL_CLAIM, {"account": self.account, "slug": slug})
            success = ((data.get("data") or {})
                       .get("claimOctoplusReward", {})
                       .get("success", False))
            if not success:
                errors = data.get("errors")
                msg    = errors[0].get("message", str(data)) if errors else str(data)
                code   = (errors[0].get("extensions") or {}).get("errorCode", "") if errors else ""
                log("❌", "Claim failed", f"{msg} ({code})", indent=3)
            return success
        except Exception as e:
            log("❌", "Claim error", str(e), indent=3)
            return False

# ─────────────────────────────────────────────
#  TIMING
# ─────────────────────────────────────────────
def get_target_and_end():
    """
    Returns (target_ts, end_ts) where:
      target_ts = today at 5:02am UK (when codes become claimable)
      end_ts    = today at 5:20am UK (hard cutoff)
    """
    now   = datetime.now(UK_TZ)
    base  = now.replace(hour=TARGET_HOUR, minute=0,          second=0, microsecond=0)
    target = now.replace(hour=TARGET_HOUR, minute=TARGET_MIN, second=0, microsecond=0)
    end   = base.timestamp() + POLL_WINDOW
    return target.timestamp(), end


def wait_for_target():
    section("Schedule")
    now            = datetime.now(UK_TZ)
    target_ts, end = get_target_and_end()
    secs_to_target = target_ts - now.timestamp()
    secs_to_end    = end - now.timestamp()

    log("🌍", "UK time",    now.strftime("%A %d %b %Y  %H:%M:%S %Z"))
    log("🎯", "Target",     f"5:0{TARGET_MIN}am UK (codes claimable)")
    log("⏱ ", "Hard cutoff", "5:20am UK")

    if FORCE_RUN:
        log("⚡", "FORCE_RUN", "Time guard bypassed — 2 min window")
        close_section()
        return True, now.timestamp() + 2 * 60

    # Already past hard cutoff
    if secs_to_end <= 0:
        log("⏰", "Too late", f"Past 5:20am — stale trigger, aborting")
        close_section()
        return False, 0

    # Too far before target (wrong-season cron fired)
    if secs_to_target > MAX_PRE_SLEEP:
        log("⏳", "Too early",
            f"{secs_to_target:.0f}s before {TARGET_HOUR}:0{TARGET_MIN}am — aborting")
        close_section()
        return False, 0

    # Sleep until TARGET_BUFFER seconds before target
    wake_at = target_ts - TARGET_BUFFER
    sleep_s = wake_at - now.timestamp()

    if sleep_s > 0:
        log("😴", "Sleeping",
            f"{sleep_s:.1f}s → waking at 5:0{TARGET_MIN - 0}am - {TARGET_BUFFER}s")
        close_section()
        time.sleep(sleep_s)
    else:
        log("✅", "On time",
            f"Already within poll window at {ts()}")
        close_section()

    return True, end

# ─────────────────────────────────────────────
#  WORKER
# ─────────────────────────────────────────────
def worker(acc, end_time, on_done, summary):
    label   = acc["label"]
    api_key = acc["api_key"]
    account = acc["account"]

    acc_header(label, mask(account))

    if not api_key or not account:
        log("❌", "Credentials", "Missing — check secrets", indent=3)
        summary[label] = "❌  Missing credentials"
        on_done()
        return

    client = OctopusClient(api_key, account, label)

    log("🔑", "Auth", "Exchanging API key for token...", indent=3)
    if not client.authenticate():
        summary[label] = "❌  Token exchange failed"
        on_done()
        return

    poll_count = 0

    try:
        while time.time() < end_time:
            client.ensure_token()

            # Log verbosely on first poll and every 2 minutes thereafter
            verbose = (poll_count == 0 or poll_count % (120 // POLL_INTERVAL) == 0)
            result  = client.get_claimable_slug(verbose=verbose)
            poll_count += 1

            if result is False:
                # Permanent stop — already claimed or not enrolled
                summary[label] = "⏸   Already claimed this week or not enrolled"
                return

            if result:
                slug = result
                log("🎟 ", "Offer found", f"{slug} at {ts()}", indent=3)
                for attempt in range(1, CLAIM_RETRIES + 1):
                    if client.claim(slug):
                        log("✅", "Claimed",
                            f"Successfully at {ts()} 🎉", indent=3)
                        summary[label] = "✅  Claimed successfully"
                        return
                    log("⚠️ ", f"Attempt {attempt}", "Failed — retrying in 2s", indent=3)
                    time.sleep(2)
                log("❌", "Claim", f"All {CLAIM_RETRIES} attempts failed", indent=3)
                summary[label] = "❌  All claim attempts failed"
                return

            time.sleep(POLL_INTERVAL)

        remaining = max(0, end_time - time.time())
        log("⏰", "Expired",
            f"20 min window closed at {ts()} — no claimable offer found", indent=3)
        summary[label] = "⏸   Window expired — no claimable offer found"

    finally:
        on_done()

# ─────────────────────────────────────────────
#  SUMMARY
# ─────────────────────────────────────────────
def print_summary(summary):
    section("Summary")
    for acc in ACCOUNTS:
        label = acc["label"]
        safe_print(f"  │    {label}: {summary.get(label, '⏸  No result recorded')}")
    close_section()

# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
def main():
    header("OCTOPUS COFFEE BOT  ·  PRODUCTION")
    safe_print(f"  Started : {datetime.now(UK_TZ).strftime('%A %d %b %Y  %H:%M:%S %Z')}")
    safe_print(f"  Mode    : {'⚡ FORCE_RUN (manual)' if FORCE_RUN else '🕐 Scheduled run'}")

    should_run, end_time = wait_for_target()
    if not should_run:
        sys.exit(0)

    section("Workers")

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

    all_done.wait(timeout=POLL_WINDOW + 60)
    for t in threads:
        t.join(timeout=5)

    close_section()
    print_summary(summary)
    header(f"FINISHED  ·  {datetime.now(UK_TZ).strftime('%H:%M:%S %Z')}")


if __name__ == "__main__":
    main()
