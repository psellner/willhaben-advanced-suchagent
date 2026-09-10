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

def build_api_url(search, keyword=None):
    """Baut die REST-URL aus einer willhaben-Suchseiten-URL oder aus Einzelfeldern.

    `keyword` überschreibt den Suchbegriff der Suche - so entsteht aus einer
    Suche je Schreibweise eine eigene Abfrage.
    """
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

    if keyword is not None:
        params["keyword"] = keyword

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
    status = (ad.get("advertStatus") or {}).get("id") or ""
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
        "seo": seo,
        "title": attr(ad, "HEADING"),
        "body": attr(ad, "BODY_DYN"),
        "price": price,
        "price_display": attr(ad, "PRICE_FOR_DISPLAY") or "Preis auf Anfrage",
        "location": attr(ad, "LOCATION"),
        "postcode": attr(ad, "POSTCODE"),
        "state": attr(ad, "STATE"),
        "published": attr(ad, "PUBLISHED_String"),
        # Epoch in Millisekunden. Nur zum Sortieren der vereinigten
        # Trefferliste gebraucht, deshalb nicht in der Oberfläche sichtbar.
        "published_ts": int(attr(ad, "PUBLISHED", "0") or 0),
        "private": attr(ad, "ISPRIVATE") == "1",
        "url": "https://www.willhaben.at/iad/" + seo if seo else "",
        "image": "https://cache.willhaben.at/mmo/" + mmo if mmo else "",
        # Auch die Suche kennt den Status, nicht nur die Detailseite. Sie
        # liefert reservierte Inserate ganz normal mit aus - in der Vorschau
        # sollen sie als solche erkennbar sein.
        "status": status,
        "active": status in LIVE_STATUS,
    }


def search_keywords(search):
    """Die Schreibweisen, mit denen diese Suche abgefragt wird.

    willhabens API kennt nur einen `keyword`-Parameter, und der entscheidet
    schon, welche Inserate überhaupt ankommen: "play station 5" und "ps5"
    liefern deutlich verschiedene Trefferlisten. Deshalb wird je Schreibweise
    einmal abgefragt und danach vereinigt.

    Leere Liste heißt: den Suchbegriff nehmen, der ohnehin in `params` oder
    in der URL steht. Dafür steht das einzelne None.
    """
    out, seen = [], set()
    for k in (search.get("keywords") or []):
        k = (k or "").strip()
        # Doppelte Schreibweisen kosten einen Request und bringen nichts.
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out or [None]


def url_keyword(url):
    """Der Suchbegriff, der tatsächlich in einer fertigen Abfrage-URL steht."""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    return q.get("keyword", "")


def fetch_one(search, keyword=None):
    url = build_api_url(search, keyword)
    raw = http_get(url, headers={
        "Accept": "application/json",
        "User-Agent": UA,
        "x-wh-client": WH_CLIENT,
    })
    data = json.loads(raw.decode("utf-8"))
    ads = data.get("advertSummaryList", {}).get("advertSummary", []) or []
    return [parse_ad(a) for a in ads], data.get("rowsFound", 0), url


def fetch(search):
    """Alle Schreibweisen abfragen und zu einer Trefferliste vereinigen.

    Gibt (Inserate, größte Gesamtzahl, Herkunft je Schreibweise) zurück. Die
    Herkunft trägt die Vorschau, damit sichtbar wird, was jede Schreibweise
    beisteuert. Fällt eine einzelne aus, laufen die übrigen weiter - fällt
    keine durch, kommt der Fehler wie bisher heraus.
    """
    by_id, sources, failed = {}, [], 0
    for kw in search_keywords(search):
        src = {"keyword": kw}
        try:
            ads, found, url = fetch_one(search, kw)
        except Exception as e:
            failed += 1
            src["url"] = build_api_url(search, kw)
            src["keyword"] = kw if kw is not None else url_keyword(src["url"])
            src["error"] = str(e)
            src["last_exception"] = e
            sources.append(src)
            log("Suchbegriff „%s“ fehlgeschlagen: %s" % (src["keyword"], e))
            continue
        src["url"] = url
        src["keyword"] = kw if kw is not None else url_keyword(url)
        src["found"] = found
        src["fetched"] = len(ads)
        # Wie viele Inserate nur über diese Schreibweise hereinkommen - das
        # zeigt in der Vorschau, ob sich eine Variante überhaupt lohnt.
        src["only_here"] = sum(1 for a in ads if a["id"] not in by_id)
        for a in ads:
            by_id.setdefault(a["id"], a)
        sources.append(src)

    if failed and failed == len(sources):
        raise sources[0].pop("last_exception")
    for src in sources:
        src.pop("last_exception", None)

    merged = sorted(by_id.values(), key=lambda a: a["published_ts"], reverse=True)
    total = max([s.get("found", 0) for s in sources] or [0])
    return merged, total, sources


