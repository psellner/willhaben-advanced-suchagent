#!/usr/bin/env python3
"""
Weboberfläche + Poller in einem Prozess.

Der Poller läuft als Hintergrund-Thread und liest die Konfiguration vor
jedem Durchlauf neu ein. Änderungen aus der Oberfläche greifen damit ohne
Neustart.

Endpunkte:
    GET    /                     Oberfläche
    GET    /healthz              Healthcheck, ohne Passwortschutz
    GET    /api/state            Konfiguration + Laufzeitstatus
    POST   /api/geocode          Adresse zu Koordinaten (OpenStreetMap)
    POST   /api/reverse          Koordinaten zu Adresse
    PUT    /api/config           gesamte Konfiguration speichern
    POST   /api/preview          Suche testen, ohne etwas zu verschicken
    POST   /api/reset            Zustand einer Suche verwerfen
    POST   /api/test-telegram    Testnachricht verschicken
"""

import base64
import json
import os
import urllib.parse
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import agent

PORT = int(os.environ.get("PORT", "8088"))
UI_USER = os.environ.get("UI_USER", "")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

STATUS = {
    "started": None,
    "last_poll": None,
    "next_poll": None,
    "interval": 60,
    "sent_total": 0,
    "searches": {},
    "last_error": None,
}
WAKE = threading.Event()


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- geocoding

# Nominatim (OpenStreetMap) verlangt einen aussagekräftigen User-Agent und
# höchstens eine Anfrage pro Sekunde. Beides wird hier eingehalten; Anfragen
# entstehen ohnehin nur, wenn jemand in der Oberfläche auf Suchen drückt.
GEO_URL = "https://nominatim.openstreetmap.org/search"
GEO_UA = "willhaben-agent/1.0 (self-hosted search notifier)"
GEO_CACHE = {}
GEO_LOCK = threading.Lock()
_geo_last = [0.0]


def geocode(query):
    query = " ".join(query.split())
    if not query:
        return []
    with GEO_LOCK:
        if query in GEO_CACHE:
            return GEO_CACHE[query]
        gap = time.time() - _geo_last[0]
        if gap < 1.1:
            time.sleep(1.1 - gap)
        # Auf Österreich eingegrenzt: eine bloße Postleitzahl wie "4020"
        # landet sonst in Norwegen, weil vierstellige PLZ mehrdeutig sind.
        url = GEO_URL + "?" + urllib.parse.urlencode({
            "q": query, "format": "jsonv2", "limit": "5",
            "addressdetails": "1", "countrycodes": "at"})
        raw = agent.http_get(url, headers={
            "User-Agent": GEO_UA, "Accept-Language": "de"}, timeout=15)
        _geo_last[0] = time.time()
        hits = [{"label": r.get("display_name", ""),
                 "lat": round(float(r["lat"]), 4),
                 "lon": round(float(r["lon"]), 4)}
                for r in json.loads(raw.decode("utf-8"))]
        GEO_CACHE[query] = hits
        return hits


REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"


def reverse_geocode(lat, lon):
    """Koordinaten zu einer lesbaren Adresse, für den Standortknopf."""
    key = ("rev", round(float(lat), 4), round(float(lon), 4))
    with GEO_LOCK:
        if key in GEO_CACHE:
            return GEO_CACHE[key]
        gap = time.time() - _geo_last[0]
        if gap < 1.1:
            time.sleep(1.1 - gap)
        url = REVERSE_URL + "?" + urllib.parse.urlencode({
            "lat": lat, "lon": lon, "format": "jsonv2", "zoom": "16"})
        raw = agent.http_get(url, headers={
            "User-Agent": GEO_UA, "Accept-Language": "de"}, timeout=15)
        _geo_last[0] = time.time()
        d = json.loads(raw.decode("utf-8"))
        a = d.get("address") or {}
        # Kurzform statt der vollen Kette: Straße, PLZ, Ort reicht als Anzeige.
        parts = [a.get("road"), a.get("postcode"),
                 a.get("city") or a.get("town") or a.get("village")
                 or a.get("municipality")]
        label = ", ".join(p for p in parts if p) or d.get("display_name", "")
        GEO_CACHE[key] = label
        return label


