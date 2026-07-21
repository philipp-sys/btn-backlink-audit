#!/usr/bin/env python3
"""
BTN Münzen — Backlink-Audit-Report aus Semrush-Backlink-Audit-CSV
==================================================================

Datenquelle: CSV-Export aus dem Semrush **Backlink-Audit-Tool**
(Web-UI → Backlink Audit → Domains/Backlinks → Export). Braucht KEINEN
API-Zugriff — die Lösung für Pläne ohne API-Units.

Der Export enthält die ECHTEN Semrush-Toxic-Scores, Semrushs eigene
List-Einordnung (disavow/for-review), Anchor-Typen, Domain/Page Authority
Score und First/Last-Seen — also eine bessere Disavow-Grundlage als eine
Heuristik.

Aufruf:
    python scripts/report_from_audit.py <pfad-zur-csv> [--email]

Erzeugt:
    data/backlink_audit_<YYYY_MM>.pdf
    data/disavow_<YYYY_MM>.txt
Mit --email zusätzlich Versand an REPORT_RECIPIENTS (SMTP-Secrets nötig).
"""

import csv
import json
import os
import smtplib
import sys
from collections import Counter
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backlink_audit as ba  # Wiederverwendung: SVG-Charts, Palette, Helfer

DOMAIN = ba.DOMAIN
ROOT = ba.ROOT
TEMPLATE_DIR = ba.TEMPLATE_DIR
PROJECT_ID = "28417044"
PROFILE_FILE = ROOT / "data" / "profile.json"

# Toxic-Score-Schwellen (Semrush-Konvention: 0–44 niedrig, 45–59 mittel, 60+ hoch)
TOX_CANDIDATE = 45
TOX_HIGH = 60


def _i(v, d=0):
    return ba._to_int(v, d)


def load_rows(csv_path: str) -> list[dict]:
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def tld_of(domain: str) -> str:
    return domain.rsplit(".", 1)[-1].lower() if "." in domain else ""


def _delta(cur: int, prev: int) -> dict:
    ab = cur - prev
    return {"abs": ab, "pct": round(ab / prev * 100, 1) if prev else 0.0}


def load_profile() -> dict | None:
    if PROFILE_FILE.exists():
        try:
            return json.loads(PROFILE_FILE.read_text())
        except json.JSONDecodeError:
            return None
    return None


def profile_context(profile: dict | None) -> dict:
    """Profil-Kopfzahlen + 24-Monats-Trend aus data/profile.json (optional)."""
    hist = sorted((profile or {}).get("history", []), key=lambda x: x.get("date", ""))
    if len(hist) < 2:
        return {"has": False}
    cur, prev = hist[-1], hist[-2]
    follow, nofollow = profile.get("follow"), profile.get("nofollow")
    follow_chart = ""
    if follow is not None and nofollow is not None:
        follow_chart = ba.svg_stacked_ratio([
            ("Follow", follow, ba.C_NAVY), ("Nofollow", nofollow, ba.C_GREY)])
    return {
        "has": True,
        "backlinks": cur["backlinks"], "ref_domains": cur["domains"], "ascore": cur["score"],
        "prev_date": prev["date"],
        "d_backlinks": _delta(cur["backlinks"], prev["backlinks"]),
        "d_domains": _delta(cur["domains"], prev["domains"]),
        "d_ascore": {"abs": cur["score"] - prev["score"]},
        "chart_domains": ba.svg_area(hist, "domains", ba.C_NAVY, "Referring Domains"),
        "chart_backlinks": ba.svg_area(hist, "backlinks", ba.C_GREY, "Backlinks"),
        "chart_follow": follow_chart,
    }