# Die Suche liefert immer nur die letzten `rows` Inserate (neueste zuerst).
# Ältere, längst gefundene Treffer fallen aus diesem Fenster und würden nie
# wieder auf Preisänderungen geprüft. willhabens Detailseite ist als
# Next.js-App gebaut und lädt ihre Daten clientseitig über genau so eine
# /_next/data/<buildId>/...json-URL nach - dieselbe Attributsstruktur wie
# die Suche, aber pro Inserat einzeln abrufbar. Der buildId wechselt bei
# jedem Deploy, deshalb wird er bei jeder vollen Preisprüfung neu geholt statt
# gecacht.
BUILD_ID_RE = re.compile(r'"buildId":"([^"]+)"')


def fetch_build_id():
    raw = http_get("https://www.willhaben.at/iad/kaufen-und-verkaufen/marktplatz",
                    headers={"User-Agent": UA, "Accept": "text/html"})
    m = BUILD_ID_RE.search(raw.decode("utf-8", "ignore"))
    if not m:
        raise RuntimeError("buildId nicht in der willhaben-Seite gefunden")
    return m.group(1)


# Zustände, in denen es das Inserat noch gibt. "reserved" gehört dazu: eine
# Reservierung ist keine Löschung, sie platzt oft genug, und ein Preisrutsch
# darauf bleibt interessant. Alles andere (verkauft, abgelaufen, gelöscht)
# gilt als weg und wird aus der Preisverfolgung genommen.
LIVE_STATUS = ("active", "reserved")


def parse_ad_detail(detail, seo):
    """Ein Inserat aus der Detailseite lesen.

    Die Detailseite benennt fast alles anders als die Suche: der Titel steht
    in `description`, Ort und Bundesland in `advertAddressDetails`, das Bild
    in `advertImageList`. Nur Preis und Anbieterart heißen gleich. Deshalb
    ein eigener Parser statt parse_ad() - der fand hier bloß den Preis und
    ließ Titel, Ort und Link leer, was die Preisänderungsmeldung unbrauchbar
    machte.
    """
    addr = detail.get("advertAddressDetails") or {}
    p2pp = detail.get("p2ppOptions")
    bilder = (detail.get("advertImageList") or {}).get("advertImage") or []
    try:
        price = float(attr(detail, "PRICE", "0") or 0)
    except ValueError:
        price = 0.0
    status = (detail.get("advertStatus") or {}).get("id") or ""
    return {
        "id": str(detail.get("id") or ""),
        "seo": seo,
        "title": detail.get("description") or "",
        "body": attr(detail, "DESCRIPTION"),
        "price": price,
        "price_display": attr(detail, "PRICE_FOR_DISPLAY") or "Preis auf Anfrage",
        "location": addr.get("postalName") or attr(detail, "LOCATION/ADDRESS_2"),
        "postcode": addr.get("postCode") or "",
        "state": addr.get("province") or attr(detail, "LOCATION/ADDRESS_4"),
        # "2026-09-01T12:00:29+0200" -> dieselbe Form wie aus der Suche.
        "published": (detail.get("publishedDate") or "")[:19],
        "published_ts": 0,
        "private": attr(detail, "ISPRIVATE") == "1",
        # None heißt "die Seite sagt nichts dazu" - dann gilt der gemerkte
        # Stand aus der Suche. Sagt sie etwas, ist das der aktuelle Stand.
        "paylivery": (bool(p2pp.get("deliveryOptions")) if p2pp is not None else None),
        # Gleiches Attribut wie in der Suche, nur an anderer Stelle im JSON.
        "coords": parse_coords(attr(detail, "COORDINATES")),
        "url": "https://www.willhaben.at/iad/" + seo if seo else "",
        "image": (bilder[0].get("mainImageUrl") if bilder else "") or "",
        "status": status,
        "active": status in LIVE_STATUS,
    }


