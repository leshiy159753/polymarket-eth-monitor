# Polymarket ETH Up/Down 15m Monitor — Railway Edition

A lightweight Python worker that continuously polls the Polymarket
**Ethereum Up or Down - 15 Minutes** prediction market and logs structured
price/probability state to stdout every `POLL_INTERVAL` seconds.

Designed for headless, long-running deployment on [Railway](https://railway.app)
or any container platform. All output uses Python's `logging` module —
no ANSI colour codes, no interactive terminal.

---

## What It Logs

Every poll cycle the worker emits structured log lines such as:

```
2026-03-11T14:05:00Z  INFO      market=eth-updown-15m-1773238500  status=ACTIVE  closes_in=07:32  volume=$48231  liquidity=$3120
2026-03-11T14:05:00Z  INFO        outcome=UP    gamma=54.2%  mid=54.0%  bid=53.5%  ask=54.5%
2026-03-11T14:05:00Z  INFO        outcome=DOWN  gamma=45.8%  mid=46.0%  bid=45.5%  ask=46.5%
2026-03-11T14:05:00Z  INFO        book[UP]   asks=54.5%x120  55.0%x80   bids=53.5%x200  53.0%x150
2026-03-11T14:05:00Z  INFO        book[DOWN] asks=46.5%x90   47.0%x60   bids=45.5%x180  45.0%x100
```

---

## Deploy to Railway

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/<you>/polymarket-eth-monitor.git
git push -u origin main
```

### 2. Create a Railway project

1. Go to [railway.app](https://railway.app) and click **New Project**.
2. Choose **Deploy from GitHub repo** and select your repository.
3. Railway will auto-detect `railway.json` and use Nixpacks to build.

### 3. Set environment variables

In the Railway dashboard open your service → **Variables** tab and add:

| Variable        | Value  | Notes                          |
|-----------------|--------|--------------------------------|
| `POLL_INTERVAL` | `5`    | Seconds between REST polls     |
| `LOG_LEVEL`     | `INFO` | `DEBUG` / `INFO` / `WARNING`   |

### 4. Deploy

Railway deploys automatically on every push to `main`.
Check the **Logs** tab to see live output.

---

## Run Locally

### Install dependencies

```bash
pip install -r requirements.txt
```

### Continuous monitor (default)

```bash
python polymarket_eth_monitor.py
```

Override env vars inline:

```bash
POLL_INTERVAL=10 LOG_LEVEL=DEBUG python polymarket_eth_monitor.py
```

### One-shot snapshot

Fetch a single snapshot of a specific event slug and exit:

```bash
python polymarket_eth_monitor.py --once eth-updown-15m-1773238500
```

Omit the slug to snapshot the current 15-minute slot:

```bash
python polymarket_eth_monitor.py --once
```

---

## Environment Variables

| Variable        | Default | Description                                                  |
|-----------------|---------|--------------------------------------------------------------|
| `POLL_INTERVAL` | `5`     | Seconds between each REST poll cycle                         |
| `LOG_LEVEL`     | `INFO`  | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR`    |

---

## Project Structure

```
polymarket-railway/
  polymarket_eth_monitor.py   # Main worker script
  requirements.txt            # Python dependencies
  Procfile                    # Railway / Heroku process type
  railway.json                # Railway build + deploy config
  .env.example                # Template for local env vars
  README.md                   # This file
```

---

## Notes

- **WebSocket feed** is optional. If `websocket-client` is installed the worker
  also subscribes to the CLOB real-time feed for live price updates between
  REST polls. The REST polling path works fine without it.
- **Graceful shutdown**: the worker handles `SIGTERM` (sent by Railway during
  deploys/restarts) and exits cleanly after the current poll completes.
- **Auto-restart**: `railway.json` sets `restartPolicyType: ON_FAILURE` with
  up to 10 retries, so transient network errors won't kill the deployment.
