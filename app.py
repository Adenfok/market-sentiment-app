"""
US Equity Market Sentiment Dashboard
------------------------------------
Tracks CNN Fear & Greed, AAII Investor Sentiment Survey, and NAAIM Exposure Index,
then scores whether sentiment favors entering the US equity market (contrarian lens).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Cache live pulls briefly so refresh storms don't hammer sources
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
CACHE_TTL_SEC = 300

# Long-term AAII averages (published by AAII)
AAII_AVG = {"bullish": 37.5, "neutral": 31.5, "bearish": 31.0}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def fetch_fear_greed(session: requests.Session) -> dict[str, Any]:
    """CNN Fear & Greed Index via public dataviz endpoint."""
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {
        **BROWSER_HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://edition.cnn.com",
        "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    }
    r = session.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    payload = r.json()
    fg = payload["fear_and_greed"]
    score = round(float(fg["score"]), 1)
    rating = str(fg.get("rating", "unknown")).replace("_", " ").title()
    return {
        "source": "CNN Fear & Greed Index",
        "source_url": "https://edition.cnn.com/markets/fear-and-greed",
        "value": score,
        "rating": rating,
        "previous_close": round(float(fg.get("previous_close", score)), 1),
        "previous_1_week": round(float(fg.get("previous_1_week", score)), 1),
        "previous_1_month": round(float(fg.get("previous_1_month", score)), 1),
        "as_of": fg.get("timestamp"),
        "scale": "0 = Extreme Fear · 100 = Extreme Greed",
        "ok": True,
        "error": None,
    }


def _strip_html(html: str) -> str:
    """Collapse HTML to plain text for resilient table parsing."""
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _aaii_from_rows(
    rows: list[dict[str, Any]],
    *,
    provider: str,
    source_url: str,
) -> dict[str, Any]:
    """Normalize a list of weekly AAII rows into the API payload."""
    if not rows:
        raise ValueError("No AAII rows")
    latest = rows[0]
    prior = rows[1] if len(rows) > 1 else None
    spread = round(latest["bullish"] - latest["bearish"], 1)
    return {
        "source": "AAII Investor Sentiment Survey",
        "source_url": source_url,
        "provider": provider,
        "as_of": latest["date"],
        "bullish": round(latest["bullish"], 1),
        "neutral": round(latest["neutral"], 1),
        "bearish": round(latest["bearish"], 1),
        "spread": spread,
        "historical_avg": AAII_AVG,
        "prior_week": prior,
        "history": rows[:8],
        "ok": True,
        "error": None,
    }


def _fetch_aaii_official(session: requests.Session) -> dict[str, Any]:
    """Primary: AAII historical results table (often bot-blocked by Imperva)."""
    url = "https://www.aaii.com/sentimentsurvey/sent_results"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    html = r.text
    if "Pardon Our Interruption" in html or "you were a bot" in html.lower():
        raise ValueError("AAII site bot challenge (Imperva)")

    text = _strip_html(html)
    row_re = re.compile(
        r"(?P<date>(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2})"
        r"\s+(?P<bull>\d{1,2}\.\d)\s*%\s+"
        r"(?P<neut>\d{1,2}\.\d)\s*%\s+"
        r"(?P<bear>\d{1,2}\.\d)\s*%",
        re.I,
    )
    rows = [
        {
            "date": m.group("date"),
            "bullish": float(m.group("bull")),
            "neutral": float(m.group("neut")),
            "bearish": float(m.group("bear")),
        }
        for m in row_re.finditer(text)
    ]
    if not rows:
        raise ValueError("Could not parse AAII survey table")
    return _aaii_from_rows(
        rows,
        provider="aaii.com",
        source_url="https://www.aaii.com/sentimentsurvey",
    )


def _fetch_aaii_macromicro(session: requests.Session) -> dict[str, Any]:
    """
    Fallback: MacroMicro republishes AAII weekly bull/neut/bear.

    Page embeds series_last_rows as JSON-like string ordered:
      [bearish series pairs], [neutral pairs], [bullish pairs]
    each pair = [date, value].
    """
    url = "https://en.macromicro.me/charts/20828/us-aaii-sentimentsurvey"
    r = session.get(url, timeout=25)
    r.raise_for_status()
    html = r.text

    m = re.search(r'"series_last_rows"\s*:\s*"(\[\[\[.+?\]\]\])"', html)
    if not m:
        # Sometimes the string is not escaped the same way
        m = re.search(r"series_last_rows\"?\s*:\s*\"?(\[\[\[.+?\]\]\])", html)
    if not m:
        raise ValueError("MacroMicro missing series_last_rows")

    raw = m.group(1)
    # Unescape \" if present
    raw = raw.replace('\\"', '"').replace("\\/", "/")
    try:
        import json

        matrix = json.loads(raw)
    except json.JSONDecodeError:
        # Manual parse: [["date","val"], ...] x 3 series
        triples = re.findall(
            r'\[\["(\d{4}-\d{2}-\d{2})","([\d.]+)"\],\["(\d{4}-\d{2}-\d{2})","([\d.]+)"\]\]',
            raw,
        )
        if len(triples) < 3:
            raise ValueError("Could not decode MacroMicro AAII matrix") from None
        # triples[0]=bear, [1]=neut, [2]=bull — each (prior_date, prior_val, last_date, last_val)
        bear_p, bear_l = float(triples[0][1]), float(triples[0][3])
        neut_p, neut_l = float(triples[1][1]), float(triples[1][3])
        bull_p, bull_l = float(triples[2][1]), float(triples[2][3])
        last_date, prior_date = triples[2][2], triples[2][0]
        rows = [
            {
                "date": last_date,
                "bullish": bull_l,
                "neutral": neut_l,
                "bearish": bear_l,
            },
            {
                "date": prior_date,
                "bullish": bull_p,
                "neutral": neut_p,
                "bearish": bear_p,
            },
        ]
        return _aaii_from_rows(
            rows,
            provider="macromicro.me",
            source_url="https://www.aaii.com/sentimentsurvey",
        )

    # matrix: [bear_points, neut_points, bull_points]; each point [date, value]
    if len(matrix) < 3:
        raise ValueError("MacroMicro AAII matrix incomplete")

    def _pts(series: list) -> list[tuple[str, float]]:
        out = []
        for pt in series:
            if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                out.append((str(pt[0]), float(pt[1])))
        return out

    bear_pts = _pts(matrix[0])
    neut_pts = _pts(matrix[1])
    bull_pts = _pts(matrix[2])
    if not (bear_pts and neut_pts and bull_pts):
        raise ValueError("MacroMicro AAII points empty")

    # MacroMicro lists older then newer within each series → reverse to newest-first
    n = min(len(bear_pts), len(neut_pts), len(bull_pts))
    rows = []
    for i in range(n - 1, -1, -1):
        rows.append(
            {
                "date": bull_pts[i][0],
                "bullish": bull_pts[i][1],
                "neutral": neut_pts[i][1],
                "bearish": bear_pts[i][1],
            }
        )

    return _aaii_from_rows(
        rows,
        provider="macromicro.me",
        source_url="https://www.aaii.com/sentimentsurvey",
    )


def _fetch_aaii_macromicro_series_pages(session: requests.Session) -> dict[str, Any]:
    """Secondary fallback: scrape latest % from individual MacroMicro series pages."""
    specs = {
        "bullish": "https://en.macromicro.me/series/6783/aaii-sentiment-survey-bullish",
        "neutral": "https://en.macromicro.me/series/6784/aaii-sentiment-survey-neutral",
        "bearish": "https://en.macromicro.me/series/6785/aaii-sentiment-survey-bearish",
    }
    values: dict[str, float] = {}
    for key, url in specs.items():
        r = session.get(url, timeout=20)
        r.raise_for_status()
        # Prefer values that look like survey percentages (20–60 range)
        pcts = [float(x) for x in re.findall(r">\s*([\d]{2}\.[\d]{1,2})\s*%?\s*<", r.text)]
        plausible = [p for p in pcts if 5.0 <= p <= 80.0]
        if not plausible:
            raise ValueError(f"No AAII value on series page for {key}")
        values[key] = plausible[0]

    total = values["bullish"] + values["neutral"] + values["bearish"]
    if not (95.0 <= total <= 105.0):
        raise ValueError(f"AAII series pages sum to {total:.1f}, expected ~100")

    return _aaii_from_rows(
        [
            {
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "bullish": values["bullish"],
                "neutral": values["neutral"],
                "bearish": values["bearish"],
            }
        ],
        provider="macromicro.me/series",
        source_url="https://www.aaii.com/sentimentsurvey",
    )


def fetch_aaii(session: requests.Session) -> dict[str, Any]:
    """
    AAII Investor Sentiment Survey (weekly).

    Tries official AAII first; falls back to MacroMicro mirrors when AAII
    returns a bot challenge or unparsable HTML.
    """
    errors: list[str] = []
    for fetcher in (
        _fetch_aaii_official,
        _fetch_aaii_macromicro,
        _fetch_aaii_macromicro_series_pages,
    ):
        try:
            return fetcher(session)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fetcher.__name__}: {exc}")
    raise ValueError("All AAII sources failed → " + " | ".join(errors))


def fetch_vix(session: requests.Session) -> dict[str, Any]:
    """CBOE VIX (S&P 500 volatility / fear gauge) via Yahoo Finance chart API."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1mo"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    payload = r.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("Yahoo VIX chart returned no result")

    meta = result[0].get("meta") or {}
    value = meta.get("regularMarketPrice")
    if value is None:
        raise ValueError("Yahoo VIX missing regularMarketPrice")

    value = round(float(value), 2)
    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    prev_close = round(float(prev_close), 2) if prev_close is not None else None

    # Walk daily closes for ~1 week and ~1 month deltas when available
    timestamps = result[0].get("timestamp") or []
    closes = (
        ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    )
    pairs = [
        (ts, cl)
        for ts, cl in zip(timestamps, closes)
        if cl is not None
    ]
    prev_1w = None
    prev_1m = None
    if pairs:
        last_ts = pairs[-1][0]
        for ts, cl in reversed(pairs[:-1]):
            age_days = (last_ts - ts) / 86400
            if prev_1w is None and age_days >= 5:
                prev_1w = round(float(cl), 2)
            if prev_1m is None and age_days >= 20:
                prev_1m = round(float(cl), 2)
                break

    entry_zone = value > 30

    return {
        "source": "CBOE VIX (S&P 500)",
        "source_url": "https://finance.yahoo.com/quote/%5EVIX/",
        "symbol": meta.get("symbol", "^VIX"),
        "value": value,
        "previous_close": prev_close,
        "previous_1_week": prev_1w,
        "previous_1_month": prev_1m,
        "entry_zone": entry_zone,  # True when VIX > 30
        "threshold": 30,
        "scale": "Higher VIX = more fear/volatility · VIX > 30 = entry zone",
        "ok": True,
        "error": None,
    }


