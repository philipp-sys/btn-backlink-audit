#!/usr/bin/env python3
"""
BTN Münzen — Monatlicher Backlink-Audit (v2, beratender Report)
================================================================

Läuft via GitHub Actions (siehe .github/workflows/monthly-backlink-audit.yml)
am 25. jedes Monats.

Was v2 gegenüber v1 leistet:
  - 24 Monate echte Historie aus Semrush `backlinks_historical`
    -> Trend-Charts sind ab dem allerersten Lauf gefüllt, Monatsvergleich
       funktioniert sofort (kein Kaltstart mehr).
  - Volle Datentiefe: Overview, Historie, Ref-Domains, Authority-Score-
    Verteilung, Anchors, TLD- und Länder-Mix, neue & verlorene Links.
  - Toxicity-Engine + Disavow: transparente, nachvollziehbare Bewertung aus
    echten Semrush-Rohsignalen (Domain Authority Score, Spam-TLDs, Spam-
    Anchors, Geo, Sitewide). Erzeugt eine Google-konforme disavow.txt zum
    manuellen Review (NIE auto-disavow).
    HINWEIS: Semrushs eigener "Toxic Score" aus dem Backlink-Audit-Tool ist
    nicht über die API abrufbar (nur im Web-UI). Diese Engine reproduziert die
    zugrundeliegenden Marker transparent — die offizielle Semrush-Liste bleibt
    der maßgebliche Cross-Check.
  - Interpretation + Empfehlungen: Klartext-Zusammenfassung und priorisierte,
    umsetzbare Handlungsempfehlungen, automatisch aus den Daten abgeleitet.
  - Neues, agentur-taugliches PDF-Layout (branded Inline-SVG-Charts, kein
    Fremd-Look, keine schweren Chart-Abhängigkeiten).

Benötigte Secrets (als GitHub Actions Secrets, siehe README.md):
  SEMRUSH_API_KEY
  SMTP_USER, SMTP_PASSWORD, SMTP_HOST, SMTP_PORT
  REPORT_RECIPIENTS        (kommagetrennt)
  GSC_SERVICE_ACCOUNT_JSON (optional, Base64 — nur falls GSC-Teil aktiv)
"""

import base64
import json
import os
import re
import smtplib
import time
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
# Nur bei Bedarf lesen (default leer), damit das Modul auch ohne API-Key
# importiert werden kann — z.B. für den CSV-basierten Report (report_from_audit.py),
# der keine API braucht.
SEMRUSH_API_KEY = os.environ.get("SEMRUSH_API_KEY", "")

ROOT = Path(__file__).resolve().parent.parent
HISTORY_FILE = ROOT / "data" / "history.json"
TEMPLATE_DIR = ROOT / "templates"
STAMP = datetime.now().strftime("%Y_%m")
OUTPUT_PDF = ROOT / "data" / f"backlink_audit_{STAMP}.pdf"
OUTPUT_DISAVOW = ROOT / "data" / f"disavow_{STAMP}.txt"

# BTN-Branding-Palette (zentral, damit Script-Charts und Template konsistent sind)
C_NAVY = "#1a3156"
C_RED = "#e30613"
C_GREEN = "#217c21"
C_GREY = "#777777"
C_LIGHT = "#efeff0"
C_AMBER = "#c77d0a"

# Schwellenwerte für Alerts im Mail-Betreff/Text
ALERT_THRESHOLDS = {
    "referring_domains_drop_pct": -5,   # Warnung wenn Ref-Domains um >5% fallen
    "authority_score_drop": -2,         # Warnung wenn Authority Score um >=2 Punkte fällt
    "lost_domains_abs": 20,             # Warnung wenn >20 Domains verloren gingen
    "toxic_share_pct": 30,              # Warnung wenn >30% der Domains potenziell toxisch
}

# Abruf-Limits — Semrush rechnet die Backlink-Reports PRO ZEILE ab, daher sind
# die großen Listen die teuersten Calls. Bewusst schlank gehalten, um das
# API-Unit-Budget zu schonen (leicht anpassbar, falls mehr Tiefe gewünscht):
TOX_CANDIDATE_LIMIT = 150   # niedrigst-autoritäre Ref-Domains für Disavow-Auswahl
NEWLOST_LIMIT = 60          # je Richtung (neu / verloren)
ANCHOR_LIMIT = 20           # Top-Anchors
TOP_REFDOMAINS_LIMIT = 15   # stärkste verweisende Domains
HISTORY_MONTHS = 24         # Monatstrend für die Charts

