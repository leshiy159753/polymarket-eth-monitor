#!/usr/bin/env python3
"""
Polymarket ETH Up/Down 15m Monitor — Railway Edition
======================================================
Логика:
  1. Каждые POLL_INTERVAL секунд находит текущий активный 15-минутный рынок ETH.
  2. Следит за ценами токенов UP и DOWN из CLOB API.
  3. Если цена любого токена опускается до <= ALERT_THRESHOLD (default 0.01, т.е. 1%):
       - фиксирует событие в памяти (однократно за рынок)
  4. Когда рынок закрывается (closed=True или active=False):
       - запрашивает финальный исход (winning outcome)
       - отправляет итог в Telegram бот
  5. Переходит к следующему рынку.

Env vars (Railway Variables):
  TELEGRAM_BOT_TOKEN   — токен бота @BotFather
  TELEGRAM_CHAT_ID     — chat_id куда слать сообщения
  POLL_INTERVAL        — секунды между опросами (default: 10)
  ALERT_THRESHOLD      — порог цены токена (default: 0.01)
  LOG_LEVEL            — уровень логов (default: INFO)
"""

import json
import logging
import os
import signal
import sys
import time
import threading
import requests
from datetime import datetime, timezone
from typing import Optional

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
POLL_INTERVAL: int    = int(os.getenv("POLL_INTERVAL", "10"))
LOG_LEVEL: str        = os.getenv("LOG_LEVEL", "INFO").upper()
ALERT_THRESHOLD: float = float(os.getenv("ALERT_THRESHOLD", "0.01"))
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str   = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
log = logging.getLogger("polymarket")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GAMMA_API         = "https://gamma-api.polymarket.com"
CLOB_API          = "https://clob.polymarket.com"
EVENT_SLUG_PREFIX = "eth-updown-15m"
SLOT_SECONDS      = 900  # 15 minutes

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = threading.Event()

def _handle_signal(signum, frame):
    log.info("Shutdown signal received (%s), stopping...", signum)
    _shutdown.set()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("[Telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping send")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        log.info("[Telegram] Message sent OK")
        return True
    except Exception as e:
        log.error("[Telegram] Send error: %s", e)
        return False

# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def current_slot_ts() -> int:
    return (int(time.time()) // SLOT_SECONDS) * SLOT_SECONDS

def build_slug(ts: int) -> str:
    return f"{EVENT_SLUG_PREFIX}-{ts}"

# ---------------------------------------------------------------------------
# Gamma API
# ---------------------------------------------------------------------------
def fetch_event(slug: str) -> Optional[dict]:
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        log.error("[Gamma] fetch_event error: %s", e)
        return None

def fetch_active_eth_events(limit: int = 5) -> list:
    try:
        r = requests.get(
            f"{GAMMA_API}/events",
            params={"slug_contains": EVENT_SLUG_PREFIX, "active": "true",
                    "closed": "false", "_limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("[Gamma] fetch_active_eth_events error: %s", e)
        return []

# ---------------------------------------------------------------------------
# CLOB API
# ---------------------------------------------------------------------------
def clob_get(path: str, params: dict = None) -> Optional[dict]:
    try:
        r = requests.get(f"{CLOB_API}{path}", params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def fetch_midpoint(token_id: str) -> Optional[float]:
    d = clob_get("/midpoint", {"token_id": token_id})
    if d and "mid" in d:
        return float(d["mid"])
    return None

# ---------------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------------
def parse_tokens(market: dict) -> dict:
    """Возвращает dict вида {'up': 'token_id_...', 'down': 'token_id_...'}"""
    tokens = {}
    raw = market.get("tokens") or market.get("clobTokenIds")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []

    outcomes_raw = market.get("outcomes", "[]")
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except Exception:
            outcomes = []
    else:
        outcomes = list(outcomes_raw)

    if raw and isinstance(raw[0], dict):
        for t in raw:
            label = t.get("outcome", "").strip().lower()
            tokens[label] = t.get("token_id") or t.get("tokenId")
    elif raw and isinstance(raw[0], str):
        for i, tid in enumerate(raw):
            label = outcomes[i].lower() if i < len(outcomes) else f"outcome_{i}"
            tokens[label] = tid
    return tokens

def parse_market(market: dict) -> dict:
    tokens = parse_tokens(market)

    prices_raw = market.get("outcomePrices", "[]")
    if isinstance(prices_raw, str):
        try:
            prices_raw = json.loads(prices_raw)
        except Exception:
            prices_raw = []

    outcomes_raw = market.get("outcomes", "[]")
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except Exception:
            outcomes = []
    else:
        outcomes = list(outcomes_raw)

    outcome_prices = {}
    for i, o in enumerate(outcomes):
        p = 0.0
        if i < len(prices_raw) and prices_raw[i]:
            try:
                p = float(prices_raw[i])
            except Exception:
                p = 0.0
        outcome_prices[o.lower()] = p

    # Determine winner (winnerIndex or resolved outcome)
    winner = None
    winner_index = market.get("winnerIndex")
    if winner_index is not None and int(winner_index) < len(outcomes):
        winner = outcomes[int(winner_index)].lower()

    return {
        "condition_id":   market.get("conditionId") or market.get("condition_id", ""),
        "question":       market.get("question", ""),
        "active":         bool(market.get("active", False)),
        "closed":         bool(market.get("closed", False)),
        "tokens":         tokens,
        "outcome_prices": outcome_prices,
        "volume":         float(market.get("volume") or 0),
        "liquidity":      float(market.get("liquidity") or 0),
        "end_date":       market.get("endDateIso") or market.get("end_date_iso"),
        "winner":         winner,
    }

def get_winner_from_event(event: dict) -> Optional[str]:
    """Получает финальный исход из закрытого события."""
    markets = event.get("markets", [])
    if not markets:
        return None
    m = parse_market(markets[0])
    if m["winner"]:
        return m["winner"]
    # Fallback: победитель — тот у кого цена = 1.0
    for label, price in m["outcome_prices"].items():
        if price >= 0.99:
            return label
    return None

# ---------------------------------------------------------------------------
# Find active market
# ---------------------------------------------------------------------------
def find_active_event():
    slot = current_slot_ts()
    for offset in [0, SLOT_SECONDS, -SLOT_SECONDS]:
        slug = build_slug(slot + offset)
        ev = fetch_event(slug)
        if ev and ev.get("markets"):
            mi = parse_market(ev["markets"][0])
            if mi["active"] and not mi["closed"]:
                return ev, mi

    events = fetch_active_eth_events(3)
    for ev in events:
        if ev.get("markets"):
            return ev, parse_market(ev["markets"][0])

    return None, None

def wait_for_close(slug: str, timeout_sec: int = 1200) -> Optional[dict]:
    """Ждёт закрытия рынка, возвращает финальный event dict."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline and not _shutdown.is_set():
        ev = fetch_event(slug)
        if ev and ev.get("markets"):
            mi = parse_market(ev["markets"][0])
            if mi["closed"] or not mi["active"]:
                log.info("[%s] Market closed.", slug)
                return ev
        _shutdown.wait(POLL_INTERVAL)
    return None

# ---------------------------------------------------------------------------
# Core monitoring loop for one market
# ---------------------------------------------------------------------------
def monitor_market(event: dict, minfo: dict):
    slug = event.get("slug", "?")
    log.info("=== Monitoring market: %s ===", slug)
    log.info("Question: %s", minfo["question"])
    log.info("Volume: $%.0f  Liquidity: $%.0f", minfo["volume"], minfo["liquidity"])

    # Track which outcomes have already triggered the low-price alert
    alerted: set = set()

    while not _shutdown.is_set():
        # Refresh market state
        fresh_ev = fetch_event(slug)
        if fresh_ev and fresh_ev.get("markets"):
            minfo = parse_market(fresh_ev["markets"][0])

        # Check if market is closed
        if minfo["closed"] or not minfo["active"]:
            log.info("[%s] Market no longer active — fetching final outcome.", slug)
            break

        # Fetch live CLOB midpoint prices
        tokens = minfo["tokens"]
        prices = {}
        for label, token_id in tokens.items():
            if token_id:
                mid = fetch_midpoint(token_id)
                if mid is None:
                    # fallback to gamma price
                    mid = minfo["outcome_prices"].get(label)
                prices[label] = mid

        # Log current state
        price_str = "  ".join(
            f"{lbl.upper()}={v*100:.1f}%" if v is not None else f"{lbl.upper()}=N/A"
            for lbl, v in prices.items()
        )
        log.info("[%s] %s", slug, price_str)

        # Check threshold: price <= ALERT_THRESHOLD
        for label, price in prices.items():
            if price is None:
                continue
            if price <= ALERT_THRESHOLD and label not in alerted:
                alerted.add(label)
                msg = (
                    f"⚠️ <b>Polymarket LOW PRICE ALERT</b>\n"
                    f"Market: <code>{slug}</code>\n"
                    f"Outcome: <b>{label.upper()}</b>\n"
                    f"Price: <b>{price*100:.2f}%</b> (≤ {ALERT_THRESHOLD*100:.0f}%)\n"
                    f"Question: {minfo['question']}\n"
                    f"Volume: ${minfo['volume']:.0f}"
                )
                log.info("[ALERT] %s price dropped to %.2f%% — sending Telegram", label.upper(), price * 100)
                send_telegram(msg)

        _shutdown.wait(POLL_INTERVAL)

    # Market closed — get final outcome
    final_ev = fetch_event(slug)
    winner = None
    if final_ev:
        winner = get_winner_from_event(final_ev)

    # Build result message
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    alerted_str = ", ".join(a.upper() for a in alerted) if alerted else "none"

    if winner:
        winner_emoji = "🟢" if winner == "up" else "🔴"
        result_line = f"{winner_emoji} <b>Winner: {winner.upper()}</b>"
    else:
        result_line = "❓ <b>Winner: unknown (check manually)</b>"

    # Prices at close
    final_minfo = parse_market(final_ev["markets"][0]) if (final_ev and final_ev.get("markets")) else minfo
    final_prices = "  ".join(
        f"{lbl.upper()}={v*100:.1f}%"
        for lbl, v in final_minfo["outcome_prices"].items()
    )

    summary_msg = (
        f"📊 <b>Polymarket Market Result</b>\n"
        f"Market: <code>{slug}</code>\n"
        f"Question: {minfo['question']}\n"
        f"Closed at: {now_utc}\n"
        f"{result_line}\n"
        f"Final prices: {final_prices}\n"
        f"Volume: ${minfo['volume']:.0f}\n"
        f"Outcomes that hit ≤{ALERT_THRESHOLD*100:.0f}% during market: {alerted_str}"
    )

    log.info("[%s] RESULT: winner=%s  alerted=%s", slug, winner, alerted_str)
    send_telegram(summary_msg)

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def run_monitor():
    log.info("Polymarket ETH Up/Down 15m Monitor starting")
    log.info("poll=%ds  threshold=%.0f%%  telegram=%s",
             POLL_INTERVAL, ALERT_THRESHOLD * 100,
             "configured" if TELEGRAM_BOT_TOKEN else "NOT SET")

    seen_slugs: set = set()

    while not _shutdown.is_set():
        event, minfo = find_active_event()

        if event is None:
            log.warning("No active market found, retrying in %ds...", POLL_INTERVAL)
            _shutdown.wait(POLL_INTERVAL)
            continue

        slug = event.get("slug", "")

        if slug in seen_slugs:
            # Already processed this market, wait for next slot
            log.debug("[%s] Already processed, waiting for next market...", slug)
            _shutdown.wait(POLL_INTERVAL)
            continue

        seen_slugs.add(slug)
        monitor_market(event, minfo)

    log.info("Monitor stopped cleanly.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    run_monitor()