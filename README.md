# US Equity Sentiment Dashboard

Web app that pulls classic market-sentiment gauges and answers:

> **Is it a good time to enter the US equity market?**

## Indicators

| Indicator | Source | Cadence | Contrarian read |
|-----------|--------|---------|-----------------|
| **VIX (S&P 500)** | CBOE via Yahoo Finance | Live / daily | **&gt;40 = +3** · 20–23 = +1 · 23–30 = 0 |
| **RSP RSI(14)** | Invesco S&P 500 Equal Weight (Yahoo) | Daily | **≤30 = +3** · 30–33 = +2 · 33–40 = +1 |
| **Fear & Greed Index** | [CNN](https://edition.cnn.com/markets/fear-and-greed) | Daily | ≤10 = +3 · 10–25 = +2 · fear −1 · greed +1 · ≥75 = −2 overhype |
| **AAII Survey** | [AAII](https://www.aaii.com/sentimentsurvey) | Weekly | Half weight (raw scores × 0.5) |
| **NAAIM Exposure** | Official current print is **subscription-only** (Aug 1, 2026+). Free mirrors may freeze on last public reading — **excluded from score if &gt;14 days old**. Optional: set `NAAIM_MANUAL_VALUE` | Weekly | Half weight when fresh/manual · Strong hold / take-profit bands |

**Hard override:** when **VIX > 30**, verdict is at least **Favorable to Enter** (even if other gauges are mixed).

**Verdict from total points:**

| Total score | Verdict | Suggested equity |
|-------------|---------|------------------|
| **≥ +6** | Strong Buy Zone | 100% |
| **≥ +2** and **&lt; +6**, **and** (VIX pts ≥ 2 **or** RSI pts ≥ 2) | Favorable to Enter | 100% |
| **≥ +2** but **not** VIX/RSI confirmed | Neutral — Hold / Selective | **100%** (don’t over-trade) |
| **≥ 0** and **&lt; +2** | Neutral — Hold / Selective | **100%** |
| **≥ −2.5** and **&lt; 0** | Soft Caution — Trim Lightly | ~75% (~65% if SPY below 200DMA) |
| **≥ −5** and **&lt; −2.5** | Hard Caution — Reduce Risk | ~50% (~40% if SPY below 200DMA) |
| **&lt; −5** | Poor Entry Zone (avoid) | 0% |

**Confirmed Favorable** (VIX≥2 or RSI≥2) is required so Favorable is not driven only by surveys/F&G.

**Price regime (200DMA):** SPY vs 200-day SMA is shown as a tag on **Neutral / Soft·Hard Caution only** (not on Strong Buy, Favorable, or Avoid). It nudges Caution weights slightly when below the average; it never forces Neutral to cash.

## Run locally

```powershell
cd $HOME\market-sentiment-app
python -m pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5050**

- API: `/api/sentiment`
- Force refresh (daily gauges only): `/api/sentiment?refresh=1`
- Force AAII weekly re-pull: `/api/sentiment?refresh_aaii=1`
- Force NAAIM weekly re-pull: `/api/sentiment?refresh_naaim=1`
- Force both weekly: `/api/sentiment?refresh_weekly=1`
- Health: `/health`

## Backtest (scorecard effectiveness)

10-year SPY backtest using the **same scoring rules** as the live app.

### Historical inputs (recommended)

Copy files here (default names):

```
market-sentiment-app/data/historical/aaii_sentiment.xlsx
market-sentiment-app/data/historical/naaim_exposure.xlsx
market-sentiment-app/data/historical/fear-greed.csv
```

**Fear & Greed history** (2011→present) is auto-downloaded from  
[whit3rabbit/fear-greed-data](https://github.com/whit3rabbit/fear-greed-data) if the CSV is missing:

```powershell
python backtest.py --years 10 --refresh-fng
```

Or pass any path:

```powershell
python backtest.py --years 10 `
  --aaii "C:\Users\you\Downloads\AAII sentiment.xlsx" `
  --naaim "C:\Users\you\Downloads\NAAIM_Data.xlsx" `
  --fng "C:\Users\you\Downloads\fear-greed.csv"
```

**Expected formats**
- **AAII:** sheet `SENTIMENT` with Date / Bullish / Neutral / Bearish
- **NAAIM:** first sheet with Date + Mean/Average or NAAIM Number
- **F&G:** `Date,Fear Greed,Rating` (GitHub canonical CSV)

```powershell
cd $HOME\market-sentiment-app
python backtest.py --years 10 --out data/backtest
```

Outputs in `data/backtest/`:
- `report.txt` — full performance + effectiveness rating
- `summary.json` — machine-readable metrics
- `equity_curves.csv` — strategy equities
- `forward_buckets_21d.csv` — 21-day forward returns by verdict

**Weekly caches (AAII + NAAIM):** stored under `data/*_cache.json` for ~6 days. Normal **Refresh** re-pulls only **VIX / RSP / Fear & Greed**.

### NAAIM after the Aug 2026 paywall

NAAIM’s *current* weekly Exposure Index is no longer free on [naaim.org](https://naaim.org/programs/naaim-exposure-index/). Free republishers often stop updating. The app:

1. Still tries MacroMicro / YCharts / CEIC for a last-known print (display only if stale).
2. **Excludes NAAIM from the total score** when `as_of` is older than **14 days**.
3. Lets subscribers inject a current value:

```powershell
$env:NAAIM_MANUAL_VALUE = "82.5"
$env:NAAIM_MANUAL_AS_OF = "2026-08-13"   # optional
$env:NAAIM_MANUAL_PRIOR = "79.7"         # optional
python app.py
```

On Render: set the same variables under **Environment**.

## Deploy on Render (always online)

1. Push this folder to a **GitHub** repository (public is fine).
2. Go to [https://dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint**  
   (or **Web Service** and point at the repo).
3. If using Blueprint, Render reads `render.yaml` automatically.
4. If using Web Service manually:
   - **Runtime:** Python  
   - **Build command:** `pip install -r requirements.txt`  
   - **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120`  
   - **Health check path:** `/health`
5. Deploy → open the `https://….onrender.com` URL and share it.

**Free tier note:** the service may sleep after ~15 minutes of idle traffic; the first visit can take ~30–60 seconds to wake. Your PC can sleep — the site still works.

## Notes

- Data is cached for 5 minutes server-side.
- Sentiment is context only — not a complete investment process.
- Not financial advice.
