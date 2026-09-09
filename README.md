# willhaben-advanced-suchagent

Eigener Suchagent für willhaben mit Weboberfläche und Telegram-Push. Fragt
beliebig viele Suchen im Minutentakt ab statt zweimal am Tag.

## Wie es funktioniert

willhaben rendert seine Suchseite aus einer JSON-REST-API, die auch direkt
ansprechbar ist:

```
https://ad-search.willhaben.at/restapi/v2/search/atz/seo/<pfad>?<parameter>
```

Zwingend ist der Header `x-wh-client` — ohne ihn antwortet die API mit
HTTP 400. `sort=1` liefert die neuesten Inserate zuerst, mit Zeitstempel
sekundengenau.

Der Agent merkt sich gesehene Inserats-IDs in `data/state.json` und meldet
nur, was neu ist und durch die Filter kommt. Beim allerersten Lauf einer
Suche wird der aktuelle Bestand stumm als bekannt markiert, damit nicht 30
Altmeldungen auf einmal kommen.

Ein Prozess, zwei Teile: ein Poller-Thread und ein HTTP-Server für die
Oberfläche. Der Poller liest die Konfiguration vor jedem Durchlauf neu,
Änderungen greifen also ohne Neustart. Speichern in der Oberfläche weckt
ihn sofort.

## Oberfläche

Nach dem Start erreichbar unter `http://localhost:8088`. Dort lassen sich
beliebig viele Suchen anlegen, einzeln aktivieren und löschen.

Der wichtigste Knopf ist **Vorschau**. Er fragt die Suche live ab und zeigt
für jedes Inserat, ob es durchkäme, und bei Verwerfung welcher Filter
gegriffen hat. Es wird dabei nichts verschickt und nichts als gesehen
vermerkt. Filter lassen sich damit nachschärfen, ohne den Chat zuzumüllen.

**Zustand verwerfen** setzt eine Suche zurück. Der nächste Lauf markiert den
Bestand stumm neu, statt alles auf einmal zu melden.

Eine Suche wird über ihren **Namen** verwaltet — beim Umbenennen beginnt sie
wieder von vorn.

## Filter

Drei Ebenen, von grob nach fein.

**1. Bei willhaben** — der wirksamste Filter, weil er die Treffermenge schon
vor dem Abruf reduziert. Im Test drückte das die Zahl von 7435 auf 430:

| Parameter | Bedeutung |
|---|---|
| `keyword` | Suchbegriff |
| `PRICE_FROM` / `PRICE_TO` | Preisspanne in Euro |
| `ISPRIVATE` | `1` = nur Privatanbieter |
| `areaId` | Bundesland |
| `periode` | Suchzeitraum in Tagen |
| `paylivery` | nur PayLivery-Inserate |
| `ATTRIBUTE_TREE` | Kategorie |

Am bequemsten stellt man die Suche im Browser ein und fügt die fertige URL
in der Oberfläche ein. Dann muss man keinen Parameter selbst kennen.

**2. Wörter** — das ist der normale Weg in der Oberfläche. Wörter werden
mit Komma getrennt eingetippt, Groß- und Kleinschreibung ist egal, und
einfache deutsche Mehrzahlformen werden mitgetroffen (`Spiel` trifft auch
`Spiele`, `Controller` auch `Controllern`).

| Feld | Wirkt auf |
|---|---|
| `require_words` | eines davon muss im Titel stehen |
| `block_title_words` | nirgends im Titel |
| `block_words` | nicht als Hauptartikel |
| `block_text_words` | nicht in Titel oder Beschreibung |
| `price_min` / `price_max` | Preis, `0` gilt als "keine Angabe" |
| `private_only` | Privatanbieter |

### Erreichbarkeit: PayLivery oder Umkreis

willhaben kennt serverseitig nur "nur PayLivery" (`paylivery=1`). Eine
Oder-Verknüpfung gibt es dort nicht, deshalb rechnet der Agent sie selbst.
Jedes Inserat liefert `p2penabled` als PayLivery-Kennzeichen und
`COORDINATES` als Standort; die Entfernung ist die Luftlinie zum
eingestellten Bezugspunkt.

Vier Modi in `filter.reach.mode`:

| Modus | Bedeutung |
|---|---|
| `off` | kein Erreichbarkeitsfilter |
| `paylivery` | nur PayLivery, wird an willhaben durchgereicht |
| `radius` | nur innerhalb von `km` um `lat`/`lon` |
| `paylivery_or_radius` | PayLivery **oder** nah genug |

Nur bei `paylivery` wird `paylivery=1` mitgeschickt. Bei der Oder-Variante
darf das nicht passieren, sonst fehlen genau die Inserate, die allein über
die Entfernung hereinkommen.

Den Bezugspunkt gibt man als Adresse, Ort oder Postleitzahl ein; bei
mehreren Treffern wählt man aus einer Liste. Der Knopf "Mein Standort"
fragt stattdessen den Browser und trägt die zugehörige Adresse ein.

Aufgelöst wird über Nominatim (OpenStreetMap), eingeschränkt auf
Österreich — ohne das landet eine bloße Postleitzahl wie `4020` in
Norwegen, weil vierstellige PLZ weltweit mehrdeutig sind. Der Server hält
die Vorgaben von Nominatim ein: eigener User-Agent, höchstens eine Anfrage
pro Sekunde, Ergebnisse werden zwischengespeichert. Angefragt wird nur auf
Knopfdruck.

Suchtext beziehungsweise Koordinaten gehen dabei an OpenStreetMap.
Gespeichert wird das Ergebnis ausschließlich in `data/config.json`, und
das Verzeichnis ist von git ausgeschlossen.

