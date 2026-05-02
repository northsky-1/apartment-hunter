#!/usr/bin/env python3
"""
apartment_hunter.py — Daily Helsinki + Espoo apartment search and scoring.

Fetches new listings from Oikotie's public listing API, filters by your criteria,
scores each one against district €/m² baselines, and writes a dated HTML report.

Usage:
    python3 apartment_hunter.py             # fetch + score + write today's report
    python3 apartment_hunter.py --open      # also open the report in your browser
    python3 apartment_hunter.py --history   # print summary of past runs

Configuration: edit criteria.json and market_baseline.json next to this file.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
# Reports go into docs/ so GitHub Pages can serve them. Override with REPORTS_DIR env var.
REPORTS_DIR = Path(os.environ.get("REPORTS_DIR", str(ROOT / "docs")))
SEEN_FILE = ROOT / "seen_listings.json"
CRITERIA_FILE = ROOT / "criteria.json"
BASELINE_FILE = ROOT / "market_baseline.json"

OIKOTIE_API = "https://asunnot.oikotie.fi/api/cards"
# Oikotie location codes: [id, level, name]. Level 4 = city.
LOCATIONS = {
    "Helsinki": [64, 6, "Helsinki"],
    "Espoo":    [49, 6, "Espoo"],
}

# Oikotie's API uses three rotating per-session headers: OTA-token, OTA-loaded, OTA-cuid.
# We try several extraction strategies because Oikotie has reshuffled their bootstrap a few times.
HOMEPAGE_URL = "https://asunnot.oikotie.fi/"
TOKEN_ENDPOINT = "https://asunnot.oikotie.fi/user/token"  # observed in dev tools

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------- session / fetch ----------

def _fetch(url: str, headers: dict | None = None, timeout: int = 20) -> tuple[int, str]:
    """Fetch a URL and return (status, body_text). Doesn't raise on non-200."""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return e.code, body


def get_session_tokens() -> dict[str, str]:
    """Try multiple strategies to get Oikotie's per-session OTA-* headers.

    Strategy A: hit the dedicated /user/token endpoint, which returns JSON with
                {token, loaded, cuid} fields.
    Strategy B: scrape the homepage HTML for embedded token data.
    """
    tokens: dict[str, str] = {}

    # --- A: token endpoint
    status, body = _fetch(TOKEN_ENDPOINT, headers={"Accept": "application/json"})
    if status == 200 and body:
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                # Common field names seen across versions
                t = data.get("token") or data.get("api-token") or data.get("apiToken")
                l = data.get("loaded") or data.get("Loaded")
                c = data.get("cuid") or data.get("Cuid")
                if t: tokens["OTA-token"] = t
                if l: tokens["OTA-loaded"] = l
                if c: tokens["OTA-cuid"] = c
        except json.JSONDecodeError:
            pass

    if len(tokens) >= 2:
        return tokens

    # --- B: homepage HTML — Oikotie embeds tokens as <meta> tags:
    #   <meta name="api-token" content="...">
    #   <meta name="loaded"    content="...">
    #   <meta name="cuid"      content="...">
    status, body = _fetch(HOMEPAGE_URL)
    if status == 200 and body:
        for key, meta_name in [
            ("OTA-token",  "api-token"),
            ("OTA-loaded", "loaded"),
            ("OTA-cuid",   "cuid"),
        ]:
            if key in tokens:
                continue
            # Match meta tags in either attribute order
            patterns = [
                rf'<meta\s+name="{meta_name}"\s+content="([^"]+)"',
                rf'<meta\s+content="([^"]+)"\s+name="{meta_name}"',
            ]
            for pat in patterns:
                m = re.search(pat, body)
                if m:
                    tokens[key] = m.group(1)
                    break

    return tokens