def compute_rsi(closes: list[float], period: int = 14) -> float | None:
    """Wilder RSI from a list of closing prices (oldest → newest)."""
    if len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def fetch_rsp_rsi(session: requests.Session, period: int = 14) -> dict[str, Any]:
    """
    RSP (Invesco S&P 500 Equal Weight ETF) daily RSI(14) via Yahoo Finance.
    Equal-weight S&P is a broader market-breadth-style pulse than cap-weighted SPY.
    """
    url = "https://query1.finance.yahoo.com/v8/finance/chart/RSP?interval=1d&range=6mo"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    payload = r.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("Yahoo RSP chart returned no result")

    meta = result[0].get("meta") or {}
    timestamps = result[0].get("timestamp") or []
    closes_raw = (
        ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    )
    pairs = [
        (ts, float(cl))
        for ts, cl in zip(timestamps, closes_raw)
        if cl is not None
    ]
    if len(pairs) < period + 2:
        raise ValueError(f"Need at least {period + 2} RSP closes for RSI")

    closes = [c for _, c in pairs]
    rsi = compute_rsi(closes, period=period)
    if rsi is None:
        raise ValueError("Could not compute RSP RSI")

    # Prior RSI snapshots: drop last 5 and last 21 closes and recompute
    rsi_1w = compute_rsi(closes[:-5], period=period) if len(closes) > period + 6 else None
    rsi_1m = compute_rsi(closes[:-21], period=period) if len(closes) > period + 22 else None

    price = float(meta.get("regularMarketPrice") or closes[-1])
    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    last_ts = pairs[-1][0]
    as_of = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "source": "RSP RSI(14)",
        "source_url": "https://finance.yahoo.com/quote/RSP/",
        "symbol": "RSP",
        "name": "Invesco S&P 500 Equal Weight ETF",
        "period": period,
        "value": round(float(rsi), 1),
        "price": round(price, 2),
        "previous_close": round(float(prev_close), 2) if prev_close is not None else None,
        "previous_1_week": round(float(rsi_1w), 1) if rsi_1w is not None else None,
        "previous_1_month": round(float(rsi_1m), 1) if rsi_1m is not None else None,
        "as_of": as_of,
        "oversold": float(rsi) <= 30,
        "overbought": float(rsi) >= 70,
        "scale": "RSI(14): ≤30 oversold (entry favor) · ≥70 overbought (caution)",
        "ok": True,
        "error": None,
    }


