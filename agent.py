#!/usr/bin/env python3
"""
willhaben-agent - Kernlogik: Suche abfragen, filtern, per Telegram melden.

Nur Standardbibliothek. Wird sowohl von server.py (Weboberfläche + Poller)
als auch direkt auf der Kommandozeile benutzt:

    python agent.py --dry-run    zeigt Treffer und Verwerfungsgründe
    python agent.py --once       ein Durchlauf, dann Ende
    python agent.py              Dauerbetrieb
"""

import json
import math
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone

CONFIG_PATH = os.environ.get("WH_CONFIG", "/app/config.json")
STATE_PATH = os.environ.get("WH_STATE", "/app/state/seen.json")

SEARCH_HOST = "https://ad-search.willhaben.at"
# Ohne diesen Header antwortet die API mit HTTP 400.
WH_CLIENT = "api@willhaben.at;responsive_web;server;1.0.0;desktop"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Maximale Zahl gemerkter IDs pro Suche, damit state/seen.json nicht endlos wächst.
MAX_SEEN_PER_SEARCH = 3000

# config.json wird von der Weboberfläche geschrieben und vom Poller gelesen.
CONFIG_LOCK = threading.Lock()


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    print("[%s] %s" % (ts, msg), flush=True)


# ---------------------------------------------------------------- http

def http_get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- willhaben

def build_api_url(search):
    """Baut die REST-URL aus einer willhaben-Suchseiten-URL oder aus Einzelfeldern."""
    if search.get("url"):
        parsed = urllib.parse.urlparse(search["url"])
        # /iad/kaufen-und-verkaufen/marktplatz -> kaufen-und-verkaufen/marktplatz
        path = parsed.path.strip("/")
        if path.startswith("iad/"):
            path = path[4:]
        params = dict(urllib.parse.parse_qsl(parsed.query))
    else:
        path = search.get("path") or "kaufen-und-verkaufen/marktplatz"
        params = dict(search.get("params") or {})
        if search.get("keyword"):
            params["keyword"] = search["keyword"]

    reach = (search.get("filter") or {}).get("reach") or {}
    if reach.get("mode") == "paylivery":
        # Reine PayLivery-Suche kann willhaben selbst filtern. Bei der
        # Oder-Variante darf das nicht passieren, sonst fehlen die Inserate,
        # die nur über die Entfernung hereinkommen.
        params["paylivery"] = "1"

    params = {k: v for k, v in params.items() if v not in (None, "")}
    params["rows"] = str(search.get("rows", 30))
    params["sort"] = "1"          # published.descending - neueste zuerst
    params["isNavigation"] = "true"
    params.pop("page", None)

    return "%s/restapi/v2/search/atz/seo/%s?%s" % (
        SEARCH_HOST, path, urllib.parse.urlencode(params))


def attr(ad, name, default=""):
    for a in ad.get("attributes", {}).get("attribute", []):
        if a.get("name") == name:
            vals = a.get("values") or []
            return vals[0] if vals else default
    return default


def parse_coords(s):
    """"47.56721,15.628924" -> (47.56721, 15.628924), sonst None."""
    try:
        lat, lon = s.split(",")
        return float(lat), float(lon)
    except (ValueError, AttributeError):
        return None


def haversine_km(a, b):
    """Luftlinie in Kilometern zwischen zwei (lat, lon)-Paaren."""
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = p2 - p1
    dl = math.radians(b[1] - a[1])
    h = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(h))


def parse_ad(ad):
    seo = attr(ad, "SEO_URL")
    mmo = attr(ad, "MMO")
    try:
        price = float(attr(ad, "PRICE", "0") or 0)
    except ValueError:
        price = 0.0
    return {
        # p2penabled ist willhabens Kennzeichen für PayLivery, geprüft gegen
        # den Serverfilter paylivery=1: dort sind alle Treffer "true".
        "paylivery": attr(ad, "p2penabled") == "true",
        "coords": parse_coords(attr(ad, "COORDINATES")),
        "id": str(ad.get("id") or attr(ad, "ADID")),
        "title": attr(ad, "HEADING"),
        "body": attr(ad, "BODY_DYN"),
        "price": price,
        "price_display": attr(ad, "PRICE_FOR_DISPLAY") or "Preis auf Anfrage",
        "location": attr(ad, "LOCATION"),
        "postcode": attr(ad, "POSTCODE"),
        "state": attr(ad, "STATE"),
        "published": attr(ad, "PUBLISHED_String"),
        "private": attr(ad, "ISPRIVATE") == "1",
        "url": "https://www.willhaben.at/iad/" + seo if seo else "",
        "image": "https://cache.willhaben.at/mmo/" + mmo if mmo else "",
    }