def fetch_listings(city: str, size: int = 100, tokens: dict | None = None) -> list[dict[str, Any]]:
    loc = LOCATIONS[city]
    params = {
        "cardType": 100,                       # 100 = apartments for sale
        "locations": json.dumps([loc], ensure_ascii=False),
        "size": size,
        "sortBy": "published_sort_desc",
    }
    url = OIKOTIE_API + "?" + urllib.parse.urlencode(params, safe="[]\"")
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if tokens:
        headers.update(tokens)
    status, body = _fetch(url, headers=headers, timeout=30)
    if status != 200:
        snippet = body[:300].replace("\n", " ") if body else ""
        raise RuntimeError(f"HTTP {status} from /api/cards. Response: {snippet}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response from /api/cards: {body[:300]}")
    return data.get("cards", [])


# ---------- parsing ----------

def parse_int(s: Any) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    digits = re.sub(r"[^\d]", "", str(s))
    return int(digits) if digits else None


def parse_float(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    cleaned = re.sub(r"[^\d.,]", "", str(s)).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_rooms(s: str | None) -> int | None:
    """'2h+kk', '3h+k+s' → 2, 3."""
    if not s:
        return None
    m = re.match(r"\s*(\d+)\s*h", s.lower())
    return int(m.group(1)) if m else None


def normalize_listing(card: dict[str, Any]) -> dict[str, Any]:
    """Map Oikotie's card shape to our flat schema."""
    price_str = card.get("price")
    price = parse_int(price_str)
    size_str = card.get("size") or card.get("roomConfiguration") or ""
    size_m2 = parse_float(card.get("areaLiving") or card.get("size"))
    rooms = parse_rooms(card.get("rooms") or card.get("roomConfiguration"))
    district = (card.get("district") or "").strip()
    if not district:
        # Sometimes it's nested under 'location'
        loc = card.get("location") or {}
        district = (loc.get("district") or loc.get("city") or "").strip()
    city = ""
    loc = card.get("location") or {}
    if isinstance(loc, dict):
        city = (loc.get("city") or "").strip()
    return {
        "id":          str(card.get("id") or card.get("cardId") or ""),
        "url":         card.get("url") or f"https://asunnot.oikotie.fi/myytavat-asunnot/{card.get('id','')}",
        "title":       card.get("description") or card.get("address") or "",
        "address":     card.get("address") or "",
        "city":        city,
        "district":    district,
        "rooms":       rooms,
        "rooms_text":  card.get("rooms") or card.get("roomConfiguration") or "",
        "size_m2":     size_m2,
        "price_eur":   price,
        "price_per_m2": (price / size_m2) if (price and size_m2) else None,
        "build_year":  parse_int(card.get("buildYear")),
        "floor":       card.get("floor"),
        "image":       card.get("imageUrl") or card.get("image") or "",
        "raw":         card,
    }


# ---------- criteria filter + scoring ----------

def passes_criteria(l: dict, c: dict) -> tuple[bool, list[str]]:
    """Return (matches, list_of_reasons_failed)."""
    fails = []
    if l["price_eur"] is not None and l["price_eur"] > c["price_max_eur"]:
        fails.append(f"price {l['price_eur']:,}€ > {c['price_max_eur']:,}€")
    if l["size_m2"] is not None and l["size_m2"] < c["size_min_m2"]:
        fails.append(f"size {l['size_m2']}m² < {c['size_min_m2']}m²")
    if l["rooms"] is not None:
        if l["rooms"] < c["rooms_min"] or l["rooms"] > c["rooms_max"]:
            fails.append(f"rooms {l['rooms']} outside {c['rooms_min']}–{c['rooms_max']}")
    return (len(fails) == 0, fails)


def lookup_baseline(district: str, baseline: dict) -> dict:
    if not district:
        return baseline["default"]
    # exact, then case-insensitive, then prefix
    if district in baseline["districts"]:
        return baseline["districts"][district]
    lower_map = {k.lower(): v for k, v in baseline["districts"].items()}
    if district.lower() in lower_map:
        return lower_map[district.lower()]
    for k, v in baseline["districts"].items():
        if district.lower().startswith(k.lower()) or k.lower().startswith(district.lower()):
            return v
    return baseline["default"]


def has_keyword(text: str, *keywords: str) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in keywords)