# Toxicity-Signale ---------------------------------------------------------
# Spam-lastige TLDs, überproportional in Link-Farmen / PBNs vertreten.
SPAM_TLDS = {
    "xyz", "top", "space", "click", "online", "site", "buzz", "icu", "cyou",
    "live", "cfd", "sbs", "rest", "quest", "bond", "monster", "shop", "fun",
    "loan", "work", "gdn", "dog", "beauty", "skin", "hair", "makeup", "cc",
    "casino", "poker", "bet", "win", "vip",
}
# Kostenlose/Blog-Hoster, die massenhaft für Spam-Subdomains genutzt werden.
FREEHOST_RE = re.compile(
    r"\.(blogspot\.com|wordpress\.com|weebly\.com|blogspot\.[a-z.]+|"
    r"medium\.com|tumblr\.com|livejournal\.com|over-blog\.com)$",
    re.IGNORECASE,
)
# Spam-/Money-Anchor-Muster (Casino, SEO-Link-Verkauf, Adult, Pharma etc.).
# Bewusst spezifisch gehalten: nur eindeutige Spam-/Money-Begriffe. KEIN bloßes
# "backlink", weil markengebundene Anchors wie "backlinks for btn-muenzen.de"
# textlich branded sind (die Toxizität der Quelle wird separat im Disavow erfasst).
SPAM_ANCHOR_RE = re.compile(
    r"casino|jetons|poker|slot|gambl|\bbet\b|betting|roulette|blackjack|"
    r"\bseo links?\b|links? dealer|link-legion|\btg @|telegram|"
    r"porn|\bsex\b|escort|viagra|cialis|pharma|\bloan\b|payday|crypto|"
    r"replica|\bvpn\b|\bcbd\b",
    re.IGNORECASE,
)
# Als für BTN plausibel geltende Länder (Münzhandel, DACH + große Westmärkte).
PLAUSIBLE_GEO = {"de", "at", "ch", "us", "gb", "fr", "nl", "be", "it", "es", ""}


# --------------------------------------------------------------------------
# 1. Semrush-Zugriff
# --------------------------------------------------------------------------

# Statuscodes, die auf transiente Drosselung/Serverfehler hindeuten und einen
# Retry rechtfertigen. 403 gehört dazu, weil Semrush teure Reports (große
# Backlink-/Ref-Domain-Listen) bei zu vielen Anfragen kurzfristig mit 403
# drosselt — ein Backoff löst das meist.
RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}


class SemrushUnitsExhausted(Exception):
    """Das API-Unit-Guthaben des Semrush-Accounts ist erschöpft (ERROR 132).
    Kein Retry sinnvoll — der Lauf beendet sich sauber mit einer Hinweis-Mail."""


def semrush_request(report_type: str, extra_params: dict, attempts: int = 4) -> str:
    """Ruft einen Semrush-Report ab und gibt die Rohantwort (CSV, ';'-getrennt) zurück.

    Robust gegen transiente Fehler: bei Drosselung (403/429), 5xx-Serverfehlern
    oder Netzwerkproblemen wird mit wachsendem Backoff wiederholt. Andere
    4xx-Client-Fehler (falsche Parameter) scheitern sofort — Retry hülfe nicht.
    """
    export_columns = extra_params.pop("export_columns", "")
    params = {
        "key": SEMRUSH_API_KEY,
        "type": report_type,
        "target": DOMAIN,
        "target_type": "root_domain",
        **extra_params,
    }
    if export_columns:
        params["export_columns"] = export_columns

    last_exc = None
    for attempt in range(attempts):
        try:
            resp = requests.get(SEMRUSH_API_URL, params=params, timeout=60)
        except requests.RequestException as exc:            # Timeout / Verbindung
            last_exc = exc
        else:
            # Leeres API-Unit-Guthaben (ERROR 132) -> sofort abbrechen, kein Retry.
            if "UNITS BALANCE IS ZERO" in resp.text or "ERROR 132" in resp.text:
                raise SemrushUnitsExhausted(resp.text.strip())
            if resp.status_code not in RETRYABLE_STATUS:
                resp.raise_for_status()                     # andere 4xx -> sofort scheitern
                if resp.text.startswith("ERROR"):
                    raise RuntimeError(f"Semrush-Fehler bei {report_type}: {resp.text}")
                return resp.text
            last_exc = requests.HTTPError(
                f"{resp.status_code} bei {report_type} (Drosselung/Serverfehler)")
        if attempt < attempts - 1:
            time.sleep(3 * (attempt + 1))                   # 3s, 6s, 9s
    raise last_exc


def parse_csv(raw: str) -> list[dict]:
    lines = [l for l in raw.strip().splitlines() if l]
    if len(lines) < 2:
        return []
    header = lines[0].split(";")
    return [dict(zip(header, line.split(";"))) for line in lines[1:]]


def _to_int(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 2. Datenabruf (Voll)
# --------------------------------------------------------------------------

def fetch_overview() -> dict:
    raw = semrush_request("backlinks_overview", {
        "export_columns": "total,domains_num,ips_num,follows_num,nofollows_num,score,trust_score,urls_num"
    })
    ov = parse_csv(raw)[0]
    return {
        "total_backlinks": _to_int(ov.get("total")),
        "referring_domains": _to_int(ov.get("domains_num")),
        "referring_ips": _to_int(ov.get("ips_num")),
        "follow": _to_int(ov.get("follows_num")),
        "nofollow": _to_int(ov.get("nofollows_num")),
        "authority_score": _to_int(ov.get("score")),
        "trust_score": _to_int(ov.get("trust_score")),
        "referring_urls": _to_int(ov.get("urls_num")),
    }


def fetch_historical(months: int = 24) -> list[dict]:
    """24 Monate Monatstrend (Backlinks, Referring Domains, Authority Score)."""
    raw = semrush_request("backlinks_historical", {"display_limit": str(months)})
    rows = parse_csv(raw)
    out = []
    for r in rows:
        ts = _to_int(r.get("date"))
        if not ts:
            continue
        out.append({
            "ts": ts,
            "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m"),
            "backlinks": _to_int(r.get("backlinks_num")),
            "domains": _to_int(r.get("domains_num")),
            "score": _to_int(r.get("score")),
        })
    out.sort(key=lambda x: x["ts"])          # chronologisch aufsteigend
    return out