# Eine Inseratsadresse endet auf "-<ID>". Der Slug davor ist willhaben egal,
# aufgelöst wird allein über die Zahl am Ende.
AD_ID_RE = re.compile(r"-(\d+)$")


def seo_from_url(url, fallback_path=""):
    """Aus einer willhaben-Adresse den Detailpfad ziehen.

    Verträgt alles, was beim Kopieren anfällt: mit und ohne Schema, mit
    angehängter Suchabfrage oder Anker, mit oder ohne Schrägstrich am Ende.
    Eine nackte Inserats-ID genügt ebenfalls; der Slug wird dann erfunden,
    weil willhaben ihn ohnehin nicht prüft. Leerer Rückgabewert heißt: das
    war keine Adresse eines einzelnen Inserats.
    """
    url = (url or "").strip()
    if url.isdigit():
        vertical = (fallback_path or "kaufen-und-verkaufen").split("/")[0]
        return "%s/d/inserat-%s" % (vertical, url)
    path = urllib.parse.urlparse(url).path.strip("/")
    # Auch ohne Schema ("www.willhaben.at/iad/...") landet der Host im Pfad.
    cut = path.find("iad/")
    if cut != -1:
        path = path[cut + 4:]
    return path if AD_ID_RE.search(path) else ""


def check_ad(search, url, geocode=None):
    """Ein einzelnes Inserat gegen eine Suche prüfen.

    Beantwortet zwei Fragen, die in der Praxis gern verwechselt werden:
    kommt das Inserat durch den Filter, und liefert die Suche es überhaupt
    an? Ein Inserat kann jeden Filter passieren und trotzdem nie im Chat
    landen, weil keine der Schreibweisen darauf passt oder weil es aus dem
    Abfragefenster (`rows`) herausgerutscht ist.

    `geocode` ist der letzte Rückfall für die Entfernung: eine Funktion, die
    zu einem Ortsnamen Koordinaten liefert. Woher die Entfernung stammt,
    steht am Ende in `coords_source`.
    """
    seo = seo_from_url(url, search.get("path"))
    if not seo:
        raise ValueError("Das ist keine Adresse eines einzelnen Inserats.")

    ad = fetch_ad_detail(fetch_build_id(), seo)
    # Nur ein Teil der Detailseiten führt Koordinaten, die Suche führt sie
    # immer. Ohne sie fällt die Entfernungsprüfung auf "unbekannt" zurück und
    # verwirft ein Inserat, das in Wahrheit ums Eck liegt.
    source = "Detailseite" if ad["coords"] else ""

    # Je Schreibweise eine Abfrage - genau die, die auch der Durchlauf macht.
    window = []
    for kw in search_keywords(search):
        row = {"keyword": kw or url_keyword(build_api_url(search)), "hit": False}
        try:
            ads, _, _ = fetch_one(search, kw)
            row["fetched"] = len(ads)
            treffer = next((a for a in ads if a["id"] == ad["id"]), None)
            row["hit"] = treffer is not None
            if treffer:
                source = source or "Suchtreffer"
                _fill_from_search(ad, treffer)
        except Exception as e:
            row["error"] = str(e)
        window.append(row)

    if not ad["coords"]:
        treffer = _probe_by_title(search, ad)
        if treffer:
            source = "Titelsuche"
            _fill_from_search(ad, treffer)

    if not ad["coords"] and geocode:
        # Postleitzahl statt Adresse: die Straße nennt das Inserat nicht, und
        # für einen Radius über zig Kilometer genügt der Ortsmittelpunkt.
        try:
            hits = geocode("%s %s" % (ad["postcode"], ad["location"]))
        except Exception:
            hits = []
        if hits:
            ad["coords"] = (hits[0]["lat"], hits[0]["lon"])
            source = "Postleitzahl, daher ungefähr"

    ok, why = matches(ad, search.get("filter"))
    km = distance_km(ad, (search.get("filter") or {}).get("reach"))

    row = dict(ad)
    row.pop("coords", None)
    row.pop("published_ts", None)
    row["ok"] = ok
    row["reason"] = why
    row["km"] = round(km, 1) if km is not None else None
    return {"ad": row, "ok": ok, "reason": why, "window": window,
            "in_window": any(w.get("hit") for w in window),
            "coords_source": source}