def fetch(search):
    raw = http_get(build_api_url(search), headers={
        "Accept": "application/json",
        "User-Agent": UA,
        "x-wh-client": WH_CLIENT,
    })
    data = json.loads(raw.decode("utf-8"))
    ads = data.get("advertSummaryList", {}).get("advertSummary", []) or []
    return [parse_ad(a) for a in ads], data.get("rowsFound", 0)


# ---------------------------------------------------------------- filter

# Trennt den Hauptartikel von der Zubehör-Aufzählung im Titel.
# "PS5 Slim Digital + 2 DualSense Controller" -> "PS5 Slim Digital"
HEAD_SPLIT = re.compile(
    r"\s*(?:[+|&,/]|\b(?:inkl|inklusive|incl|mit|sowie|dazu|plus)\b|\s-\s)", re.I)


def title_head(title):
    """Der Teil des Titels vor der ersten Aufzählung - dort steht das Hauptprodukt.

    Verhindert, dass ein Zubehör-Ausschluss ein echtes Bundle verwirft
    ("PS5 Slim Digital + 2 DualSense Controller + Ladestation").
    """
    head = HEAD_SPLIT.split(title, 1)[0].strip()
    # Zu kurz geraten (z.B. Titel beginnt mit Trenner) - dann lieber ganzer Titel.
    return head if len(head) >= 4 else title


# Deutsche Endungen, die als dieselbe Wortform gelten sollen: wer "Spiel"
# ausschließt, meint auch "Spiele"; wer "Controller" sagt, auch "Controllern".
PLURAL = r"(?:e|en|er|ern|s|n)?"


def term_to_regex(term):
    """Ein eingetipptes Wort in ein sicheres Muster übersetzen.

    Sonderzeichen werden entwertet, damit ein Wort wie "c++" nicht als Regex
    verstanden wird. Leerzeichen dürfen im Inserat mehrfach oder gar nicht
    stehen ("playstation 5" trifft auch "playstation  5").
    """
    term = term.strip()
    if not term:
        return None
    core = r"\s*".join(re.escape(p) for p in term.split())
    pre = r"\b" if term[:1].isalnum() else ""
    if term[-1:].isalpha():
        post = PLURAL + r"\b"
    elif term[-1:].isalnum():
        post = r"\b"
    else:
        post = ""
    return pre + core + post


def _patterns(f, regex_key, words_key):
    """Muster aus dem Expertenfeld und aus den eingetippten Wörtern mischen.

    Rückgabe sind Paare (Muster, Anzeigetext), damit die Begründung das
    Wort nennt und nicht den erzeugten regulären Ausdruck.
    """
    out = [(p, p) for p in (f.get(regex_key) or []) if p]
    for w in (f.get(words_key) or []):
        rx = term_to_regex(w)
        if rx:
            out.append((rx, w))
    return out


def _search_any(pairs, text, label):
    """Erstes zutreffendes Muster melden. Ungültige Regex nicht verschlucken."""
    for pat, shown in pairs:
        try:
            if re.search(pat, text, re.I):
                return "%s: %s" % (label, shown)
        except re.error as e:
            return "ungültiges Muster „%s“ (%s)" % (shown, e)
    return None


def matches(ad, f):
    """Prüft ein Inserat gegen einen Filter.

    Gibt (True, "") zurück oder (False, Grund). Der Grund wird in der
    Vorschau angezeigt und nennt das auslösende Wort.
    """
    if not f:
        return True, ""

    pmax = f.get("price_max")
    if pmax not in (None, "") and ad["price"] > float(pmax):
        return False, "Preis %.0f über %s" % (ad["price"], pmax)

    pmin = f.get("price_min")
    # Preis 0 heißt "keine Angabe" - nicht als Unterschreitung werten.
    if pmin not in (None, "") and 0 < ad["price"] < float(pmin):
        return False, "Preis %.0f unter %s" % (ad["price"], pmin)

    if f.get("private_only") and not ad["private"]:
        return False, "gewerblicher Anbieter"

    title = ad["title"]
    full = title + " " + ad["body"]

    rx = f.get("title_regex")
    if rx:
        try:
            if not re.search(rx, title, re.I):
                return False, "Titel passt nicht auf /%s/" % rx
        except re.error as e:
            return False, "ungültiges Titel-Muster (%s)" % e

    req = [w for w in (f.get("require_words") or []) if w.strip()]
    if req and not any(re.search(term_to_regex(w), title, re.I) for w in req):
        return False, "Titel enthält keines von: %s" % ", ".join(req)

    # Der Hauptartikel wird getrennt geprüft, damit Zubehör als Beigabe im
    # Bundle erlaubt bleibt.
    blocks = [
        (title, _patterns(f, "exclude_title", "block_title_words"), "im Titel"),
        (title_head(title), _patterns(f, "exclude_title_head", "block_words"),
         "als Hauptartikel"),
        (full, _patterns(f, "exclude_body", "block_text_words"), "im Text"),
    ]
    for text, pairs, label in blocks:
        hit = _search_any(pairs, text, label)
        if hit:
            return False, hit

    ok, why = reachable(ad, f.get("reach"))
    if not ok:
        return False, why

    return True, ""