def score_listing(l: dict, c: dict, baseline: dict) -> dict:
    w = c["weights"]
    score = 0.0
    notes = []
    bl = lookup_baseline(l["district"], baseline)
    bl_mid = (bl["low"] + bl["high"]) / 2

    # --- price vs market (40)
    if l["price_per_m2"] and bl_mid:
        ratio = l["price_per_m2"] / bl_mid  # <1 = below market
        # Linear: ratio 0.80 → full points, 1.00 → half points, 1.20 → 0
        pts = max(0.0, min(1.0, (1.20 - ratio) / 0.40)) * w["price_vs_market"]
        score += pts
        delta_pct = (ratio - 1) * 100
        notes.append(
            f"€/m²: {l['price_per_m2']:,.0f} vs district mid {bl_mid:,.0f} "
            f"({delta_pct:+.0f}%) [{pts:.0f}/{w['price_vs_market']}]"
        )
    else:
        notes.append(f"€/m² baseline missing for '{l['district']}' [0/{w['price_vs_market']}]")

    # --- criteria match (20)
    cm = 0
    if l["rooms"] in (c.get("rooms_preferred") or []):
        cm += 10
    elif l["rooms"] is not None and c["rooms_min"] <= l["rooms"] <= c["rooms_max"]:
        cm += 6
    if l["size_m2"] and l["size_m2"] >= c["size_min_m2"] + 10:  # comfortable margin
        cm += 5
    elif l["size_m2"] and l["size_m2"] >= c["size_min_m2"]:
        cm += 3
    if l["price_eur"] and l["price_eur"] <= c["price_max_eur"] * 0.85:
        cm += 5
    elif l["price_eur"] and l["price_eur"] <= c["price_max_eur"]:
        cm += 2
    cm = min(cm, w["criteria_match"])
    score += cm
    notes.append(f"criteria fit [{cm}/{w['criteria_match']}]")

    # --- location (15) — metro / train mention in raw text + title/address
    raw_text = (
        json.dumps(l["raw"], ensure_ascii=False)
        + " " + (l.get("title") or "")
        + " " + (l.get("address") or "")
        + " " + (l.get("district") or "")
    ).lower()
    loc_pts = 0
    if has_keyword(raw_text, "metro", "metroasema"):
        loc_pts += 10
    if has_keyword(raw_text, "juna", "rautatie", "lähijuna", "asema"):
        loc_pts += 5
    loc_pts = min(loc_pts, w["location"])
    score += loc_pts
    notes.append(f"transit mentions [{loc_pts}/{w['location']}]")

    # --- parking (10)
    parking_pts = 0
    if has_keyword(raw_text, "autopaikka", "parkkipaikka", "autohalli", "autotalli", "parking"):
        parking_pts = w["parking"]
    score += parking_pts
    notes.append(f"parking [{parking_pts}/{w['parking']}]")

    # --- character (10) — high floor, view, large balcony
    char_pts = 0
    floor_str = str(l.get("floor") or "")
    floor_match = re.search(r"(\d+)", floor_str)
    if floor_match and int(floor_match.group(1)) >= 5:
        char_pts += 4
    if has_keyword(raw_text, "merinäköala", "merinakoala", "sea view", "näköala merelle", "näköala"):
        char_pts += 4
    if has_keyword(raw_text, "iso parveke", "suuri parveke", "lasitettu parveke", "kattoterassi", "terassi"):
        char_pts += 3
    if has_keyword(raw_text, "takka", "saunallinen", "oma sauna"):
        char_pts += 1
    char_pts = min(char_pts, w["character"])
    score += char_pts
    notes.append(f"character [{char_pts}/{w['character']}]")

    # --- risk penalty (up to -5) — old building, no recent pipe renovation
    risk_pen = 0
    if l["build_year"] and l["build_year"] < 1980:
        if not has_keyword(raw_text, "putkiremontti tehty", "linjasaneeraus tehty", "putket uusittu"):
            risk_pen = w["risk_penalty_max"]
            notes.append(f"⚠ pre-1980 build, no putkiremontti mentioned [-{risk_pen}]")
    score -= risk_pen

    score = max(0.0, min(100.0, score))

    # verdict
    if score >= 75:
        verdict = "STRONG BUY — investigate today"
    elif score >= 60:
        verdict = "Worth a viewing"
    elif score >= 45:
        verdict = "Mediocre — only if specifics line up"
    else:
        verdict = "Skip"

    return {
        "score": round(score, 1),
        "verdict": verdict,
        "baseline_low": bl["low"],
        "baseline_high": bl["high"],
        "notes": notes,
    }