def _fill_from_search(ad, treffer):
    """Was die Suche besser weiß als die Detailseite, nachtragen."""
    if not ad["coords"]:
        ad["coords"] = treffer["coords"]
    if ad.get("paylivery") is None:
        ad["paylivery"] = treffer["paylivery"]


def _probe_by_title(search, ad):
    """Das Inserat über seinen eigenen Titel suchen, nur wegen der Koordinaten.

    Greift, wenn es aus dem Abfragefenster der Suche gerutscht ist. Preis-
    und Anbieterfilter bleiben dabei weg: gesucht wird dieses eine Inserat,
    nicht die Trefferliste.
    """
    titel = " ".join((ad["title"] or "").split())[:60]
    if not titel:
        return None
    probe = {"path": search.get("path") or "kaufen-und-verkaufen/marktplatz",
             "rows": 200, "params": {"keyword": titel}}
    try:
        ads, _, _ = fetch_one(probe, titel)
    except Exception:
        return None
    return next((a for a in ads if a["id"] == ad["id"]), None)


class AdGone(Exception):
    """Unter dieser Adresse liegt kein Inserat mehr."""


def fetch_ad_detail(build_id, seo):
    """Aktuellen Stand eines einzelnen Inserats über seine Detailseite holen."""
    seo = seo.rstrip("/")
    url = "https://www.willhaben.at/_next/data/%s/iad/%s.json" % (build_id, seo)
    raw = http_get(url, headers={"Accept": "application/json", "User-Agent": UA})
    data = json.loads(raw.decode("utf-8"))
    detail = (data.get("pageProps") or {}).get("advertDetails")
    if not detail:
        # Gelöschte und abgelaufene Inserate liefern kein 404, sondern eine
        # 200-Antwort mit einer Weiterleitung auf die Kategorie. Ohne diese
        # Prüfung endet das in einem KeyError statt in einer klaren Aussage.
        raise AdGone(seo)
    return parse_ad_detail(detail, seo)


# ---------------------------------------------------------------- filter

# Trennt den Hauptartikel von der Zubehör-Aufzählung im Titel.
# "PS5 Slim Digital + 2 DualSense Controller" -> "PS5 Slim Digital"
# "und" und "samt" gehören dazu: "Verkaufe ps5 und spiele" verkauft die
# Konsole, nicht die Spiele. Ohne diese Trenner gilt der ganze Titel als
# Hauptartikel und ein Zubehör-Ausschluss verwirft das Inserat.
HEAD_SPLIT = re.compile(
    r"\s*(?:[+|&,/]|\b(?:inkl|inklusive|incl|mit|und|samt|sowie|dazu|plus)\b)", re.I)

# Der Gedankenstrich ist zweideutig. Mal hängt er Zubehör an
# ("PS5 Konsole - 2 Controller"), mal beschreibt er das Produkt selbst
# ("PS 5 GTA VI Limited Edition - DualSense Wireless Controller"). Er trennt
# deshalb nur, wenn dahinter eine Menge steht oder die Aufzählung weitergeht.
# Im Zweifel trennt er nicht - lieber ein Zubehör-Inserat zu viel im Chat als
# eine Konsole zu wenig.
DASH = re.compile(r"\s-\s")
DASH_LIST = re.compile(
    r"^\s*\d|[+&]|\b(?:inkl|inklusive|incl|mit|und|samt|sowie|dazu|plus)\b", re.I)


def title_head(title):
    """Der Teil des Titels vor der ersten Aufzählung - dort steht das Hauptprodukt.

    Verhindert, dass ein Zubehör-Ausschluss ein echtes Bundle verwirft
    ("PS5 Slim Digital + 2 DualSense Controller + Ladestation").
    """
    head = HEAD_SPLIT.split(title, 1)[0].strip()
    strich = DASH.search(title)
    if strich and strich.start() < len(head) and DASH_LIST.search(title[strich.end():]):
        head = title[:strich.start()].strip()
    # Zu kurz geraten (z.B. Titel beginnt mit Trenner) - dann lieber ganzer Titel.
    return head if len(head) >= 4 else title


