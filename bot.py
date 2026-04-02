from __future__ import annotations

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Any
from zoneinfo import ZoneInfo

import httpx

API_AUTH     = "https://api.octopus.energy/v1/graphql/"
API_INTERNAL = "https://api.backend.octopus.energy/v1/graphql/"
UK_TZ        = ZoneInfo("Europe/London")

# Bot runs for up to this long from launch — cron job controls WHEN it runs
POLL_WINDOW_SECONDS  = 30 * 60   # 30-minute safety-net max runtime
BASE_POLL_INTERVAL   = 0.5
CLAIM_RETRIES        = 3
TOKEN_REFRESH_AFTER  = 55 * 60

NERO_KEYWORDS = {"caffe-nero", "caffenero", "nero"}

def is_nero_offer(slug: str) -> bool:
    return any(keyword in slug.lower() for keyword in NERO_KEYWORDS)


# ── Logging — always show UK/BST time ────────────────────────────────────────
class UKFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=UK_TZ)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(UKFormatter(fmt="%(asctime)s  %(levelname)-8s  %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
log = logging.getLogger("octopus-bot")
# ─────────────────────────────────────────────────────────────────────────────


GQL_OBTAIN_TOKEN = """
mutation ObtainToken($apiKey: String!) {
  obtainKrakenToken(input: { APIKey: $apiKey }) {
    token
    refreshToken
  }
}
"""

GQL_REFRESH_TOKEN = """
mutation RefreshToken($refreshToken: String!) {
  obtainKrakenToken(input: { refreshToken: $refreshToken }) {
    token
    refreshToken
  }
}
"""

GQL_CHECK = """
query Offers($account: String!) {
  octoplusOfferGroups(accountNumber: $account, first: 10) {
    edges {
      node {
        octoplusOffers {
          slug
          claimAbility {
            canClaimOffer
            cannotClaimReason
          }
        }
      }
    }
  }
}
"""

GQL_CLAIM = """
mutation Claim($account: String!, $slug: String!) {
  claimOctoplusReward(accountNumber: $account, offerSlug: $slug) {
    rewardId
  }
}
"""


class CheckState(Enum):
    CLAIMABLE       = auto()
    WAIT            = auto()
    ALREADY_CLAIMED = auto()
    OUT_OF_STOCK    = auto()   # Codes genuinely exhausted — stop polling
    NOT_ENROLLED    = auto()
    NO_GROUPS       = auto()
    API_ERROR       = auto()


class WorkerResult(Enum):
    CLAIMED         = auto()
    ALREADY_CLAIMED = auto()
    OUT_OF_STOCK    = auto()
    NOT_ENROLLED    = auto()
    NO_GROUPS       = auto()
    WINDOW_EXPIRED  = auto()
    MISSING_CREDS   = auto()
    TOKEN_FAILED    = auto()
    CLAIM_FAILED    = auto()
    API_ERROR       = auto()


RESULT_LABEL = {
    WorkerResult.CLAIMED:         "✅  Claimed Caffe Nero successfully",
    WorkerResult.ALREADY_CLAIMED: "⏸   Already claimed this week",
    WorkerResult.OUT_OF_STOCK:    "🚫  Out of stock — codes exhausted",
    WorkerResult.NOT_ENROLLED:    "❌  Not enrolled in Octoplus",
    WorkerResult.NO_GROUPS:       "⚠️   No Octoplus offer groups returned",
    WorkerResult.WINDOW_EXPIRED:  "⏸   Max runtime reached — no Nero offer found",
    WorkerResult.MISSING_CREDS:   "❌  Missing credentials",
    WorkerResult.TOKEN_FAILED:    "❌  Token exchange failed",
    WorkerResult.CLAIM_FAILED:    "❌  Claim failed after retries",
    WorkerResult.API_ERROR:       "❌  API error",
}


@dataclass(frozen=True)
class Account:
    label: str
    api_key: str
    account_number: str

    @property
    def is_valid(self) -> bool:
        return bool(self.api_key and self.account_number)

    @property
    def masked(self) -> str:
        return (self.account_number[:4] + "••••••") if self.account_number else "── NOT SET ──"


@dataclass
class CheckOutcome:
    state: CheckState
    slug: str | None   = None
    reason: str | None = None
    code: str | None   = None


@dataclass
class TokenManager:
    account: Account
    client: httpx.AsyncClient
    token: str | None         = field(default=None, init=False)
    refresh_token: str | None = field(default=None, init=False)
    acquired_monotonic: float = field(default=0.0,  init=False)

    async def get_valid_token(self) -> str | None:
        age = time.monotonic() - self.acquired_monotonic
        if self.token and age < TOKEN_REFRESH_AFTER:
            return self.token
        if self.refresh_token:
            token = await self._refresh()
            if token:
                return token
        return await self._authenticate()

    async def _authenticate(self) -> str | None:
        return await self._exchange(GQL_OBTAIN_TOKEN, {"apiKey": self.account.api_key}, "API key auth")

    async def _refresh(self) -> str | None:
        return await self._exchange(GQL_REFRESH_TOKEN, {"refreshToken": self.refresh_token}, "token refresh")

    async def _exchange(self, query: str, variables: dict[str, Any], action: str) -> str | None:
        try:
            r = await self.client.post(
                API_AUTH,
                json={"query": query, "variables": variables},
            )
            r.raise_for_status()
            payload    = r.json()
            token_data = (payload.get("data") or {}).get("obtainKrakenToken") or {}
            token      = token_data.get("token")
            if token:
                self.token              = token
                self.refresh_token      = token_data.get("refreshToken")
                self.acquired_monotonic = time.monotonic()
                log.info("[%s] Auth OK via %s", self.account.label, action)
                return token
            log.error("[%s] No token during %s: %s", self.account.label, action, payload.get("errors"))
            return None
        except httpx.HTTPStatusError as exc:
            log.error("[%s] Auth HTTP %s during %s", self.account.label, exc.response.status_code, action)
            return None
        except Exception as exc:
            log.error("[%s] Auth error during %s: %s", self.account.label, action, exc)
            return None


def load_accounts() -> list[Account]:
    accounts: list[Account] = []
    i = 1
    while True:
        api_key = os.environ.get(f"OCTO_APIKEY_{i}", "").strip()
        account = os.environ.get(f"OCTO_ACC_{i}", "").strip()
        if not api_key and not account:
            break
        accounts.append(Account(label=f"Account {i}", api_key=api_key, account_number=account))
        i += 1
    return accounts


def interpret_reason(reason: str | None) -> CheckState | None:
    if not reason:
        return None
    if reason == "MAX_CLAIMS_PER_PERIOD_REACHED":
        return CheckState.ALREADY_CLAIMED
    if reason == "OUT_OF_STOCK":
        # Codes genuinely exhausted for this week — no point polling further
        return CheckState.OUT_OF_STOCK
    return None


async def check_reward(
    client: httpx.AsyncClient,
    token: str,
    account: Account,
    verbose: bool = False,
) -> CheckOutcome:
    try:
        r = await client.post(
            API_INTERNAL,
            headers={"Authorization": token},
            json={"query": GQL_CHECK, "variables": {"account": account.account_number}},
        )
        r.raise_for_status()
        payload = r.json()
        errors  = payload.get("errors") or []

        if errors:
            first = errors[0]
            code  = (first.get("extensions") or {}).get("errorCode")
            msg   = first.get("message", "")

            if code == "KT-GB-9319":
                if verbose:
                    log.info("[%s] Offers not available yet (%s)", account.label, code)
                return CheckOutcome(state=CheckState.WAIT, code=code, reason=msg)

            if code == "KT-GB-9316":
                log.warning("[%s] Not enrolled in Octoplus (%s)", account.label, code)
                return CheckOutcome(state=CheckState.NOT_ENROLLED, code=code, reason=msg)

            log.warning("[%s] API error %s: %s", account.label, code, msg)
            return CheckOutcome(state=CheckState.API_ERROR, code=code, reason=msg)

        groups = (payload.get("data") or {}).get("octoplusOfferGroups") or {}
        edges  = groups.get("edges") or []

        if verbose:
            log.info("[%s] %d offer group(s) returned", account.label, len(edges))

        if not edges:
            log.warning("[%s] No offer groups returned", account.label)
            return CheckOutcome(state=CheckState.NO_GROUPS)

        for group in edges:
            for offer in group.get("node", {}).get("octoplusOffers") or []:
                slug      = offer.get("slug", "unknown")
                ability   = offer.get("claimAbility") or {}
                can_claim = bool(ability.get("canClaimOffer"))
                reason    = ability.get("cannotClaimReason")

                if not is_nero_offer(slug):
                    if verbose:
                        log.info("[%s] slug=%-40s ⏭  Skipping — not a Nero offer", account.label, slug)
                    continue

                if verbose:
                    if can_claim:
                        log.info("[%s] slug=%-40s ✅ CLAIMABLE", account.label, slug)
                    else:
                        log.info("[%s] slug=%-40s ⏸  %s", account.label, slug, reason or "not claimable")

                if can_claim:
                    return CheckOutcome(state=CheckState.CLAIMABLE, slug=slug)

                inferred = interpret_reason(reason)
                if inferred == CheckState.ALREADY_CLAIMED:
                    log.info("[%s] Already claimed this week (%s)", account.label, reason)
                    return CheckOutcome(state=CheckState.ALREADY_CLAIMED, reason=reason)
                if inferred == CheckState.OUT_OF_STOCK:
                    log.info("[%s] Nero offer is out of stock (%s) — stopping", account.label, reason)
                    return CheckOutcome(state=CheckState.OUT_OF_STOCK, reason=reason)

        return CheckOutcome(state=CheckState.WAIT)

    except httpx.HTTPStatusError as exc:
        log.warning("[%s] Check HTTP %s", account.label, exc.response.status_code)
        return CheckOutcome(state=CheckState.API_ERROR, reason=f"HTTP {exc.response.status_code}")
    except Exception as exc:
        log.warning("[%s] Check error: %s", account.label, exc)
        return CheckOutcome(state=CheckState.API_ERROR, reason=str(exc))


async def claim_reward(
    client: httpx.AsyncClient,
    token: str,
    account: Account,
    slug: str,
) -> bool:
    for attempt in range(1, CLAIM_RETRIES + 1):
        try:
            log.info("[%s] Claim attempt %d/%d slug=%s", account.label, attempt, CLAIM_RETRIES, slug)
            r = await client.post(
                API_INTERNAL,
                headers={"Authorization": token},
                json={
                    "query": GQL_CLAIM,
                    "variables": {"account": account.account_number, "slug": slug},
                },
            )
            r.raise_for_status()
            payload   = r.json()
            reward_id = ((payload.get("data") or {}).get("claimOctoplusReward") or {}).get("rewardId")

            if reward_id:
                log.info("[%s] Claimed! Reward ID: %s", account.label, reward_id)
                return True

            errors = payload.get("errors") or []
            if errors:
                msg  = errors[0].get("message", str(payload))
                code = (errors[0].get("extensions") or {}).get("errorCode")
                log.warning("[%s] Claim rejected: %s (%s)", account.label, msg, code)
            else:
                log.warning("[%s] No rewardId in response: %s", account.label, payload)

        except httpx.HTTPStatusError as exc:
            log.warning("[%s] Claim HTTP %s", account.label, exc.response.status_code)
        except Exception as exc:
            log.warning("[%s] Claim error: %s", account.label, exc)

        if attempt < CLAIM_RETRIES:
            backoff = 2 ** (attempt - 1)
            log.info("[%s] Retrying in %ds...", account.label, backoff)
            await asyncio.sleep(backoff)

    return False


async def worker(account: Account, client: httpx.AsyncClient, end_ts: float) -> WorkerResult:
    log.info("[%s] Worker started (%s)", account.label, account.masked)

    if not account.is_valid:
        log.error("[%s] Missing credentials", account.label)
        return WorkerResult.MISSING_CREDS

    tm    = TokenManager(account=account, client=client)
    token = await tm.get_valid_token()
    if not token:
        return WorkerResult.TOKEN_FAILED

    polls           = 0
    start_monotonic = time.monotonic()

    while time.time() < end_ts:
        token = await tm.get_valid_token()
        if not token:
            return WorkerResult.TOKEN_FAILED

        verbose = polls == 0 or polls % max(1, int(120 / BASE_POLL_INTERVAL)) == 0
        outcome = await check_reward(client, token, account, verbose=verbose)
        polls  += 1

        if outcome.state == CheckState.CLAIMABLE and outcome.slug:
            elapsed = time.monotonic() - start_monotonic
            log.info("[%s] 🎟  Nero offer found after %d polls (%.1fs): %s",
                     account.label, polls, elapsed, outcome.slug)

            token = await tm.get_valid_token()
            if not token:
                return WorkerResult.TOKEN_FAILED

            if await claim_reward(client, token, account, outcome.slug):
                log.info("[%s] 🎉 Done in %.1fs after %d polls",
                         account.label, time.monotonic() - start_monotonic, polls)
                return WorkerResult.CLAIMED
            return WorkerResult.CLAIM_FAILED

        if outcome.state == CheckState.ALREADY_CLAIMED:
            return WorkerResult.ALREADY_CLAIMED

        if outcome.state == CheckState.OUT_OF_STOCK:
            return WorkerResult.OUT_OF_STOCK

        if outcome.state == CheckState.NOT_ENROLLED:
            return WorkerResult.NOT_ENROLLED

        if outcome.state == CheckState.NO_GROUPS:
            return WorkerResult.NO_GROUPS

        await asyncio.sleep(BASE_POLL_INTERVAL)

    log.info("[%s] Max runtime reached after %d polls", account.label, polls)
    return WorkerResult.WINDOW_EXPIRED


def print_summary(accounts: list[Account], results: list[WorkerResult]) -> None:
    w = 60
    print("\n" + "═" * w)
    print(f"{'SUMMARY':^{w}}")
    print("═" * w)
    for account, result in zip(accounts, results):
        print(f"  {account.label:<14} {RESULT_LABEL[result]}")
    print("═" * w + "\n")


def resolve_exit_code(results: list[WorkerResult]) -> int:
    failures = {WorkerResult.MISSING_CREDS, WorkerResult.TOKEN_FAILED,
                WorkerResult.CLAIM_FAILED, WorkerResult.API_ERROR}
    count = sum(1 for r in results if r in failures)
    if count == 0:
        return 0
    if count == len(results):
        return 3
    return 2


async def async_main() -> int:
    now = datetime.now(UK_TZ)
    print("════════════════════════════════════════════════════════════")
    print("  OCTOPUS COFFEE BOT · PRODUCTION")
    print(f"  {now.strftime('%A %d %b %Y %H:%M:%S %Z')}")
    print(f"  Max runtime: {POLL_WINDOW_SECONDS // 60} minutes")
    print("════════════════════════════════════════════════════════════")

    accounts = load_accounts()
    if not accounts:
        log.error("No accounts found — set OCTO_APIKEY_1/OCTO_ACC_1 etc.")
        return 1

    log.info("Loaded %d account(s)", len(accounts))

    # No time-of-day check — start polling immediately
    end_ts = time.time() + POLL_WINDOW_SECONDS
    log.info("Polling until %s",
             datetime.fromtimestamp(end_ts, tz=UK_TZ).strftime("%H:%M:%S %Z"))

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5, read=10, write=10, pool=5),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ) as client:
        tasks       = [asyncio.create_task(worker(acc, client, end_ts), name=acc.label) for acc in accounts]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[WorkerResult] = []
    for acc, res in zip(accounts, raw_results):
        if isinstance(res, BaseException):
            log.error("[%s] Unhandled exception: %s", acc.label, res, exc_info=res)
            results.append(WorkerResult.CLAIM_FAILED)
        else:
            results.append(res)

    print_summary(accounts, results)
    code = resolve_exit_code(results)
    log.info("Exit code %d", code)
    return code


def main() -> None:
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
