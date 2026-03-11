# Polymarket ETH Up/Down 15m Monitor — Railway Edition

A Python worker that monitors every 15-minute **Ethereum Up or Down** market on Polymarket.

**What it does:**
1. Finds the current active 15m ETH market automatically
2. Polls CLOB prices every `POLL_INTERVAL` seconds
3. If any outcome price drops to **≤ ALERT_THRESHOLD** (default 1%) — sends a Telegram alert (once per outcome per market)
4. When the market closes — fetches the final winner and sends a result summary to Telegram
5. Moves on to the next market automatically

---

## Telegram Messages

**Low price alert** (when UP or DOWN hits ≤ 1%):
```
⚠️ Polymarket LOW PRICE ALERT
Market: eth-updown-15m-1773238500
Outcome: DOWN
Price: 0.80% (≤ 1%)
Question: Will ETH be higher in 15 minutes?
Volume: $48231
```

**Market result** (on close):
```
📊 Polymarket Market Result
Market: eth-updown-15m-1773238500
Question: Will ETH be higher in 15 minutes?
Closed at: 2026-03-11 14:15 UTC
🟢 Winner: UP
Final prices: UP=100.0%  DOWN=0.0%
Volume: $48231
Outcomes that hit ≤1% during market: DOWN
```

---

## Deploy to Railway

### 1. Create Telegram Bot
1. Message [@BotFather](https://t.me/BotFather) → `/newbot`
2. Copy the token
3. Get your chat_id: message [@userinfobot](https://t.me/userinfobot)

### 2. Push to GitHub
```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/<you>/polymarket-eth-monitor.git
git push -u origin main
```

### 3. Create Railway project
1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
2. Select your repository — Railway auto-detects `railway.json`

### 4. Set environment variables

In Railway dashboard → your service → **Variables** tab:

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Token from @BotFather |
| `TELEGRAM_CHAT_ID` | ✅ | — | Your Telegram chat ID |
| `POLL_INTERVAL` | — | `10` | Seconds between polls |
| `ALERT_THRESHOLD` | — | `0.01` | Price threshold (0.01 = 1%) |
| `LOG_LEVEL` | — | `INFO` | DEBUG / INFO / WARNING |

---

## Run Locally

```bash
pip install -r requirements.txt
cp .env.example .env  # fill in your values
export $(cat .env | xargs)
python polymarket_eth_monitor.py
```

---

## Project Structure

```
polymarket-railway/
  polymarket_eth_monitor.py   # Main worker
  requirements.txt            # requests>=2.31.0
  Procfile                    # worker: python polymarket_eth_monitor.py
  railway.json                # Nixpacks build + restart policy
  .env.example                # Env vars template
  README.md                   # This file
```