def fetch_refdomains(limit: int, sort: str, columns: str) -> list[dict]:
    raw = semrush_request("backlinks_refdomains", {
        "export_columns": columns, "display_sort": sort, "display_limit": str(limit),
    })
    return parse_csv(raw)


def fetch_ascore_profile() -> list[dict]:
    raw = semrush_request("backlinks_ascore_profile", {})
    return parse_csv(raw)


def fetch_anchors(limit: int = 30) -> list[dict]:
    raw = semrush_request("backlinks_anchors", {
        "export_columns": "anchor,domains_num,backlinks_num",
        "display_sort": "domains_num_desc", "display_limit": str(limit),
    })
    return parse_csv(raw)


def fetch_tld(limit: int = 12) -> list[dict]:
    raw = semrush_request("backlinks_tld", {"display_limit": str(limit)})
    return parse_csv(raw)


def fetch_geo(limit: int = 12) -> list[dict]:
    raw = semrush_request("backlinks_geo", {"display_limit": str(limit)})
    return parse_csv(raw)


def _host(url: str) -> str:
    m = re.sub(r"^https?://", "", url or "", flags=re.IGNORECASE)
    host = m.split("/")[0].lower()
    return host[4:] if host.startswith("www.") else host


def fetch_new_lost(limit: int = 200) -> dict:
    """Neue & verlorene Links über Semrushs eigene newlink/lostlink-Flags.
    Die Roh-API kennt kein Server-Filter auf diese Flags (nur type/zone/ip/
    refdomain/anchor sind filterbar) -> wir ziehen die jüngsten Links und
    filtern die Boolean-Spalten clientseitig. Funktioniert ab Lauf 1,
    unabhängig von unserer Snapshot-Historie."""
    cols = "page_ascore,source_url,anchor,newlink,lostlink,first_seen,last_seen"

    def pull(sort: str) -> list[dict]:
        raw = semrush_request("backlinks", {
            "export_columns": cols, "display_sort": sort, "display_limit": str(limit),
        })
        return parse_csv(raw)

    def is_true(v) -> bool:
        return str(v).strip().lower() == "true"

    def domains_from(rows: list[dict]) -> list[str]:
        seen, out = set(), []
        for r in rows:
            host = _host(r.get("source_url", ""))
            if host and host not in seen:
                seen.add(host)
                out.append(host)
        return out

    new_rows = [r for r in pull("first_seen_desc") if is_true(r.get("newlink"))]
    lost_rows = [r for r in pull("last_seen_desc") if is_true(r.get("lostlink"))]
    return {
        "new_backlinks": len(new_rows),
        "lost_backlinks": len(lost_rows),
        "new_domains": domains_from(new_rows)[:12],
        "lost_domains": domains_from(lost_rows)[:12],
    }


# --------------------------------------------------------------------------
# 3. Toxicity-Engine + Disavow
# --------------------------------------------------------------------------

def _tld(domain: str) -> str:
    return domain.rsplit(".", 1)[-1].lower() if "." in domain else ""


def toxicity_score(dom: dict) -> tuple[int, list[str]]:
    """Composite-Toxicity (0..100) aus echten Semrush-Signalen + Begründung."""
    domain = (dom.get("domain") or "").lower()
    ascore = _to_int(dom.get("domain_ascore"))
    backlinks = _to_int(dom.get("backlinks_num"))
    country = (dom.get("country") or "").lower()
    score, reasons = 0, []

    if ascore <= 2:
        score += 50
        reasons.append(f"Authority Score {ascore} (nahezu null)")
    elif ascore <= 5:
        score += 35
        reasons.append(f"sehr niedriger Authority Score {ascore}")
    elif ascore <= 10:
        score += 18
        reasons.append(f"niedriger Authority Score {ascore}")

    if _tld(domain) in SPAM_TLDS:
        score += 20
        reasons.append(f"Spam-lastige TLD .{_tld(domain)}")
    if FREEHOST_RE.search(domain):
        score += 15
        reasons.append("Free-Host-/PBN-Subdomain")
    if country and country not in PLAUSIBLE_GEO and ascore <= 10:
        score += 10
        reasons.append(f"unplausibles Herkunftsland ({country})")
    if ascore <= 3 and backlinks >= 5:
        score += 10
        reasons.append(f"{backlinks} Links von Null-Authority-Domain (sitewide-Muster)")

    return min(score, 100), reasons


