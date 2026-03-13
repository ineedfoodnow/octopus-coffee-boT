import requests
import time
import threading
import os

API_BACKEND = "https://api.backend.octopus.energy/v1/graphql/"

ACCOUNTS = [
    {
        "api_key": os.environ.get("OCTO_APIKEY_1"),
        "account": os.environ.get("OCTO_ACC_1")
    },
    {
        "api_key": os.environ.get("OCTO_APIKEY_2"),
        "account": os.environ.get("OCTO_ACC_2")
    }
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


def make_session(api_key):
    s = requests.Session()
    s.auth = (api_key, "")
    return s


def check_reward(session, account):

    try:
        r = session.post(
            API_BACKEND,
            json={"query": CHECK_QUERY, "variables": {"account": account}},
            timeout=10
        )

        data = r.json()

        if not data or "data" not in data or not data["data"]:
            print("Invalid API response:", data)
            return None, False

        groups = data["data"]["octoplusOfferGroups"]

        if not groups:
            print("No offer groups found")
            return None, True

        edges = groups.get("edges", [])

        claimable_slug = None
        any_offer_found = False

        for group in edges:

            offers = group["node"].get("octoplusOffers", [])

            for offer in offers:

                any_offer_found = True

                slug = offer.get("slug")
                can_claim = offer.get("claimAbility", {}).get("canClaimOffer")

                if can_claim:
                    claimable_slug = slug
                    break

            if claimable_slug:
                break

        if claimable_slug:
            return claimable_slug, False

        # offers exist but none claimable → already claimed
        if any_offer_found:
            return None, True

        return None, False

    except Exception as e:
        print("Check error:", e)
        return None, False


def claim_reward(session, account, slug):

    print("Attempting claim:", slug)

    try:

        r = session.post(
            API_BACKEND,
            json={
                "query": CLAIM_MUTATION,
                "variables": {"account": account, "slug": slug}
            },
            timeout=10
        )

        print("Claim response:", r.json())

    except Exception as e:

        print("Claim error:", e)


def worker(acc):

    api_key = acc["api_key"]
    account = acc["account"]

    if not api_key or not account:
        print("Missing credentials")
        return

    print("Starting worker for account:", account)

    session = make_session(api_key)

    end_time = time.time() + 600

    while time.time() < end_time:

        print("Checking rewards:", account)

        slug, already_claimed = check_reward(session, account)

        if slug:

            print("Reward available:", slug)

            claim_reward(session, account, slug)

            print("Claim completed:", account)

            return

        if already_claimed:

            print("Reward already claimed for this period:", account)

            return

        time.sleep(2)

    print("Finished polling window:", account)


def main():

    print("Starting Octopus coffee bot")

    threads = []

    for acc in ACCOUNTS:

        t = threading.Thread(target=worker, args=(acc,))
        t.start()

        threads.append(t)

    for t in threads:
        t.join()

    print("Bot finished")


if __name__ == "__main__":
    main()