# Deutsche Endungen, die als dieselbe Wortform gelten sollen: wer "Spiel"
# ausschließt, meint auch "Spiele"; wer "Controller" sagt, auch "Controllern".
PLURAL = r"(?:e|en|er|ern|s|n)?"

# Was zwischen zwei Wortteilen stehen darf. Getippt wird "play station 5",
# im Inserat steht "PlayStation-5", "playstation 5" oder "PS5" - für den
# Filter ist das dasselbe Produkt.
SEP = r"[\s._/-]*"

# Umlaute werden in Inseraten oft umschrieben und umgekehrt. Wer "hülle"
# eintippt, meint auch "huelle".
UMLAUT = {"ä": "(?:ä|ae)", "ö": "(?:ö|oe)", "ü": "(?:ü|ue)", "ß": "(?:ß|ss)"}

# Zerlegt einen Begriff in Buchstaben-, Ziffern- und Sonderzeichenblöcke:
# "ps5" -> "ps", "5"; "play station 5" -> "play", "station", "5". Dadurch ist
# es egal, ob der Begriff getrennt eingetippt wurde oder zusammen.
CHUNK = re.compile(r"[^\W\d_]+|\d+|[^\w\s]+")


def chunk_regex(chunk):
    """Ein Wortteil als Muster - Sonderzeichen entwertet, Umlaute geöffnet."""
    if chunk[:1].isalpha():
        return "".join(UMLAUT.get(c.lower(), re.escape(c)) for c in chunk)
    return re.escape(chunk)


def term_to_regex(term):
    """Ein eingetipptes Wort in ein sicheres Muster übersetzen.

    Sonderzeichen werden entwertet, damit ein Wort wie "c++" nicht als Regex
    verstanden wird. Zwischen den Wortteilen darf im Inserat ein beliebiges
    Trennzeichen stehen oder gar keines: "ps 5" trifft auch PS5, PS-5 und ps.5.
    """
    term = term.strip()
    chunks = CHUNK.findall(term)
    if not chunks:
        return None
    core = SEP.join(chunk_regex(c) for c in chunks)
    pre = r"\b" if term[:1].isalnum() else ""
    if term[-1:].isalpha():
        # Eine angehängte Ziffer beendet das Wort ebenso wie eine Wortgrenze:
        # "vr" und "psvr" sollen auch "VR2" und "PSVR2" treffen, sonst rutscht
        # eine VR-Brille als Konsole durch.
        post = PLURAL + r"(?=\d|\b)"
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

    req = [(term_to_regex(w), w) for w in (f.get("require_words") or []) if w.strip()]
    req = [(rx, w) for rx, w in req if rx]
    if req and not any(re.search(rx, title, re.I) for rx, _ in req):
        return False, "Titel enthält keines von: %s" % ", ".join(w for _, w in req)

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
    ads, total, sources = fetch(search)
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
        row.pop("published_ts", None)
        out.append(row)
    return out, total, sources


# ---------------------------------------------------------------- telegram

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_price(p):
    """Preis als Zahl (aus dem Zustand, ohne die willhaben-Anzeigeform)."""
    if p == int(p):
        return "%d €" % int(p)
    return ("%.2f €" % p).replace(".", ",")


def send_telegram_message(tg, caption, image=None):
    token = tg["token"]
    chat_id = tg["chat_id"]

    if image:
        try:
            http_post_json("https://api.telegram.org/bot%s/sendPhoto" % token,
                           {"chat_id": chat_id, "photo": image,
                            "caption": caption, "parse_mode": "HTML"})
            return
        except Exception as e:
            log("  sendPhoto fehlgeschlagen (%s), fallback auf Text" % e)

    http_post_json("https://api.telegram.org/bot%s/sendMessage" % token,
                   {"chat_id": chat_id, "text": caption, "parse_mode": "HTML",
                    "disable_web_page_preview": False})


def send_telegram(tg, ad, reach=None):
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

    caption = ("\U0001F195 <b>Neuer Treffer</b>\n<b>%s</b>\n%s  ·  %s\n%s  ·  %s%s\n<i>%s</i>\n\n%s") % (
        esc(ad["title"]), esc(ad["price_display"]), seller,
        esc(loc), esc(ad["state"]), extra, esc(when), ad["url"])
    send_telegram_message(tg, caption, ad["image"])