def analyze_toxicity(candidates: list[dict]) -> dict:
    """Bewertet Ref-Domains, erzeugt Disavow-Kandidaten (score >= 50)."""
    flagged = []
    for dom in candidates:
        sc, reasons = toxicity_score(dom)
        if sc >= 50:
            flagged.append({
                "domain": (dom.get("domain") or "").lower(),
                "ascore": _to_int(dom.get("domain_ascore")),
                "backlinks": _to_int(dom.get("backlinks_num")),
                "country": dom.get("country") or "—",
                "score": sc,
                "level": "hoch" if sc >= 70 else "mittel",
                "reasons": reasons,
            })
    flagged.sort(key=lambda x: (-x["score"], x["ascore"]))
    return {
        "flagged": flagged,
        "count": len(flagged),
        "high": sum(1 for f in flagged if f["level"] == "hoch"),
    }


def build_disavow_file(flagged: list[dict]) -> str:
    """Google-konforme disavow.txt (Domain-Ebene) — ausdrücklich zum Review."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    head = [
        f"# Disavow-KANDIDATEN für {DOMAIN} — generiert {today}",
        "# ------------------------------------------------------------------",
        "# ACHTUNG: NICHT ungeprüft hochladen. Disavow ist SEO-sensibel — ein",
        "# falsch disavowter guter Link kann Rankings kosten. Diese Liste ist",
        "# eine heuristische Vorauswahl (Authority Score, Spam-TLDs, Anchors,",
        "# Geo). Vor dem Upload manuell prüfen und mit der offiziellen Semrush-",
        "# Backlink-Audit-Liste abgleichen.",
        "# Upload: Google Search Console -> Disavow-Tool -> Datei hochladen.",
        "# ------------------------------------------------------------------",
        "",
    ]
    lines = [f"domain:{f['domain']}" for f in flagged]
    return "\n".join(head + lines) + "\n"


# --------------------------------------------------------------------------
# 4. Anchor-Klassifikation
# --------------------------------------------------------------------------

def classify_anchors(anchors: list[dict]) -> dict:
    branded = money = spam = 0
    spam_examples = []
    brand_re = re.compile(r"btn|münzen|muenzen", re.IGNORECASE)
    for a in anchors:
        text = a.get("anchor", "") or ""
        doms = _to_int(a.get("domains_num"))
        if SPAM_ANCHOR_RE.search(text):
            spam += doms
            if len(spam_examples) < 5 and text.strip():
                spam_examples.append(text[:70])
        elif brand_re.search(text) or DOMAIN in text:
            branded += doms
        else:
            money += doms
    total = branded + money + spam or 1
    return {
        "branded": branded, "money": money, "spam": spam,
        "branded_pct": round(branded / total * 100),
        "money_pct": round(money / total * 100),
        "spam_pct": round(spam / total * 100),
        "spam_examples": spam_examples,
    }


# --------------------------------------------------------------------------
# 5. Deltas & Alerts (Monatsvergleich direkt aus der Semrush-Historie)
# --------------------------------------------------------------------------

def compute_deltas(current: dict, history: list[dict]) -> dict:
    """Vergleicht aktuelle Live-Werte mit dem Vormonats-Punkt der Semrush-Historie."""
    if len(history) < 2:
        return {"has_previous": False}
    prev = history[-2]
    d = {"has_previous": True, "previous_date": prev["date"]}
    for key, cur_val, prev_val in [
        ("referring_domains", current["referring_domains"], prev["domains"]),
        ("total_backlinks", current["total_backlinks"], prev["backlinks"]),
        ("authority_score", current["authority_score"], prev["score"]),
    ]:
        abs_d = cur_val - prev_val
        pct = round(abs_d / prev_val * 100, 1) if prev_val else 0.0
        d[key] = {"abs": abs_d, "pct": pct}
    return d


def check_alerts(deltas: dict, tox: dict, current: dict, low_as_share: int) -> list[str]:
    alerts = []
    if deltas.get("has_previous"):
        rd = deltas["referring_domains"]
        if rd["pct"] <= ALERT_THRESHOLDS["referring_domains_drop_pct"]:
            alerts.append(f"Referring Domains um {rd['pct']}% gefallen ({rd['abs']:+d})")
        if rd["abs"] < 0 and abs(rd["abs"]) >= ALERT_THRESHOLDS["lost_domains_abs"]:
            alerts.append(f"{abs(rd['abs'])} Referring Domains netto verloren ggü. Vormonat")
        asr = deltas["authority_score"]
        if asr["abs"] <= ALERT_THRESHOLDS["authority_score_drop"]:
            alerts.append(f"Authority Score um {asr['abs']} Punkte gefallen")
    if low_as_share is not None and low_as_share >= ALERT_THRESHOLDS["toxic_share_pct"]:
        tail = (f"; {tox['count']} konkrete Disavow-Kandidaten"
                if tox.get("available") and tox["count"] else "")
        alerts.append(f"{low_as_share}% der Referring Domains mit sehr niedriger "
                      f"Autorität (AS ≤ 5){tail} — prüfen")
    return alerts


# --------------------------------------------------------------------------
# 6. Interpretation & Empfehlungen
# --------------------------------------------------------------------------

def _de_num(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def build_narrative(current, deltas, history, tox, anchors_cls, low_as_share) -> dict:
    rd = current["referring_domains"]
    tox_share = round(tox["count"] / (rd or 1) * 100)
    y_ago = history[-13] if len(history) > 12 else None
    growth_12 = (rd - y_ago["domains"]) if y_ago else None

    parts = []
    if deltas.get("has_previous"):
        r = deltas["referring_domains"]
        verb = "gewachsen" if r["abs"] >= 0 else "gesunken"
        parts.append(
            f"Das Backlink-Profil von {DOMAIN} zählt aktuell {_de_num(rd)} "
            f"verweisende Domains und {_de_num(current['total_backlinks'])} Backlinks. "
            f"Gegenüber dem Vormonat ist die Zahl der Referring Domains um "
            f"{abs(r['abs'])} ({r['pct']:+.1f}%) {verb}."
        )
    else:
        parts.append(
            f"Das Backlink-Profil von {DOMAIN} zählt aktuell {_de_num(rd)} "
            f"verweisende Domains und {_de_num(current['total_backlinks'])} Backlinks."
        )
    if growth_12 is not None:
        parts.append(
            f"Über 12 Monate hat sich das Profil um {growth_12:+d} Referring "
            f"Domains verändert (Authority Score aktuell {current['authority_score']})."
        )
    if low_as_share:
        s = (f"Auffällig: rund {low_as_share}% der verweisenden Domains haben einen "
             f"sehr niedrigen Authority Score (≤ 5) — typisch für Link-Spam- und "
             f"PBN-Netzwerke.")
        if tox.get("available") and tox["count"]:
            s += (f" {tox['count']} davon sind mit weiteren Spam-Signalen (Spam-TLDs, "
                  f"Free-Host, unplausible Herkunft) als konkrete Disavow-Kandidaten "
                  f"markiert, was auf eine mögliche Negative-SEO-Belastung hindeutet.")
        parts.append(s)
    if anchors_cls["spam"]:
        parts.append(
            f"Zusätzlich sind Money-/Spam-Anchors erkennbar "
            f"({anchors_cls['spam_pct']}% der analysierten Anchor-Domains, z.B. "
            f"Casino-/SEO-Link-Texte), die nicht zum Münzhandel passen."
        )

    # Datenlücke transparent machen (statt sie als "sauberes Profil" auszugeben).
    data_gap = (low_as_share is None) or (not tox.get("available", True))
    if data_gap:
        parts.append(
            "Hinweis: Einzelne Semrush-Reports waren in diesem Lauf temporär nicht "
            "abrufbar (API-Drosselung). Betroffene Abschnitte sind entsprechend "
            "gekennzeichnet und erscheinen im nächsten Lauf vollständig."
        )

    if (low_as_share or 0) >= 40 or anchors_cls["spam_pct"] >= 15:
        verdict, verdict_class = "Handlungsbedarf: Spam-Belastung", "neg"
    elif deltas.get("has_previous") and deltas["referring_domains"]["pct"] <= -5:
        verdict, verdict_class = "Beobachten: rückläufig", "amber"
    elif data_gap:
        verdict, verdict_class = "Teildaten — nächsten Lauf prüfen", "amber"
    else:
        verdict, verdict_class = "Stabil", "pos"

    return {"summary": " ".join(parts), "verdict": verdict, "verdict_class": verdict_class,
            "tox_share": tox_share, "low_as_share": low_as_share, "growth_12": growth_12}


def build_recommendations(current, deltas, tox, anchors_cls, narrative) -> list[dict]:
    recs = []
    if tox["count"]:
        recs.append({
            "prio": "Hoch",
            "text": f"{tox['count']} potenziell toxische Domains ({narrative['tox_share']}% "
                    f"des Profils, davon {tox['high']} hochgradig) im Anhang als "
                    f"disavow.txt prüfen. Nach Abgleich mit der Semrush-Backlink-Audit-"
                    f"Liste über die Search Console disavowen.",
        })
    if anchors_cls["spam"]:
        recs.append({
            "prio": "Hoch",
            "text": "Money-/Spam-Anchors (Casino/SEO-Links) deuten auf gezielten "
                    "Link-Spam hin. Muster dokumentieren, betroffene Domains ins "
                    "Disavow aufnehmen und Entwicklung monatlich beobachten.",
        })
    if deltas.get("has_previous") and deltas["referring_domains"]["pct"] <= -5:
        recs.append({
            "prio": "Mittel",
            "text": f"Netto-Rückgang der Referring Domains um "
                    f"{deltas['referring_domains']['pct']}%. Verlorene Qualitäts-"
                    f"Domains (siehe Abschnitt „Neue & verlorene Links“) auf Rückgewinnung prüfen.",
        })
    if anchors_cls["branded_pct"] < 40:
        recs.append({
            "prio": "Mittel",
            "text": f"Branded-Anchor-Anteil bei nur {anchors_cls['branded_pct']}%. "
                    f"Für ein natürliches Profil den Anteil markengebundener Anker "
                    f"(„BTN Münzen“, Domainname) durch gezieltes Marken-Linkbuilding stärken.",
        })
    recs.append({
        "prio": "Laufend",
        "text": "Hochwertige verweisende Domains (Wikipedia, Fachportale, IHK) als "
                "Referenz pflegen und für Linkbuilding-Outreach in vergleichbaren "
                "seriösen Umfeldern nutzen.",
    })
    return recs


# --------------------------------------------------------------------------
# 7. Inline-SVG-Charts (branded, ohne Fremd-Abhängigkeiten)
# --------------------------------------------------------------------------

def svg_area(history: list[dict], key: str, color: str, label: str) -> str:
    """Schlichtes Flächendiagramm eines Monatstrends."""
    if len(history) < 2:
        return ""
    W, H, PB, PT, PL = 520, 150, 26, 14, 8
    vals = [h[key] for h in history]
    vmin, vmax = min(vals), max(vals)
    span = (vmax - vmin) or 1
    n = len(history)

    def x(i):
        return PL + i * (W - 2 * PL) / (n - 1)

    def y(v):
        return PT + (H - PT - PB) * (1 - (v - vmin) / span)

    pts = [(x(i), y(v)) for i, v in enumerate(vals)]
    line = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    area = f"{PL:.1f},{H - PB:.1f} " + line + f" {W - PL:.1f},{H - PB:.1f}"
    ticks = ""
    for i in (0, n // 2, n - 1):
        ticks += (f'<text x="{x(i):.0f}" y="{H - 8}" font-size="8" fill="{C_GREY}" '
                  f'text-anchor="middle">{history[i]["date"]}</text>')
    return f'''<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{label}">
<polygon points="{area}" fill="{color}" fill-opacity="0.12"/>
<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2"/>
<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="3" fill="{color}"/>
<text x="{PL}" y="10" font-size="8" fill="{C_GREY}">max {_de_num(vmax)}</text>
<text x="{PL}" y="{H - PB + 2}" font-size="8" fill="{C_GREY}">min {_de_num(vmin)}</text>
{ticks}
</svg>'''


def svg_hbars(items: list[tuple], unit: str = "") -> str:
    """Horizontales Balkendiagramm. items = [(label, value, highlight_bool)]."""
    if not items:
        return ""
    W, rowh, PL = 520, 22, 150
    vmax = max((v for _, v, _ in items), default=1) or 1
    H = rowh * len(items) + 6
    rows = ""
    for i, (label, val, hot) in enumerate(items):
        yy = i * rowh + 4
        bw = (W - PL - 60) * val / vmax
        col = C_RED if hot else C_NAVY
        rows += (
            f'<text x="0" y="{yy + 13}" font-size="10" fill="{C_NAVY}">{label}</text>'
            f'<rect x="{PL}" y="{yy + 3}" width="{bw:.1f}" height="12" rx="2" fill="{col}"/>'
            f'<text x="{PL + bw + 6:.1f}" y="{yy + 13}" font-size="9" fill="{C_GREY}">{_de_num(val)}{unit}</text>'
        )
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">'
            f'{rows}</svg>')


def svg_stacked_ratio(segments: list[tuple]) -> str:
    """Einzelner gestapelter Balken. segments = [(label, value, color)]."""
    total = sum(v for _, v, _ in segments) or 1
    W, H = 520, 34
    x, bar, legend, lx = 0, "", "", 0
    for label, val, color in segments:
        w = W * val / total
        bar += f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="18" fill="{color}"/>'
        pct = round(val / total * 100)
        legend += (f'<rect x="{lx}" y="24" width="9" height="9" fill="{color}"/>'
                   f'<text x="{lx + 13}" y="32" font-size="9" fill="{C_NAVY}">{label} {pct}%</text>')
        lx += 130
        x += w
    return (f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">'
            f'{bar}{legend}</svg>')


def bucket_ascore(profile: list[dict]) -> str:
    """AS-Verteilung in Klassen; die toxische 0–5-Klasse wird rot hervorgehoben."""
    buckets = [("AS 0–5", 0, 5), ("AS 6–10", 6, 10), ("AS 11–20", 11, 20),
               ("AS 21–40", 21, 40), ("AS 41–60", 41, 60), ("AS 61–100", 61, 100)]
    items = []
    for label, lo, hi in buckets:
        s = sum(_to_int(r.get("domains_num")) for r in profile
                if lo <= _to_int(r.get("ascore")) <= hi)
        items.append((label, s, lo == 0))
    return svg_hbars(items)


# --------------------------------------------------------------------------
# 8. Report rendern (HTML -> PDF)
# --------------------------------------------------------------------------

def render_pdf(ctx: dict) -> Path:
    from weasyprint import HTML
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    env.filters["thousands"] = _de_num
    template = env.get_template("report_template.html")
    html_str = template.render(**ctx)
    OUTPUT_PDF.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html_str).write_pdf(str(OUTPUT_PDF))
    return OUTPUT_PDF


# --------------------------------------------------------------------------
# 9. Mail
# --------------------------------------------------------------------------

def send_email(pdf_path: Path, disavow_path, narrative: dict,
               alerts: list[str], current: dict) -> None:
    recipients = [r.strip() for r in os.environ["REPORT_RECIPIENTS"].split(",") if r.strip()]
    subject = f"BTN Backlink-Audit {datetime.now():%m/%Y} — {narrative['verdict']}"
    if alerts:
        subject += " · Auffälligkeiten"

    body = [
        f"Backlink-Audit für {DOMAIN} — {datetime.now():%d.%m.%Y}",
        "=" * 52, "",
        "ZUSAMMENFASSUNG",
        narrative["summary"], "",
        f"Status: {narrative['verdict']}", "",
    ]
    if alerts:
        body += ["AUFFÄLLIGKEITEN"] + [f"  - {a}" for a in alerts] + [""]
    body += [
        "KENNZAHLEN",
        f"  Backlinks gesamt:  {_de_num(current['total_backlinks'])}",
        f"  Referring Domains: {_de_num(current['referring_domains'])}",
        f"  Authority Score:   {current['authority_score']}",
        "",
        "Vollständiger Report im Anhang (PDF).",
    ]
    if disavow_path:
        body += ["Disavow-Kandidaten als disavow.txt im Anhang — bitte vor dem "
                 "Upload manuell prüfen (siehe Hinweis in der Datei)."]
    body += ["", "-- automatisch generiert · LangeWeile UG --"]

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText("\n".join(body), "plain", "utf-8"))

    for path, subtype in [(pdf_path, "pdf"), (disavow_path, "plain")]:
        if not path:
            continue
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), _subtype=subtype)
        part.add_header("Content-Disposition", "attachment", filename=path.name)
        msg.attach(part)

    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
        server.starttls()
        server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        server.sendmail(os.environ["SMTP_USER"], recipients, msg.as_string())


# --------------------------------------------------------------------------
# 10. Historie (durabler Record; Trends kommen aus der Semrush-Historie)
# --------------------------------------------------------------------------

def load_history() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def save_snapshot(current: dict, tox: dict, anchors_cls: dict) -> None:
    hist = load_history()
    hist.append({
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "overview": current,
        "toxic_domains": tox["count"],
        "toxic_high": tox["high"],
        "anchor_split": {k: anchors_cls[k] for k in ("branded_pct", "money_pct", "spam_pct")},
    })
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(hist, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# 11. GSC-Kontext (optional, unverändert aus v1)
# --------------------------------------------------------------------------

def fetch_gsc_data() -> dict | None:
    sa_b64 = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    if not sa_b64:
        return None
    from datetime import timedelta
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_info = json.loads(base64.b64decode(sa_b64))
    creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    service = build("searchconsole", "v1", credentials=creds)
    body = {
        "startDate": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"),
        "endDate": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
        "dimensions": ["date"],
    }
    resp = service.searchanalytics().query(siteUrl=f"https://{DOMAIN}/", body=body).execute()
    rows = resp.get("rows", [])
    return {
        "clicks_30d": sum(r["clicks"] for r in rows),
        "impressions_30d": sum(r["impressions"] for r in rows),
    }


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _safe(fn, default, label):
    """Sekundäre Reports dürfen einen Lauf nicht komplett scheitern lassen.
    Ausnahme: leeres API-Unit-Guthaben wird durchgereicht -> Hinweis-Mail."""
    try:
        return fn()
    except SemrushUnitsExhausted:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {label} nicht verfügbar: {exc}")
        return default


def send_units_notice(detail: str) -> None:
    """Kurze Hinweis-Mail, wenn das Semrush-API-Unit-Guthaben leer ist —
    damit der Ausfall nicht stumm bleibt, sondern jeden Monat sichtbar wird."""
    try:
        recipients = [r.strip() for r in os.environ["REPORT_RECIPIENTS"].split(",") if r.strip()]
        msg = MIMEText(
            "Der monatliche BTN Backlink-Audit konnte diesen Monat nicht laufen, "
            "weil das Semrush-API-Unit-Guthaben aufgebraucht ist.\n\n"
            f"Semrush-Meldung: {detail}\n\n"
            "Bitte das Guthaben prüfen (Semrush → Profil → Subscription info → "
            "API units) und ggf. nachbuchen. Der nächste planmäßige Lauf versucht "
            "es automatisch erneut — ein manueller Lauf ist über GitHub Actions "
            "(„Run workflow“) jederzeit möglich, sobald wieder Units da sind.\n\n"
            "-- automatisch generiert · LangeWeile UG --",
            "plain", "utf-8")
        msg["Subject"] = (f"BTN Backlink-Audit {datetime.now():%m/%Y} — "
                          f"übersprungen (Semrush-Units leer)")
        msg["From"] = os.environ["SMTP_USER"]
        msg["To"] = ", ".join(recipients)
        with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"])) as server:
            server.starttls()
            server.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            server.sendmail(os.environ["SMTP_USER"], recipients, msg.as_string())
        print("Hinweis-Mail (Semrush-Units leer) verschickt.")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Hinweis-Mail konnte nicht verschickt werden: {exc}")


def main():
    try:
        run_audit()
    except SemrushUnitsExhausted as exc:
        print(f"[Abbruch] Semrush-Units aufgebraucht — {exc}")
        send_units_notice(str(exc))


def run_audit():
    print(f"[1/7] Overview + {HISTORY_MONTHS}-Monats-Historie für {DOMAIN}...")
    current = fetch_overview()
    history = fetch_historical(HISTORY_MONTHS)

    print("[2/7] Detaildaten (Ref-Domains, AS-Profil, Anchors, TLD, Geo)...")
    ascore_profile = _safe(fetch_ascore_profile, None, "AS-Profil")
    ascore_available = ascore_profile is not None
    ascore_profile = ascore_profile or []
    anchors = _safe(lambda: fetch_anchors(ANCHOR_LIMIT), [], "Anchors")
    tld = _safe(lambda: fetch_tld(10), [], "TLD-Verteilung")
    geo = _safe(lambda: fetch_geo(10), [], "Geo-Verteilung")
    top_refdomains = _safe(
        lambda: fetch_refdomains(TOP_REFDOMAINS_LIMIT, "domain_ascore_desc",
                                 "domain_ascore,domain,backlinks_num,country"),
        [], "Top-Ref-Domains")

    print("[3/7] Neue & verlorene Links...")
    new_lost = _safe(lambda: fetch_new_lost(NEWLOST_LIMIT), {
        "new_backlinks": 0, "lost_backlinks": 0, "new_domains": [], "lost_domains": []},
        "Neu/Verlust")

    print("[4/7] Toxicity-Analyse + Disavow-Kandidaten...")
    tox_candidates = _safe(
        lambda: fetch_refdomains(TOX_CANDIDATE_LIMIT, "domain_ascore_asc",
                                 "domain_ascore,domain,backlinks_num,country"),
        None, "Toxicity-Kandidaten")
    tox = analyze_toxicity(tox_candidates or [])
    tox["available"] = tox_candidates is not None
    anchors_cls = classify_anchors(anchors)
    # Anteil niedrig-autoritärer Domains (AS <= 5) aus der VOLLEN AS-Verteilung
    # -> belastbarer als die (gekappte) Kandidatenliste. None = Daten fehlten,
    # damit eine API-Lücke nicht faelschlich als "sauberes Profil" erscheint.
    if ascore_available:
        low_as = sum(_to_int(r.get("domains_num")) for r in ascore_profile
                     if _to_int(r.get("ascore")) <= 5)
        low_as_share = round(low_as / (current["referring_domains"] or 1) * 100)
    else:
        low_as_share = None

    print("[5/7] Interpretation, Empfehlungen, Deltas...")
    deltas = compute_deltas(current, history)
    narrative = build_narrative(current, deltas, history, tox, anchors_cls, low_as_share)
    recommendations = build_recommendations(current, deltas, tox, anchors_cls, narrative)
    alerts = check_alerts(deltas, tox, current, low_as_share)
    gsc = _safe(fetch_gsc_data, None, "GSC-Kontext")

    disavow_path = None
    if tox["flagged"]:
        OUTPUT_DISAVOW.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_DISAVOW.write_text(build_disavow_file(tox["flagged"]))
        disavow_path = OUTPUT_DISAVOW

    print("[6/7] Charts + PDF rendern...")
    tld_items = [(r.get("zone", "?"), _to_int(r.get("domains_num")),
                  r.get("zone", "").lower() in SPAM_TLDS) for r in tld]
    plausible_geo_names = {"germany", "austria", "switzerland", "united states",
                           "united kingdom", "france", "netherlands", "belgium",
                           "italy", "spain", ""}
    geo_items = [(r.get("country", "?"), _to_int(r.get("domains_num")),
                  r.get("country", "").lower() not in plausible_geo_names) for r in geo]
    ctx = {
        "domain": DOMAIN,
        "generated_at": datetime.now().strftime("%d.%m.%Y"),
        "current": current, "deltas": deltas, "history": history,
        "narrative": narrative, "recommendations": recommendations,
        "alerts": alerts, "gsc": gsc,
        "top_refdomains": top_refdomains, "top_anchors": anchors[:12],
        "anchors_cls": anchors_cls, "tox": tox, "new_lost": new_lost,
        "colors": {"navy": C_NAVY, "red": C_RED, "green": C_GREEN,
                   "grey": C_GREY, "amber": C_AMBER},
        "chart_domains": svg_area(history, "domains", C_NAVY, "Referring Domains 24 Monate"),
        "chart_backlinks": svg_area(history, "backlinks", C_GREY, "Backlinks 24 Monate"),
        "chart_ascore": bucket_ascore(ascore_profile),
        "chart_tld": svg_hbars(tld_items),
        "chart_geo": svg_hbars(geo_items),
        "chart_followratio": svg_stacked_ratio([
            ("Follow", current["follow"], C_NAVY),
            ("Nofollow", current["nofollow"], C_GREY)]),
        "chart_anchors": svg_stacked_ratio([
            ("Branded", anchors_cls["branded"], C_NAVY),
            ("Sonstige", anchors_cls["money"], C_GREY),
            ("Spam", anchors_cls["spam"], C_RED)]),
    }
    pdf_path = render_pdf(ctx)

    print("[7/7] Mail verschicken + Snapshot speichern...")
    send_email(pdf_path, disavow_path, narrative, alerts, current)
    save_snapshot(current, tox, anchors_cls)
    print(f"Fertig. Verdict: {narrative['verdict']} · "
          f"toxisch: {tox['count']} · Alerts: {len(alerts)}")


if __name__ == "__main__":
    main()
