#!/usr/bin/env python3
"""
Polymarket ETH Up/Down 15m Monitor  —  Railway Edition
=======================================================
Tracks the current active "Ethereum Up or Down - 15 Minutes" market
on Polymarket and logs structured state every POLL_INTERVAL seconds.

Designed for long-running deployment on Railway (or any container host).
No interactive terminal output; all output goes through Python logging.

Env vars:
  POLL_INTERVAL   seconds between REST polls          (default: 5)
  LOG_LEVEL       Python logging level string         (default: INFO)

Optional:
  pip install websocket-client   enables real-time WS price feed
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
# Config from environment
# ---------------------------------------------------------------------------
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "5"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    stream=sys.stdout,
)
log = logging.getLogger("polymarket")

# ---------------------------------------------------------------------------
# Optional WebSocket dependency
# ---------------------------------------------------------------------------
try:
    import websocket
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    log.warning("websocket-client not installed — WebSocket feed disabled")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GAMMA_API         = "https://gamma-api.polymarket.com"
CLOB_API          = "https://clob.polymarket.com"
WSS_URL           = "wss://ws-subscriptions-clob.polymarket.com/ws/"
EVENT_SLUG_PREFIX = "eth-updown-15m"
SLOT_SECONDS      = 900  # 15 minutes

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = threading.Event()


def _handle_signal(signum, frame):
    log.info("Shutdown signal received (sig=%s), stopping...", signum)
    _shutdown.set()


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT,  _handle_signal)

# ---------------------------------------------------------------------------
# Color stubs — plain strings only (Railway logs don't support ANSI)
# ---------------------------------------------------------------------------
def green(s):  return str(s)
def red(s):    return str(s)
def yellow(s): return str(s)
def cyan(s):   return str(s)
def bold(s):   return str(s)

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


def fetch_price(token_id: str, side: str) -> Optional[float]:
    d = clob_get("/price", {"token_id": token_id, "side": side})
    if d and "price" in d:
        return float(d["price"])
    return None


def fetch_book(token_id: str) -> Optional[dict]:
    return clob_get("/book", {"token_id": token_id})


def fetch_all_clob(tokens: dict) -> dict:
    result = {}
    for label, token_id in tokens.items():
        if not token_id:
            continue
        result[f"{label}_mid"]  = fetch_midpoint(token_id)
        result[f"{label}_bid"]  = fetch_price(token_id, "buy")
        result[f"{label}_ask"]  = fetch_price(token_id, "sell")
        result[f"{label}_book"] = fetch_book(token_id)
    return result


# ---------------------------------------------------------------------------
# Market parsing
# ---------------------------------------------------------------------------
def parse_tokens(market: dict) -> dict:
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
    }


# ---------------------------------------------------------------------------
# Formatting helpers (plain text — no ANSI)
# ---------------------------------------------------------------------------
def fmt_pct(p: float) -> str:
    return f"{p * 100:.1f}%"


def fmt_countdown(end_date_str: Optional[str]) -> str:
    if not end_date_str:
        return "N/A"
    try:
        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        secs = int((end_dt - datetime.now(timezone.utc)).total_seconds())
        if secs < 0:
            return "EXPIRED"
        m, s = divmod(secs, 60)
        return f"{m:02d}:{s:02d}"
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Structured log output
# ---------------------------------------------------------------------------
def log_state(event: dict, minfo: dict, clob: dict):
    slug   = event.get("slug", "?")
    status = "ACTIVE" if minfo["active"] and not minfo["closed"] else "CLOSED"
    countdown = fmt_countdown(minfo["end_date"])

    log.info(
        "market=%s  status=%s  closes_in=%s  volume=$%.0f  liquidity=$%.0f",
        slug, status, countdown, minfo["volume"], minfo["liquidity"],
    )

    for label in ["up", "down"]:
        gp  = minfo["outcome_prices"].get(label, 0.0)
        mid = clob.get(f"{label}_mid")
        bid = clob.get(f"{label}_bid")
        ask = clob.get(f"{label}_ask")

        mid_s = f"{mid * 100:.1f}%" if mid is not None else "--"
        bid_s = f"{bid * 100:.1f}%" if bid is not None else "--"
        ask_s = f"{ask * 100:.1f}%" if ask is not None else "--"

        log.info(
            "  outcome=%-4s  gamma=%s  mid=%s  bid=%s  ask=%s",
            label.upper(), fmt_pct(gp), mid_s, bid_s, ask_s,
        )

    # Order book top-3 per side
    for label in ["up", "down"]:
        book = clob.get(f"{label}_book")
        if not book:
            continue
        bids = book.get("bids", [])[:3]
        asks = book.get("asks", [])[:3]
        ask_line = "  ".join(
            f"{float(a['price']) * 100:.1f}%x{float(a['size']):.0f}" for a in asks
        ) or "empty"
        bid_line = "  ".join(
            f"{float(b['price']) * 100:.1f}%x{float(b['size']):.0f}" for b in bids
        ) or "empty"
        log.info("  book[%s]  asks=%s  bids=%s", label.upper(), ask_line, bid_line)


# ---------------------------------------------------------------------------
# WebSocket monitor (optional)
# ---------------------------------------------------------------------------
ws_updates: dict = {}


class WSMonitor:
    def __init__(self, token_ids: list):
        self.token_ids = token_ids
        self.ws = None
        self._t = None

    def _on_open(self, ws):
        for tid in self.token_ids:
            ws.send(json.dumps({"auth": {}, "type": "market", "markets": [tid]}))
        log.info("[WS] Subscribed to %d tokens", len(self.token_ids))

    def _on_message(self, ws, msg):
        try:
            d = json.loads(msg)
            key = d.get("asset_id") or d.get("market", "")
            if key:
                ws_updates[key] = d
        except Exception:
            pass

    def _on_error(self, ws, err):
        log.warning("[WS] Error: %s", err)

    def _on_close(self, ws, *a):
        log.info("[WS] Disconnected")

    def start(self):
        if not WS_AVAILABLE:
            return
        self.ws = websocket.WebSocketApp(
            WSS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._t = threading.Thread(
            target=self.ws.run_forever,
            kwargs={"ping_interval": 30},
            daemon=True,
        )
        self._t.start()

    def stop(self):
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


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


# ---------------------------------------------------------------------------
# Main monitor loop
# ---------------------------------------------------------------------------
def run_monitor():
    log.info("Polymarket ETH Up/Down 15m Monitor starting")
    log.info("poll_interval=%ds  log_level=%s  websocket=%s",
             POLL_INTERVAL, LOG_LEVEL, "enabled" if WS_AVAILABLE else "disabled")

    last_slug  = None
    ws_monitor = None

    while not _shutdown.is_set():
        event, minfo = find_active_event()

        if event is None:
            log.warning("No active market found, retrying in %ds...", POLL_INTERVAL)
            _shutdown.wait(POLL_INTERVAL)
            continue

        slug = event.get("slug", "")

        if slug != last_slug:
            log.info("New market detected: %s", slug)
            if ws_monitor:
                ws_monitor.stop()
            token_ids = [v for v in minfo["tokens"].values() if v]
            ws_monitor = WSMonitor(token_ids)
            ws_monitor.start()
            last_slug = slug

        clob = fetch_all_clob(minfo["tokens"])
        log_state(event, minfo, clob)

        if ws_updates:
            log.info("[WS] Buffer: %d tokens with live updates", len(ws_updates))

        _shutdown.wait(POLL_INTERVAL)

        # Refresh market state
        fresh = fetch_event(slug)
        if fresh and fresh.get("markets"):
            minfo = parse_market(fresh["markets"][0])
            event = fresh

    log.info("Monitor stopped cleanly.")
    if ws_monitor:
        ws_monitor.stop()


# ---------------------------------------------------------------------------
# One-shot snapshot
# ---------------------------------------------------------------------------
def run_once(slug: str):
    log.info("One-shot snapshot: %s", slug)
    ev = fetch_event(slug)
    if not ev:
        log.error("Event not found: %s", slug)
        sys.exit(1)
    market = ev["markets"][0] if ev.get("markets") else {}
    minfo  = parse_market(market)
    clob   = fetch_all_clob(minfo["tokens"])
    log_state(ev, minfo, clob)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--once":
        target = sys.argv[2] if len(sys.argv) > 2 else build_slug(current_slot_ts())
        run_once(target)
    else:
        run_monitor()
