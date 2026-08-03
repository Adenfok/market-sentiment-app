"""
US Equity Market Sentiment Dashboard
------------------------------------
Tracks CNN Fear & Greed, AAII Investor Sentiment Survey, and NAAIM Exposure Index,
then scores whether sentiment favors entering the US equity market (contrarian lens).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
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

# Short cache for daily/live gauges (VIX, F&G, RSP)
_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
CACHE_TTL_SEC = 300

# Weekly gauges — disk cache; Refresh does not re-hit these sources
# 6 days covers one survey cycle until the next weekly print
WEEKLY_CACHE_TTL_SEC = 6 * 24 * 3600
AAII_CACHE_TTL_SEC = WEEKLY_CACHE_TTL_SEC  # alias
DATA_DIR = Path(__file__).resolve().parent / "data"
AAII_CACHE_PATH = DATA_DIR / "aaii_cache.json"
NAAIM_CACHE_PATH = DATA_DIR / "naaim_cache.json"

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
        "cached": False,
        "stale": False,
        "cache_age_hours": 0.0,
    }


def _load_weekly_disk_cache(path: Path) -> tuple[dict[str, Any] | None, float | None]:
    """Return (payload, saved_unix_ts) or (None, None)."""
    try:
        if not path.exists():
            return None, None
        raw = json.loads(path.read_text(encoding="utf-8"))
        saved = float(raw.get("saved_at", 0))
        data = raw.get("data")
        if not isinstance(data, dict) or not data.get("ok"):
            return None, None
        return data, saved
    except Exception:  # noqa: BLE001
        return None, None


def _save_weekly_disk_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Don't persist transient flags
        skip = ("cached", "stale", "cache_age_hours", "cached_at", "cache_note")
        to_store = {k: v for k, v in data.items() if k not in skip}
        payload = {
            "saved_at": time.time(),
            "saved_at_iso": datetime.now(timezone.utc).isoformat(),
            "data": to_store,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _annotate_weekly_cache(
    data: dict[str, Any],
    *,
    cached: bool,
    saved_ts: float | None,
    stale: bool = False,
) -> dict[str, Any]:
    out = dict(data)
    out["cached"] = cached
    out["stale"] = stale
    if saved_ts:
        out["cache_age_hours"] = round((time.time() - saved_ts) / 3600, 1)
        out["cached_at"] = datetime.fromtimestamp(saved_ts, tz=timezone.utc).isoformat()
    else:
        out["cache_age_hours"] = 0.0
    return out


def _load_aaii_disk_cache() -> tuple[dict[str, Any] | None, float | None]:
    return _load_weekly_disk_cache(AAII_CACHE_PATH)


def _save_aaii_disk_cache(data: dict[str, Any]) -> None:
    _save_weekly_disk_cache(AAII_CACHE_PATH, data)


def _annotate_aaii_cache(
    data: dict[str, Any],
    *,
    cached: bool,
    saved_ts: float | None,
    stale: bool = False,
) -> dict[str, Any]:
    return _annotate_weekly_cache(
        data, cached=cached, saved_ts=saved_ts, stale=stale
    )


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


def _fetch_aaii_network(session: requests.Session) -> dict[str, Any]:
    """
    Live AAII pull.

    Prefer MacroMicro (less bot-blocking) over aaii.com. Official AAII last.
    """
    errors: list[str] = []
    for fetcher in (
        _fetch_aaii_macromicro,
        _fetch_aaii_macromicro_series_pages,
        _fetch_aaii_official,
    ):
        try:
            return fetcher(session)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fetcher.__name__}: {exc}")
    raise ValueError("All AAII sources failed → " + " | ".join(errors))


def get_aaii(session: requests.Session, *, force: bool = False) -> dict[str, Any]:
    """
    Weekly AAII with long-lived disk cache.

    - Normal / Refresh: reuse cache if younger than AAII_CACHE_TTL_SEC (6 days).
    - force=True (refresh_aaii=1): try network; on failure keep last good cache.
    - Never fail hard if a previous successful reading exists on disk.
    """
    cached, saved_ts = _load_aaii_disk_cache()
    age = (time.time() - saved_ts) if saved_ts else None
    fresh_enough = age is not None and age < AAII_CACHE_TTL_SEC

    if cached and fresh_enough and not force:
        return _annotate_aaii_cache(cached, cached=True, saved_ts=saved_ts, stale=False)

    try:
        live = _fetch_aaii_network(session)
        _save_aaii_disk_cache(live)
        return _annotate_aaii_cache(live, cached=False, saved_ts=time.time(), stale=False)
    except Exception as exc:  # noqa: BLE001
        if cached:
            # Prefer last week's good reading over empty UI / error banner noise
            out = _annotate_aaii_cache(
                cached,
                cached=True,
                saved_ts=saved_ts,
                stale=True,
            )
            out["error"] = None
            out["cache_note"] = f"Using saved weekly AAII (live fetch failed: {exc})"
            return out
        raise


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

    entry_zone = value > 30  # hard override: at least Favorable to Enter

    return {
        "source": "CBOE VIX (S&P 500)",
        "source_url": "https://finance.yahoo.com/quote/%5EVIX/",
        "symbol": meta.get("symbol", "^VIX"),
        "value": value,
        "previous_close": prev_close,
        "previous_1_week": prev_1w,
        "previous_1_month": prev_1m,
        "entry_zone": entry_zone,
        "threshold": 30,
        "scale": "VIX >30 = Favorable override · >40 = +3 pts · 20–23 = +1 · 23–30 = 0",
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


def fetch_spy_regime(session: requests.Session, window: int = 200) -> dict[str, Any]:
    """
    SPY close vs 200-day SMA — price-regime context only for Neutral / Caution.

    Not used for Strong Buy / Favorable / Avoid (those are already clear).
    """
    # ~14 months covers 200 trading days + buffer for holidays
    url = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&range=18mo"
    r = session.get(url, timeout=20)
    r.raise_for_status()
    payload = r.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("Yahoo SPY chart returned no result")

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
    if len(pairs) < window:
        raise ValueError(f"Need at least {window} SPY closes for {window}DMA")

    closes = [c for _, c in pairs]
    sma = sum(closes[-window:]) / window
    price = float(meta.get("regularMarketPrice") or closes[-1])
    last_ts = pairs[-1][0]
    as_of = datetime.fromtimestamp(last_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    above = price >= sma
    pct_vs = ((price / sma) - 1.0) * 100.0

    return {
        "source": "SPY vs 200-day SMA",
        "source_url": "https://finance.yahoo.com/quote/SPY/",
        "symbol": "SPY",
        "price": round(price, 2),
        "sma_200": round(sma, 2),
        "window": window,
        "above_200dma": above,
        "pct_vs_200dma": round(pct_vs, 2),
        "label": "Above 200DMA" if above else "Below 200DMA",
        "as_of": as_of,
        "scale": "Tag only for Neutral / Soft·Hard Caution — not a standalone signal",
        "ok": True,
        "error": None,
    }


def _naaim_payload(
    *,
    value: float,
    as_of: str | None,
    prior_week: float | None,
    history: list[dict[str, Any]],
    provider: str,
    source_url: str,
    quarter_avg: float | None = None,
) -> dict[str, Any]:
    """Normalize NAAIM readings into the API payload."""
    return {
        "source": "NAAIM Exposure Index",
        "source_url": source_url,
        "provider": provider,
        "value": round(float(value), 2),
        "as_of": as_of,
        "quarter_avg": quarter_avg,
        "prior_week": round(float(prior_week), 2) if prior_week is not None else None,
        "history": history,
        "scale": "-200 leveraged short · 0 cash · 100 fully invested · 200 leveraged long",
        "ok": True,
        "error": None,
        "cached": False,
        "stale": False,
        "cache_age_hours": 0.0,
    }


def _fetch_naaim_macromicro(session: requests.Session) -> dict[str, Any]:
    """
    Primary: MacroMicro republishes weekly NAAIM Exposure Index.

    Chart embeds series_last_rows as JSON-like string:
      series[0] = NAAIM index points [date, value] (older → newer)
      series[1] = often a moving average (ignore for scoring)
    Official NAAIM site no longer publishes the free weekly table.
    """
    url = "https://en.macromicro.me/charts/46198/naaim-exposure-index"
    r = session.get(url, timeout=25)
    r.raise_for_status()
    html = r.text

    m = re.search(r'"series_last_rows"\s*:\s*"(\[\[\[.+?\]\]\])"', html)
    if not m:
        m = re.search(r"series_last_rows\"?\s*:\s*\"?(\[\[\[.+?\]\]\])", html)
    if not m:
        raise ValueError("MacroMicro NAAIM missing series_last_rows")

    raw = m.group(1).replace('\\"', '"').replace("\\/", "/")
    try:
        matrix = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not decode MacroMicro NAAIM matrix: {exc}") from exc

    if not matrix or not isinstance(matrix, list):
        raise ValueError("MacroMicro NAAIM matrix empty")

    # series[0] = Exposure Index
    series = matrix[0]
    if not isinstance(series, list) or not series:
        raise ValueError("MacroMicro NAAIM series[0] empty")

    points: list[tuple[str, float]] = []
    for pt in series:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            try:
                points.append((str(pt[0]), float(pt[1])))
            except (TypeError, ValueError):
                continue
    if not points:
        raise ValueError("MacroMicro NAAIM points unparsable")

    # MacroMicro lists older → newer → reverse to newest-first
    points = list(reversed(points))
    value = points[0][1]
    as_of = points[0][0]
    prior = points[1][1] if len(points) > 1 else None
    history = [{"date": d, "value": round(v, 2)} for d, v in points[:8]]

    # Optional: series[1] may be MA; not used for score
    return _naaim_payload(
        value=value,
        as_of=as_of,
        prior_week=prior,
        history=history,
        provider="macromicro.me",
        # Keep official program page as the conceptual source link
        source_url="https://naaim.org/programs/naaim-exposure-index/",
    )


def _fetch_naaim_official(session: requests.Session) -> dict[str, Any]:
    """
    Fallback: official NAAIM page.

    As of mid-2026 the free page often no longer embeds the weekly number
    (subscriber / members-only). Kept as a secondary source if they restore it.
    """
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
        raise ValueError("Could not parse NAAIM exposure number (page may be members-only)")

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

    return _naaim_payload(
        value=value,
        as_of=history[0]["date"] if history else (posted.group(1) if posted else None),
        prior_week=history[1]["value"] if len(history) > 1 else None,
        history=history,
        provider="naaim.org",
        source_url="https://naaim.org/programs/naaim-exposure-index/",
        quarter_avg=float(q_avg.group(1)) if q_avg else None,
    )


def _fetch_naaim_network(session: requests.Session) -> dict[str, Any]:
    """
    Live NAAIM pull.

    Prefer MacroMicro (still free / less bot-blocking). Official NAAIM last —
    public weekly table is often gone.
    """
    errors: list[str] = []
    for fetcher in (_fetch_naaim_macromicro, _fetch_naaim_official):
        try:
            return fetcher(session)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fetcher.__name__}: {exc}")
    raise ValueError("All NAAIM sources failed → " + " | ".join(errors))


def get_naaim(session: requests.Session, *, force: bool = False) -> dict[str, Any]:
    """
    Weekly NAAIM with long-lived disk cache.

    Same policy as AAII: Refresh does not re-hit NAAIM; use refresh_naaim=1 or
    refresh_weekly=1 to force a live pull.
    Primary live source: MacroMicro chart (official site often paywalled).
    """
    cached, saved_ts = _load_weekly_disk_cache(NAAIM_CACHE_PATH)
    age = (time.time() - saved_ts) if saved_ts else None
    fresh_enough = age is not None and age < WEEKLY_CACHE_TTL_SEC

    if cached and fresh_enough and not force:
        return _annotate_weekly_cache(cached, cached=True, saved_ts=saved_ts, stale=False)

    try:
        live = _fetch_naaim_network(session)
        _save_weekly_disk_cache(NAAIM_CACHE_PATH, live)
        return _annotate_weekly_cache(live, cached=False, saved_ts=time.time(), stale=False)
    except Exception as exc:  # noqa: BLE001
        if cached:
            out = _annotate_weekly_cache(
                cached,
                cached=True,
                saved_ts=saved_ts,
                stale=True,
            )
            out["error"] = None
            out["cache_note"] = f"Using saved weekly NAAIM (live fetch failed: {exc})"
            return out
        raise


# ---------------------------------------------------------------------------
# Scoring (contrarian equity-entry lens)
# ---------------------------------------------------------------------------

def score_fear_greed(value: float) -> dict[str, Any]:
    """
    CNN Fear & Greed — hybrid:
      ≤10  → +3 (capitulation)
      10–25 → +2 (extreme fear, contrarian)
      25–45 → −1 (fear = risk-off regime)
      45–55 →  0
      55–75 → +1 (greed = risk-on)
      ≥75   → −2 (extreme greed / overhype)
    """
    if value <= 10:
        pts, label, signal = (
            3.0,
            "Capitulation fear (≤10)",
            "Deep capitulation — strongest F&G entry score",
        )
    elif value <= 25:
        pts, label, signal = (
            2.0,
            "Extreme Fear",
            "Extreme fear band — contrarian entry favor",
        )
    elif value < 45:
        pts, label, signal = (
            -1.0,
            "Fear (risk-off)",
            "Risk-off tape — fear often persists; not a buy-the-dip score alone",
        )
    elif value < 55:
        pts, label, signal = (
            0.0,
            "Neutral",
            "Sentiment not extreme either way",
        )
    elif value < 75:
        pts, label, signal = (
            1.0,
            "Greed (risk-on)",
            "Risk-on regime — momentum/participation constructive",
        )
    else:
        pts, label, signal = (
            -2.0,
            "Extreme Greed (overhype)",
            "Overhype / extreme greed — poor entry; elevated risk of pullback",
        )
    return {"points": pts, "label": label, "signal": signal}


def score_aaii(bullish: float, bearish: float) -> dict[str, Any]:
    """
    AAII survey — same bands as before, but half weight
    (+2→+1, +1→+0.5, −1→−0.5, −2→−1).
    """
    spread = bullish - bearish
    if bearish >= 50 or spread <= -20:
        raw, label, signal = 2, "Extreme retail pessimism", "Contrarian buy (half weight)"
    elif bearish >= 40 or spread <= -8:
        raw, label, signal = 1, "Retail lean bearish", "Mild contrarian favor (half weight)"
    elif abs(spread) < 8 and 28 <= bullish <= 42:
        raw, label, signal = 0, "Balanced / near average", "No strong edge from survey"
    elif bullish >= 50 or spread >= 20:
        raw, label, signal = -2, "Extreme retail optimism", "Crowd optimistic (half weight)"
    elif bullish >= 42 or spread >= 8:
        raw, label, signal = -1, "Retail lean bullish", "Mild caution (half weight)"
    else:
        raw, label, signal = 0, "Mildly mixed", "Neutral reading"
    return {
        "points": raw * 0.5,
        "label": label,
        "signal": signal,
        "spread": round(spread, 1),
        "raw_points": raw,
    }


def score_naaim(value: float) -> dict[str, Any]:
    """
    NAAIM exposure — same bands as before, half weight
    (+2→+1, +1→+0.5, −1→−0.5, −2→−1).

      <40%     → raw +2 → +1.0
      40–64%   → raw +1 → +0.5
      64–98%   → 0 Strong hold
      98–100%  → raw −1 → −0.5 take profit
      >100%    → raw −2 → −1.0
    """
    if value < 40:
        raw, label, signal = (
            2,
            "Very low exposure",
            "Managers defensive — buy opportunity (half weight)",
        )
    elif value < 64:
        raw, label, signal = (
            1,
            "Below-average exposure (40–64%)",
            "Room to add risk (half weight)",
        )
    elif value < 98:
        raw, label, signal = (
            0,
            "Strong hold mode (64–98%)",
            "Strong hold mode — managers committed long; stay invested",
        )
    elif value <= 100:
        raw, label, signal = (
            -1,
            "Near fully invested (98–100%)",
            "Room to take profit (half weight)",
        )
    else:
        raw, label, signal = (
            -2,
            "Leveraged / extreme long (>100%)",
            "Crowded long — elevated risk (half weight)",
        )
    return {
        "points": raw * 0.5,
        "label": label,
        "signal": signal,
        "raw_points": raw,
    }


def score_vix(value: float) -> dict[str, Any]:
    """
    CBOE VIX scoring (user rules):
      >40      → +3  panic / strong entry
      (30, 40] → +2  elevated stress
      (23, 30] →  0  choppy / hard range (user: ~22–30)
      [20, 23] → +1  healthy correction often finishing
      [15, 20) →  0  normal
      [12, 15) → −1  complacency
      <12      → −2  extreme complacency

    Hard override (verdict floor): VIX > 30 → at least Favorable (entry_zone flag).
    Points for VIX > 40 remain +3 (panic).
    """
    if value > 40:
        pts, label, signal = (
            3.0,
            "Panic / extreme vol",
            "Strong entry zone — VIX > 40 (+3)",
        )
    elif value > 30:
        pts, label, signal = (
            2.0,
            "Elevated stress (30–40)",
            "High vol stress — hard override: at least Favorable to Enter",
        )
    elif value > 23:
        # covers ~22–30 choppy zone (22–23 handled as healthy-correction +1 below)
        pts, label, signal = (
            0.0,
            "Choppy / hard range (23–30)",
            "Volatile range — often grindy; no edge from VIX alone",
        )
    elif value >= 20:
        pts, label, signal = (
            1.0,
            "Healthy correction vol (20–23)",
            "Typical healthy pullback vol — correction often late-stage",
        )
    elif value >= 15:
        pts, label, signal = (
            0.0,
            "Normal vol (15–20)",
            "Normal regime — neutral for entry timing",
        )
    elif value >= 12:
        pts, label, signal = (
            -1.0,
            "Low vol / complacency",
            "Calm markets — weaker fear-based entry",
        )
    else:
        pts, label, signal = (
            -2.0,
            "Extreme complacency",
            "Very low VIX — poor fear-based entry",
        )
    return {
        "points": pts,
        "label": label,
        "signal": signal,
        # Hard override for conclusion floor (Favorable+)
        "entry_zone": value > 30,
    }


def score_rsp_rsi(value: float) -> dict[str, Any]:
    """
    RSP RSI(14):
      ≤30     → +3 (rare deep oversold)
      30–33   → +2
      33–40   → +1
      40–60   →  0
      60–70   → −1
      ≥70     → −2
    """
    if value <= 30:
        pts, label, signal = (
            3.0,
            "Deep oversold (≤30)",
            "Rare deep oversold — strongest RSI mean-reversion score",
        )
    elif value <= 33:
        pts, label, signal = (
            2.0,
            "Oversold (30–33)",
            "Oversold equal-weight S&P — strong entry favor",
        )
    elif value <= 40:
        pts, label, signal = (
            1.0,
            "Soft oversold (33–40)",
            "Soft oversold — constructive for staged entries",
        )
    elif value < 60:
        pts, label, signal = (
            0.0,
            "Neutral momentum",
            "RSP RSI mid-range — no strong overbought/oversold edge",
        )
    elif value < 70:
        pts, label, signal = (
            -1.0,
            "Elevated momentum",
            "RSP RSI elevated — momentum strong; watch for stretch",
        )
    else:
        pts, label, signal = (
            -2.0,
            "Overbought",
            "RSP RSI ≥ 70 — overbought; weaker mean-reversion entry",
        )
    return {
        "points": pts,
        "label": label,
        "signal": signal,
        "oversold": value <= 33,
        "overbought": value >= 70,
    }


# Soft vs hard Caution split (full 5-gauge panel). Soft = mild stretch; hard = deeper.
SOFT_CAUTION_FLOOR = -2.5  # total in [SOFT_CAUTION_FLOOR, 0) → soft
# total in [AVOID_CUT, SOFT_CAUTION_FLOOR) → hard; total < AVOID_CUT → avoid
AVOID_CUT = -5.0


def suggested_equity_weight(
    verdict_class: str,
    *,
    above_200dma: bool | None = None,
) -> float:
    """
    Suggested equity allocation in [0, 1] for the live app / tiered backtest.

    Don't over-trade Neutral: stay fully invested (1.0) — mixed mid-range is not a
    sell signal. Soft Caution trims lightly; Hard Caution cuts more; Avoid = cash.
    Price regime (200DMA) only nudges Caution weights, never Neutral.
    """
    if verdict_class in ("strong-buy", "strong_buy"):
        return 1.0
    if verdict_class in ("buy", "favorable"):
        return 1.0
    if verdict_class == "neutral":
        return 1.0  # stay invested — do not cut just because score is flat
    if verdict_class in ("caution-soft", "soft_caution", "caution"):
        # mild trim; slightly deeper only if clearly below 200DMA
        if above_200dma is False:
            return 0.65
        return 0.75
    if verdict_class in ("caution-hard", "hard_caution"):
        if above_200dma is False:
            return 0.40
        return 0.50
    if verdict_class == "avoid":
        return 0.0
    return 1.0


def build_conclusion(
    fg: dict[str, Any],
    aaii: dict[str, Any],
    naaim: dict[str, Any],
    vix: dict[str, Any],
    rsp_rsi: dict[str, Any],
    spy_regime: dict[str, Any] | None = None,
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
            "suggested_allocation": 1.0,
            "price_regime": None,
        }

    total = sum(s["points"] for s in available)
    max_pts = 2 * len(available)
    # Hard override uses VIX > 30 (not only the +3 panic band at >40)
    vix_entry = bool(vix.get("ok") and vix.get("value", 0) > 30)

    # Verdict from total score (points can be half-integers with AAII/NAAIM ×0.5)
    #   Strong Buy Zone              total ≥ +6
    #   Favorable to Enter           total ≥ +2  AND confirmed (VIX pts≥2 OR RSI pts≥2)
    #   Neutral — Hold / Selective   total ≥  0  (or total≥2 but unconfirmed)
    #   Soft Caution                 total ≥ −2.5 and < 0
    #   Hard Caution                 total ≥ −5  and < −2.5
    #   Poor Entry Zone (avoid)      total < −5
    #
    # Confirmed Favorable (backtest 2021–2026-06): hit ~76% / mean ~+3.3% 21d
    # Avoid total < -5 (10y): 21d down ~42%, mean ~0 (slightly negative)
    vix_pts = float(scores["vix"]["points"]) if scores["vix"] else 0.0
    rsi_pts = float(scores["rsp_rsi"]["points"]) if scores["rsp_rsi"] else 0.0
    favorable_confirmed = (vix_pts >= 2.0) or (rsi_pts >= 2.0)

    if total >= 6:
        verdict, vclass = "Strong Buy Zone", "strong-buy"
        summary = (
            "Sentiment is broadly pessimistic or under-invested. Historically this is a "
            "better environment for adding US equity exposure (contrarian)."
        )
    elif total >= 2:
        if favorable_confirmed:
            verdict, vclass = "Favorable to Enter", "buy"
            summary = (
                "Score is constructive and confirmed by elevated VIX and/or soft/deep "
                "RSP RSI oversold. Conditions support staged entries into US equities."
            )
        else:
            # Soft +2…+6 without vol/RSI confirm → Neutral (not true Favorable)
            verdict, vclass = "Neutral — Hold / Selective", "neutral"
            summary = (
                f"Raw score is +{total:g} but Favorable requires confirmation "
                f"(VIX pts ≥ 2 or RSI pts ≥ 2). Currently VIX={vix_pts:g}, RSI={rsi_pts:g}. "
                "Stay invested — do not over-trade a mid-range unconfirmed score."
            )
    elif total >= 0:
        verdict, vclass = "Neutral — Hold / Selective", "neutral"
        summary = (
            "Signals are mixed or mid-range. Stay invested; this is not a sell signal. "
            "Prefer high-quality names and disciplined adds — avoid active de-risking "
            "just because the score is flat."
        )
    elif total >= SOFT_CAUTION_FLOOR:
        # Soft Caution: mild optimism/complacency stretch (−2.5 … 0)
        verdict, vclass = "Soft Caution — Trim Lightly", "caution-soft"
        summary = (
            "Mild stretch toward complacency/optimism (soft caution). Stay mostly "
            "invested; trim new risk lightly rather than a full de-risk. Prefer patience "
            "on aggressive adds until the score improves."
        )
    elif total >= AVOID_CUT:
        # Hard Caution: deeper stretch (−5 … −2.5)
        verdict, vclass = "Hard Caution — Reduce Risk", "caution-hard"
        summary = (
            "Sentiment is more stretched (hard caution). Reduce new risk and consider "
            "trimming; avoid chasing. Still not a full Avoid — wait for better levels "
            "or a clearer stress signal before adding."
        )
    else:
        # total < -5
        verdict, vclass = "Poor Entry Zone", "avoid"
        summary = (
            "Deep overhype / crowded risk-on (total < −5). Historically a weaker "
            "forward-return regime than milder caution — prioritize de-risking and no chase."
        )

    # Hard rule: VIX > 30 floors the verdict at Favorable (implies VIX pts ≥ 2 → confirmed)
    caution_classes = ("caution", "caution-soft", "caution-hard")
    if vix_entry:
        if vclass in caution_classes or vclass in ("avoid", "neutral"):
            verdict, vclass = "Favorable to Enter", "buy"
            summary = (
                f"VIX is {vix['value']} (> 30) — hard override to Favorable to Enter "
                "(vol confirmation met). "
            ) + summary
        elif vclass == "buy":
            summary = (
                f"VIX is {vix['value']} (> 30) — vol confirmation for Favorable. "
            ) + summary

    # Price regime (200DMA): tag Neutral / Caution only — not SB / Fav / Avoid
    regime_payload: dict[str, Any] | None = None
    above_200: bool | None = None
    if spy_regime and spy_regime.get("ok"):
        above_200 = bool(spy_regime.get("above_200dma"))
        regime_payload = {
            "symbol": spy_regime.get("symbol", "SPY"),
            "price": spy_regime.get("price"),
            "sma_200": spy_regime.get("sma_200"),
            "above_200dma": above_200,
            "pct_vs_200dma": spy_regime.get("pct_vs_200dma"),
            "label": spy_regime.get("label"),
            "as_of": spy_regime.get("as_of"),
            "applies": False,
            "note": None,
        }
        if vclass in ("neutral", "caution-soft", "caution-hard"):
            regime_payload["applies"] = True
            if above_200:
                tag = " · above 200DMA"
                regime_payload["note"] = (
                    f"SPY {spy_regime['price']} is above its 200-day SMA "
                    f"({spy_regime['sma_200']}) — trend still supportive; hold bias "
                    "within this sentiment band."
                )
                summary = summary + (
                    f" Price regime: SPY above 200DMA ({spy_regime['pct_vs_200dma']:+.1f}%)."
                )
            else:
                tag = " · below 200DMA"
                regime_payload["note"] = (
                    f"SPY {spy_regime['price']} is below its 200-day SMA "
                    f"({spy_regime['sma_200']}) — weaker trend; be more defensive "
                    "within this sentiment band."
                )
                summary = summary + (
                    f" Price regime: SPY below 200DMA ({spy_regime['pct_vs_200dma']:+.1f}%) "
                    "— prefer more defensive sizing."
                )
            # Append short tag to the human verdict for Neutral/Caution
            verdict = verdict + tag
        else:
            regime_payload["note"] = (
                "200DMA tag applies only to Neutral / Caution (not shown on this verdict)."
            )

    alloc = suggested_equity_weight(vclass, above_200dma=above_200)

    details = []
    if scores["vix"]:
        zone = (
            "HARD OVERRIDE (VIX > 30 → ≥ Favorable)"
            if vix_entry
            else "no VIX override (VIX ≤ 30)"
        )
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
    if regime_payload and regime_payload.get("applies") and regime_payload.get("note"):
        details.append(regime_payload["note"])
    details.append(
        f"Suggested equity weight: {alloc:.0%} "
        f"(Neutral stays fully invested; Soft Caution ~75%; Hard Caution ~50%; Avoid 0%)."
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
        "favorable_confirmed": favorable_confirmed,
        "suggested_allocation": alloc,
        "price_regime": regime_payload,
        "scores": {k: _score_payload(v) for k, v in scores.items()},
    }


DISCLAIMER = (
    "Educational / research tool only — not investment advice. Sentiment indicators are "
    "noisy and work best as contrarian context, not as a standalone timing system. "
    "Always consider valuation, macro, earnings, liquidity, and your own risk tolerance."
)


def collect_all(
    force: bool = False,
    force_aaii: bool = False,
    force_naaim: bool = False,
) -> dict[str, Any]:
    """
    force: re-fetch daily/live gauges (ignores 5‑min cache).
    force_aaii / force_naaim: re-fetch weekly gauges (normally skipped for 6 days).
    """
    now = time.time()
    weekly_force = force_aaii or force_naaim
    if (
        not force
        and not weekly_force
        and _CACHE["data"]
        and (now - _CACHE["ts"]) < CACHE_TTL_SEC
    ):
        return _CACHE["data"]

    session = _session()
    errors: list[str] = []

    try:
        fg = fetch_fear_greed(session)
    except Exception as exc:  # noqa: BLE001
        fg = {"ok": False, "error": str(exc), "source": "CNN Fear & Greed Index"}
        errors.append(f"Fear & Greed: {exc}")

    # Weekly surveys — disk-cached; normal Refresh does not re-hit these
    try:
        aaii = get_aaii(session, force=force_aaii)
    except Exception as exc:  # noqa: BLE001
        aaii = {
            "ok": False,
            "error": str(exc),
            "source": "AAII Investor Sentiment Survey",
            "cached": False,
            "stale": False,
        }
        errors.append(f"AAII: {exc}")

    try:
        naaim = get_naaim(session, force=force_naaim)
    except Exception as exc:  # noqa: BLE001
        naaim = {
            "ok": False,
            "error": str(exc),
            "source": "NAAIM Exposure Index",
            "cached": False,
            "stale": False,
        }
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

    try:
        spy_regime = fetch_spy_regime(session)
    except Exception as exc:  # noqa: BLE001
        spy_regime = {"ok": False, "error": str(exc), "source": "SPY vs 200-day SMA"}
        errors.append(f"SPY regime: {exc}")

    conclusion = build_conclusion(fg, aaii, naaim, vix, rsp_rsi, spy_regime=spy_regime)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fear_greed": fg,
        "aaii": aaii,
        "naaim": naaim,
        "vix": vix,
        "rsp_rsi": rsp_rsi,
        "spy_regime": spy_regime,
        "conclusion": conclusion,
        "errors": errors,
        "cache_policy": {
            "live_ttl_sec": CACHE_TTL_SEC,
            "weekly_ttl_sec": WEEKLY_CACHE_TTL_SEC,
            "aaii_from_cache": bool(aaii.get("cached")),
            "naaim_from_cache": bool(naaim.get("cached")),
        },
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
    from flask import request

    # refresh=1 → re-pull daily gauges only (VIX / F&G / RSP)
    # refresh_aaii=1 / refresh_naaim=1 → force that weekly series
    # refresh_weekly=1 → force both AAII + NAAIM
    force = request.args.get("refresh") == "1"
    force_weekly = request.args.get("refresh_weekly") == "1"
    force_aaii = force_weekly or request.args.get("refresh_aaii") == "1"
    force_naaim = force_weekly or request.args.get("refresh_naaim") == "1"
    data = collect_all(
        force=force or force_aaii or force_naaim,
        force_aaii=force_aaii,
        force_naaim=force_naaim,
    )
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