def build_context(rows: list[dict], profile: dict | None = None) -> dict:
    total = len(rows)
    # --- Toxicity / Disavow (echte Semrush-Scores) ---
    flagged = []
    for r in rows:
        tox = _i(r.get("Toxic Score"))
        semr_list = (r.get("List") or "").strip().lower()
        if tox >= TOX_CANDIDATE or semr_list == "disavow":
            flagged.append({
                "domain": (r.get("Source Domain") or "").strip().lower(),
                "toxic": tox,
                "ascore": _i(r.get("Domain Authority Score")),
                "anchor_type": (r.get("Anchor Type") or "—").strip(),
                "nofollow": (r.get("No Follow") or "").strip().lower() == "true",
                "first_seen": (r.get("First Seen") or "").strip(),
                "last_seen": (r.get("Last Seen") or "").strip(),
                "semrush_list": semr_list,
                "level": "hoch" if tox >= TOX_HIGH else "mittel",
            })
    # nach Toxic Score, dann niedriger AS zuerst
    flagged.sort(key=lambda x: (-x["toxic"], x["ascore"]))
    disavow_high = sum(1 for f in flagged if f["toxic"] >= TOX_HIGH)
    semrush_disavow = sum(1 for r in rows if (r.get("List") or "").strip().lower() == "disavow")

    tox_scores = [_i(r.get("Toxic Score")) for r in rows]
    avg_toxic = round(sum(tox_scores) / total, 1) if total else 0
    nofollow_pct = round(sum(1 for r in rows if (r.get("No Follow") or "").strip().lower() == "true")
                         / (total or 1) * 100)

    # --- Anchor-Typen ---
    at = Counter((r.get("Anchor Type") or "unknown").strip().lower() for r in rows)
    branded = at.get("branded", 0)
    money = at.get("money", 0) + at.get("naked", 0) + at.get("compound", 0) + at.get("organic", 0)
    other = at.get("unknown", 0) + at.get("empty", 0)
    spam_examples = []
    for r in rows:
        a = (r.get("Anchor") or "").strip()
        if a and ba.SPAM_ANCHOR_RE.search(a) and len(spam_examples) < 6:
            spam_examples.append(a[:70])

    # --- Charts ---
    tox_buckets = [("0–14", 0, 14), ("15–29", 15, 29), ("30–44", 30, 44),
                   ("45–59", 45, 59), ("60–100", 60, 100)]
    tox_items = [(lbl, sum(1 for t in tox_scores if lo <= t <= hi), lo >= 45)
                 for lbl, lo, hi in tox_buckets]
    chart_toxic = ba.svg_hbars(tox_items)

    as_buckets = [("AS 0–5", 0, 5), ("AS 6–10", 6, 10), ("AS 11–20", 11, 20),
                  ("AS 21–40", 21, 40), ("AS 41–60", 41, 60), ("AS 61–100", 61, 100)]
    as_vals = [_i(r.get("Domain Authority Score")) for r in rows]
    as_items = [(lbl, sum(1 for v in as_vals if lo <= v <= hi), lo == 0)
                for lbl, lo, hi in as_buckets]
    chart_ascore = ba.svg_hbars(as_items)

    tld_c = Counter(tld_of((r.get("Source Domain") or "").strip().lower()) for r in rows)
    tld_items = [(f".{z}", n, z in ba.SPAM_TLDS) for z, n in tld_c.most_common(10) if z]
    chart_tld = ba.svg_hbars(tld_items)

    chart_anchor = ba.svg_stacked_ratio([
        ("Branded", branded, ba.C_NAVY),
        ("Money/URL", money, ba.C_GREY),
        ("Sonstige", other, "#b9bfc9")])

    # --- Neueste Links ---
    dated = [f for f in ({
        "domain": (r.get("Source Domain") or "").strip().lower(),
        "toxic": _i(r.get("Toxic Score")),
        "first_seen": (r.get("First Seen") or "").strip(),
        "anchor_type": (r.get("Anchor Type") or "—").strip(),
        "level": "hoch" if _i(r.get("Toxic Score")) >= TOX_HIGH else ("mittel" if _i(r.get("Toxic Score")) >= TOX_CANDIDATE else "niedrig"),
    } for r in rows) if f["first_seen"]]
    newest = sorted(dated, key=lambda x: x["first_seen"], reverse=True)[:10]

    # --- Verdict + Zusammenfassung ---
    cand_share = round(len(flagged) / (total or 1) * 100)
    if disavow_high >= 3 or cand_share >= 20:
        verdict, vclass = "Handlungsbedarf: Disavow empfohlen", "neg"
    elif len(flagged):
        verdict, vclass = "Beobachten", "amber"
    else:
        verdict, vclass = "Unauffällig", "pos"

    summary = (
        f"Der Semrush-Backlink-Audit für {DOMAIN} umfasst {total} auditierte "
        f"verweisende Domains mit einem durchschnittlichen Toxic Score von "
        f"{avg_toxic}. {len(flagged)} davon ({cand_share}%) erreichen einen Toxic "
        f"Score ≥ {TOX_CANDIDATE} und gelten als Disavow-Kandidaten, "
        f"{disavow_high} davon sind hochgradig toxisch (≥ {TOX_HIGH}). "
        f"Der Nofollow-Anteil liegt bei {nofollow_pct}%."
    )
    if spam_examples:
        summary += (" Unter den Anchor-Texten finden sich klare Spam-/Money-Muster "
                    "(z.B. Casino-/SEO-Link-Texte), die nicht zum Münzhandel passen.")

    alerts = []
    if cand_share >= 20:
        alerts.append(f"{cand_share}% der auditierten Domains sind Disavow-Kandidaten "
                      f"(Toxic ≥ {TOX_CANDIDATE})")
    if disavow_high:
        alerts.append(f"{disavow_high} hochgradig toxische Domains (Toxic ≥ {TOX_HIGH}) — "
                      f"zeitnah disavowen")

    # --- Empfehlungen ---
    recs = []
    if flagged:
        recs.append({"prio": "Hoch",
                     "text": f"{len(flagged)} Disavow-Kandidaten (im Anhang als disavow.txt) "
                             f"prüfen und über die Google Search Console disavowen — "
                             f"beginnend mit den {disavow_high} hochgradig toxischen."})
    if spam_examples:
        recs.append({"prio": "Hoch",
                     "text": "Money-/Spam-Anchors (Casino/SEO-Links) deuten auf gezielten "
                             "Link-Spam / mögliche Negative-SEO hin — Muster monatlich verfolgen."})
    recs.append({"prio": "Mittel",
                 "text": "Im Semrush-Backlink-Audit die geprüften Domains als „Whitelist“ bzw. "
                         "„Disavow“ einsortieren, damit die Liste über die Monate sauber bleibt."})
    recs.append({"prio": "Laufend",
                 "text": "Monatlich den aktualisierten CSV-Export ziehen — der Report zeigt "
                         "neue toxische Domains und die Entwicklung der Disavow-Liste."})

    return {
        "domain": DOMAIN,
        "generated_at": datetime.now().strftime("%d.%m.%Y"),
        "project_id": PROJECT_ID,
        "profile": profile_context(profile),
        "verdict": verdict, "verdict_class": vclass, "summary": summary, "alerts": alerts,
        "audited_domains": total, "disavow_count": len(flagged), "disavow_high": disavow_high,
        "semrush_disavow": semrush_disavow, "avg_toxic": avg_toxic, "nofollow_pct": nofollow_pct,
        "flagged": flagged, "newest": newest, "spam_examples": spam_examples,
        "recommendations": recs,
        "chart_toxic": chart_toxic, "chart_ascore": chart_ascore,
        "chart_tld": chart_tld, "chart_anchor": chart_anchor,
    }


