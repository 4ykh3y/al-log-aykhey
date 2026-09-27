"""Surveille les nouvelles offres Action Logement et envoie une notification (ntfy, e-mail optionnel)."""
import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests

API_CANDIDATES = [
    "https://api.logement-actionlogement.fr/api/v1/demands/public/offers-overview",
    "https://api.logement-actionlogement.fr/api/v1/public/offers-overview",
    "https://api.logement-actionlogement.fr/api/v1/demands/offers-overview",
]
SEARCH_URL = "https://logement-actionlogement.fr/search"
SEEN_FILE = Path(__file__).parent / "seen.json"

# Criteres de recherche (copies depuis la requete du site)
PAYLOAD = {
    "municipalities": [
        {"code": "91225", "postcode": "91450"},  # Etiolles
        {"code": "91174", "postcode": "91100"},  # Corbeil-Essonnes
        {"code": "91340", "postcode": "91090"},  # Lisses
        {"code": "91521", "postcode": "91130"},  # Ris-Orangis
        {"code": "91617", "postcode": "91250"},  # Tigery
        {"code": "91228", "postcode": "91000"},  # Evry-Courcouronnes
    ],
    "searchRadiusInKm": 5,
    "typologyCodes": [],
    "productGuids": [],
    "offerCategories": [],
}

# Filtres optionnels cote script (None = desactive)
LOYER_MAX = None          # ex. 750
EXCLURE_A_LA_NUIT = True  # ignore les offres facturees a la nuit

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json",
    "origin": "https://logement-actionlogement.fr",
    "referer": "https://logement-actionlogement.fr/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
}

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
SMTP_USER = os.environ.get("SMTP_USER")          # adresse Gmail d'envoi (optionnel)
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")  # mot de passe d'application Gmail
MAIL_TO = os.environ.get("MAIL_TO")


def notify(title: str, body: str, url: str = SEARCH_URL) -> None:
    if NTFY_TOPIC:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title.encode("utf-8"), "Click": url, "Tags": "house"},
            timeout=20,
        )
    if SMTP_USER and SMTP_PASSWORD and MAIL_TO:
        msg = MIMEText(f"{body}\n\n{url}", "plain", "utf-8")
        msg["Subject"], msg["From"], msg["To"] = title, SMTP_USER, MAIL_TO
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)


def find_api() -> str:
    """Teste les URL candidates et retourne la premiere qui repond sans connexion."""
    statuses = []
    for url in API_CANDIDATES:
        try:
            r = requests.post(url, params={"size": 1, "page": 0},
                              json=PAYLOAD, headers=HEADERS, timeout=30)
        except requests.RequestException as e:
            statuses.append(f"{url.split('/api/v1/')[1]} -> erreur {e.__class__.__name__}")
            continue
        statuses.append(f"{url.split('/api/v1/')[1]} -> {r.status_code}")
        if r.ok:
            print("API utilisee :", url)
            return url
    detail = "\n".join(statuses)
    notify("Alerte Action Logement : acces refuse",
           f"Aucune URL publique ne repond :\n{detail}")
    sys.exit("Aucune URL exploitable :\n" + detail)


def fetch_all() -> list[dict]:
    api = find_api()
    offers, page = [], 0
    while True:
        r = requests.post(api, params={"size": 20, "page": page},
                          json=PAYLOAD, headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json()
        items = extract_items(data)
        offers.extend(items)
        total_pages = data.get("totalPages") if isinstance(data, dict) else None
        if not items or total_pages is None or page + 1 >= total_pages or page >= 10:
            break
        page += 1
    return offers


def extract_items(data) -> list[dict]:
    """Trouve la liste d'offres quelle que soit la forme exacte de la reponse."""
    if isinstance(data, list):
        return data
    for key in ("content", "offers", "items", "results", "data", "elements"):
        val = data.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            sub = extract_items(val)
            if sub:
                return sub
    return []


def pick(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d[k]
    return default


def offer_id(o: dict) -> str:
    val = pick(o, "guid", "offerGuid", "id", "offerId", "reference")
    return str(val) if val is not None else json.dumps(o, sort_keys=True)[:200]


def describe(o: dict) -> str:
    lodging = o.get("lodging") if isinstance(o.get("lodging"), dict) else {}
    src = {**lodging, **o}
    typo = pick(src, "typology", "typologyLabel", "lodgingType", "title", default="Logement")
    if isinstance(typo, dict):
        typo = pick(typo, "label", "code", default="Logement")
    surface = pick(src, "surface", "livingArea", "area")
    city = pick(src, "city", "municipality", "municipalityName", "town")
    if isinstance(city, dict):
        city = pick(city, "name", "label")
    postcode = pick(src, "postcode", "postalCode", "zipCode")
    rent = pick(src, "rent", "rentAmount", "price", "totalRent", "amount")
    unit = pick(src, "rentPeriodicity", "priceUnit", "periodicity", default="")
    avail = pick(src, "availabilityDate", "availableFrom", "availableDate")

    parts = [str(typo)]
    if surface:
        parts.append(f"{surface} m2")
    line1 = " - ".join(parts)
    line2 = " ".join(str(x) for x in (city, f"({postcode})" if postcode else None) if x)
    line3 = f"{rent} EUR {unit}".strip() if rent else ""
    line4 = f"Disponible : {avail}" if avail else ""
    return "\n".join(x for x in (line1, line2, line3, line4) if x)


def keep(o: dict) -> bool:
    text = json.dumps(o, ensure_ascii=False).lower()
    if EXCLURE_A_LA_NUIT and ("night" in text or "nuit" in text):
        return False
    if LOYER_MAX is not None:
        rent = pick(o, "rent", "rentAmount", "price", "totalRent", "amount")
        try:
            if rent is not None and float(rent) > LOYER_MAX:
                return False
        except (TypeError, ValueError):
            pass
    return True


def main() -> None:
    offers = fetch_all()
    print(f"{len(offers)} offres recues")
    if offers:
        print("Exemple d'offre brute :", json.dumps(offers[0], ensure_ascii=False)[:1500])

    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {}
    first_run = not seen
    new = [o for o in offers if offer_id(o) not in seen]

    for o in new:
        seen[offer_id(o)] = describe(o).split("\n")[0]

    if first_run:
        notify("Alerte Action Logement activee",
               f"{len(offers)} offres actuelles enregistrees. Tu recevras uniquement les nouvelles.")
    else:
        for o in (x for x in new if keep(x)):
            notify("Nouvelle offre Action Logement", describe(o))
            print("Nouvelle :", describe(o).replace("\n", " | "))

    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
