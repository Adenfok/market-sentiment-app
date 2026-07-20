"""
Professional backtest of the market-sentiment scorecard vs S&P 500 (SPY).

Reconstructs daily scorecard points using the *same* rules as app.py:

  Market data (Yahoo Finance)
    • VIX          — score_vix()
    • RSP RSI(14)  — score_rsp_rsi()
    • Fear & Greed — score_fear_greed() when CNN history is available

  Weekly surveys (Excel drop-in)
    • AAII  — put file at data/historical/aaii_sentiment.xlsx
    • NAAIM — put file at data/historical/naaim_exposure.xlsx

    Or pass paths:
      python backtest.py --aaii "C:/path/AAII sentiment.xlsx" --naaim "C:/path/NAAIM.xlsx"

Strategies
  1) buy_hold
  2) scorecard_binary    — long when favorable/strong else cash
  3) scorecard_tiered    — allocation by verdict (app language)
  4) vix_override_binary — binary + VIX>30 via verdict

Usage:
  python backtest.py
  python backtest.py --years 10 --out data/backtest
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

# Reuse production scorecard rules (single source of truth)
from app import (
    BROWSER_HEADERS,
    compute_rsi,
    score_aaii,
    score_fear_greed,
    score_naaim,
    score_rsp_rsi,
    score_vix,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_AAII_XLSX = ROOT / "data" / "historical" / "aaii_sentiment.xlsx"
DEFAULT_NAAIM_XLSX = ROOT / "data" / "historical" / "naaim_exposure.xlsx"
DEFAULT_FNG_CSV = ROOT / "data" / "historical" / "fear-greed.csv"
# Canonical historical F&G (2011+) maintained by whit3rabbit
FNG_GITHUB_RAW = (
    "https://raw.githubusercontent.com/whit3rabbit/fear-greed-data/main/fear-greed.csv"
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
TRADING_DAYS = 252
RF_ANNUAL = 0.02  # flat risk-free for Sharpe (approx.)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def yahoo_daily(
    session: requests.Session,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Download daily OHLCV-ish series from Yahoo; returns list of {date, close}."""
    url = YAHOO_CHART.format(symbol=symbol)
    params = {
        "period1": int(start.timestamp()),
        "period2": int(end.timestamp()),
        "interval": "1d",
        "events": "history",
    }
    r = session.get(url, params=params, timeout=45)
    r.raise_for_status()
    payload = r.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError(f"Yahoo returned no data for {symbol}")

    ts = result[0].get("timestamp") or []
    closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    rows: list[dict[str, Any]] = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
        rows.append({"date": d, "close": float(c)})
    if len(rows) < 100:
        raise ValueError(f"Insufficient history for {symbol}: {len(rows)} bars")
    return rows


