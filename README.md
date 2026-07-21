# BTN Backlink-Audit — monatlich, automatisch

Zieht jeden Monat einen Backlink-Snapshot von Semrush für btn-muenzen.de,
vergleicht ihn mit 24 Monaten Historie, baut daraus einen beratenden PDF-Report
im BTN-Design und schickt ihn per Mail. Läuft komplett über GitHub Actions —
kein eigener Server nötig.

## Was der Report enthält (v2)

- **Zusammenfassung & Gesamtbewertung** — Klartext-Interpretation der Lage
  (kein reines Zahlenblatt), automatisch aus den Daten abgeleitet.
- **24-Monats-Trend** — Referring Domains & Backlinks als Chart, gefüllt ab dem
  ersten Lauf (Quelle: Semrush `backlinks_historical`).
- **Toxische Backlinks & Disavow** — transparente Toxicity-Heuristik aus echten
  Semrush-Signalen (Authority Score, Spam-TLDs, Free-Host-/PBN-Muster, Herkunft,
  Anchor). Erzeugt eine Google-konforme **`disavow.txt`** als Mail-Anhang —
  ausdrücklich zum manuellen Review, **nie** Auto-Disavow.
- **Profil-Qualität** — Authority-Score-Verteilung, Follow/Nofollow.
- **Herkunft** — TLD- und Länder-Mix mit Hervorhebung untypischer Quellen.
- **Anchor-Analyse** — Branded/Sonstige/Spam-Split + Spam-Anchor-Erkennung.
- **Neue & verlorene Links**, **priorisierte Empfehlungen**, **Top-Domains**.

> Semrushs eigener „Toxic Score" aus dem Backlink-Audit-Tool ist nicht über die
> API abrufbar (nur im Web-UI). Die Heuristik reproduziert die zugrunde­liegenden
> Marker transparent und dient als automatisierte Vorauswahl; die offizielle
> Semrush-Liste (Projekt 28417044, Backlink Audit) bleibt der maßgebliche Cross-Check.

## Setup (einmalig, ~15 Min)

1. **Repo anlegen**: Diesen Ordner in ein privates GitHub-Repo pushen
   (`btn-backlink-audit` o.ä.).

2. **Secrets hinterlegen**: Repo → Settings → Secrets and variables →
   Actions → "New repository secret":

   | Secret | Wert |
   |---|---|
   | `SEMRUSH_API_KEY` | Dein Semrush-API-Key (Account → API-Zugriff) |
   | `SMTP_HOST` | z.B. `smtp.gmail.com` oder euer Mailserver |
   | `SMTP_PORT` | z.B. `587` |
   | `SMTP_USER` | Absender-Mailadresse |
   | `SMTP_PASSWORD` | App-Passwort (nicht das normale Passwort — bei Gmail unter "App-Passwörter" erzeugen, 2FA muss aktiv sein) |
   | `REPORT_RECIPIENTS` | Empfänger, kommagetrennt, z.B. `m.hoepner@btn-muenzen.de,philipp@langeweile.io` |
   | `GSC_SERVICE_ACCOUNT_JSON` | *(optional, siehe unten)* |

3. **Testlauf**: Im Reiter "Actions" → "Monatlicher Backlink-Audit" →
   "Run workflow" (manueller Trigger über `workflow_dispatch`). Prüft,
   ob alles durchläuft, bevor der erste echte Monatslauf kommt.

4. Danach läuft's automatisch am 25. jedes Monats, 07:00 UTC.

> **Wichtig:** GitHub Actions startet geplante Läufe (`schedule`) nur vom
> **Default-Branch** (`main`). Der Workflow muss also auf `main` liegen, damit
> der Monats-Cron greift. Der manuelle Testlauf (`workflow_dispatch`) geht
> dagegen von jedem Branch. Solange der Code noch auf einem Feature-Branch
> liegt, feuert der automatische Monatslauf nicht — erst nach dem Merge nach
> `main`.

## Kosten / Limits

Jeder Lauf verbraucht Semrush-API-Units. Die volle Datentiefe (Overview,
Historie, Ref-Domains, AS-Profil, Anchors, TLD, Geo, Neu/Verlust, Toxicity-
Kandidaten ≈ 9 Reports) liegt grob bei **~150–250 Units/Monat**, abhängig von
eurem Plan. Bei monatlicher Frequenz vernachlässigbar — aber gleicher API-Key/
Kontingent wie Projekt 28417044, kurz prüfen ob's Überschneidungen mit dem
monatlichen Reporting-Abzug gibt.

Die Toxicity-Analyse zieht die 400 Domains mit dem niedrigsten Authority Score
(`backlinks_refdomains`, Sortierung aufsteigend) als Disavow-Kandidaten-Pool.
Der ausgewiesene Anteil „niedrige Autorität (AS ≤ 5)" wird dagegen aus der
vollständigen Authority-Score-Verteilung (`backlinks_ascore_profile`) berechnet
und ist damit nicht durch dieses Limit gedeckelt.

## GSC-Anbindung (optional, Phase 2)

Aktuell holt das Script nur einen groben Klick-/Impressions-Trend als
Kontext — **keine internen Verlinkungsdaten**, denn:

> Die Search-Console-API liefert keine Daten zum "Links"-Report
> (interne/externe Links). Das gibt's nur im GSC-Webinterface, nicht
> über die API.

Für **interne Verlinkung** ist der bereits laufende **Semrush Site
Audit** (Projekt 28417044) die richtige Quelle — der crawlt die Seite
und erkennt strukturelle interne Link-Probleme (Orphan Pages, kaputte
interne Links etc.). Das als eigenen Report-Teil anzubinden ist bewusst
nicht Teil von Schritt 1, weil die Site-Audit-API Crawl-/Snapshot-IDs
braucht und dafür ein eigener kleiner Baustein sinnvoller ist als es in
dieses Script reinzuquetschen. Sag Bescheid, wenn das als nächstes
dran soll — baue ich als `scripts/internal_links_audit.py` mit eigenem
Abschnitt im PDF.

Falls der GSC-Kontext trotzdem rein soll: Google-Cloud-Projekt anlegen,
Service Account erstellen, JSON-Key herunterladen, den Service-Account
(die Mailadresse `...@...iam.gserviceaccount.com`) in der Search
Console als Nutzer (lesend reicht) unter btn-muenzen.de hinzufügen,
dann den JSON-Inhalt base64-kodieren (`base64 -w0 key.json`) und als
`GSC_SERVICE_ACCOUNT_JSON`-Secret hinterlegen.

## Alerts

Das Script schlägt im Mail-Betreff Alarm, wenn:
- Referring Domains um mehr als 5 % fallen
- mehr als 20 Referring Domains gegenüber Vormonat verloren gehen
- der Authority Score um 2+ Punkte fällt

Schwellenwerte stehen oben im Script (`ALERT_THRESHOLDS`) — einfach
anpassen, falls sich das nach ein paar Monaten Praxis als zu
empfindlich/unempfindlich rausstellt.

## Dateien

```
scripts/backlink_audit.py   Hauptscript
templates/report_template.html   PDF-Layout (BTN-Branding)
data/history.json            Monats-Snapshots (wird vom Workflow committed)
.github/workflows/           Cron-Trigger
```
