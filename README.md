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
| **NAAIM Exposure** | [NAAIM](https://naaim.org/programs/naaim-exposure-index/) | Weekly | Half weight · Strong hold / take-profit bands |

**Hard override:** when **VIX > 30**, verdict is at least **Favorable to Enter** (even if other gauges are mixed).

**Verdict from total points:**

| Total score | Verdict |
|-------------|---------|
| **≥ +6** | Strong Buy Zone |
| **≥ +2** and **&lt; +6**, **and** (VIX pts ≥ 2 **or** RSI pts ≥ 2) | Favorable to Enter |
| **≥ +2** but **not** VIX/RSI confirmed | Neutral — Selective Entry |
| **≥ 0** and **&lt; +2** | Neutral — Selective Entry |
| **≥ −3** and **&lt; 0** | Caution — Wait for Better Levels |
| **&lt; −3** | Poor Entry Zone (avoid) |

**Confirmed Favorable** (VIX≥2 or RSI≥2) is required so Favorable is not driven only by surveys/F&G.

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
