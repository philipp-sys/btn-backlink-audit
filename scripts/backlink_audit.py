#!/usr/bin/env python3
"""
BTN Münzen — Monatlicher Backlink-Audit
=========================================

Läuft via GitHub Actions (siehe .github/workflows/monthly-backlink-audit.yml)
am 25. jedes Monats (analog zum bestehenden SEO-Reporting-Rhythmus, nach
Abschluss des Semrush-Crawls).

Ablauf:
  1. Backlink-Snapshot von Semrush ziehen (Domain-Overview + Ref-Domains + Anchors)
  2. Snapshot mit letztem gespeicherten Snapshot (data/history.json) vergleichen
  3. GSC-Performance-Trend als Kontext dazuholen (optional, siehe fetch_gsc_data)
  4. HTML-Report rendern (Jinja2 + BTN-Branding) -> PDF (WeasyPrint)
  5. PDF per Mail verschicken; bei größeren Deltas Kurz-Alert direkt im Mailtext
  6. Neuen Snapshot in data/history.json anhängen (wird vom Workflow zurückcommitted)

Benötigte Secrets (als GitHub Actions Secrets hinterlegen, siehe README.md):
  SEMRUSH_API_KEY
  SMTP_USER, SMTP_PASSWORD, SMTP_HOST, SMTP_PORT
  REPORT_RECIPIENTS        (kommagetrennt, z.B. "m.hoepner@btn-muenzen.de,philipp@langeweile.example")
  GSC_SERVICE_ACCOUNT_JSON (optional, Base64-kodiert — nur falls GSC-Teil aktiv)
"""

import base64
import json
import os
import smtplib
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader

# --------------------------------------------------------------------------
# Konfiguration
# --------------------------------------------------------------------------

DOMAIN = "btn-muenzen.de"
SEMRUSH_API_URL = "https://api.semrush.com/analytics/v1/"
SEMRUSH_API_KEY = os.environ["SEMRUSH_API_KEY"]

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.json"
TEMPLATE_DIR = ROOT / "templates"
OUTPUT_PDF = ROOT / "data" / f"backlink_audit_{datetime.now():%Y_%m}.pdf"

# Schwellenwerte für Alerts im Mail-Betreff/Text
ALERT_THRESHOLDS = {
    "referring_domains_drop_pct": -5,   # Warnung wenn Ref-Domains um >5% fallen
    "authority_score_drop": -2,         # Warnung wenn Authority Score um >=2 Punkte fällt
    "lost_domains_abs": 20,             # Warnung wenn >20 Domains verloren gingen
}


# --------------------------------------------------------------------------
# 1. Semrush: Backlink-Snapshot ziehen
# --------------------------------------------------------------------------

def semrush_request(report_type: str, extra_params: dict) -> str:
    """Ruft einen Semrush-Report ab und gibt die Rohantwort (CSV, semikolon-getrennt) zurück."""
    params = {
        "key": SEMRUSH_API_KEY,
        "type": report_type,
        "target": DOMAIN,
        "target_type": "root_domain",
        "export_columns": extra_params.pop("export_columns", ""),
        **extra_params,
    }
    resp = requests.get(SEMRUSH_API_URL, params=params, timeout=60)
    resp.raise_for_status()
    if resp.text.startswith("ERROR"):
        raise RuntimeError(f"Semrush-Fehler bei {report_type}: {resp.text}")
    return resp.text


def parse_csv(raw: str) -> list[dict]:
    lines = [l for l in raw.strip().splitlines() if l]
    if len(lines) < 2:
        return []
    header = lines[0].split(";")
    return [dict(zip(header, line.split(";"))) for line in lines[1:]]


def fetch_backlink_snapshot() -> dict:
    """Zieht Overview, Top-Ref-Domains und Anchor-Text-Verteilung."""
    overview_raw = semrush_request("backlinks_overview", {
        "export_columns": "total,domains_num,ips_num,follows_num,nofollows_num,score,trust_score,urls_num"
    })
    overview = parse_csv(overview_raw)[0]

    refdomains_raw = semrush_request("backlinks_refdomains", {
        "export_columns": "domain_ascore,domain,backlinks_num,ip,country",
        "display_sort": "domain_ascore_desc",
        "display_limit": "20",
    })
    top_refdomains = parse_csv(refdomains_raw)

    anchors_raw = semrush_request("backlinks_anchors", {
        "export_columns": "anchor,domains_num,backlinks_num",
        "display_sort": "domains_num_desc",
        "display_limit": "15",
    })
    anchors = parse_csv(anchors_raw)

    return {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "overview": {
            "total_backlinks": int(overview.get("total", 0)),
            "referring_domains": int(overview.get("domains_num", 0)),
            "referring_ips": int(overview.get("ips_num", 0)),
            "follow": int(overview.get("follows_num", 0)),
            "nofollow": int(overview.get("nofollows_num", 0)),
            "authority_score": int(overview.get("score", 0)),
            "trust_score": int(overview.get("trust_score", 0)),
            "referring_urls": int(overview.get("urls_num", 0)),
        },
        "top_refdomains": top_refdomains,
        "top_anchors": anchors,
    }


# --------------------------------------------------------------------------
# 2. GSC: Performance-Trend als Kontext (optional — Platzhalter)
# --------------------------------------------------------------------------
#
# HINWEIS: Die Search Console API liefert KEINE internen Verlinkungsdaten
# (das "Links"-Report gibt es nur im GSC-UI, nicht über die API). Für
# interne Verlinkung ist stattdessen der Semrush Site Audit (Projekt
# 28417044) die richtige Quelle — das ist bewusst NICHT Teil dieses
# Scripts (Site-Audit-API ist projekt-/Crawl-ID-basiert und komplexer;
# als Phase-2-Erweiterung sauberer trennbar). Siehe README.md.
#
# Diese Funktion holt nur den Klick-/Impressions-Trend als Kontext dazu,
# ob Backlink-Wachstum sich in Sichtbarkeit niederschlägt.