def build_disavow(flagged: list[dict]) -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    head = [
        f"# Disavow-KANDIDATEN für {DOMAIN} — generiert {today}",
        f"# Quelle: Semrush Backlink Audit (Projekt {PROJECT_ID}), Toxic Score >= {TOX_CANDIDATE}",
        "# ------------------------------------------------------------------",
        "# ACHTUNG: Vor dem Upload manuell prüfen. Ein faelschlich disavowter",
        "# guter Link kann Rankings kosten.",
        "# Upload: Google Search Console -> Disavow-Tool -> Datei hochladen.",
        "# ------------------------------------------------------------------",
        "",
    ]
    seen, lines = set(), []
    for f in flagged:
        d = f["domain"]
        if d and d not in seen:
            seen.add(d)
            lines.append(f"domain:{d}")
    return "\n".join(head + lines) + "\n"


def render_pdf(ctx: dict, out_pdf: Path) -> Path:
    from weasyprint import HTML
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    env.filters["thousands"] = ba._de_num
    html = env.get_template("audit_report_template.html").render(**ctx)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(out_pdf))
    return out_pdf


def send_email(pdf_path: Path, disavow_path: Path, ctx: dict) -> None:
    recipients = [r.strip() for r in os.environ["REPORT_RECIPIENTS"].split(",") if r.strip()]
    subject = f"BTN Backlink-Audit {datetime.now():%m/%Y} — {ctx['verdict']}"
    body = [
        f"Backlink-Audit für {DOMAIN} — {datetime.now():%d.%m.%Y}",
        "=" * 52, "",
        "ZUSAMMENFASSUNG", ctx["summary"], "",
        f"Status: {ctx['verdict']}", "",
        "KENNZAHLEN",
    ]
    if ctx["profile"]["has"]:
        p = ctx["profile"]
        body += [
            f"  Backlinks gesamt:    {ba._de_num(p['backlinks'])} ({p['d_backlinks']['abs']:+d})",
            f"  Referring Domains:   {ba._de_num(p['ref_domains'])} ({p['d_domains']['abs']:+d})",
            f"  Authority Score:     {p['ascore']} ({p['d_ascore']['abs']:+d})",
        ]
    body += [
        f"  Auditierte Domains:  {ctx['audited_domains']}",
        f"  Disavow-Kandidaten:  {ctx['disavow_count']} (davon {ctx['disavow_high']} hochgradig)",
        f"  Ø Toxic Score:       {ctx['avg_toxic']}",
        "",
        "Vollständiger Report im Anhang (PDF).",
        "Disavow-Kandidaten als disavow.txt im Anhang — vor dem Upload prüfen.",
        "", "-- automatisch generiert · LangeWeile UG --",
    ]
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText("\n".join(body), "plain", "utf-8"))
    for path, sub in [(pdf_path, "pdf"), (disavow_path, "plain")]:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype=sub)
        part.add_header("Content-Disposition", "attachment", filename=path.name)
        msg.attach(part)
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(os.environ["SMTP_USER"], recipients, msg.as_string())


