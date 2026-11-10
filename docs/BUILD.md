# Protokollant – Bauplan

Setzt [SPEC.md](SPEC.md) um. Die Spezifikation sagt, was das System tut; dieser Plan sagt, in welcher Reihenfolge und womit es gebaut wird.

## Regeln

- Ein Prozess für die Web-App, ein Prozess für den Worker, eine SQLite-Datei.
- Abhängigkeiten nur, wenn sie mehr als ein paar Zeilen sparen.
- Die Zustandsübergänge liegen an einer Stelle (`meetings.py`). UI, API und MCP rufen dieselben Funktionen auf, damit die Einwilligungs-Sperre nicht umgangen werden kann.
- Jeder Schritt endet mit grünen Tests. Abnahmekriterien aus SPEC Abschnitt 9 werden 1:1 zu Tests.

## Struktur

```
protokollant/
  app.py          FastAPI, Routen, Anmeldung
  db.py           SQLite-Verbindung, Schema, Trigger
  meetings.py     Zustandsautomat, Einwilligung, Freigabe, Löschung
  worker.py       Job-Schleife: normalisieren, transkribieren, Sprecher, Entwurf
  engines.py      Whisper, pyannote, Sprachmodell hinter je einer Funktion
  drafting.py     Prompt, Schema, Prüfung der Quellstellen
  openverein.py   Vorschau und Übergabe
  minutes.py      Vorlage, PDF, Beschlussbuch-Export
  mcp_server.py   lesend plus „Entwurf anlegen“
  templates/      Jinja-Seiten, Protokollvorlage
  static/         recorder.js, style.css
tests/
deploy/           docker-compose.yml, Caddyfile, Dockerfile
```

## Abhängigkeiten

| Paket | Wofür |
|---|---|
| fastapi, uvicorn, jinja2, python-multipart, itsdangerous | Web, Sitzungs-Cookie |
| httpx | OpenVerein und Sprachmodell |
| weasyprint | PDF |
| mcp | MCP-Server |
| Extra `ml`: faster-whisper, pyannote.audio | nur im Worker |
| pytest | Tests |

Passwörter: `hashlib.scrypt` aus der Standardbibliothek. Konten legt `protokollant user add` an.

## Bauschritte

| # | Baut | Fertig, wenn |
|---|---|---|
| 1 | `db.py`: Schema aus SPEC 7, Trigger gegen `UPDATE`/`DELETE` auf `consent_event`, `resolution`, `audit` | Trigger-Tests grün |
| 2 | `meetings.py`: Zustände, E1 bis E11 | Sperre, Nachzügler, Nein, Widerruf, „nicht aufnehmen“ getestet |
| 3 | `app.py`: Anmeldung, Sitzung anlegen, Einwilligungsseite, Teil-Upload, Datei-Upload mit Erklärung | `409` über HTTP getestet |
| 4 | `static/recorder.js`: Aufnahme, Teil-Upload alle 10 s, Pegel | Handtest im Browser |
| 5 | `worker.py`, `engines.py`: Job-Tabelle, Wiederholung nach Fehler, ffmpeg | Lauf mit Test-Engines; ein echter Lauf mit kurzer Datei |
| 6 | `drafting.py`: Schema-Ausgabe, Verwerfen ohne Quellstelle, Stimmen nur aus dem Transkript | Tests mit manipulierten Modellantworten |
| 7 | Prüfansicht, Einzelbestätigung, Freigabe, Löschung von Audio und Transkript | keine Dateien nach Freigabe |
| 8 | `minutes.py`: Vorlage, PDF, Beschlussbuch mit Nummern | PDF erzeugt, Nummern lückenlos |
| 9 | `openverein.py`: Vorschau, Übergabe, Task-ID-Ablage, `409` bei abgeschlossenen Versammlungen | Test gegen Fake-Server; Handtest gegen OpenVerein |
| 10 | `mcp_server.py`, `deploy/`, Offline-CI, README | Release-Kandidat |
| 11 | GitHub: Veröffentlichung und GEO | siehe unten |

## Tests

Echte Modelle laufen nicht in der CI. `engines.py` hat genau drei Funktionen (`transcribe`, `diarize`, `complete`), die Tests per Monkeypatch ersetzen. Ein markierter Test `@pytest.mark.ml` läuft bei Bedarf mit echten Modellen.

## Bekannte Grenzen der Bauumgebung

- Entwicklungsrechner: 4 Kerne, 7 GB RAM, kein Docker. `deploy/` lässt sich hier nicht starten und muss in der CI oder auf Zielhardware geprüft werden.
- Vereinsserver: bisher nur ein kleines Sprachmodell (3B) installiert. Das reicht zum Durchstich, nicht als Qualitätsaussage für deutsche Protokolle.
- pyannote braucht einmalig einen Hugging-Face-Token mit angenommenen Bedingungen.
- Offen vor einem produktiven Einsatz: Prüfung vorhandener Projekte, Testsatz, Rechtsfragen (SPEC 10, 12, 13).

## GitHub und GEO (Schritt 11, nach dem Bau)

Ziel: Menschen und KI-Suchsysteme finden das Projekt bei Fragen wie „Vereinsprotokoll KI lokal“ und geben es korrekt wieder.

- README: erster Absatz sagt in zwei Sätzen, was es ist, für wen und was es nicht tut. Danach Funktionen, Einwilligung, Installation.
- Abschnitt mit echten Fragen und direkten Antworten (Ist eine Aufnahme ohne Einwilligung möglich? Läuft es offline? Was passiert mit dem Audio?).
- Nur gemessene Aussagen. Keine Genauigkeitszahl ohne Testsatz, kein „DSGVO-konform“ als Pauschalaussage.
- Vergleichstabelle mit Meetily und Scriberr, erst nach Prüfung der Repos.
- Repo-Beschreibung, Topics (`verein`, `protokoll`, `whisper`, `self-hosted`, `dsgvo`, `mcp`, `speech-to-text`, `german`), `CITATION.cff`, `llms.txt`, Screenshots mit Alt-Text, Release mit Notizen, Verweise aus OpenVerein und von ki-klub-werder.de.
- Das Anlegen des öffentlichen Repos erfolgt erst nach ausdrücklicher Bestätigung.