def send_telegram_price_change(tg, ad, old_price, reach=None):
    seller = "privat" if ad["private"] else "gewerblich"
    loc = " ".join(x for x in [ad["postcode"], ad["location"]] if x)
    arrow = "\U0001F53B" if ad["price"] < old_price else "\U0001F53A"   # ▾/▴

    marks = []
    if ad.get("paylivery"):
        marks.append("PayLivery")
    km = distance_km(ad, reach)
    if km is not None:
        marks.append("%.0f km" % km)
    extra = ("\n" + esc(" · ".join(marks))) if marks else ""

    caption = ("%s <b>Preisänderung</b>\n<b>%s</b>\n%s → %s  ·  %s\n%s  ·  %s%s\n\n%s") % (
        arrow, esc(ad["title"]), format_price(old_price), esc(ad["price_display"]),
        seller, esc(loc), esc(ad["state"]), extra, ad["url"])
    send_telegram_message(tg, caption, ad["image"])


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
    cfg.setdefault("price_check_interval", 1800)
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

# Wie viele der zuletzt gesehenen Treffer pro Suche für die volle
# Preisprüfung vorgemerkt bleiben (dort kostet jede Preisprüfung einen
# eigenen HTTP-Request). Kleiner als MAX_SEEN_PER_SEARCH, damit eine Suche
# mit vielen alten Treffern die volle Prüfung nicht ausufern lässt.
MAX_TRACKED_PRICES = 300

# Höchstzahl neuer Treffer, die ein einzelner Durchlauf meldet. Greift, wenn
# eine laufende Suche erweitert wird - eine zusätzliche Schreibweise in
# `keywords` macht auf einen Schlag lauter Bestandsinserate sichtbar, die
# sonst alle gleichzeitig im Chat landen würden. Der Rest bleibt ungemerkt
# und kommt in den nächsten Durchläufen nach, es geht also nichts verloren.
# Je Suche über `max_notify_per_run` überschreibbar.
MAX_NOTIFY_PER_RUN = 8


def search_entry(state, name):
    """Zustand einer Suche holen, altes Format (nur Liste von IDs) migrieren."""
    entry = state.setdefault(name, {})
    if isinstance(entry, list):
        entry = {"seen": entry, "prices": {}, "seo": {}}
        state[name] = entry
    entry.setdefault("seen", [])
    entry.setdefault("prices", {})
    entry.setdefault("seo", {})
    # Was die Detailseite nicht hergibt und nur die Suche kennt.
    entry.setdefault("extra", {})
    return entry


def seen_ids(entry):
    if isinstance(entry, dict):
        return entry.get("seen", [])
    return entry or []