def main():
    if len(sys.argv) < 2:
        sys.exit("Aufruf: python scripts/report_from_audit.py <pfad-zur-csv> [--email]")
    csv_path = sys.argv[1]
    do_email = "--email" in sys.argv[2:]
    stamp = datetime.now().strftime("%Y_%m")
    out_pdf = ROOT / "data" / f"backlink_audit_{stamp}.pdf"
    out_disavow = ROOT / "data" / f"disavow_{stamp}.txt"

    print(f"[1/3] Lese Semrush-Backlink-Audit-CSV: {csv_path}")
    rows = load_rows(csv_path)
    profile = load_profile()
    if profile:
        print(f"      + Profil-Daten (profile.json): {len(profile.get('history', []))} Monate")
    ctx = build_context(rows, profile)

    print(f"[2/3] Rendere PDF ({ctx['audited_domains']} Domains, "
          f"{ctx['disavow_count']} Disavow-Kandidaten)...")
    render_pdf(ctx, out_pdf)
    out_disavow.parent.mkdir(parents=True, exist_ok=True)
    out_disavow.write_text(build_disavow(ctx["flagged"]))

    if do_email:
        print("[3/3] Verschicke Mail...")
        send_email(out_pdf, out_disavow, ctx)
    else:
        print("[3/3] (kein --email; nur Dateien erzeugt)")
    print(f"Fertig. PDF: {out_pdf.name} · Disavow: {out_disavow.name} · "
          f"Verdict: {ctx['verdict']}")


if __name__ == "__main__":
    main()