Geprüft auf einem festen Satz von 50 Inseraten: die Oder-Menge ist exakt
die Vereinigung der beiden Einzelmengen, und Linz nach Wien ergibt 155 km.

**3. Reguläre Ausdrücke** — für Sonderfälle. Die Felder `title_regex`,
`exclude_title`, `exclude_title_head` und `exclude_body` gelten
zusätzlich zu den Wörtern. Sie werden derzeit nur in `data/config.json`
gepflegt; die Oberfläche zeigt sie nicht an, lässt sie beim Speichern aber
unangetastet. Dasselbe gilt für willhaben-Parameter ohne eigenes Feld.

### Schreibweisen bei den Pflichtwörtern

Zwischen den Teilen eines mehrteiligen Worts steht `\s*`, also null oder
mehr Leerzeichen. Ein mit Leerzeichen getipptes Wort deckt deshalb auch die
zusammengeschriebene Form ab, umgekehrt aber nicht:

| eingetippt | trifft |
|---|---|
| `play station 5` | Play Station 5, PlayStation 5, Playstation5 |
| `playstation 5` | PlayStation 5, Playstation5 — **nicht** Play Station 5 |
| `ps 5` | PS 5, PS5 |

Mehrteilige Begriffe also lieber getrennt schreiben. `playstation 5`
zusätzlich zu `play station 5` einzutragen bringt nichts.

Gemessen an 200 Inseraten der PS5-Suche: die Titelpflicht verwirft 67
davon. Darunter waren Poster, Monitore, Lenkräder, PSVR2 und PSP — und
genau eine echte Konsole, betitelt "PlayStation fünf digitale Edition".
Deshalb steht `playstation fünf` mit in der Liste. Die Titelpflicht lohnt
sich also deutlich, sie braucht nur die Schreibvarianten.

### Warum es „Hauptartikel“ getrennt gibt

Im Titel eines Bundles ist Zubehör eine Beigabe, nicht das Produkt. Ein
Ausschluss von `Controller` auf dem ganzen Titel verwirft
"PS5 Slim Digital + 2 DualSense Controller" — also genau die guten
Angebote. Deshalb prüft `block_words` nur den Teil des Titels vor der
ersten Aufzählung, also vor dem ersten `+`, `inkl`, `|`, `&` oder `,`.
"Playstation 5 Spiele" fliegt damit raus, das Bundle bleibt drin.

### Wörter statt Teilstrings

Aus Wörtern erzeugt der Agent Muster mit Wortgrenzen. Das ist der Grund,
warum ein Ausschluss `kaufe` nicht jedes "Verkaufe ..." mitnimmt und
`Spiel` nicht "Beispiel" trifft. Sonderzeichen werden entwertet, ein Wort
wie `c++` funktioniert also ohne Weiteres.

Wer direkt in `data/config.json` oder über die API schreibt, muss bei
regulären Ausdrücken aufpassen: in JSON bedeutet ein einfacher
Backslash vor dem b das Steuerzeichen Backspace, die Wortgrenze braucht
einen doppelten Backslash. Der Server lehnt Muster mit Steuerzeichen
deshalb ab, statt sie still wirkungslos zu übernehmen.

## Einrichtung

### 1. Telegram-Bot

In Telegram `@BotFather` anschreiben, `/newbot`, Namen vergeben. Der Bot
antwortet mit dem Token. Dann dem eigenen Bot einmal `/start` schicken und
die Chat-ID abholen:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

### 2. Starten

```bash
cp .env.example .env      # Token und Chat-ID eintragen
docker compose up -d --build
```

Oberfläche öffnen: `http://localhost:8088`. Beim ersten Start wird
`data/config.json` aus `config.example.json` angelegt.

## Betrieb

`POLL_INTERVAL` steht auf 60 Sekunden. Bei ~130 KB pro Abfrage und Suche
sind das rund 180 MB am Tag. Unter 30 Sekunden ist wenig sinnvoll, weil
neue Inserate ohnehin erst mit Freischaltung sichtbar werden. Nach Fehlern
verdoppelt der Agent den Abstand bis maximal das 16-Fache und geht bei der
ersten erfolgreichen Abfrage wieder auf den Normaltakt.

Schlägt der Telegram-Versand fehl, wird die Inserats-ID **nicht** als
gesehen vermerkt. Der nächste Durchlauf versucht es erneut, statt den
Treffer still zu verlieren.

Die Oberfläche ist standardmäßig ungeschützt. Für den Betrieb auf der NAS
`UI_USER` und `UI_PASSWORD` in der `.env` setzen, dann verlangt der Server
HTTP-Basic-Auth.

### Datenverzeichnis

Konfiguration und Zustand liegen zusammen in `data/`, das als ganzes
Verzeichnis in den Container gemountet wird. Das ist Absicht: die
Oberfläche schreibt `config.json` per atomarem Rename, und das scheitert,
wenn der Bind-Mount auf einer einzelnen Datei liegt.

### Kommandozeile

Der Agent läuft auch ohne Oberfläche:

```bash
docker compose run --rm willhaben-agent python /app/agent.py --dry-run
docker compose run --rm willhaben-agent python /app/agent.py --once
```

## Deployment auf die NAS

Der Workflow unter `.github/workflows/docker.yml` baut bei jedem Push auf
`main` ein Multi-Arch-Image und schiebt es nach Docker Hub. Nötig sind die
Repository-Secrets `DOCKERHUB_USERNAME` und `DOCKERHUB_TOKEN`.

Auf der NAS dann mit `compose.prod.yaml` starten — die zieht das fertige
Image, statt lokal zu bauen:

```bash
docker compose -f compose.prod.yaml up -d
```