# ---------- state ----------

def load_seen() -> dict[str, str]:
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text())
    return {}


def save_seen(seen: dict[str, str]) -> None:
    SEEN_FILE.write_text(json.dumps(seen, indent=2, ensure_ascii=False))


# ---------- HTML report ----------

REPORT_CSS = """
:root { color-scheme: light; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #222; background: #fafaf8; }
h1 { margin-bottom: 4px; }
.sub { color: #666; margin-bottom: 24px; }
.summary { display: flex; gap: 16px; margin-bottom: 24px; flex-wrap: wrap; }
.stat { background: white; border: 1px solid #e6e6e0; border-radius: 8px; padding: 12px 16px; min-width: 140px; }
.stat .n { font-size: 22px; font-weight: 600; }
.stat .l { color: #888; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
.card { background: white; border: 1px solid #e6e6e0; border-radius: 10px; padding: 16px;
        margin-bottom: 14px; display: flex; gap: 16px; }
.card.new { border-left: 4px solid #2a9d4a; }
.card img { width: 180px; height: 130px; object-fit: cover; border-radius: 6px; flex-shrink: 0; background: #eee; }
.card .body { flex: 1; min-width: 0; }
.card h3 { margin: 0 0 4px 0; font-size: 16px; }
.card h3 a { color: #1a4d8c; text-decoration: none; }
.card h3 a:hover { text-decoration: underline; }
.facts { color: #555; font-size: 13px; margin: 4px 0 8px 0; }
.score { float: right; text-align: right; }
.score .n { font-size: 32px; font-weight: 700; line-height: 1; }
.score .v { font-size: 12px; color: #666; }
.s-strong .n { color: #2a9d4a; }
.s-good .n { color: #1a4d8c; }
.s-mid .n { color: #b8860b; }
.s-skip .n { color: #999; }
.notes { font-size: 12px; color: #555; margin-top: 8px; }
.notes li { margin-bottom: 2px; list-style: none; padding-left: 0; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; margin-right: 6px;
       background: #f0ede5; color: #555; }
.tag.new { background: #d4edd9; color: #1a5e2a; }
.tag.below { background: #d4edd9; color: #1a5e2a; }
.tag.above { background: #f5d9d9; color: #8c1a1a; }
.section-h { font-size: 18px; margin: 28px 0 8px 0; padding-bottom: 4px; border-bottom: 1px solid #ddd; }
"""


def s_class(score: float) -> str:
    if score >= 75: return "s-strong"
    if score >= 60: return "s-good"
    if score >= 45: return "s-mid"
    return "s-skip"


def render_listing(l: dict, sc: dict, is_new: bool) -> str:
    price = f"{l['price_eur']:,}€".replace(",", " ") if l["price_eur"] else "—"
    size = f"{l['size_m2']:.1f} m²" if l["size_m2"] else "—"
    ppm = f"{l['price_per_m2']:,.0f} €/m²".replace(",", " ") if l["price_per_m2"] else "—"
    rooms = l["rooms_text"] or (f"{l['rooms']}h" if l["rooms"] else "—")
    bl_mid = (sc["baseline_low"] + sc["baseline_high"]) / 2
    if l["price_per_m2"]:
        if l["price_per_m2"] < bl_mid * 0.95:
            mkt_tag = '<span class="tag below">below market</span>'
        elif l["price_per_m2"] > bl_mid * 1.05:
            mkt_tag = '<span class="tag above">above market</span>'
        else:
            mkt_tag = '<span class="tag">at market</span>'
    else:
        mkt_tag = ""
    new_tag = '<span class="tag new">NEW TODAY</span>' if is_new else ""
    notes_html = "".join(f"<li>• {html.escape(n)}</li>" for n in sc["notes"])
    img_html = f'<img src="{html.escape(l["image"])}" alt="">' if l["image"] else '<div class="card-img-placeholder" style="width:180px;height:130px;background:#eee;border-radius:6px;flex-shrink:0;"></div>'
    return f"""
    <div class="card {'new' if is_new else ''}">
      {img_html}
      <div class="body">
        <div class="score {s_class(sc['score'])}">
          <div class="n">{sc['score']:.0f}</div>
          <div class="v">{html.escape(sc['verdict'])}</div>
        </div>
        <h3><a href="{html.escape(l['url'])}" target="_blank">{html.escape(l['address'] or l['title'])}</a></h3>
        <div class="facts">
          {html.escape(l['district'] or l['city'])} · {html.escape(rooms)} · {size} · {price} · {ppm}
          {f"· built {l['build_year']}" if l['build_year'] else ""}
        </div>
        <div>{new_tag}{mkt_tag}</div>
        <ul class="notes">{notes_html}</ul>
      </div>
    </div>
    """


