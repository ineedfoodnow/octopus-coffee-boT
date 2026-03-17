from __future__ import annotations

import asyncio
import logging
import os
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

CLAIM_RETRIES           = 2
TOKEN_REFRESH_AFTER     = 55 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("octopus-test-bot")

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
    NOT_ENROLLED    = auto()
    NO_GROUPS       = auto()
    API_ERROR       = auto()


class TestResult(Enum):
    CLAIMED             = auto()
    ALREADY_CLAIMED     = auto()
    NOT_ENROLLED        = auto()
    NO_GROUPS           = auto()
    NO_CLAIMABLE_OFFER  = auto()
    MISSING_CREDS       = auto()
    TOKEN_FAILED        = auto()
    CLAIM_FAILED        = auto()
    API_ERROR           = auto()


RESULT_LABEL = {
    TestResult.CLAIMED:            "✅  Claimed successfully",
    TestResult.ALREADY_CLAIMED:    "⏸   Already claimed this week",
    TestResult.NOT_ENROLLED:       "❌  Not enrolled in Octoplus",
    TestResult.NO_GROUPS:          "⚠️   No Octoplus offer groups returned",
    TestResult.NO_CLAIMABLE_OFFER: "⏸   No claimable offer right now — expected outside 5am window",
    TestResult.MISSING_CREDS:      "❌  Missing credentials",
    TestResult.TOKEN_FAILED:       "❌  Token exchange failed",
    TestResult.CLAIM_FAILED:       "❌  Claim failed after retries",
    TestResult.API_ERROR:          "❌  API error",
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
    token: str | None          = field(default=None, init=False)
    refresh_token: str | None  = field(default=None, init=False)
    acquired_monotonic: float  = field(default=0.0,  init=False)

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
        return None  # Keep polling — codes not dropped yet
    return None


async def check_reward(
    client: httpx.AsyncClient,
    token: str,
    account: Account,
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
                log.info("[%s] Offers not available yet (%s)", account.label, code)
                return CheckOutcome(state=CheckState.WAIT, code=code, reason=msg)

            if code == "KT-GB-9316":
                log.warning("[%s] Not enrolled in Octoplus (%s)", account.label, code)
                return CheckOutcome(state=CheckState.NOT_ENROLLED, code=code, reason=msg)

            log.warning("[%s] API error %s: %s", account.label, code, msg)
            return CheckOutcome(state=CheckState.API_ERROR, code=code, reason=msg)

        groups = (payload.get("data") or {}).get("octoplusOfferGroups") or {}
        edges  = groups.get("edges") or []

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

                if can_claim:
                    log.info("[%s] slug=%-40s ✅ CLAIMABLE", account.label, slug)
                    return CheckOutcome(state=CheckState.CLAIMABLE, slug=slug)

                log.info("[%s] slug=%-40s ⏸  %s", account.label, slug, reason or "not claimable")

                inferred = interpret_reason(reason)
                if inferred == CheckState.ALREADY_CLAIMED:
                    log.info("[%s] Already claimed this week (%s)", account.label, reason)
                    return CheckOutcome(state=CheckState.ALREADY_CLAIMED, reason=reason)

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
            await asyncio.sleep(1)

    return False


async def inspect_account(account: Account, client: httpx.AsyncClient) -> TestResult:
    log.info("[%s] Inspecting %s", account.label, account.masked)

    if not account.is_valid:
        log.error("[%s] Missing credentials", account.label)
        return TestResult.MISSING_CREDS

    tm    = TokenManager(account=account, client=client)
    token = await tm.get_valid_token()
    if not token:
        return TestResult.TOKEN_FAILED

    outcome = await check_reward(client, token, account)

    if outcome.state == CheckState.CLAIMABLE and outcome.slug:
        claimed = await claim_reward(client, token, account, outcome.slug)
        return TestResult.CLAIMED if claimed else TestResult.CLAIM_FAILED

    if outcome.state == CheckState.ALREADY_CLAIMED:
        return TestResult.ALREADY_CLAIMED

    if outcome.state == CheckState.NOT_ENROLLED:
        return TestResult.NOT_ENROLLED

    if outcome.state == CheckState.NO_GROUPS:
        return TestResult.NO_GROUPS

    if outcome.state == CheckState.API_ERROR:
        return TestResult.API_ERROR

    return TestResult.NO_CLAIMABLE_OFFER


def print_summary(accounts: list[Account], results: list[TestResult]) -> None:
    w = 60
    print("\n" + "═" * w)
    print(f"{'TEST SUMMARY':^{w}}")
    print("═" * w)
    for account, result in zip(accounts, results):
        print(f"  {account.label:<14} {RESULT_LABEL[result]}")
    print("═" * w + "\n")


async def async_main() -> int:
    print("════════════════════════════════════════════════════════════")
    print("  OCTOPUS COFFEE BOT · TEST")
    print(f"  {datetime.now(UK_TZ).strftime('%A %d %b %Y %H:%M:%S %Z')}")
    print("════════════════════════════════════════════════════════════")

    accounts = load_accounts()
    if not accounts:
        log.error("No accounts found — set OCTO_APIKEY_1/OCTO_ACC_1 etc.")
        return 1

    log.info("Loaded %d account(s)", len(accounts))

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5, read=10, write=10, pool=5),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ) as client:
        tasks       = [asyncio.create_task(inspect_account(acc, client)) for acc in accounts]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[TestResult] = []
    for acc, res in zip(accounts, raw_results):
        if isinstance(res, BaseException):
            log.error("[%s] Unhandled exception: %s", acc.label, res, exc_info=res)
            results.append(TestResult.API_ERROR)
        else:
            results.append(res)

    print_summary(accounts, results)
    return 0


def main() -> None:
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