def fetch_naaim(session: requests.Session) -> dict[str, Any]:
    """NAAIM Exposure Index from official NAAIM page."""
    url = "https://naaim.org/programs/naaim-exposure-index/"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    html = r.text
    text = _strip_html(html)

    # Prefer headline number (handles curly apostrophe + HTML between label and value)
    m = re.search(
        r"this\s+week['\u2019]?s\s+NAAIM\s+Exposure\s+Index\s+number\s+is\*?\s*:?\s*([\d.]+)",
        text,
        re.I,
    )
    # Fall back to first row of the published history table
    hist = re.findall(
        r"(\d{2}/\d{2}/\d{4})\s+(-?\d+(?:\.\d+)?)",
        text,
    )
    if m:
        value = float(m.group(1))
    elif hist:
        value = float(hist[0][1])
    else:
        raise ValueError("Could not parse NAAIM exposure number")

    history = [{"date": d, "value": float(v)} for d, v in hist[:8]]

    posted = re.search(
        r"Posted on\s+([A-Za-z]+,?\s+[A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        text,
    )
    q_avg = re.search(
        r"Last Quarter Average(?:\s*\([^)]*\))?\s*([\d]+\.\d+)",
        text,
        re.I,
    )

    return {
        "source": "NAAIM Exposure Index",
        "source_url": "https://naaim.org/programs/naaim-exposure-index/",
        "value": value,
        "as_of": history[0]["date"] if history else (posted.group(1) if posted else None),
        "quarter_avg": float(q_avg.group(1)) if q_avg else None,
        "prior_week": history[1]["value"] if len(history) > 1 else None,
        "history": history,
        "scale": "-200 leveraged short · 0 cash · 100 fully invested · 200 leveraged long",
        "ok": True,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Scoring (contrarian equity-entry lens)
# ---------------------------------------------------------------------------

def score_fear_greed(value: float) -> dict[str, Any]:
    """Lower fear → better contrarian entry. Score -2 … +2."""
    if value <= 25:
        pts, label, signal = 2, "Extreme Fear", "Strong buy zone (contrarian)"
    elif value <= 45:
        pts, label, signal = 1, "Fear", "Favorable for gradual entries"
    elif value < 55:
        pts, label, signal = 0, "Neutral", "Sentiment not extreme either way"
    elif value < 75:
        pts, label, signal = -1, "Greed", "Caution — optimism elevated"
    else:
        pts, label, signal = -2, "Extreme Greed", "Poor contrarian entry zone"
    return {"points": pts, "label": label, "signal": signal}


def score_aaii(bullish: float, bearish: float) -> dict[str, Any]:
    """Elevated retail bearishness historically aligns with better forward returns."""
    spread = bullish - bearish
    if bearish >= 50 or spread <= -20:
        pts, label, signal = 2, "Extreme retail pessimism", "Strong contrarian buy signal"
    elif bearish >= 40 or spread <= -8:
        pts, label, signal = 1, "Retail lean bearish", "Favorable for entries"
    elif abs(spread) < 8 and 28 <= bullish <= 42:
        pts, label, signal = 0, "Balanced / near average", "No strong edge from survey"
    elif bullish >= 50 or spread >= 20:
        pts, label, signal = -2, "Extreme retail optimism", "Poor contrarian entry zone"
    elif bullish >= 42 or spread >= 8:
        pts, label, signal = -1, "Retail lean bullish", "Caution — crowd optimistic"
    else:
        pts, label, signal = 0, "Mildly mixed", "Neutral reading"
    return {"points": pts, "label": label, "signal": signal, "spread": round(spread, 1)}


def score_naaim(value: float) -> dict[str, Any]:
    """
    NAAIM average equity exposure (contrarian).

    User bands:
      40–64%  → +1
      64–98%  →  0  (Strong hold mode)
      98–100% → -1  (Room to take profit)
    Extremes:
      < 40%   → +2
      > 100%  → -2
    Boundaries: [40,64) +1 · [64,98) 0 · [98,100] -1
    """
    if value < 40:
        pts, label, signal = (
            2,
            "Very low exposure",
            "Managers defensive — buy opportunity",
        )
    elif value < 64:
        pts, label, signal = (
            1,
            "Below-average exposure (40–64%)",
            "Room for managers to add risk — constructive for entries",
        )
    elif value < 98:
        pts, label, signal = (
            0,
            "Strong hold mode (64–98%)",
            "Strong hold mode — managers committed long; stay invested, no extreme signal",
        )
    elif value <= 100:
        pts, label, signal = (
            -1,
            "Near fully invested (98–100%)",
            "Room to take profit — managers almost fully invested; consider trimming",
        )
    else:
        pts, label, signal = (
            -2,
            "Leveraged / extreme long (>100%)",
            "Crowded leveraged long — elevated risk; prioritize taking profit / risk cut",
        )
    return {"points": pts, "label": label, "signal": signal}


def score_vix(value: float) -> dict[str, Any]:
    """
    CBOE VIX — elevated vol = fear = better contrarian entry.

    Rule: VIX > 30 is an entry zone (+2).
    """
    if value > 40:
        pts, label, signal = (
            2,
            "Panic / extreme vol",
            "Strong entry zone — VIX well above 30",
        )
    elif value > 30:
        pts, label, signal = (
            2,
            "Elevated fear (entry zone)",
            "Entry point — VIX > 30 signals market stress",
        )
    elif value >= 25:
        pts, label, signal = 1, "Elevated vol", "Approaching stress — watch for VIX > 30"
    elif value >= 15:
        pts, label, signal = 0, "Normal vol regime", "No fear spike — neutral for entry timing"
    elif value >= 12:
        pts, label, signal = -1, "Low vol / complacency", "Calm markets — weaker contrarian entry"
    else:
        pts, label, signal = -2, "Extreme complacency", "Very low VIX — poor fear-based entry"
    return {
        "points": pts,
        "label": label,
        "signal": signal,
        "entry_zone": value > 30,
    }


def score_rsp_rsi(value: float) -> dict[str, Any]:
    """
    RSP RSI(14) — equal-weight S&P momentum / mean-reversion gauge.

    Oversold (low RSI) favors entry; overbought (high RSI) favors caution.
    """
    if value <= 30:
        pts, label, signal = (
            2,
            "Oversold",
            "RSP RSI ≤ 30 — oversold equal-weight S&P; strong mean-reversion entry favor",
        )
    elif value <= 40:
        pts, label, signal = (
            1,
            "Soft oversold",
            "RSP RSI in soft oversold zone — constructive for staged entries",
        )
    elif value < 60:
        pts, label, signal = (
            0,
            "Neutral momentum",
            "RSP RSI mid-range — no strong overbought/oversold edge",
        )
    elif value < 70:
        pts, label, signal = (
            -1,
            "Elevated momentum",
            "RSP RSI elevated — momentum strong but not extreme; watch for stretch",
        )
    else:
        pts, label, signal = (
            -2,
            "Overbought",
            "RSP RSI ≥ 70 — overbought equal-weight S&P; weaker entry / consider patience",
        )
    return {
        "points": pts,
        "label": label,
        "signal": signal,
        "oversold": value <= 30,
        "overbought": value >= 70,
    }


def build_conclusion(
    fg: dict[str, Any],
    aaii: dict[str, Any],
    naaim: dict[str, Any],
    vix: dict[str, Any],
    rsp_rsi: dict[str, Any],
) -> dict[str, Any]:
    scores = {
        "fear_greed": score_fear_greed(fg["value"]) if fg.get("ok") else None,
        "aaii": score_aaii(aaii["bullish"], aaii["bearish"]) if aaii.get("ok") else None,
        "naaim": score_naaim(naaim["value"]) if naaim.get("ok") else None,
        "vix": score_vix(vix["value"]) if vix.get("ok") else None,
        "rsp_rsi": score_rsp_rsi(rsp_rsi["value"]) if rsp_rsi.get("ok") else None,
    }

    available = [s for s in scores.values() if s is not None]
    if not available:
        return {
            "total_points": 0,
            "max_points": 0,
            "verdict": "Data unavailable",
            "verdict_class": "neutral",
            "summary": "Could not load sentiment sources. Try again shortly.",
            "details": [],
            "disclaimer": DISCLAIMER,
            "scores": scores,
            "vix_entry_zone": False,
        }

    total = sum(s["points"] for s in available)
    max_pts = 2 * len(available)
    vix_entry = bool(vix.get("ok") and vix.get("value", 0) > 30)

    # 5 gauges max ±10; keep Strong Buy as a clear multi-signal threshold
    if total >= 6:
        verdict, vclass = "Strong Buy Zone", "strong-buy"
        summary = (
            "Sentiment is broadly pessimistic or under-invested. Historically this is a "
            "better environment for adding US equity exposure (contrarian)."
        )
    elif total >= 2:
        verdict, vclass = "Favorable to Enter", "buy"
        summary = (
            "Overall sentiment leans cautious. Conditions support staged or gradual "
            "entries into US equities rather than waiting for perfect calm."
        )
    elif total >= 0:
        verdict, vclass = "Neutral — Selective Entry", "neutral"
        summary = (
            "Signals are mixed or mid-range. Not a screaming buy or sell. Prefer "
            "high-quality names, dollar-cost averaging, and position sizing discipline."
        )
    elif total >= -3:
        verdict, vclass = "Caution — Wait for Better Levels", "caution"
        summary = (
            "Crowd optimism / high exposure is elevated. Entering aggressively now has a "
            "weaker risk/reward from a pure sentiment perspective."
        )
    else:
        verdict, vclass = "Poor Entry Zone", "avoid"
        summary = (
            "Extreme greed and/or crowded long positioning dominate. Contrarian history "
            "suggests waiting for a fear spike or pullback before new risk."
        )

    # Hard rule: VIX > 30 is treated as an entry point
    if vix_entry:
        if vclass in ("caution", "avoid", "neutral"):
            verdict, vclass = "Favorable to Enter", "buy"
        summary = (
            f"VIX is {vix['value']} (> 30 entry threshold) — market stress / fear is elevated, "
            "which this model treats as an entry zone. "
            + summary
        )

    details = []
    if scores["vix"]:
        zone = "ENTRY ZONE (VIX > 30)" if vix_entry else "below entry threshold (≤ 30)"
        details.append(
            f"VIX at {vix['value']} — {zone}: {scores['vix']['signal']}."
        )
    if scores["rsp_rsi"]:
        details.append(
            f"RSP RSI(14) at {rsp_rsi['value']} ({scores['rsp_rsi']['label']}): "
            f"{scores['rsp_rsi']['signal']}."
        )
    if scores["fear_greed"]:
        details.append(
            f"Fear & Greed at {fg['value']} ({scores['fear_greed']['label']}): "
            f"{scores['fear_greed']['signal']}."
        )
    if scores["aaii"]:
        details.append(
            f"AAII bull {aaii['bullish']}% / bear {aaii['bearish']}% "
            f"(spread {aaii['spread']:+.1f} pts): {scores['aaii']['signal']}."
        )
    if scores["naaim"]:
        details.append(
            f"NAAIM exposure {naaim['value']}%: {scores['naaim']['signal']}."
        )

    def _score_payload(v: dict[str, Any] | None) -> dict[str, Any] | None:
        if not v:
            return None
        out = {
            "points": v["points"],
            "label": v["label"],
            "signal": v["signal"],
        }
        for key in ("entry_zone", "oversold", "overbought"):
            if key in v:
                out[key] = v[key]
        return out

    return {
        "total_points": total,
        "max_points": max_pts,
        "min_points": -max_pts,
        "verdict": verdict,
        "verdict_class": vclass,
        "summary": summary,
        "details": details,
        "disclaimer": DISCLAIMER,
        "vix_entry_zone": vix_entry,
        "scores": {k: _score_payload(v) for k, v in scores.items()},
    }


DISCLAIMER = (
    "Educational / research tool only — not investment advice. Sentiment indicators are "
    "noisy and work best as contrarian context, not as a standalone timing system. "
    "Always consider valuation, macro, earnings, liquidity, and your own risk tolerance."
)


def collect_all(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _CACHE["data"] and (now - _CACHE["ts"]) < CACHE_TTL_SEC:
        return _CACHE["data"]

    session = _session()
    errors: list[str] = []

    try:
        fg = fetch_fear_greed(session)
    except Exception as exc:  # noqa: BLE001
        fg = {"ok": False, "error": str(exc), "source": "CNN Fear & Greed Index"}
        errors.append(f"Fear & Greed: {exc}")

    try:
        aaii = fetch_aaii(session)
    except Exception as exc:  # noqa: BLE001
        aaii = {"ok": False, "error": str(exc), "source": "AAII Investor Sentiment Survey"}
        errors.append(f"AAII: {exc}")

    try:
        naaim = fetch_naaim(session)
    except Exception as exc:  # noqa: BLE001
        naaim = {"ok": False, "error": str(exc), "source": "NAAIM Exposure Index"}
        errors.append(f"NAAIM: {exc}")

    try:
        vix = fetch_vix(session)
    except Exception as exc:  # noqa: BLE001
        vix = {"ok": False, "error": str(exc), "source": "CBOE VIX (S&P 500)"}
        errors.append(f"VIX: {exc}")

    try:
        rsp_rsi = fetch_rsp_rsi(session)
    except Exception as exc:  # noqa: BLE001
        rsp_rsi = {"ok": False, "error": str(exc), "source": "RSP RSI(14)"}
        errors.append(f"RSP RSI: {exc}")

    conclusion = build_conclusion(fg, aaii, naaim, vix, rsp_rsi)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fear_greed": fg,
        "aaii": aaii,
        "naaim": naaim,
        "vix": vix,
        "rsp_rsi": rsp_rsi,
        "conclusion": conclusion,
        "errors": errors,
    }
    _CACHE["ts"] = now
    _CACHE["data"] = payload
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sentiment")
def api_sentiment():
    force = False
    from flask import request

    force = request.args.get("refresh") == "1"
    data = collect_all(force=force)
    return jsonify(data)


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    import os

    # Warm cache once at startup for snappier first paint
    try:
        collect_all(force=True)
        print("Initial data load OK")
    except Exception as exc:  # noqa: BLE001
        print(f"Initial data load failed (will retry on request): {exc}")

    port = int(os.environ.get("PORT", "5050"))
    # 0.0.0.0 required on Render / cloud hosts; local still works via localhost
    host = os.environ.get("HOST", "0.0.0.0")
    print("\n  US Equity Sentiment Dashboard")
    print(f"  Listening on http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)
