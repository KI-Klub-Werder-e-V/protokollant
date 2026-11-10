# Mitmachen bei Protokollant

Schön, dass Du mithelfen willst! Sicherheitslücken bitte **nicht** als Issue melden, sondern wie in [SECURITY.md](SECURITY.md) beschrieben. Für alle Beiträge gilt unser [Verhaltenskodex](CODE_OF_CONDUCT.md).

## Einrichtung

Voraussetzungen: Python 3.12 und [uv](https://docs.astral.sh/uv/).

```sh
uv run pytest
```

## Wie wir arbeiten

- Das Verhalten steht in [docs/SPEC.md](docs/SPEC.md), die Reihenfolge in [docs/BUILD.md](docs/BUILD.md). Eine Änderung am Verhalten ändert zuerst die Spezifikation.
- Jeder Zustandswechsel einer Sitzung läuft über `protokollant/meetings.py`. Oberfläche, API und MCP rufen dieselben Funktionen auf, damit die Einwilligungs-Sperre nicht umgangen werden kann.
- Prüfprotokoll-Einträge entstehen in derselben Transaktion wie die Änderung.
- Neue Logik bringt einen Test mit. Tests mit echten Modellen bekommen die Markierung `@pytest.mark.ml`.
- Texte in der Oberfläche sind deutsch und verwenden „Du“.

## Keine echten Aufnahmen

Bitte lade nie echte Sitzungsaufnahmen, Transkripte oder Namen in Issues, Pull Requests oder Testdaten hoch. Testaufnahmen entstehen nur mit Einwilligung aller Sprechenden und werden als solche gekennzeichnet.

## Pull Requests

1. Branch anlegen, Änderung mit Test.
2. `uv run pytest` ist grün.
3. Pull Request mit kurzer Beschreibung: was, warum, wie getestet.