def distance_km(ad, reach):
    """Luftlinie vom Bezugspunkt zum Inserat, oder None wenn unbekannt."""
    if not reach or reach.get("lat") is None or reach.get("lon") is None:
        return None
    if not ad.get("coords"):
        return None
    return haversine_km((float(reach["lat"]), float(reach["lon"])), ad["coords"])


def reachable(ad, reach):
    """Kommt man an das Inserat heran?

    willhaben kann nur "nur PayLivery" filtern. Die Oder-Verknüpfung
    ("PayLivery oder nah genug") gibt es dort nicht, deshalb wird sie hier
    gerechnet: PayLivery aus p2penabled, die Entfernung aus den Koordinaten
    des Inserats.
    """
    reach = reach or {}
    mode = reach.get("mode", "off")
    if mode == "off":
        return True, ""

    pay = ad.get("paylivery")
    km = distance_km(ad, reach)
    limit = reach.get("km")
    near = km is not None and limit is not None and km <= float(limit)

    if mode == "paylivery":
        return (True, "") if pay else (False, "kein PayLivery")

    if mode == "radius":
        if near:
            return True, ""
        if km is None:
            return False, "Entfernung unbekannt"
        return False, "%.0f km entfernt" % km

    if mode == "paylivery_or_radius":
        if pay or near:
            return True, ""
        if km is None:
            return False, "kein PayLivery, Entfernung unbekannt"
        return False, "kein PayLivery und %.0f km entfernt" % km

    return True, ""


def evaluate(search, limit=None):
    """Suche abfragen und jedes Inserat bewerten - Grundlage der Vorschau."""
    ads, total = fetch(search)
    if limit:
        ads = ads[:limit]
    reach = (search.get("filter") or {}).get("reach")
    out = []
    for ad in ads:
        ok, why = matches(ad, search.get("filter"))
        row = dict(ad)
        row["ok"] = ok
        row["reason"] = why
        km = distance_km(ad, reach)
        row["km"] = round(km, 1) if km is not None else None
        row.pop("coords", None)     # Rohkoordinaten muss die Oberfläche nicht sehen
        out.append(row)
    return out, total


# ---------------------------------------------------------------- telegram

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def send_telegram(tg, ad, reach=None):
    token = tg["token"]
    chat_id = tg["chat_id"]

    seller = "privat" if ad["private"] else "gewerblich"
    loc = " ".join(x for x in [ad["postcode"], ad["location"]] if x)
    when = ad["published"].replace("T", " ").replace("Z", "")

    marks = []
    if ad.get("paylivery"):
        marks.append("PayLivery")
    km = distance_km(ad, reach)
    if km is not None:
        marks.append("%.0f km" % km)
    extra = ("\n" + esc(" · ".join(marks))) if marks else ""

    caption = ("<b>%s</b>\n%s  ·  %s\n%s  ·  %s%s\n<i>%s</i>\n\n%s") % (
        esc(ad["title"]), esc(ad["price_display"]), seller,
        esc(loc), esc(ad["state"]), extra, esc(when), ad["url"])

    if ad["image"]:
        try:
            http_post_json("https://api.telegram.org/bot%s/sendPhoto" % token,
                           {"chat_id": chat_id, "photo": ad["image"],
                            "caption": caption, "parse_mode": "HTML"})
            return
        except Exception as e:
            log("  sendPhoto fehlgeschlagen (%s), fallback auf Text" % e)

    http_post_json("https://api.telegram.org/bot%s/sendMessage" % token,
                   {"chat_id": chat_id, "text": caption, "parse_mode": "HTML",
                    "disable_web_page_preview": False})


# ---------------------------------------------------------------- config/state

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return default