def run_search(tg, search, state, dry_run=False):
    name = search.get("name") or "unbenannt"
    entry = search_entry(state, name)
    seen = entry["seen"]
    prices = entry["prices"]
    seo_map = entry["seo"]
    extra = entry["extra"]
    seen_set = set(seen)
    first_run = not seen_set
    if dry_run:
        # Im Trockenlauf soll sichtbar werden, was durchkäme.
        first_run = False

    def track(ad):
        prices[ad["id"]] = ad["price"]
        if ad.get("seo"):
            seo_map[ad["id"]] = ad["seo"]
        # Koordinaten und PayLivery stehen nur in der Suchantwort. Die volle
        # Preisprüfung braucht sie später für Entfernung und Kennzeichnung.
        extra[ad["id"]] = {"coords": list(ad["coords"]) if ad.get("coords") else None,
                           "paylivery": bool(ad.get("paylivery"))}

    ads, _, _ = fetch(search)
    if not ads:
        log("%s: 0 Treffer zurück - Suche prüfen" % name)
        return 0

    reach = (search.get("filter") or {}).get("reach")
    new = [a for a in ads if a["id"] not in seen_set]
    new.reverse()   # ältestes zuerst, damit die Chat-Reihenfolge stimmt

    sent, failed = 0, set()
    # Treffer, die diesmal nicht mehr in den Chat passen. Sie bleiben
    # ungemerkt, damit der nächste Durchlauf sie erneut aufgreift.
    deferred, hits = set(), 0
    limit = int(search.get("max_notify_per_run") or MAX_NOTIFY_PER_RUN)

    # Preisänderungen bei Inseraten, die schon bekannt sind und weiter zum
    # Filter passen. Preis 0 heißt "keine Angabe" und zählt nicht als
    # Änderung; ohne bekannten alten Preis (z.B. nach Migration) wird der
    # Preis nur stumm gemerkt, es gibt sonst eine Phantom-Meldung.
    for ad in ads:
        if ad["id"] not in seen_set:
            continue
        ok, _ = matches(ad, search.get("filter"))
        if not ok:
            continue
        old_price = prices.get(ad["id"])
        if not (old_price and ad["price"] and old_price != ad["price"]):
            track(ad)
            continue
        if dry_run:
            log("%s: [DRY] Preisänderung %s -> %s | %s"
                % (name, format_price(old_price), ad["price_display"], ad["title"][:60]))
        else:
            try:
                send_telegram_price_change(tg, ad, old_price, reach)
                log("%s: PREISÄNDERUNG %s -> %s | %s"
                    % (name, format_price(old_price), ad["price_display"], ad["title"][:60]))
            except Exception as e:
                log("%s: Telegram-Fehler bei Preisänderung (%s) - erneuter Versuch später"
                    % (name, e))
                continue    # alten Preis behalten, damit die Änderung erneut auffällt
        track(ad)
        sent += 1

    for ad in new:
        ok, why = matches(ad, search.get("filter"))
        if not ok:
            continue
        if first_run:
            track(ad)
            continue
        hits += 1
        if hits > limit and not dry_run:
            deferred.add(ad["id"])
            continue
        if dry_run:
            log("%s: [DRY] %s | %s" % (name, ad["price_display"], ad["title"][:60]))
        else:
            try:
                send_telegram(tg, ad, reach)
                log("%s: TREFFER %s | %s" % (name, ad["price_display"], ad["title"][:60]))
            except Exception as e:
                log("%s: Telegram-Fehler (%s) - erneuter Versuch später" % (name, e))
                failed.add(ad["id"])
                continue
        track(ad)
        sent += 1

    if deferred:
        log("%s: %d weitere Treffer auf die nächsten Durchläufe verschoben "
            "(höchstens %d Meldungen je Lauf)" % (name, len(deferred), limit))

    # Nicht zugestellte und zurückgestellte Treffer bleiben ungemerkt, damit
    # sie wiederkommen.
    fresh = [a["id"] for a in ads
             if a["id"] not in seen_set
             and a["id"] not in failed
             and a["id"] not in deferred]
    entry["seen"] = (fresh + seen)[:MAX_SEEN_PER_SEARCH]
    # Preise/SEO-Pfade nur für die neuesten Treffer behalten - das begrenzt
    # sowohl den Speicher als auch die Kosten der vollen Preisprüfung.
    kept = set(entry["seen"][:MAX_TRACKED_PRICES])
    entry["prices"] = {k: v for k, v in prices.items() if k in kept}
    entry["seo"] = {k: v for k, v in seo_map.items() if k in kept}
    entry["extra"] = {k: v for k, v in extra.items() if k in kept}

    if first_run:
        log("%s: Erstlauf, %d Inserate als bekannt markiert (keine Meldung)"
            % (name, len(ads)))
    return sent


