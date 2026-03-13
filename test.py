import requests
import json

email = "pinogaglianone@gmail.com"
password = "pettirosso50"

urls = [
    "https://api.octopus.energy/v1/accounts/login/",
    "https://api.octopus.energy/v1/accounts/token/",
    "https://api.octopus.energy/v1/login/",
    "https://octopus.energy/api/login/",
    "https://login.octopus.energy/oauth/token",
    "https://api.octopus.energy/oauth/token",
    "https://api.krakenflex.systems/v1/tokens/",
    "https://api.octopus.energy/v1/tokens/",
]

for url in urls:
    print("\n=== TESTING:", url)
    try:
        r = requests.post(url, json={"email": email, "password": password})
        print("STATUS:", r.status_code)
        print("TEXT:", r.text[:400])
    except Exception as e:
        print("ERROR:", e)
