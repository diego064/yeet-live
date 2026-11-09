"""Free live-data engine for YEET Live.

Replaces the original's paid Unusual Whales CSV uploads + static
snapshot.json with Yahoo Finance (yfinance) — free, no API key.

What is free, and what is NOT real-time:
- Yahoo US equity / options data is delayed ~15 minutes and is
  end-of-day / snapshot options-chain data, NOT tick-by-tick
  whale flow. There is no free, legal, real-time options-flow
  feed (Unusual Whales, FlowAlgo, Cheddar Flow are all paid).
- This module is honest about that: every contract is labelled
  with its source and fetch time, and "unusual" here means
  *volume vs open interest on the public chain*, not a
  proprietary alert. It is a research lead, never a trade signal.

Unusual-activity heuristic (public chain only):
  premium = volume * lastPrice * 100
  unusual if volume >= 100 and premium >= $10,000 and
  (volume >= openInterest  OR  volume >= 500  OR  premium >= $100,000)

Bid/ask: the chain's bid and ask are recorded per contract so the
spread position of the last price can be read as a lean (near ask =
likely bought, near bid = likely sold). That is a guess from a
~15-min delayed snapshot, not a fill — Yahoo publishes no trade tape.
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SNAPSHOT = DATA / "live_snapshot.json"
EVENTS_FILE = DATA / "events.json"
WATCHLIST_FILE = DATA / "watchlist.json"

CACHE_TTL_SECONDS = 300  # 5 minutes, Yahoo options refresh at this pace anyway
MAX_EXPIRIES_PER_TICKER = 3
MIN_VOLUME = 100
MIN_PREMIUM = 10_000

DEFAULT_TICKERS = [
    "SPY", "QQQ", "NVDA", "AMD", "ORCL", "MU", "SMCI", "MRNA", "META",
    "TSLA", "NET", "MRVL", "SMTC", "NVTS", "IBRX", "AAPL", "MSFT",
    "GOOGL", "AMZN", "AVGO", "TSM", "PLTR", "COIN", "HOOD", "GME",
    "AMC", "BABA", "SMH", "NFLX", "CRWV", "IREN", "SNDK", "VRT",
    "LITE", "TSEM", "SOUN", "RKLB", "CRSP", "ESTC", "ADBE",
]


def load_watchlist() -> list[str]:
    try:
        tickers = json.loads(WATCHLIST_FILE.read_text())
        return [str(t).upper().strip() for t in tickers if str(t).strip()]
    except Exception:
        return list(DEFAULT_TICKERS)


def load_events() -> list[dict]:
    try:
        return json.loads(EVENTS_FILE.read_text())
    except Exception:
        return []


def save_events(events: list[dict]) -> None:
    EVENTS_FILE.write_text(json.dumps(events, indent=2))


def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _safe_int(v, default=0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


def fetch_ticker(symbol: str) -> dict:
    """One ticker: live quote + nearest option expiries, unusual contracts only."""
    import yfinance as yf

    symbol = symbol.upper().strip().lstrip("$")
    tk = yf.Ticker(symbol)
    out: dict = {
        "ticker": symbol, "spot": None, "prev_close": None, "change_pct": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "contracts": [], "call_premium": 0.0, "put_premium": 0.0,
        "call_volume": 0, "put_volume": 0, "error": None,
        "next_earnings": None,
    }
    try:
        fi = tk.fast_info
        out["spot"] = _safe_float(fi.get("lastPrice"), 0.0) or None
        out["prev_close"] = _safe_float(fi.get("previousClose"), 0.0) or None
        if out["spot"] and out["prev_close"]:
            out["change_pct"] = round(
                100 * (out["spot"] - out["prev_close"]) / out["prev_close"], 2)
    except Exception as exc:  # quote failed, options may still work
        out["error"] = f"quote: {exc}"
    if not out["spot"]:
        # Fallback: last daily close from history (history endpoint is
        # more reliable than fast_info through some proxies).
        try:
            hist = tk.history(period="5d", interval="1d")
            if len(hist):
                out["spot"] = _safe_float(hist["Close"].iloc[-1]) or None
                out["prev_close"] = _safe_float(hist["Close"].iloc[-2]) if len(hist) > 1 else None
                if out["spot"] and out["prev_close"]:
                    out["change_pct"] = round(
                        100 * (out["spot"] - out["prev_close"]) / out["prev_close"], 2)
        except Exception:
            pass
    expiries: list = []
    for _attempt in range(3):  # Yahoo options endpoint times out occasionally; retry
        try:
            expiries = list(tk.options or [])[:MAX_EXPIRIES_PER_TICKER]
            break
        except Exception as exc:
            out["error"] = f"options: {exc}"
            expiries = []
    if not expiries:
        return out
    spot = out["spot"] or 0.0
    for expiry in expiries:
        try:
            chain = tk.option_chain(expiry)
        except Exception:
            continue
        for kind, frame in (("C", chain.calls), ("P", chain.puts)):
            try:
                rows = frame.to_dict("records")
            except Exception:
                continue
            for r in rows:
                vol = _safe_int(r.get("volume"))
                oi = _safe_int(r.get("openInterest"))
                price = _safe_float(r.get("lastPrice"))
                if vol < MIN_VOLUME or price <= 0:
                    continue
                premium = vol * price * 100
                if premium < MIN_PREMIUM:
                    continue
                # Unusual: volume beating open interest, or big absolute size/premium
                unusual = vol >= max(oi, 1) or vol >= 500 or premium >= 100_000
                if not unusual:
                    continue
                strike = _safe_float(r.get("strike"))
                # Sanity: drop far-deep-ITM junk strikes (e.g. a $2,300 put on
                # an $835 stock) — stale/illiquid Yahoo rows that would
                # otherwise dominate the premium ranking.
                if spot and not (0.5 * spot <= strike <= 1.5 * spot):
                    continue
                bid = _safe_float(r.get("bid"))
                ask = _safe_float(r.get("ask"))
                spread_pos = None
                if bid and ask and ask > bid:
                    spread_pos = round(min(1.0, max(0.0, (price - bid) / (ask - bid))), 2)
                contract = {
                    "underlying_symbol": symbol, "ticker": symbol,
                    "date": date.today().isoformat(),
                    "expiry": str(expiry), "strike": strike,
                    "type": kind, "side": None,  # Yahoo has no side; never invent one
                    "volume": vol, "open_interest": oi,
                    "premium": round(premium, 2), "last_price": round(price, 2),
                    "bid": round(bid, 2) if bid else None,
                    "ask": round(ask, 2) if ask else None,
                    # Last-price position in the bid/ask spread (0=bid, 1=ask):
                    # a lean only, never a fill.
                    "spread_position": spread_pos,
                    "implied_volatility": _safe_float(r.get("impliedVolatility")),
                    "vol_oi": round(vol / max(oi, 1), 2),
                    "moneyness": ("ITM" if spot and (
                        (kind == "C" and strike < spot) or
                        (kind == "P" and strike > spot)) else "OTM"),
                    "days_to_expiry": max(
                        0, (date.fromisoformat(str(expiry)) - date.today()).days),
                }
                out["contracts"].append(contract)
                if kind == "C":
                    out["call_premium"] += premium
                    out["call_volume"] += vol
                else:
                    out["put_premium"] += premium
                    out["put_volume"] += vol
    out["contracts"].sort(key=lambda c: c["premium"], reverse=True)
    out["contracts"] = out["contracts"][:25]  # top 25 per ticker
    out["call_premium"] = round(out["call_premium"], 2)
    out["put_premium"] = round(out["put_premium"], 2)
    return out


def fetch_earnings_events(tickers: list[str]) -> list[dict]:
    """Free earnings dates from Yahoo — added as events, flagged earnings-related
    so RAMBO (which excludes earnings, like the original) keeps them separate."""
    import yfinance as yf

    events: list[dict] = []
    for symbol in tickers:
        try:
            df = yf.Ticker(symbol).get_earnings_dates(limit=12)
            if df is None or df.empty:
                continue
            # yfinance returns past dates too — keep only the next upcoming one
            future = [(ts, row) for ts, row in df.iterrows()
                      if ts.date() >= date.today()]
            if not future:
                continue
            ts, row = sorted(future, key=lambda x: x[0])[0]
            d = ts.date().isoformat()
            events.append({
                "ticker": symbol, "title": f"{symbol} earnings",
                "date": d, "end_date": d, "time": "Before/after market",
                "category": "Earnings", "status": "Confirmed",
                "earnings_related": True,
                "url": f"https://finance.yahoo.com/quote/{symbol}",
                "summary": "Scheduled earnings report (Yahoo Finance calendar).",
                "note": "Auto-added from Yahoo earnings calendar. RAMBO excludes earnings matches, as in the original.",
            })
        except Exception:
            continue
    return events


def build_snapshot(tickers: list[str] | None = None) -> dict:
    tickers = tickers or load_watchlist()
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_ticker, t): t for t in tickers}
        for fut in as_completed(futures):
            try:
                r = fut.result()
                results[r["ticker"]] = r
            except Exception as exc:
                results[futures[fut]] = {"ticker": futures[fut], "error": str(exc),
                                         "contracts": [], "spot": None}
    contracts = [c for r in results.values() for c in r.get("contracts", [])]
    contracts.sort(key=lambda c: c["premium"], reverse=True)
    earnings = fetch_earnings_events(tickers)
    snapshot = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Yahoo Finance via yfinance (free, ~15-min delayed US data)",
        "tickers_scanned": len(tickers),
        "tickers_ok": sum(1 for r in results.values() if not r.get("error")),
        "tickers": results,
        "contracts": contracts,
        "total_call_premium": round(sum(r.get("call_premium", 0) for r in results.values()), 2),
        "total_put_premium": round(sum(r.get("put_premium", 0) for r in results.values()), 2),
        "earnings_events": earnings,
        "errors": {k: v.get("error") for k, v in results.items() if v.get("error")},
    }
    return snapshot


def save_snapshot(snapshot: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snapshot, indent=2))


def load_snapshot(max_age_seconds: int = CACHE_TTL_SECONDS) -> dict | None:
    try:
        snap = json.loads(SNAPSHOT.read_text())
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(
            snap["fetched_at"].replace("Z", "+00:00"))).total_seconds()
        snap["_age_seconds"] = age
        snap["_stale"] = age > max_age_seconds
        return snap
    except Exception:
        return None


def get_snapshot(tickers: list[str] | None = None, force_refresh: bool = False) -> dict:
    """Cached-first: return the saved snapshot if fresh, else fetch live."""
    if not force_refresh:
        snap = load_snapshot()
        if snap and not snap.get("_stale"):
            return snap
    snap = build_snapshot(tickers)
    save_snapshot(snap)
    snap["_age_seconds"] = 0
    snap["_stale"] = False
    return snap


def match_events(events: list[dict], contracts: list[dict], as_of: str | None = None) -> list[dict]:
    """Same matching rule as the original rambo.match: confirmed,
    non-earnings event, contract printed on/before the event end date
    and expiring on/after it. Earnings are excluded from RAMBO."""
    as_of = as_of or date.today().isoformat()
    results = []
    for event in events:
        if event.get("status") != "Confirmed" or event.get("earnings_related"):
            continue
        if event["end_date"] < as_of:
            continue
        rows = [p for p in contracts
                if p.get("underlying_symbol") == event["ticker"]
                and p.get("date", "") <= event["end_date"]
                and p.get("expiry", "") >= event["end_date"]]
        if not rows:
            continue
        calls = sum(p["premium"] for p in rows if p["type"] == "C")
        puts = sum(p["premium"] for p in rows if p["type"] == "P")
        direction = ("Mixed" if calls and puts else "Call-side" if calls
                     else "Put-side" if puts else "Unclear")
        results.append({**event, "prints": rows, "direction": direction,
                        "days_until": (date.fromisoformat(event["date"]) - date.fromisoformat(as_of)).days})
    return sorted(results, key=lambda r: (r["date"], r["ticker"]))


def wari_score(contracts: list[dict], benchmark: set[str] | None = None) -> dict:
    """WARI-style: call-premium share of the flow vs the benchmark tickers.
    0-100 disagreement is computed in app.py against SPY's move; here we
    return the raw shares so the page can show its working."""
    bench = benchmark or {"SPY", "QQQ", "IWM"}
    all_calls = sum(c["premium"] for c in contracts if c["type"] == "C")
    all_puts = sum(c["premium"] for c in contracts if c["type"] == "P")
    b_calls = sum(c["premium"] for c in contracts
                  if c["type"] == "C" and c["ticker"] in bench)
    b_puts = sum(c["premium"] for c in contracts
                 if c["type"] == "P" and c["ticker"] in bench)
    total = all_calls + all_puts
    b_total = b_calls + b_puts
    return {
        "call_share": round(all_calls / total, 3) if total else None,
        "put_share": round(all_puts / total, 3) if total else None,
        "benchmark_call_share": round(b_calls / b_total, 3) if b_total else None,
        "total_premium": round(total, 2),
        "call_premium": round(all_calls, 2),
        "put_premium": round(all_puts, 2),
    }


def pari_percentile(symbol: str = "SPY", period: str = "5d", interval: str = "5m") -> dict:
    """PARI-style momentum percentile from Yahoo closes only (as in the
    original pari.py): where is the latest close in its own recent range?
    Returns {} on failure — never a synthetic score."""
    import yfinance as yf
    import pandas as pd

    try:
        hist = yf.Ticker(symbol).history(period=period, interval=interval)
        closes = pd.to_numeric(hist["Close"], errors="coerce").dropna()
        if len(closes) < 12:
            return {}
        latest = float(closes.iloc[-1])
        pct = float((closes < latest).mean() * 100)
        chg = float(100 * (latest - float(closes.iloc[0])) / float(closes.iloc[0]))
        return {"symbol": symbol, "interval": interval, "latest": round(latest, 2),
                "percentile": round(pct, 1), "change_pct": round(chg, 2),
                "bars": len(closes),
                "posture": ("Bullish" if pct >= 65 else "Bearish" if pct <= 35 else "Neutral")}
    except Exception:
        return {}
