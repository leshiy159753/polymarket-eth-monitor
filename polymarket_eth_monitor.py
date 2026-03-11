#!/usr/bin/env python3
"""
Polymarket ETH Up/Down 15m Monitor — Railway Edition
Logika:
  1. Kazhdye POLL_INTERVAL sekund nakhodit tekushchiy aktivnyy 15-minutnyy rynok ETH.
  2. Sledit za tsenami tokenov UP i DOWN iz CLOB API.
  3. Esli tsena lyubogo tokena opuskaetsya do <= ALERT_THRESHOLD (default 0.01):
       - fiksiruet sobitye v pamyati (odnokratno za rynok)
       - otpravlyaet alert v Telegram
  4. Kogda rynok zakryvaetsya — zhdet winnerIndex s retriami, otpravlyaet itog v Telegram.
  5. Sokhranyet statistiku v stats.json.
  6. Polling Telegram komand: /stats — pokazyvaet statistiku.

Env vars:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLL_INTERVAL, ALERT_THRESHOLD, LOG_LEVEL, STATS_FILE
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

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
ALERT_THRESHOLD = float(os.getenv("ALERT_THRESHOLD", "0.01"))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STATS_FILE = os.getenv("STATS_FILE", "stats.json")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
log = logging.getLogger("polymarket")

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
EVENT_SLUG_PREFIX = "eth-updown-15m"
SLOT_SECONDS = 900

_shutdown = threading.Event()

def _handle_signal(signum, frame):
    log.info("Shutdown signal received (%s), stopping...", signum)
    _shutdown.set()

signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

_stats_lock = threading.Lock()

def load_stats():
    default = {
        "total_markets": 0,
        "alerted_markets": 0,
        "outcomes": {"alerted_won": 0, "alerted_lost": 0},
        "history": []
    }
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r") as f:
                data = json.load(f)
            for k, v in default.items():
                if k not in data:
                    data[k] = v
            if "outcomes" not in data:
                data["outcomes"] = default["outcomes"]
            return data
    except Exception as e:
        log.warning("Could not load stats: %s", e)
    return default

def save_stats(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        log.error("Could not save stats: %s", e)

def record_market_result(slug, alerted_labels, winner):
    with _stats_lock:
        stats = load_stats()
        stats["total_markets"] += 1
        entry = {
            "slug": slug,
            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "alerted": list(alerted_labels),
            "winner": winner,
            "alerted_won": False,
        }
        if alerted_labels:
            stats["alerted_markets"] += 1
            if winner and winner in alerted_labels:
                stats["outcomes"]["alerted_won"] += 1
                entry["alerted_won"] = True
                log.info("[Stats] Alerted token %s WON!", winner)
            elif winner:
                stats["outcomes"]["alerted_lost"] += 1
                log.info("[Stats] Alerted token(s) %s lost (winner=%s)", alerted_labels, winner)
        stats["history"] = ([entry] + stats["history"])[:50]
        save_stats(stats)
        log.info("[Stats] Saved. total=%d alerted=%d won=%d lost=%d",
                 stats["total_markets"], stats["alerted_markets"],
                 stats["outcomes"]["alerted_won"], stats["outcomes"]["alerted_lost"])

def format_stats_message():
    with _stats_lock:
        s = load_stats()
    total = s["total_markets"]
    alerted = s["alerted_markets"]
    won = s["outcomes"]["alerted_won"]
    lost = s["outcomes"]["alerted_lost"]
    total_ab = won + lost
    win_rate = (won / total_ab * 100) if total_ab > 0 else 0.0
    thr = ALERT_THRESHOLD * 100
    lines = [
        "<b>Polymarket &lt;={:.0f}% Token Stats</b>".format(thr),
        "",
        "Markets tracked: <b>{}</b>".format(total),
        "Markets with &lt;={:.0f}% token: <b>{}</b>".format(thr, alerted),
        "",
        "Alerted token won:  <b>{}</b>".format(won),
        "Alerted token lost: <b>{}</b>".format(lost),
        "Win rate: <b>{:.1f}%</b>  ({} resolved)".format(win_rate, total_ab),
    ]
    history = s.get("history", [])[:10]
    if history:
        lines.append("")
        lines.append("<b>Last 10 results:</b>")
        for h in history:
            alerted_str = ", ".join(h["alerted"]).upper() if h["alerted"] else "-"
            winner_str = h["winner"].upper() if h["winner"] else "?"
            badge = ""
            if h["alerted"] and h["winner"]:
                badge = " WIN" if h["alerted_won"] else " LOSS"
            lines.append("  {}  alert={}  winner={}{}".format(
                h["time"][:16], alerted_str, winner_str, badge))
    return "\n".join(lines)

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("[Telegram] BOT_TOKEN or CHAT_ID not set")
        return False
    try:
        r = requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_BOT_TOKEN),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        r.raise_for_status()
        log.info("[Telegram] Message sent OK")
        return True
    except Exception as e:
        log.error("[Telegram] Send error: %s", e)
        return False

_last_update_id = 0
_bot_username = ""

def _get_bot_username():
    global _bot_username
    if _bot_username:
        return _bot_username
    try:
        r = requests.get(
            "https://api.telegram.org/bot{}/getMe".format(TELEGRAM_BOT_TOKEN),
            timeout=5,
        )
        _bot_username = r.json().get("result", {}).get("username", "").lower()
    except Exception:
        pass
    return _bot_username

def poll_telegram_commands():
    global _last_update_id
    if not TELEGRAM_BOT_TOKEN:
        return
    log.info("[TG Commands] Polling started")
    while not _shutdown.is_set():
        try:
            r = requests.get(
                "https://api.telegram.org/bot{}/getUpdates".format(TELEGRAM_BOT_TOKEN),
                params={"offset": _last_update_id + 1, "timeout": 20},
                timeout=25,
            )
            if r.status_code == 200:
                updates = r.json().get("result", [])
                for upd in updates:
                    _last_update_id = upd["update_id"]
                    msg = upd.get("message") or upd.get("channel_post") or {}
                    text = msg.get("text", "").strip().lower()
                    if text in ("/stats", "/stats@" + _get_bot_username()):
                        log.info("[TG Commands] /stats requested")
                        send_telegram(format_stats_message())
                    elif text in ("/help", "/help@" + _get_bot_username()):
                        send_telegram(
                            "<b>Polymarket ETH Monitor</b>\n\n"
                            "/stats - статистика побед токенов\n"
                            "/help  - это сообщение"
                        )
        except Exception as e:
            log.debug("[TG Commands] poll error: %s", e)
        _shutdown.wait(5)

def current_slot_ts():
    return (int(time.time()) // SLOT_SECONDS) * SLOT_SECONDS

def build_slug(ts):
    return "{}-{}".format(EVENT_SLUG_PREFIX, ts)

def fetch_event(slug):
    try:
        r = requests.get("{}/events".format(GAMMA_API), params={"slug": slug}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        log.error("[Gamma] fetch_event error: %s", e)
        return None

def fetch_active_eth_events(limit=5):
    try:
        r = requests.get(
            "{}/events".format(GAMMA_API),
            params={"slug_contains": EVENT_SLUG_PREFIX, "active": "true",
                    "closed": "false", "_limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("[Gamma] fetch_active_eth_events error: %s", e)
        return []

def clob_get(path, params=None):
    try:
        r = requests.get("{}{}".format(CLOB_API, path), params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def fetch_midpoint(token_id):
    d = clob_get("/midpoint", {"token_id": token_id})
    if d and "mid" in d:
        return float(d["mid"])
    return None

def parse_tokens(market):
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
            label = outcomes[i].lower() if i < len(outcomes) else "outcome_{}".format(i)
            tokens[label] = tid
    return tokens

def parse_market(market):
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
    winner = None
    winner_index = market.get("winnerIndex")
    if winner_index is not None:
        try:
            idx = int(winner_index)
            if idx < len(outcomes):
                winner = outcomes[idx].lower()
        except Exception:
            pass
    return {
        "condition_id": market.get("conditionId") or market.get("condition_id", ""),
        "question": market.get("question", ""),
        "active": bool(market.get("active", False)),
        "closed": bool(market.get("closed", False)),
        "tokens": tokens,
        "outcome_prices": outcome_prices,
        "volume": float(market.get("volume") or 0),
        "liquidity": float(market.get("liquidity") or 0),
        "end_date": market.get("endDateIso") or market.get("end_date_iso"),
        "winner": winner,
    }

def get_winner_from_market(market_dict):
    m = parse_market(market_dict)
    if m["winner"]:
        return m["winner"]
    for label, price in m["outcome_prices"].items():
        if price >= 0.99:
            return label
    return None

def wait_for_winner(slug, max_retries=30, delay=10):
    log.info("[%s] Waiting for winner (up to %ds)...", slug, max_retries * delay)
    for attempt in range(max_retries):
        if _shutdown.is_set():
            break
        ev = fetch_event(slug)
        if ev and ev.get("markets"):
            market_dict = ev["markets"][0]
            winner = get_winner_from_market(market_dict)
            if winner:
                log.info("[%s] Winner found on attempt %d: %s", slug, attempt + 1, winner)
                return winner
            log.debug("[%s] Attempt %d: no winner yet (winnerIndex=%s)",
                      slug, attempt + 1, market_dict.get("winnerIndex"))
        _shutdown.wait(delay)
    log.warning("[%s] Winner not found after %d retries", slug, max_retries)
    return None

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

def monitor_market(event, minfo):
    slug = event.get("slug", "?")
    log.info("=== Monitoring market: %s ===", slug)
    log.info("Question: %s", minfo["question"])
    log.info("Volume: $%.0f  Liquidity: $%.0f", minfo["volume"], minfo["liquidity"])

    alerted = set()

    while not _shutdown.is_set():
        fresh_ev = fetch_event(slug)
        if fresh_ev and fresh_ev.get("markets"):
            minfo = parse_market(fresh_ev["markets"][0])

        if minfo["closed"] or not minfo["active"]:
            log.info("[%s] Market no longer active.", slug)
            break

        tokens = minfo["tokens"]
        prices = {}
        for label, token_id in tokens.items():
            if token_id:
                mid = fetch_midpoint(token_id)
                if mid is None:
                    mid = minfo["outcome_prices"].get(label)
                prices[label] = mid

        price_str = "  ".join(
            "{0}={1:.1f}%".format(lbl.upper(), v * 100) if v is not None else "{0}=N/A".format(lbl.upper())
            for lbl, v in prices.items()
        )
        log.info("[%s] %s", slug, price_str)

        for label, price in prices.items():
            if price is None:
                continue
            if price <= ALERT_THRESHOLD and label not in alerted:
                alerted.add(label)
                msg = (
                    "<b>Polymarket LOW PRICE ALERT</b>\n"
                    "Market: <code>{}</code>\n"
                    "Outcome: <b>{}</b>\n"
                    "Price: <b>{:.2f}%</b> (&lt;= {:.0f}%)\n"
                    "Question: {}\n"
                    "Volume: ${:.0f}"
                ).format(slug, label.upper(), price * 100,
                         ALERT_THRESHOLD * 100, minfo["question"], minfo["volume"])
                log.info("[ALERT] %s price dropped to %.2f%%", label.upper(), price * 100)
                send_telegram(msg)

        _shutdown.wait(POLL_INTERVAL)

    log.info("[%s] Market closed. Fetching winner...", slug)
    winner = wait_for_winner(slug, max_retries=30, delay=10)

    record_market_result(slug, alerted, winner)

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    alerted_str = ", ".join(a.upper() for a in alerted) if alerted else "none"

    if winner:
        winner_emoji = "UP" if winner == "up" else "DOWN"
        result_line = "<b>Winner: {} ({})</b>".format(winner.upper(), winner_emoji)
    else:
        result_line = "<b>Winner: unknown - check Polymarket manually</b>"

    upset_line = ""
    if alerted and winner:
        if winner in alerted:
            upset_line = "\n<b>UPSET! &lt;={:.0f}% token WON!</b>".format(ALERT_THRESHOLD * 100)
        else:
            upset_line = "\nFavourite won (&lt;={:.0f}% token lost as expected)".format(ALERT_THRESHOLD * 100)

    final_ev = fetch_event(slug)
    final_minfo = parse_market(final_ev["markets"][0]) if (final_ev and final_ev.get("markets")) else minfo
    final_prices = "  ".join(
        "{0}={1:.1f}%".format(lbl.upper(), v * 100)
        for lbl, v in final_minfo["outcome_prices"].items()
    )

    summary_msg = (
        "<b>Polymarket Market Result</b>\n"
        "Market: <code>{}</code>\n"
        "Question: {}\n"
        "Closed at: {}\n"
        "{}{}\n"
        "Final prices: {}\n"
        "Volume: ${:.0f}\n"
        "Tokens that hit &lt;={:.0f}%: <b>{}</b>"
    ).format(slug, minfo["question"], now_utc, result_line, upset_line,
             final_prices, minfo["volume"], ALERT_THRESHOLD * 100, alerted_str)

    log.info("[%s] RESULT: winner=%s  alerted=%s", slug, winner, alerted_str)
    send_telegram(summary_msg)

def run_monitor():
    log.info("Polymarket ETH Up/Down 15m Monitor starting")
    log.info("poll=%ds  threshold=%.0f%%  telegram=%s",
             POLL_INTERVAL, ALERT_THRESHOLD * 100,
             "configured" if TELEGRAM_BOT_TOKEN else "NOT SET")

    if TELEGRAM_BOT_TOKEN:
        tg_thread = threading.Thread(target=poll_telegram_commands, daemon=True)
        tg_thread.start()

    seen_slugs = set()

    while not _shutdown.is_set():
        event, minfo = find_active_event()

        if event is None:
            log.warning("No active market found, retrying in %ds...", POLL_INTERVAL)
            _shutdown.wait(POLL_INTERVAL)
            continue

        slug = event.get("slug", "")

        if slug in seen_slugs:
            log.debug("[%s] Already processed, waiting for next market...", slug)
            _shutdown.wait(POLL_INTERVAL)
            continue

        seen_slugs.add(slug)
        monitor_market(event, minfo)

    log.info("Monitor stopped cleanly.")

if __name__ == "__main__":
    run_monitor()