def write_report(scored: list[dict], seen: dict, today: str) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    new_ids = {l["id"] for l, _ in scored if l["id"] not in seen}

    # sort: new first, then by score desc
    scored.sort(key=lambda x: (x[0]["id"] not in new_ids, -x[1]["score"]))

    new_count = sum(1 for l, _ in scored if l["id"] in new_ids)
    strong = sum(1 for _, sc in scored if sc["score"] >= 75)
    good = sum(1 for _, sc in scored if 60 <= sc["score"] < 75)

    new_section = "".join(render_listing(l, sc, True) for l, sc in scored if l["id"] in new_ids)
    rest_section = "".join(render_listing(l, sc, False) for l, sc in scored if l["id"] not in new_ids)

    html_out = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Apartment Hunt — {today}</title>
<style>{REPORT_CSS}</style>
</head><body>
<h1>Apartment hunt — {today}</h1>
<div class="sub">Helsinki + Espoo · 2–3 rooms · ≤350 000€ · ≥45 m² · scored vs district market · <a href="history.html">past reports</a></div>

<div class="summary">
  <div class="stat"><div class="n">{len(scored)}</div><div class="l">matching listings</div></div>
  <div class="stat"><div class="n">{new_count}</div><div class="l">new today</div></div>
  <div class="stat"><div class="n">{strong}</div><div class="l">strong buys (75+)</div></div>
  <div class="stat"><div class="n">{good}</div><div class="l">worth viewing (60–74)</div></div>
</div>

{f'<div class="section-h">New today ({new_count})</div>{new_section}' if new_count else ''}
<div class="section-h">All matching listings ({len(scored) - new_count})</div>
{rest_section}

<p style="color:#888;font-size:12px;margin-top:32px">
Scoring: price-vs-market 40, criteria fit 20, transit 15, parking 10, character 10, risk -5.
Edit criteria.json or market_baseline.json to tune. Source: asunnot.oikotie.fi.
</p>
</body></html>
"""
    out = REPORTS_DIR / f"report_{today}.html"
    out.write_text(html_out, encoding="utf-8")
    # index.html is always the latest report (so the GitHub Pages root URL shows today's data)
    (REPORTS_DIR / "index.html").write_text(html_out, encoding="utf-8")
    # Build a small history page listing all dated reports
    write_history_page()
    return out


def write_history_page() -> None:
    """Write history.html listing every dated report, newest first."""
    reports = sorted(REPORTS_DIR.glob("report_*.html"), reverse=True)
    rows = "".join(
        f'<li><a href="{r.name}">{r.stem.replace("report_", "")}</a></li>\n'
        for r in reports
    )
    page = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Report history</title>
<style>{REPORT_CSS}</style></head><body>
<h1>Report history</h1>
<div class="sub"><a href="index.html">← back to today's report</a></div>
<ul>{rows}</ul>
</body></html>
"""
    (REPORTS_DIR / "history.html").write_text(page, encoding="utf-8")


# ---------- main ----------