def fetch_gsc_data() -> dict | None:
    sa_b64 = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not sa_b64:
        return None  # GSC-Teil optional, Script läuft auch ohne

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_info = json.loads(base64.b64decode(sa_b64))
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/webmasters.readonly"]
    )
    service = build("searchconsole", "v1", credentials=creds)

    request_body = {
        "startDate": _n_days_ago(30),
        "endDate": _n_days_ago(1),
        "dimensions": ["date"],
    }
    response = service.searchanalytics().query(
        siteUrl=f"https://{DOMAIN}/", body=request_body
    ).execute()

    rows = response.get("rows", [])
    total_clicks = sum(r["clicks"] for r in rows)
    total_impressions = sum(r["impressions"] for r in rows)
    return {"clicks_30d": total_clicks, "impressions_30d": total_impressions}


def _n_days_ago(n: int) -> str:
    from datetime import timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 3. Historie laden/vergleichen
# --------------------------------------------------------------------------

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


def save_history(history: list[dict]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history, indent=2, ensure_ascii=False))


def compute_deltas(current: dict, previous: dict | None) -> dict:
    if previous is None:
        return {"has_previous": False}

    cur_ov, prev_ov = current["overview"], previous["overview"]
    deltas = {"has_previous": True, "previous_date": previous["date"]}
    for key in cur_ov:
        prev_val = prev_ov.get(key, 0)
        cur_val = cur_ov[key]
        abs_delta = cur_val - prev_val
        pct_delta = (abs_delta / prev_val * 100) if prev_val else 0
        deltas[key] = {"abs": abs_delta, "pct": round(pct_delta, 1)}
    return deltas


def check_alerts(deltas: dict) -> list[str]:
    alerts = []
    if not deltas.get("has_previous"):
        return alerts
    rd = deltas.get("referring_domains", {})
    if rd.get("pct", 0) <= ALERT_THRESHOLDS["referring_domains_drop_pct"]:
        alerts.append(f"⚠️ Referring Domains um {rd['pct']}% gefallen ({rd['abs']:+d})")
    if abs(rd.get("abs", 0)) >= ALERT_THRESHOLDS["lost_domains_abs"] and rd.get("abs", 0) < 0:
        alerts.append(f"⚠️ {abs(rd['abs'])} Referring Domains verloren seit letztem Monat")
    a_score = deltas.get("authority_score", {})
    if a_score.get("abs", 0) <= ALERT_THRESHOLDS["authority_score_drop"]:
        alerts.append(f"⚠️ Authority Score um {a_score['abs']} Punkte gefallen")
    return alerts


# --------------------------------------------------------------------------
# 4. Report rendern (HTML -> PDF)
# --------------------------------------------------------------------------

def render_pdf(current: dict, deltas: dict, alerts: list[str], gsc: dict | None) -> Path:
    from weasyprint import HTML

    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("report_template.html")
    html_str = template.render(
        domain=DOMAIN,
        current=current,
        deltas=deltas,
        alerts=alerts,
        gsc=gsc,
        generated_at=datetime.now().strftime("%d.%m.%Y"),
    )
    OUTPUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_str).write_pdf(str(OUTPUT_PDF))
    return OUTPUT_PDF


# --------------------------------------------------------------------------
# 5. Mail verschicken
# --------------------------------------------------------------------------

def send_email(pdf_path: Path, alerts: list[str], current: dict, deltas: dict) -> None:
    recipients = os.environ["REPORT_RECIPIENTS"].split(",")
    subject = f"BTN Backlink-Audit {datetime.now():%m/%Y}"
    if alerts:
        subject += " — Achtung, Auffälligkeiten"

    ov = current["overview"]
    lines = [
        f"Backlink-Audit für {DOMAIN} — {datetime.now():%d.%m.%Y}",
        "",
        f"Backlinks gesamt: {ov['total_backlinks']}",
        f"Referring Domains: {ov['referring_domains']}",
        f"Authority Score: {ov['authority_score']}",
    ]
    if deltas.get("has_previous"):
        rd = deltas["referring_domains"]
        lines.append(f"Veränderung Ref-Domains ggü. Vormonat: {rd['abs']:+d} ({rd['pct']:+.1f}%)")
    if alerts:
        lines += ["", "Auffälligkeiten:"] + [f"- {a}" for a in alerts]
    lines += ["", "Vollständiger Report im Anhang (PDF).", "", "-- automatisch generiert --"]

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText("\n".join(lines), "plain"))

    with open(pdf_path, "rb") as f:
        part = MIMEApplication(f.read(), _subtype="pdf")
        part.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
        msg.attach(part)

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(os.environ["SMTP_USER"], recipients, msg.as_string())


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    print(f"[1/5] Ziehe Backlink-Snapshot für {DOMAIN} von Semrush...")
    current = fetch_backlink_snapshot()

    print("[2/5] Lade Historie und berechne Deltas...")
    history = load_history()
    previous = history[-1] if history else None
    deltas = compute_deltas(current, previous)
    alerts = check_alerts(deltas)

    print("[3/5] Hole GSC-Kontext (falls konfiguriert)...")
    gsc = fetch_gsc_data()

    print("[4/5] Rendere PDF-Report...")
    pdf_path = render_pdf(current, deltas, alerts, gsc)

    print("[5/5] Verschicke Mail...")
    send_email(pdf_path, alerts, current, deltas)

    history.append(current)
    save_history(history)
    print("Fertig. Snapshot gespeichert, Mail verschickt.")


if __name__ == "__main__":
    main()