# ---------------------------------------------------------------- poller

def poller():
    STATUS["started"] = now()
    fails = 0
    while True:
        cfg = agent.load_config()
        interval = int(os.environ.get("POLL_INTERVAL", cfg["poll_interval"]) or 60)
        STATUS["interval"] = interval

        if cfg["telegram"]["token"] and cfg["telegram"]["chat_id"]:
            try:
                sent, errors = agent.poll_once(cfg, status=STATUS["searches"])
                STATUS["sent_total"] += sent
                STATUS["last_error"] = None
                fails = fails + 1 if errors else 0
            except Exception as e:
                fails += 1
                STATUS["last_error"] = str(e)
                agent.log("Poller-Fehler: %s" % e)
        else:
            STATUS["last_error"] = "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID fehlen"

        STATUS["last_poll"] = now()
        wait = interval * min(2 ** fails, 16) if fails else interval
        STATUS["next_poll"] = datetime.fromtimestamp(
            time.time() + wait, timezone.utc).astimezone().isoformat(timespec="seconds")

        # Speichern in der Oberfläche weckt den Poller sofort.
        WAKE.wait(timeout=wait)
        WAKE.clear()


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "willhaben-agent"

    def log_message(self, fmt, *args):
        pass    # Zugriffs-Log würde nur die Poller-Ausgabe zumüllen.

    # -------------------------------------------------- helpers

    def authorized(self):
        if not UI_PASSWORD:
            return True
        want = base64.b64encode(
            ("%s:%s" % (UI_USER, UI_PASSWORD)).encode()).decode()
        got = self.headers.get("Authorization", "")
        return got == "Basic " + want

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def guard(self):
        if self.authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="willhaben-agent"')
        self.end_headers()
        return False

    # -------------------------------------------------- routes

    def do_GET(self):
        path = self.path.split("?")[0]

        # Vor der Passwortprüfung, damit ein Healthcheck auch mit gesetztem
        # UI_PASSWORD funktioniert. Gibt nichts Vertrauliches preis.
        if path == "/healthz":
            return self.send_json({"ok": True, "last_poll": STATUS["last_poll"]})

        if not self.guard():
            return

        if path in ("/", "/index.html"):
            return self.send_file(os.path.join(WEB_DIR, "index.html"),
                                  "text/html; charset=utf-8")

        if path == "/api/state":
            cfg = agent.load_config()
            tg = cfg.pop("telegram", {})
            seen = agent.load_json(agent.STATE_PATH, {})
            return self.send_json({
                "config": cfg,
                "telegram_ready": bool(tg.get("token") and tg.get("chat_id")),
                "status": STATUS,
                "seen_counts": {k: len(agent.seen_ids(v))
                                 for k, v in seen.items() if k != "_meta"},
            })

        self.send_error(404)

    def do_PUT(self):
        self.do_POST()

    def do_POST(self):
        if not self.guard():
            return
        path = self.path.split("?")[0]
        try:
            data = self.read_json()
        except ValueError as e:
            return self.send_json({"error": "ungültiges JSON: %s" % e}, 400)

        try:
            if path == "/api/config":
                return self.save_config(data)
            if path == "/api/preview":
                return self.preview(data)
            if path == "/api/reset":
                return self.reset(data)
            if path == "/api/test-telegram":
                return self.test_telegram()
            if path == "/api/geocode":
                return self.send_json({"results": geocode(data.get("q", ""))})
            if path == "/api/reverse":
                return self.send_json({"address": reverse_geocode(
                    data.get("lat"), data.get("lon"))})
        except Exception as e:
            traceback.print_exc()
            return self.send_json({"error": str(e)}, 500)

        self.send_error(404)

    # -------------------------------------------------- handlers

    PATTERN_KEYS = ("exclude_title", "exclude_title_head", "exclude_body", "include")

    def check_patterns(self, searches):
        """Steuerzeichen in Mustern melden.

        Typischer Fehler beim Schreiben über die API: in JSON ist "\\b" das
        Steuerzeichen Backspace, die Regex-Wortgrenze braucht "\\\\b". Der
        Filter greift dann stillschweigend nie. Lieber laut ablehnen.
        """
        for s in searches:
            f = s.get("filter") or {}
            pats = [f.get("title_regex")] if f.get("title_regex") else []
            for k in self.PATTERN_KEYS:
                pats += list(f.get(k) or [])
            for p in pats:
                if not isinstance(p, str):
                    continue
                bad = [c for c in p if ord(c) < 32]
                if bad:
                    return ("Muster in „%s“ enthält ein Steuerzeichen "
                            "(%s). Gemeint ist vermutlich eine Wortgrenze: in "
                            "JSON muss sie \\\\b geschrieben werden, nicht \\b."
                            % (s.get("name"), ", ".join("0x%02x" % ord(c) for c in bad)))
        return None

    def save_config(self, data):
        searches = data.get("searches")
        if not isinstance(searches, list):
            return self.send_json({"error": "searches fehlt"}, 400)

        bad = self.check_patterns(searches)
        if bad:
            return self.send_json({"error": bad}, 400)

        names = [s.get("name", "").strip() for s in searches]
        if any(not n for n in names):
            return self.send_json({"error": "jede Suche braucht einen Namen"}, 400)
        if "_meta" in names:
            # Reservierter Schlüssel im Zustand (Zeitpunkt der letzten vollen
            # Preisprüfung) - als Suchname würde er den Zustand überschreiben.
            return self.send_json({"error": "„_meta“ ist als Name reserviert"}, 400)
        if len(set(names)) != len(names):
            return self.send_json(
                {"error": "Namen müssen eindeutig sein - der Name führt den Zustand"},
                400)

        cfg = agent.load_config()
        cfg["searches"] = searches
        cfg["poll_interval"] = int(data.get("poll_interval") or 60)
        agent.save_config(cfg)
        WAKE.set()      # nächsten Durchlauf sofort auslösen
        return self.send_json({"ok": True, "searches": len(searches)})

    def preview(self, data):
        search = data.get("search") or {}
        rows, total = agent.evaluate(search, limit=int(data.get("limit") or 0) or None)
        return self.send_json({
            "total_on_willhaben": total,
            "checked": len(rows),
            "passed": sum(1 for r in rows if r["ok"]),
            "api_url": agent.build_api_url(search),
            "results": rows,
        })

    def reset(self, data):
        name = data.get("name")
        state = agent.load_json(agent.STATE_PATH, {})
        if data.get("all"):
            state = {}
        else:
            state.pop(name, None)
        agent.save_json(agent.STATE_PATH, state)
        # Nach dem Verwerfen gilt der nächste Lauf wieder als Erstlauf und
        # markiert den Bestand stumm - sonst käme der ganze Bestand als Meldung.
        return self.send_json({"ok": True})

    def test_telegram(self):
        cfg = agent.load_config()
        tg = cfg["telegram"]
        if not (tg.get("token") and tg.get("chat_id")):
            return self.send_json({"error": "Token oder Chat-ID fehlt"}, 400)
        agent.http_post_json(
            "https://api.telegram.org/bot%s/sendMessage" % tg["token"],
            {"chat_id": tg["chat_id"],
             "text": "Test aus der willhaben-Agent Oberfläche."})
        return self.send_json({"ok": True})


def seed_config():
    """Beim ersten Start die Beispielkonfiguration ins Datenverzeichnis legen."""
    if os.path.exists(agent.CONFIG_PATH):
        return
    example = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config.example.json")
    cfg = agent.load_json(example, {"poll_interval": 60, "searches": []})
    agent.save_json(agent.CONFIG_PATH, cfg)
    agent.log("Konfiguration angelegt: %s" % agent.CONFIG_PATH)


def main():
    seed_config()
    threading.Thread(target=poller, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    agent.log("Oberfläche auf http://0.0.0.0:%d%s"
              % (PORT, " (passwortgeschützt)" if UI_PASSWORD else ""))
    srv.serve_forever()


if __name__ == "__main__":
    main()
