# US Equity Sentiment Dashboard

Web app that pulls classic market-sentiment gauges and answers:

> **Is it a good time to enter the US equity market?**

## Indicators

| Indicator | Source | Cadence | Contrarian read |
|-----------|--------|---------|-----------------|
| **VIX (S&P 500)** | CBOE via Yahoo Finance | Live / daily | **VIX > 30 = entry zone** |
| **Fear & Greed Index** | [CNN](https://edition.cnn.com/markets/fear-and-greed) | Daily | Low (fear) → better entry |
| **AAII Survey** | [AAII](https://www.aaii.com/sentimentsurvey) | Weekly | High bearish % → better entry |
| **NAAIM Exposure** | [NAAIM](https://naaim.org/programs/naaim-exposure-index/) | Weekly | Low manager exposure → better entry |

Each indicator is scored **−2 … +2**. When **VIX > 30**, the model forces at least a **Favorable to Enter** verdict.

## Run locally

```powershell
cd $HOME\market-sentiment-app
python -m pip install -r requirements.txt
python app.py
```

Open **http://127.0.0.1:5050**

- API: `/api/sentiment`
- Force refresh: `/api/sentiment?refresh=1`
- Health: `/health`

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