def recheck_prices(cfg, state, dry_run=False):
    """Volle Preisprüfung über alle gemerkten Treffer, auch außerhalb des
    Abfragefensters (`rows`) der normalen Suche.

    Kostet pro Inserat einen eigenen HTTP-Request an die willhaben-
    Detailseite, deshalb deutlich seltener als der normale Durchlauf.
    Inserate, die nicht mehr aktiv sind (verkauft/gelöscht/abgelaufen),
    werden komplett vergessen - auch aus der `seen`-Liste, nicht nur aus
    der Preisverfolgung - statt endlos weiter geprüft zu werden.
    """
    tg = cfg.get("telegram", {})
    meta = state.setdefault("_meta", {})
    try:
        build_id = fetch_build_id()
    except Exception as e:
        log("Preisprüfung (voll): buildId nicht ladbar (%s)" % e)
        meta["last_error"] = str(e)
        return 0
    meta.pop("last_error", None)

    sent, checked, dropped = 0, 0, 0
    for search in cfg.get("searches", []):
        if search.get("enabled") is False:
            continue
        name = search.get("name") or "unbenannt"
        entry = search_entry(state, name)
        prices = entry["prices"]
        seo_map = entry["seo"]
        extra = entry["extra"]
        reach = (search.get("filter") or {}).get("reach")

        def forget(ad_id):
            """Ein verschwundenes Inserat aus der Preisverfolgung nehmen.

            Die `seen`-Liste bleibt bewusst unangetastet: sie ist das
            Gedächtnis, was schon gemeldet wurde. Eine ID dort zu streichen,
            die die Suche weiterhin liefert, macht das Inserat im nächsten
            Durchlauf wieder zum "neuen" Treffer - und das bei jedem
            Durchlauf erneut. Der Platz, den die ID belegt, ist dagegen
            vernachlässigbar; sie fällt ohnehin aus MAX_SEEN_PER_SEARCH.
            """
            prices.pop(ad_id, None)
            seo_map.pop(ad_id, None)
            extra.pop(ad_id, None)

        for ad_id, seo in list(seo_map.items()):
            checked += 1
            try:
                ad = fetch_ad_detail(build_id, seo)
            except AdGone:
                dropped += 1
                log("%s: %s gibt es nicht mehr - Preisverfolgung beendet"
                    % (name, ad_id))
                forget(ad_id)
                continue
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    forget(ad_id)
                else:
                    log("%s: Preisprüfung %s fehlgeschlagen (HTTP %s)"
                        % (name, ad_id, e.code))
                continue
            except Exception as e:
                log("%s: Preisprüfung %s fehlgeschlagen (%s)" % (name, ad_id, e))
                continue
            finally:
                time.sleep(0.3)    # willhaben nicht mit Einzelabfragen bombardieren

            # Rückfall für Inserate, deren Detailseite nichts dazu sagt.
            merk = extra.get(ad_id) or {}
            if merk.get("coords") and not ad.get("coords"):
                ad["coords"] = tuple(merk["coords"])
            if ad.get("paylivery") is None:
                ad["paylivery"] = bool(merk.get("paylivery"))

            if not ad["active"]:
                dropped += 1
                log("%s: %s nicht mehr verfügbar (%s) - Preisverfolgung beendet"
                    % (name, ad_id, ad.get("status") or "unbekannt"))
                forget(ad_id)
                continue

            old_price = prices.get(ad_id)
            if not (old_price and ad["price"] and old_price != ad["price"]):
                if ad["price"]:
                    prices[ad_id] = ad["price"]
                continue

            if dry_run:
                log("%s: [DRY] Preisänderung (voll) %s -> %s | %s"
                    % (name, format_price(old_price), ad["price_display"], ad["title"][:60]))
            else:
                try:
                    send_telegram_price_change(tg, ad, old_price, reach)
                    log("%s: PREISÄNDERUNG (voll) %s -> %s | %s"
                        % (name, format_price(old_price), ad["price_display"], ad["title"][:60]))
                except Exception as e:
                    log("%s: Telegram-Fehler bei Preisänderung (%s) - erneuter Versuch später"
                        % (name, e))
                    continue
            prices[ad_id] = ad["price"]
            sent += 1

    meta["checked"] = checked
    meta["changed"] = sent
    meta["dropped"] = dropped
    if checked:
        log("Preisprüfung (voll): %d Inserat(e) geprüft, %d Änderung(en), "
            "%d nicht mehr verfügbar" % (checked, sent, dropped))
    return sent


def maybe_recheck_prices(cfg, state, dry_run=False):
    """Löst recheck_prices() nur im Abstand von `price_check_interval` aus."""
    interval = int(os.environ.get(
        "PRICE_CHECK_INTERVAL", cfg.get("price_check_interval")) or 0)
    if interval <= 0 or dry_run:
        return 0
    meta = state.setdefault("_meta", {})
    last = meta.get("last_price_check", 0)
    now_ts = time.time()
    if now_ts - last < interval:
        return 0
    meta["last_price_check"] = now_ts
    return recheck_prices(cfg, state, dry_run)


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

    try:
        sent += maybe_recheck_prices(cfg, state, dry_run)
    except Exception as e:
        errors += 1
        log("Preisprüfung (voll): Fehler %s" % e)

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