def save_json(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    with CONFIG_LOCK:
        cfg = load_json(CONFIG_PATH, {}) or {}
    cfg.setdefault("poll_interval", 60)
    cfg.setdefault("searches", [])
    tg = cfg.setdefault("telegram", {})
    # Env gewinnt, damit der Token nicht in der config stehen muss.
    tg["token"] = os.environ.get("TELEGRAM_TOKEN") or tg.get("token", "")
    tg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or tg.get("chat_id", "")
    return cfg


def save_config(cfg):
    out = json.loads(json.dumps(cfg))
    # Geheimnisse bleiben in der Umgebung, nicht in der Datei.
    out.pop("telegram", None)
    with CONFIG_LOCK:
        save_json(CONFIG_PATH, out)


# ---------------------------------------------------------------- durchlauf

def run_search(tg, search, state, dry_run=False):
    name = search.get("name") or "unbenannt"
    seen = state.setdefault(name, [])
    seen_set = set(seen)
    first_run = not seen_set
    if dry_run:
        # Im Trockenlauf soll sichtbar werden, was durchkäme.
        first_run = False

    ads, _ = fetch(search)
    if not ads:
        log("%s: 0 Treffer zurück - Suche prüfen" % name)
        return 0

    new = [a for a in ads if a["id"] not in seen_set]
    new.reverse()   # ältestes zuerst, damit die Chat-Reihenfolge stimmt

    sent, failed = 0, set()
    for ad in new:
        ok, why = matches(ad, search.get("filter"))
        if not ok:
            continue
        if first_run:
            continue
        if dry_run:
            log("%s: [DRY] %s | %s" % (name, ad["price_display"], ad["title"][:60]))
        else:
            try:
                send_telegram(tg, ad, (search.get("filter") or {}).get("reach"))
                log("%s: TREFFER %s | %s" % (name, ad["price_display"], ad["title"][:60]))
            except Exception as e:
                log("%s: Telegram-Fehler (%s) - erneuter Versuch später" % (name, e))
                failed.add(ad["id"])
                continue
        sent += 1

    # Nicht zugestellte Treffer bleiben ungemerkt, damit sie wiederkommen.
    fresh = [a["id"] for a in ads if a["id"] not in seen_set and a["id"] not in failed]
    state[name] = (fresh + seen)[:MAX_SEEN_PER_SEARCH]

    if first_run:
        log("%s: Erstlauf, %d Inserate als bekannt markiert (keine Meldung)"
            % (name, len(ads)))
    return sent


def poll_once(cfg, dry_run=False, status=None):
    """Ein Durchlauf über alle aktiven Suchen. Gibt (gesendet, fehler) zurück."""
    state = load_json(STATE_PATH, {})
    tg = cfg.get("telegram", {})
    sent, errors = 0, 0

    for search in cfg.get("searches", []):
        if search.get("enabled") is False:
            continue
        name = search.get("name") or "unbenannt"
        try:
            n = run_search(tg, search, state, dry_run)
            sent += n
            if status is not None:
                status[name] = {"error": None, "sent": n}
        except urllib.error.HTTPError as e:
            errors += 1
            log("%s: HTTP %s" % (name, e.code))
            if status is not None:
                status[name] = {"error": "HTTP %s" % e.code}
        except Exception as e:
            errors += 1
            log("%s: Fehler %s" % (name, e))
            if status is not None:
                status[name] = {"error": str(e)}

    if not dry_run:
        save_json(STATE_PATH, state)
    return sent, errors


# ---------------------------------------------------------------- cli

def main():
    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv or dry_run

    cfg = load_config()
    if not cfg["searches"]:
        log("keine Suchen konfiguriert")
        return 1
    if not dry_run and not (cfg["telegram"]["token"] and cfg["telegram"]["chat_id"]):
        log("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID fehlen")
        return 1

    interval = int(os.environ.get("POLL_INTERVAL", cfg["poll_interval"]))
    log("Start: %d Suche(n), Intervall %ds%s"
        % (len(cfg["searches"]), interval, " [DRY RUN]" if dry_run else ""))

    fails = 0
    while True:
        cfg = load_config()
        sent, errors = poll_once(cfg, dry_run)
        fails = fails + 1 if errors else 0
        if sent:
            log("%d Benachrichtigung(en) gesendet" % sent)
        if once:
            return 0
        # Bei wiederholten Fehlern zurückstufen statt weiter im Takt zu hämmern.
        wait = interval * min(2 ** fails, 16) if fails else interval
        if fails:
            log("Backoff: nächster Versuch in %ds" % wait)
        time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
