import requests
import time
import threading
import os

API = "https://api.backend.octopus.energy/v1/graphql/"

ACCOUNTS = [
    {
        "email": os.environ["OCTO_EMAIL_1"],
        "password": os.environ["OCTO_PASS_1"],
        "account": os.environ["OCTO_ACC_1"]
    },
    {
        "email": os.environ["OCTO_EMAIL_2"],
        "password": os.environ["OCTO_PASS_2"],
        "account": os.environ["OCTO_ACC_2"]
    }
]

LOGIN = """
mutation Token($email:String!,$password:String!){
  obtainKrakenToken(input:{email:$email,password:$password}){
    token
  }
}
"""

CHECK = """
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

CLAIM = """
mutation Claim($account:String!,$slug:String!){
  claimOctoplusReward(accountNumber:$account,offerSlug:$slug){
    success
  }
}
"""


def login(session,email,password):

    r=session.post(API,json={
        "query":LOGIN,
        "variables":{"email":email,"password":password}
    })

    token=r.json()["data"]["obtainKrakenToken"]["token"]

    session.headers.update({"Authorization":f"JWT {token}"})


def check(session,account):

    r=session.post(API,json={
        "query":CHECK,
        "variables":{"account":account}
    })

    data=r.json()

    for g in data["data"]["octoplusOfferGroups"]["edges"]:
        for offer in g["node"]["octoplusOffers"]:
            if offer["claimAbility"]["canClaimOffer"]:
                return offer["slug"]

    return None


def claim(session,account,slug):

    session.post(API,json={
        "query":CLAIM,
        "variables":{
            "account":account,
            "slug":slug
        }
    })


def worker(acc):

    session=requests.Session()

    login(session,acc["email"],acc["password"])

    end=time.time()+600

    while time.time()<end:

        slug=check(session,acc["account"])

        if slug:
            claim(session,acc["account"],slug)
            print("Claimed",acc["email"])
            return

        time.sleep(2)


threads=[]

for acc in ACCOUNTS:

    t=threading.Thread(target=worker,args=(acc,))
    t.start()
    threads.append(t)

for t in threads:
    t.join()