def load_fear_greed_csv(path: Path) -> list[dict[str, Any]]:
    """
    Load whit3rabbit/fear-greed-data style CSV:
      Date,Fear Greed,Rating
      2011-01-03,68.0,greed
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if i == 0 and parts[0].lower().startswith("date"):
            continue
        if len(parts) < 2:
            continue
        d = parts[0]
        # normalize date
        if "/" in d:
            # M/D/YYYY fallback
            try:
                mm, dd, yy = d.split("/")
                d = f"{int(yy):04d}-{int(mm):02d}-{int(dd):02d}"
            except Exception:
                continue
        try:
            val = float(parts[1])
        except ValueError:
            continue
        out.append({"date": d, "value": val})
    out.sort(key=lambda x: x["date"])
    return out


def download_fear_greed_github(
    session: requests.Session,
    dest: Path = DEFAULT_FNG_CSV,
) -> list[dict[str, Any]]:
    """Fetch canonical fear-greed.csv from GitHub and cache locally."""
    r = session.get(FNG_GITHUB_RAW, timeout=60)
    r.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(r.text, encoding="utf-8")
    return load_fear_greed_csv(dest)


def try_fear_greed_history(
    session: requests.Session,
    start: datetime,
    *,
    csv_path: Path | None = None,
    refresh_github: bool = False,
) -> list[dict[str, Any]]:
    """
    Fear & Greed history, preferred order:
      1) Local CSV (data/historical/fear-greed.csv or --fng path)
      2) Download whit3rabbit/fear-greed-data from GitHub
      3) Fallback: short CNN live endpoint
    """
    path = Path(csv_path) if csv_path else DEFAULT_FNG_CSV

    if refresh_github or not path.exists():
        try:
            rows = download_fear_greed_github(session, dest=path)
            if len(rows) >= 50:
                return rows
        except Exception as exc:
            print(f"  GitHub F&G download failed: {exc}")

    if path.exists():
        rows = load_fear_greed_csv(path)
        if len(rows) >= 50:
            return rows

    # Fallback — CNN window (often much shorter / blocked)
    headers = {
        **BROWSER_HEADERS,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://edition.cnn.com",
        "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    }
    urls = [
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
        f"https://production.dataviz.cnn.io/index/fearandgreed/graphdata/{start.date().isoformat()}",
    ]
    for url in urls:
        try:
            r = session.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                continue
            payload = r.json()
            hist = (payload.get("fear_and_greed_historical") or {}).get("data") or []
            out: list[dict[str, Any]] = []
            for pt in hist:
                x = pt.get("x")
                y = pt.get("y")
                if x is None or y is None:
                    continue
                d = datetime.fromtimestamp(float(x) / 1000.0, tz=timezone.utc).date().isoformat()
                out.append({"date": d, "value": float(y)})
            if len(out) >= 50:
                return out
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------------
# Excel loaders (AAII / NAAIM)
# ---------------------------------------------------------------------------

def _to_iso_date(val: Any) -> str | None:
    if val is None or (isinstance(val, float) and math.isnan(val)):
        return None
    if isinstance(val, datetime):
        return val.date().isoformat()
    try:
        import pandas as pd

        ts = pd.to_datetime(val, errors="coerce")
        if pd.isna(ts):
            return None
        return ts.date().isoformat()
    except Exception:
        return None


def load_aaii_excel(path: Path) -> list[dict[str, Any]]:
    """
    AAII official-style workbook (sheet SENTIMENT).
    Columns: Date, Bullish, Neutral, Bearish as fractions (0-1) or %.
    """
    import pandas as pd

    if not path.exists():
        return []
    df = pd.read_excel(path, sheet_name="SENTIMENT", header=None)
    # Header row contains 'Reported' / 'Date' / 'Bullish'
    header_idx = None
    for i in range(min(15, len(df))):
        row = [str(x).strip().lower() if pd.notna(x) else "" for x in df.iloc[i].tolist()]
        if "bullish" in row and "bearish" in row and any("date" in c for c in row):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"Could not find AAII header row in {path}")

    headers = [str(x).strip().lower() if pd.notna(x) else f"c{j}" for j, x in enumerate(df.iloc[header_idx])]
    body = df.iloc[header_idx + 1 :].copy()
    body.columns = headers[: len(body.columns)]

    # First column is date even if named 'reported'
    date_col = body.columns[0]
    # Find bullish/neutral/bearish columns
    def find_col(*names: str) -> str | None:
        for n in names:
            for c in body.columns:
                if c == n or c.startswith(n):
                    return c
        return None

    bull_c = find_col("bullish")
    neut_c = find_col("neutral")
    bear_c = find_col("bearish")
    if not bull_c or not bear_c:
        raise ValueError(f"AAII columns missing in {path}: {list(body.columns)}")

    out: list[dict[str, Any]] = []
    for _, r in body.iterrows():
        d = _to_iso_date(r.get(date_col))
        if not d:
            continue
        try:
            bull = float(r[bull_c])
            bear = float(r[bear_c])
            neut = float(r[neut_c]) if neut_c and pd.notna(r.get(neut_c)) else max(0.0, 1.0 - bull - bear)
        except (TypeError, ValueError):
            continue
        # Convert fractions → percent if needed
        if bull <= 1.5 and bear <= 1.5:
            bull, neut, bear = bull * 100.0, neut * 100.0, bear * 100.0
        if bull + bear < 5:  # junk row
            continue
        out.append(
            {
                "date": d,
                "bullish": round(bull, 2),
                "neutral": round(neut, 2),
                "bearish": round(bear, 2),
            }
        )
    out.sort(key=lambda x: x["date"])
    return out


def load_naaim_excel(path: Path) -> list[dict[str, Any]]:
    """
    NAAIM USE_Data workbook.
    Columns: Date, Mean/Average or NAAIM Number.
    """
    import pandas as pd

    if not path.exists():
        return []
    xl = pd.ExcelFile(path)
    sheet = xl.sheet_names[0]
    df = pd.read_excel(path, sheet_name=sheet, header=0)
    cols = {str(c).strip().lower(): c for c in df.columns}

    date_c = None
    for k, c in cols.items():
        if k == "date" or "date" in k:
            date_c = c
            break
    mean_c = None
    for key in ("mean/average", "naaim number", "mean", "average"):
        if key in cols:
            mean_c = cols[key]
            break
    if date_c is None or mean_c is None:
        # fallback positional
        date_c = df.columns[0]
        mean_c = df.columns[1]

    out: list[dict[str, Any]] = []
    for _, r in df.iterrows():
        d = _to_iso_date(r.get(date_c))
        if not d:
            continue
        try:
            val = float(r[mean_c])
        except (TypeError, ValueError):
            continue
        if math.isnan(val):
            continue
        out.append({"date": d, "value": round(val, 2)})
    out.sort(key=lambda x: x["date"])
    return out


# ---------------------------------------------------------------------------
# Indicator panel + scores
# ---------------------------------------------------------------------------

@dataclass
class DayRow:
    date: str
    spy: float
    vix: float | None
    rsp: float | None
    rsp_rsi: float | None
    fng: float | None
    aaii_bull: float | None = None
    aaii_neut: float | None = None
    aaii_bear: float | None = None
    naaim: float | None = None
    pts_vix: int | None = None
    pts_rsi: int | None = None
    pts_fng: int | None = None
    pts_aaii: int | None = None
    pts_naaim: int | None = None
    total: int = 0
    n_signals: int = 0
    verdict: str = ""
    vix_entry: bool = False


def build_panel(
    spy: list[dict[str, Any]],
    vix: list[dict[str, Any]],
    rsp: list[dict[str, Any]],
    fng: list[dict[str, Any]],
    aaii: list[dict[str, Any]] | None = None,
    naaim: list[dict[str, Any]] | None = None,
    rsi_period: int = 14,
) -> list[DayRow]:
    vix_map = {r["date"]: r["close"] for r in vix}
    rsp_map = {r["date"]: r["close"] for r in rsp}
    fng_map = {r["date"]: r["value"] for r in fng}
    aaii_map = {r["date"]: r for r in (aaii or [])}
    naaim_map = {r["date"]: r["value"] for r in (naaim or [])}

    # RSI needs ordered RSP closes
    rsp_dates = sorted(rsp_map.keys())
    rsp_closes = [rsp_map[d] for d in rsp_dates]
    rsi_by_date: dict[str, float] = {}
    for i in range(len(rsp_closes)):
        window = rsp_closes[: i + 1]
        if len(window) < rsi_period + 1:
            continue
        val = compute_rsi(window, period=rsi_period)
        if val is not None:
            rsi_by_date[rsp_dates[i]] = round(val, 2)

    # Forward-fill all gauges onto SPY calendar
    last_vix = None
    last_rsi = None
    last_rsp = None
    last_fng = None
    last_aaii: dict[str, Any] | None = None
    last_naaim = None
    rows: list[DayRow] = []

    for s in spy:
        d = s["date"]
        if d in vix_map:
            last_vix = vix_map[d]
        if d in rsp_map:
            last_rsp = rsp_map[d]
        if d in rsi_by_date:
            last_rsi = rsi_by_date[d]
        if d in fng_map:
            last_fng = fng_map[d]
        if d in aaii_map:
            last_aaii = aaii_map[d]
        if d in naaim_map:
            last_naaim = naaim_map[d]

        row = DayRow(
            date=d,
            spy=s["close"],
            vix=last_vix,
            rsp=last_rsp,
            rsp_rsi=last_rsi,
            fng=last_fng,
            aaii_bull=last_aaii["bullish"] if last_aaii else None,
            aaii_neut=last_aaii["neutral"] if last_aaii else None,
            aaii_bear=last_aaii["bearish"] if last_aaii else None,
            naaim=last_naaim,
        )

        total = 0
        n = 0
        if row.vix is not None:
            sv = score_vix(row.vix)
            row.pts_vix = float(sv["points"])
            row.vix_entry = bool(sv.get("entry_zone"))
            total += row.pts_vix
            n += 1
        if row.rsp_rsi is not None:
            sr = score_rsp_rsi(row.rsp_rsi)
            row.pts_rsi = float(sr["points"])
            total += row.pts_rsi
            n += 1
        if row.fng is not None:
            sf = score_fear_greed(row.fng)
            row.pts_fng = float(sf["points"])
            total += row.pts_fng
            n += 1
        if row.aaii_bull is not None and row.aaii_bear is not None:
            sa = score_aaii(row.aaii_bull, row.aaii_bear)
            row.pts_aaii = float(sa["points"])
            total += row.pts_aaii
            n += 1
        if row.naaim is not None:
            sn = score_naaim(row.naaim)
            row.pts_naaim = float(sn["points"])
            total += row.pts_naaim
            n += 1

        row.total = total
        row.n_signals = n
        row.verdict = verdict_from_total(
            total,
            n,
            vix_entry=row.vix_entry,
            pts_vix=row.pts_vix,
            pts_rsi=row.pts_rsi,
        )
        rows.append(row)

    # Drop warm-up until RSI exists
    return [r for r in rows if r.rsp_rsi is not None and r.vix is not None]


def verdict_from_total(
    total: float,
    n_signals: int,
    *,
    vix_entry: bool,
    pts_vix: float | None = None,
    pts_rsi: float | None = None,
) -> str:
    """
    Map points → verdict (aligned with app.build_conclusion).

    App (5 gauges): strong≥6, fav≥2 + confirm, neut≥0, caution≥-3, else avoid.
    Favorable requires total≥2 AND (VIX pts≥2 OR RSI pts≥2).
    VIX > 30 (entry_zone) floors at favorable (implies VIX pts≥2).
    Scale strong/fav thresholds if fewer gauges (backtest partial panels).
    """
    scale = max(n_signals, 1) / 5.0
    strong = max(2, int(round(6 * scale)))
    fav = max(1, int(round(2 * scale)))
    caution = min(-1, int(round(-3 * scale)))

    if total >= strong:
        v = "strong_buy"
    elif total >= fav:
        v = "favorable"
    elif total >= 0:
        v = "neutral"
    elif total >= caution:
        v = "caution"
    else:
        v = "avoid"

    # Hard rule: VIX > 30 elevates to at least favorable
    if vix_entry and v in ("neutral", "caution", "avoid"):
        v = "favorable"

    # Confirmed Favorable only: need VIX pts≥2 or RSI pts≥2
    if v == "favorable":
        pv = float(pts_vix or 0.0)
        pr = float(pts_rsi or 0.0)
        if not (pv >= 2.0 or pr >= 2.0):
            v = "neutral"
    return v


def allocation_for_verdict(verdict: str, strategy: str) -> float:
    """Target equity weight in [0, 1]."""
    if strategy == "buy_hold":
        return 1.0
    if strategy == "scorecard_binary":
        return 1.0 if verdict in ("strong_buy", "favorable") else 0.0
    if strategy == "scorecard_tiered":
        return {
            "strong_buy": 1.0,
            "favorable": 1.0,
            "neutral": 0.5,   # selective entry
            "caution": 0.25,
            "avoid": 0.0,
        }.get(verdict, 0.0)
    if strategy == "vix_override_binary":
        # same as binary; vix already folded into verdict
        return 1.0 if verdict in ("strong_buy", "favorable") else 0.0
    raise ValueError(strategy)


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

@dataclass
class TradeStats:
    name: str
    start: str
    end: str
    bars: int
    total_return: float
    cagr: float
    ann_vol: float
    sharpe: float
    max_drawdown: float
    calmar: float
    time_in_market: float
    final_equity: float
    excess_vs_bh: float
    yearly: dict[str, float] = field(default_factory=dict)


def _max_drawdown(equity: list[float]) -> float:
    peak = equity[0]
    max_dd = 0.0
    for x in equity:
        peak = max(peak, x)
        dd = (x / peak) - 1.0
        max_dd = min(max_dd, dd)
    return max_dd


def _cagr(equity0: float, equity1: float, years: float) -> float:
    if equity0 <= 0 or years <= 0:
        return 0.0
    return (equity1 / equity0) ** (1.0 / years) - 1.0


def run_strategy(rows: list[DayRow], strategy: str, initial: float = 10_000.0) -> tuple[TradeStats, list[dict[str, Any]]]:
    """
    Signal on day t close → position for day t→t+1 return (next-bar close-to-close).
    Cash earns 0 (conservative; no T-bill).
    """
    equity = initial
    curve: list[dict[str, Any]] = []
    rets: list[float] = []
    invested_flags: list[float] = []

    for i in range(len(rows) - 1):
        today = rows[i]
        nxt = rows[i + 1]
        # Signal known at close t → earn return from close t to close t+1
        weight = allocation_for_verdict(today.verdict, strategy)
        r = (nxt.spy / today.spy) - 1.0
        port_r = weight * r
        equity *= 1.0 + port_r
        rets.append(port_r)
        invested_flags.append(weight)
        curve.append(
            {
                "date": nxt.date,
                "spy": nxt.spy,
                "weight": weight,
                "score": today.total,
                "verdict": today.verdict,
                "equity": round(equity, 4),
                "ret": port_r,
            }
        )

    years = (len(rows) - 1) / TRADING_DAYS
    tot = equity / initial - 1.0
    cagr = _cagr(initial, equity, years)
    if len(rets) > 1:
        mu = statistics.mean(rets)
        sd = statistics.pstdev(rets)
        ann_vol = sd * math.sqrt(TRADING_DAYS)
        sharpe = (
            ((mu * TRADING_DAYS) - RF_ANNUAL) / ann_vol if ann_vol > 0 else 0.0
        )
    else:
        ann_vol = 0.0
        sharpe = 0.0

    eq_series = [initial] + [c["equity"] for c in curve]
    mdd = _max_drawdown(eq_series)
    calmar = (cagr / abs(mdd)) if mdd < 0 else 0.0
    tim = statistics.mean(invested_flags) if invested_flags else 0.0

    # Yearly calendar returns from curve
    yearly: dict[str, float] = {}
    by_year: dict[str, list[float]] = {}
    for c in curve:
        y = c["date"][:4]
        by_year.setdefault(y, []).append(1.0 + c["ret"])
    for y, factors in by_year.items():
        acc = 1.0
        for f in factors:
            acc *= f
        yearly[y] = acc - 1.0

    stats = TradeStats(
        name=strategy,
        start=rows[0].date,
        end=rows[-1].date,
        bars=len(rows) - 1,
        total_return=tot,
        cagr=cagr,
        ann_vol=ann_vol,
        sharpe=sharpe,
        max_drawdown=mdd,
        calmar=calmar,
        time_in_market=tim,
        final_equity=equity,
        excess_vs_bh=0.0,  # filled later
        yearly=yearly,
    )
    return stats, curve


# ---------------------------------------------------------------------------
# Effectiveness analysis
# ---------------------------------------------------------------------------

def forward_return_by_bucket(rows: list[DayRow], horizon: int = 21) -> list[dict[str, Any]]:
    """Mean SPY forward return by score / verdict bucket (effectiveness diagnostic)."""
    buckets: dict[str, list[float]] = {}
    for i in range(len(rows) - horizon):
        r0 = rows[i]
        r1 = rows[i + horizon]
        fwd = (r1.spy / r0.spy) - 1.0
        key = r0.verdict
        buckets.setdefault(key, []).append(fwd)
        # also by score sign
        sign = "pos" if r0.total > 0 else ("neg" if r0.total < 0 else "zero")
        buckets.setdefault(f"score_{sign}", []).append(fwd)

    out = []
    for k, vals in sorted(buckets.items()):
        out.append(
            {
                "bucket": k,
                "n": len(vals),
                "mean_fwd": statistics.mean(vals),
                "median_fwd": statistics.median(vals),
                "hit_rate": sum(1 for v in vals if v > 0) / len(vals),
            }
        )
    return out


def effectiveness_scorecard(core: TradeStats, bh: TradeStats, buckets: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Qualitative + quantitative call on whether the live scorecard (core gauges)
    adds value vs buy-and-hold over the sample.
    """
    beats_bh = core.cagr > bh.cagr and core.max_drawdown >= bh.max_drawdown * 0.9
    better_risk = core.max_drawdown > bh.max_drawdown  # less negative is better
    better_sharpe = core.sharpe > bh.sharpe

    # Do "favorable/strong" buckets show higher fwd returns than "avoid/caution"?
    by_b = {b["bucket"]: b for b in buckets}
    good = []
    bad = []
    for g in ("strong_buy", "favorable"):
        if g in by_b:
            good.append(by_b[g]["mean_fwd"])
    for g in ("caution", "avoid"):
        if g in by_b:
            bad.append(by_b[g]["mean_fwd"])

    separation = None
    if good and bad:
        separation = statistics.mean(good) - statistics.mean(bad)

    if better_sharpe and (core.total_return >= bh.total_return * 0.85) and (separation or 0) > 0:
        rating = "SUPPORTIVE"
        summary = (
            "Core scorecard (VIX + RSP RSI) improves risk-adjusted results and shows "
            "positive separation between favorable vs caution buckets. Useful as a "
            "timing overlay, not a standalone holy grail."
        )
    elif (separation or 0) > 0.005 and not better_sharpe:
        # Directional edge exists (good buckets outperform bad) but cash drag
        # from sitting out bull markets hurts total/Sharpe vs buy-and-hold.
        rating = "DIRECTIONAL EDGE — WEAK AS STANDALONE ALLOCATOR"
        summary = (
            "Verdict buckets rank correctly: favorable/strong_buy periods have higher "
            "average forward SPY returns than caution/avoid. However, a simple long/cash "
            "rule underperforms buy-and-hold because it spends long stretches in cash "
            "during multi-year bull markets. Best use: size/add on high scores, not "
            "as a full exit system."
        )
    elif better_risk and core.sharpe >= bh.sharpe * 0.9:
        rating = "MIXED — RISK CONTROL"
        summary = (
            "Scorecard mainly helps by reducing drawdowns / time-in-market rather than "
            "beating buy-and-hold total return. Effective as a risk dial."
        )
    else:
        rating = "NOT EFFECTIVE (sample)"
        summary = (
            "Over this sample the reconstructed core scorecard did not improve "
            "risk-adjusted performance vs buy-and-hold. Treat live signals with caution "
            "and do not rely on them alone."
        )

    return {
        "rating": rating,
        "summary": summary,
        "beats_buy_hold_cagr": core.cagr > bh.cagr,
        "better_sharpe": better_sharpe,
        "shallower_drawdown": core.max_drawdown > bh.max_drawdown,
        "bucket_separation_21d": separation,
        "notes": [
            "Score rules match app.py (VIX, RSP RSI, F&G, AAII, NAAIM when data present).",
            "Weekly AAII/NAAIM are forward-filled onto daily SPY bars.",
            "Fear & Greed included only when CNN history downloads successfully.",
            "No transaction costs, taxes, or slippage modeled (optimistic).",
            "Cash earns 0% (conservative vs T-bills).",
            "Past performance is not indicative of future results.",
        ],
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def fmt_stats(s: TradeStats) -> str:
    lines = [
        f"  Strategy        : {s.name}",
        f"  Period          : {s.start} → {s.end}  ({s.bars} daily steps)",
        f"  Total return    : {pct(s.total_return)}",
        f"  CAGR            : {pct(s.cagr)}",
        f"  Ann. volatility : {pct(s.ann_vol)}",
        f"  Sharpe (rf≈2%)  : {s.sharpe:.2f}",
        f"  Max drawdown    : {pct(s.max_drawdown)}",
        f"  Calmar          : {s.calmar:.2f}",
        f"  Time in market  : {s.time_in_market * 100:.1f}%",
        f"  Final equity    : ${s.final_equity:,.2f}  (start $10,000)",
        f"  Excess vs B&H   : {pct(s.excess_vs_bh)}  (total-return gap)",
    ]
    return "\n".join(lines)


def write_outputs(
    out_dir: Path,
    stats_list: list[TradeStats],
    curves: dict[str, list[dict[str, Any]]],
    buckets: list[dict[str, Any]],
    effectiveness: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    # Summary JSON
    summary = {
        "meta": meta,
        "effectiveness": effectiveness,
        "strategies": [asdict(s) for s in stats_list],
        "forward_buckets_21d": buckets,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # Equity curves CSV
    # Align on date from buy_hold curve if present
    base = curves.get("buy_hold") or next(iter(curves.values()))
    dates = [c["date"] for c in base]
    fieldnames = ["date"] + [f"eq_{name}" for name in curves]
    with (out_dir / "equity_curves.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        by_name = {n: {c["date"]: c["equity"] for c in cur} for n, cur in curves.items()}
        for d in dates:
            row = {"date": d}
            for n in curves:
                row[f"eq_{n}"] = by_name[n].get(d, "")
            w.writerow(row)

    # Score panel sample
    with (out_dir / "forward_buckets_21d.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["bucket", "n", "mean_fwd", "median_fwd", "hit_rate"])
        w.writeheader()
        for b in buckets:
            w.writerow(b)

    # Human report
    lines = [
        "=" * 72,
        "MARKET SENTIMENT SCORECARD — BACKTEST REPORT",
        "=" * 72,
        f"Generated (UTC): {datetime.now(timezone.utc).isoformat()}",
        f"Universe: SPY (S&P 500 proxy) · Core signals: VIX + RSP RSI(14)",
        "",
        "EFFECTIVENESS VERDICT",
        "-" * 72,
        f"Rating : {effectiveness['rating']}",
        effectiveness["summary"],
        "",
        "Notes:",
    ]
    for n in effectiveness["notes"]:
        lines.append(f"  • {n}")
    lines.append("")
    lines.append("STRATEGY RESULTS")
    lines.append("-" * 72)
    for s in stats_list:
        lines.append(fmt_stats(s))
        lines.append("")
    lines.append("21-DAY FORWARD RETURN BY VERDICT / SCORE SIGN")
    lines.append("-" * 72)
    for b in buckets:
        lines.append(
            f"  {b['bucket']:<16} n={b['n']:<5} mean={pct(b['mean_fwd']):>9}  "
            f"median={pct(b['median_fwd']):>9}  hit={b['hit_rate']*100:.1f}%"
        )
    lines.append("")
    lines.append("YEARLY RETURNS")
    lines.append("-" * 72)
    years = sorted({y for s in stats_list for y in s.yearly})
    header = f"  {'Year':<6}" + "".join(f"{s.name[:16]:>18}" for s in stats_list)
    lines.append(header)
    for y in years:
        row = f"  {y:<6}"
        for s in stats_list:
            row += f"{pct(s.yearly.get(y, float('nan'))):>18}" if y in s.yearly else f"{'n/a':>18}"
        lines.append(row)
    lines.append("")
    lines.append(f"Artifacts written to: {out_dir.resolve()}")
    lines.append("=" * 72)
    report = "\n".join(lines)
    (out_dir / "report.txt").write_text(report, encoding="utf-8")
    print(report)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Backtest sentiment scorecard vs SPY")
    p.add_argument("--years", type=float, default=10.0, help="Lookback years (default 10)")
    p.add_argument("--out", type=str, default="data/backtest", help="Output directory")
    p.add_argument("--initial", type=float, default=10_000.0, help="Starting capital")
    p.add_argument(
        "--aaii",
        type=str,
        default=str(DEFAULT_AAII_XLSX),
        help="Path to AAII sentiment Excel (default: data/historical/aaii_sentiment.xlsx)",
    )
    p.add_argument(
        "--naaim",
        type=str,
        default=str(DEFAULT_NAAIM_XLSX),
        help="Path to NAAIM Excel (default: data/historical/naaim_exposure.xlsx)",
    )
    p.add_argument(
        "--fng",
        type=str,
        default=str(DEFAULT_FNG_CSV),
        help="Path to Fear & Greed CSV (default: data/historical/fear-greed.csv)",
    )
    p.add_argument(
        "--refresh-fng",
        action="store_true",
        help="Re-download Fear & Greed CSV from GitHub (whit3rabbit/fear-greed-data)",
    )
    args = p.parse_args(argv)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(args.years * 365.25) + 60)

    print(f"Downloading {args.years:.0f}y market data (SPY, VIX, RSP)…")
    sess = _session()
    spy = yahoo_daily(sess, "SPY", start, end)
    vix = yahoo_daily(sess, "%5EVIX", start, end)
    rsp = yahoo_daily(sess, "RSP", start, end)
    print(f"  SPY bars={len(spy)}  VIX={len(vix)}  RSP={len(rsp)}")

    print("Loading Fear & Greed history (GitHub / local CSV)…")
    fng_path = Path(args.fng)
    fng = try_fear_greed_history(
        sess,
        start,
        csv_path=fng_path,
        refresh_github=args.refresh_fng,
    )
    if fng:
        print(
            f"  F&G points={len(fng)}  ({fng[0]['date']} → {fng[-1]['date']})  "
            f"source={fng_path if fng_path.exists() else 'github/cnn'}"
        )
    else:
        print("  F&G unavailable")

    aaii_path = Path(args.aaii)
    naaim_path = Path(args.naaim)
    print(f"Loading AAII Excel: {aaii_path}")
    aaii = load_aaii_excel(aaii_path) if aaii_path.exists() else []
    print(f"  AAII rows={len(aaii)}" + (f"  ({aaii[0]['date']} → {aaii[-1]['date']})" if aaii else "  MISSING — place file or pass --aaii"))
    print(f"Loading NAAIM Excel: {naaim_path}")
    naaim = load_naaim_excel(naaim_path) if naaim_path.exists() else []
    print(f"  NAAIM rows={len(naaim)}" + (f"  ({naaim[0]['date']} → {naaim[-1]['date']})" if naaim else "  MISSING — place file or pass --naaim"))

    rows = build_panel(spy, vix, rsp, fng, aaii=aaii, naaim=naaim)
    if len(rows) < TRADING_DAYS * 2:
        print("ERROR: not enough aligned history to backtest.", file=sys.stderr)
        return 1

    # Trim to ~requested years from the end
    cutoff = (end - timedelta(days=int(args.years * 365.25))).date().isoformat()
    rows = [r for r in rows if r.date >= cutoff]
    print(f"Aligned sample: {rows[0].date} → {rows[-1].date}  ({len(rows)} days)")
    gauges = ["VIX", "RSP RSI(14)"]
    if any(r.fng is not None for r in rows):
        gauges.append("Fear&Greed")
    if any(r.aaii_bull is not None for r in rows):
        gauges.append("AAII")
    if any(r.naaim is not None for r in rows):
        gauges.append("NAAIM")
    print("Signals used:", " + ".join(gauges))

    strategies = [
        "buy_hold",
        "scorecard_binary",
        "scorecard_tiered",
        "vix_override_binary",
    ]
    stats_list: list[TradeStats] = []
    curves: dict[str, list[dict[str, Any]]] = {}

    for name in strategies:
        st, curve = run_strategy(rows, name, initial=args.initial)
        stats_list.append(st)
        curves[name] = curve

    bh = next(s for s in stats_list if s.name == "buy_hold")
    for s in stats_list:
        s.excess_vs_bh = s.total_return - bh.total_return

    buckets = forward_return_by_bucket(rows, horizon=21)
    # Prefer tiered as "the" scorecard implementation for effectiveness call
    core = next(s for s in stats_list if s.name == "scorecard_tiered")
    effectiveness = effectiveness_scorecard(core, bh, buckets)

    meta = {
        "years_requested": args.years,
        "sample_start": rows[0].date,
        "sample_end": rows[-1].date,
        "bars": len(rows),
        "gauges": gauges,
        "aaii_file": str(aaii_path) if aaii else None,
        "naaim_file": str(naaim_path) if naaim else None,
        "fng_file": str(fng_path) if fng else None,
        "fng_source": "whit3rabbit/fear-greed-data (GitHub CSV)",
        "aaii_rows": len(aaii),
        "naaim_rows": len(naaim),
        "fng_rows": len(fng),
        "score_rules": "Identical to app.score_* functions",
        "execution": "Signal at close t, return from close t to close t+1",
        "costs": "None modeled",
    }

    out_dir = Path(args.out)
    write_outputs(out_dir, stats_list, curves, buckets, effectiveness, meta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