def diagnose_oikotie() -> None:
    """Print enough of Oikotie's homepage to figure out where session tokens live now."""
    print("=" * 60)
    print("DIAGNOSTIC: probing Oikotie endpoints")
    print("=" * 60)

    # 1) Hit a few candidate token endpoints
    for path in ["/user/token", "/api/user/token", "/api/3.0/user/token",
                 "/session/token", "/api/session/token"]:
        url = "https://asunnot.oikotie.fi" + path
        status, body = _fetch(url, headers={"Accept": "application/json"})
        print(f"\n[GET {path}] HTTP {status}")
        print(f"  body[:400]: {body[:400]!r}")

    # 2) Dump homepage and look for token-shaped strings
    print(f"\n[GET /]")
    status, body = _fetch(HOMEPAGE_URL)
    print(f"  HTTP {status}, {len(body)} bytes")

    # Look for any meta tag, attribute, or JSON key that looks token-related
    print("\nLines mentioning 'token', 'cuid', 'loaded', 'OTA':")
    seen_lines = set()
    for line in body.split("\n"):
        low = line.lower()
        if any(k in low for k in ["token", "cuid", "ota-", '"loaded"']):
            stripped = line.strip()[:300]
            if stripped and stripped not in seen_lines:
                print(f"  {stripped}")
                seen_lines.add(stripped)
                if len(seen_lines) > 30:
                    print("  (more lines truncated)")
                    break

    # 3) Look for inline JSON blobs (next.js / nuxt / similar)
    print("\nSearching for embedded state JSON…")
    for marker in ["__NEXT_DATA__", "__NUXT__", "window.__INITIAL_STATE__", "self.__next_f"]:
        if marker in body:
            idx = body.index(marker)
            print(f"  found '{marker}' at byte {idx}")
            print(f"  snippet: {body[idx:idx+400]!r}")

    # 4) Check what /api/cards actually wants — look for any header hints in response
    print("\n[GET /api/cards without auth]")
    status, body = _fetch(OIKOTIE_API + "?cardType=100&size=1", headers={"Accept": "application/json"})
    print(f"  HTTP {status}, body: {body[:300]!r}")

    print("\n" + "=" * 60)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--open", action="store_true", help="Open report in browser when done")
    ap.add_argument("--history", action="store_true", help="List past reports")
    ap.add_argument("--diagnose", action="store_true", help="Print Oikotie endpoint diagnostics and exit")
    args = ap.parse_args()

    if args.diagnose:
        diagnose_oikotie()
        return 0

    if args.history:
        if not REPORTS_DIR.exists():
            print("No reports yet.")
            return 0
        for f in sorted(REPORTS_DIR.glob("report_*.html")):
            print(f.name)
        return 0

    criteria = json.loads(CRITERIA_FILE.read_text())
    baseline = json.loads(BASELINE_FILE.read_text())
    seen_before = load_seen()

    print("Fetching session tokens…")
    try:
        tokens = get_session_tokens()
        print(f"  got tokens: {sorted(tokens.keys())}")
    except Exception as e:
        print(f"  token fetch failed: {e}", file=sys.stderr)
        tokens = {}

    print("Fetching listings…")
    all_listings: list[dict] = []
    for city in criteria["cities"]:
        try:
            cards = fetch_listings(city, size=150, tokens=tokens)
            print(f"  {city}: {len(cards)} listings")
            for card in cards:
                all_listings.append(normalize_listing(card))
            time.sleep(1.5)
        except Exception as e:
            print(f"  {city}: ERROR — {e}", file=sys.stderr)

    # Dedupe by id
    by_id = {l["id"]: l for l in all_listings if l["id"]}
    print(f"Total unique: {len(by_id)}")

    matching: list[tuple[dict, dict]] = []
    for l in by_id.values():
        ok, _ = passes_criteria(l, criteria)
        if not ok:
            continue
        sc = score_listing(l, criteria, baseline)
        matching.append((l, sc))

    print(f"Matching criteria: {len(matching)}")

    today = dt.date.today().isoformat()
    out = write_report(matching, seen_before, today)
    print(f"Report: {out}")

    # update seen
    new_seen = dict(seen_before)
    for l, _ in matching:
        if l["id"] and l["id"] not in new_seen:
            new_seen[l["id"]] = today
    save_seen(new_seen)

    if args.open:
        webbrowser.open(out.as_uri())

    return 0


if __name__ == "__main__":
    sys.exit(main())
