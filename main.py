# main.py
import os
import time
import json
import hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlencode
from dotenv import load_dotenv

# ---- Configuration (voir plus bas comment configurer sur Replit Secrets) ----
load_dotenv()  # charge .env si présent (utile en local)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # obligatoire
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")      # obligatoire

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "3600"))  # en secondes (ex : 3600 = 1h)
MIN_SLEEP = 60  # sécurité minimale entre requêtes

# Paramètres de recherche (modifiables)
TITLES = [
    "opérateur de saisie",
    "opérateur saisie",
    "saisie de données",
    "saisie",
    "opérateur saisie",
    "data entry",
]
BRETAGNE_CITIES = ["Rennes", "Nantes", "Brest", "Saint-Brieuc", "Vannes", "Lorient", "Quimper", "Brest"]  # Nantes incluse
CONTRACT_KEYWORDS = ["CDI", "CDD", "Intérim", "Interim", "Contrat"]

SEEN_FILE = "seen.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobNotifier/1.0; +https://example.com)"
}

# ------------------------------------------------------------------------------

def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data)
    except Exception:
        return set()

def save_seen(seen_set):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen_set), f, ensure_ascii=False, indent=2)

def make_indeed_url(query, location="", start=0):
    base = "https://fr.indeed.com/jobs"
    params = {"q": query, "l": location, "start": start}
    return f"{base}?{urlencode(params)}"

def get_job_id_from_link(link):
    # Use the link string hashed if no id in URL
    return hashlib.sha1(link.encode("utf-8")).hexdigest()

def parse_indeed_search(query, location="", max_pages=2):
    """Retourne une liste d'annonces : dicts avec title, company, location, summary, link, contract"""
    results = []
    for page in range(max_pages):
        url = make_indeed_url(query, location, start=page*10)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            # Indeed moderne : liens d'offres contiennent a.tapItem
            cards = soup.select("a.tapItem") or soup.select("div.job_seen_beacon a")
            if not cards:
                # fallback: look for article tags
                cards = soup.select("article")
            if not cards:
                break
            for c in cards:
                # title
                title_tag = c.select_one("h2")
                title = title_tag.get_text(strip=True) if title_tag else c.get_text(" ", strip=True)[:80]
                # link
                href = c.get("href") or (c.select_one("a") and c.select_one("a").get("href"))
                if not href:
                    continue
                link = urljoin("https://fr.indeed.com", href)
                # company & location & summary
                company = ""
                loc = ""
                comp_tag = c.select_one(".companyName") or c.select_one(".company")
                if comp_tag:
                    company = comp_tag.get_text(strip=True)
                loc_tag = c.select_one(".companyLocation") or c.select_one(".location")
                if loc_tag:
                    loc = loc_tag.get_text(strip=True)
                summary = ""
                summ_tag = c.select_one(".job-snippet") or c.select_one(".summary")
                if summ_tag:
                    summary = summ_tag.get_text(" ", strip=True)
                # try to detect contract type in text
                whole_text = " ".join([title, company, loc, summary]).upper()
                contract = None
                for kw in CONTRACT_KEYWORDS:
                    if kw.upper() in whole_text:
                        contract = kw
                        break
                results.append({
                    "title": title,
                    "company": company,
                    "location": loc,
                    "summary": summary,
                    "link": link,
                    "contract": contract,
                    "id": get_job_id_from_link(link)
                })
            time.sleep(1)  # courte pause pour éviter spam
        except Exception as e:
            print("Erreur fetch Indeed:", e)
            break
    return results

def filter_by_region_and_contract(jobs, region_cities, accept_contracts=True, accept_remote=False):
    filtered = []
    for j in jobs:
        loc = (j.get("location") or "").lower()
        text = (j.get("title","") + " " + j.get("summary","")).lower()
        is_remote = "télétravail" in loc or "télétravail" in text or "teletravail" in text or "remote" in text
        in_region = any(city.lower() in loc or city.lower() in text for city in region_cities)
        if accept_remote and is_remote:
            pass_ok = True
        else:
            pass_ok = in_region
        if not pass_ok:
            continue
        if accept_contracts:
            # if job doesn't say contract type, still keep it (many offers omit it). But prefer those with accepted types.
            if j.get("contract"):
                if not any(kw.upper() in j["contract"].upper() for kw in CONTRACT_KEYWORDS):
                    # not in accepted keywords -> still allow if summary includes keywords
                    if not any(kw.lower() in (j.get("summary") or "").lower() for kw in ("cdi","cdd","intérim","interim")):
                        continue
            # otherwise keep
        filtered.append(j)
    return filtered

def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": False}
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print("Erreur Telegram:", e)
        return False

def format_job_message(job):
    lines = [
        f"🔔 {job['title']}",
        f"🏢 {job.get('company','')} — 📍 {job.get('location','')}",
    ]
    if job.get("contract"):
        lines.append(f"🧾 Contrat: {job.get('contract')}")
    lines.append(job.get("link"))
    if job.get("summary"):
        lines.append("")
        lines.append(job.get("summary")[:400] + ("..." if len(job.get("summary",""))>400 else ""))
    return "\n".join(lines)

def main_loop():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERREUR: TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être définis en variables d'environnement.")
        return

    seen = load_seen()
    print(f"Démarrage — {len(seen)} annonces déjà en mémoire.")
    while True:
        all_new = []
        # 1) recherches localisées en Bretagne (par villes)
        for title in TITLES:
            for city in BRETAGNE_CITIES:
                query = title
                jobs = parse_indeed_search(query, city, max_pages=1)
                candidates = filter_by_region_and_contract(jobs, BRETAGNE_CITIES, accept_contracts=True, accept_remote=False)
                for j in candidates:
                    if j["id"] not in seen:
                        all_new.append(j)
                        seen.add(j["id"])
                time.sleep(1)
        # 2) recherches télétravail (100% remote) sur toute la France
        for title in TITLES:
            query_remote = f"{title} télétravail"
            jobs = parse_indeed_search(query_remote, "France", max_pages=1)
            candidates = filter_by_region_and_contract(jobs, BRETAGNE_CITIES, accept_contracts=True, accept_remote=True)
            for j in candidates:
                # double-check remote in text/location
                if "télétravail" in (j.get("location","") + " " + j.get("summary","")).lower() or "remote" in (j.get("location","") + " " + j.get("summary","")).lower():
                    if j["id"] not in seen:
                        all_new.append(j)
                        seen.add(j["id"])
            time.sleep(1)

        # Envoi notifications sur Telegram pour chaque nouvelle annonce
        if all_new:
            print(f"{len(all_new)} nouvelles annonces trouvées — envoi Telegram...")
            for j in all_new:
                msg = format_job_message(j)
                success = send_telegram_message(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, msg)
                print("Envoyé:", j["title"], "OK" if success else "FAILED")
                time.sleep(1)  # petite pause entre envois
            save_seen(seen)
        else:
            print("Aucune nouvelle annonce cette passe.")

        # attente avant prochaine passe
        sleep_sec = max(MIN_SLEEP, CHECK_INTERVAL)
        print(f"Attente {sleep_sec} secondes avant la prochaine vérification...")
        time.sleep(sleep_sec)

if __name__ == "__main__":
    main_loop()
