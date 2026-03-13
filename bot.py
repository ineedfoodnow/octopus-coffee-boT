import requests
import time
import threading
import os

print("EMAIL1:", os.environ.get("OCTO_EMAIL_1"))
print("ACC1:", os.environ.get("OCTO_ACC_1"))

API = "https://api.backend.octopus.energy/v1/graphql/"

ACCOUNTS = [
    {
        "email": os.environ.get("OCTO_EMAIL_1"),
        "password": os.environ.get("OCTO_PASS_1"),
        "account": os.environ.get("OCTO_ACC_1")
    },
    {
        "email": os.environ.get("OCTO_EMAIL_2"),
        "password": os.environ.get("OCTO_PASS_2"),
        "account": os.environ.get("OCTO_ACC_2")
    }
]

LOGIN_MUTATION = """
mutation Token($email:String!,$password:String!){
  obtainKrakenToken(input:{email:$email,password:$password}){
    token
  }
}
"""

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


def login(session, email, password):

    print("Logging in:", email)

    try:
        r = session.post(
            API,
            json={
                "query": LOGIN_MUTATION,
                "variables": {
                    "email": email,
                    "password": password
                }
            }
        )

        data = r.json()

        if "errors" in data:
            print("Login error:", data)
            return False

        token = data["data"]["obtainKrakenToken"]["token"]

        session.headers.update({
            "Authorization": f"JWT {token}"
        })

        print("Login success:", email)

        return True

    except Exception as e:
        print("Login exception:", e)
        return False


def check_reward(session, account):

    try:
        r = session.post(
            API,
            json={
                "query": CHECK_QUERY,
                "variables": {
                    "account": account
                }
            }
        )

        data = r.json()

        for group in data["data"]["octoplusOfferGroups"]["edges"]:
            for offer in group["node"]["octoplusOffers"]:

                if offer["claimAbility"]["canClaimOffer"]:
                    return offer["slug"]

        return None

    except Exception as e:
        print("Check error:", e)
        return None


def claim_reward(session, account, slug):

    print("Attempting claim:", slug)

    try:
        r = session.post(
            API,
            json={
                "query": CLAIM_MUTATION,
                "variables": {
                    "account": account,
                    "slug": slug
                }
            }
        )

        print("Claim response:", r.json())

    except Exception as e:
        print("Claim error:", e)


def worker(acc):

    email = acc["email"]
    password = acc["password"]
    account = acc["account"]

    if not email or not password or not account:
        print("Missing credentials for account")
        return

    print("Starting worker for:", email)

    session = requests.Session()

    success = login(session, email, password)

    if not success:
        print("Login failed:", email)
        return

    end_time = time.time() + 600

    while time.time() < end_time:

        print("Checking rewards:", email)

        slug = check_reward(session, account)

        if slug:
            print("Reward available:", slug)

            claim_reward(session, account, slug)

            print("Claim completed:", email)

            return

        time.sleep(2)

    print("Finished polling window:", email)


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
