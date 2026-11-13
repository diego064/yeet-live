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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / "data"
SNAPSHOT = DATA / "live_snapshot.json"
EVENTS_FILE = DATA / "events.json"
WATCHLIST_FILE = DATA / "watchlist.json"
BIG_CONTRACTS_FILE = DATA / "big_contracts.json"
BIG_CONTRACT_MIN_PREMIUM = 1_000_000  # only track contracts at/above this premium
BIG_CONTRACT_KEEP_DAYS = 30  # drop records not seen for this long
WARI_HISTORY_FILE = DATA / "wari_history.json"
WARI_INDEXES = ["SPY", "QQQ", "IWM"]  # the indexes whose prices we track

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


def load_big_contracts() -> list[dict]:
    """Persistent log of big contracts: when each one first appeared.

    Yahoo publishes no announcement time for an options contract — the
    chain only shows today's volume / open interest. So "first seen" is
    the first refresh where the contract crossed BIG_CONTRACT_MIN_PREMIUM:
    the closest free data gets to an announcement date. History starts
    the day this log was created; anything already big before then shows
    that day as its first seen date.
    """
    try:
        records = json.loads(BIG_CONTRACTS_FILE.read_text())
        return [r for r in records if isinstance(r, dict) and r.get("key")]
    except Exception:
        return []


def save_big_contracts(records: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    BIG_CONTRACTS_FILE.write_text(json.dumps(records, indent=2))


def contract_key(c: dict) -> str:
    return f"{c.get('ticker')}|{c.get('expiry')}|{c.get('strike')}|{c.get('type')}"


def merge_big_contracts(snapshot: dict, as_of: str | None = None,
                        min_premium: float = BIG_CONTRACT_MIN_PREMIUM) -> list[dict]:
    """Fold a fresh snapshot into the big-contracts log and save it.

    New contracts get first_seen = as_of (today by default). Returning
    contracts keep their first_seen and update last_seen, peak premium
    and latest size. Records unseen for BIG_CONTRACT_KEEP_DAYS drop off.
    """
    as_of = as_of or date.today().isoformat()
    cutoff = (date.fromisoformat(as_of)
              - timedelta(days=BIG_CONTRACT_KEEP_DAYS)).isoformat()
    records = {r["key"]: r for r in load_big_contracts()
               if r.get("last_seen", "") >= cutoff}
    for c in snapshot.get("contracts", []):
        premium = _safe_float(c.get("premium"))
        if premium < min_premium:
            continue
        key = contract_key(c)
        ticker = str(c.get("ticker", ""))
        rec = records.get(key)
        if rec is None:
            records[key] = {
                "key": key, "ticker": ticker, "expiry": str(c.get("expiry")),
                "strike": _safe_float(c.get("strike")),
                "type": c.get("type"), "first_seen": as_of,
                "first_premium": round(premium, 2),
                "last_seen": as_of, "peak_premium": round(premium, 2),
                "peak_date": as_of, "days_seen": 1,
                "premium": round(premium, 2),
                "volume": _safe_int(c.get("volume")),
                "open_interest": _safe_int(c.get("open_interest")),
                "last_price": _safe_float(c.get("last_price")),
                "source": "Yahoo Finance options chain",
                "url": f"https://finance.yahoo.com/quote/{ticker}/options",
            }
        else:
            rec["last_seen"] = as_of
            rec["premium"] = round(premium, 2)
            rec["volume"] = _safe_int(c.get("volume"))
            rec["open_interest"] = _safe_int(c.get("open_interest"))
            rec["last_price"] = _safe_float(c.get("last_price"))
            if premium > rec.get("peak_premium", 0):
                rec["peak_premium"] = round(premium, 2)
                rec["peak_date"] = as_of
            try:
                rec["days_seen"] = (date.fromisoformat(as_of)
                                    - date.fromisoformat(rec["first_seen"])).days + 1
            except Exception:
                pass
    out = sorted(records.values(), key=lambda r: r.get("peak_premium", 0), reverse=True)
    save_big_contracts(out)
    return out


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


def index_price_track(tickers: dict, contracts: list[dict]) -> list[dict]:
    """Price tracking for the indexes: spot, previous close, today's move,
    and each index's own flow lean from its unusual contracts."""
    out = []
    for sym in WARI_INDEXES:
        t = tickers.get(sym, {}) or {}
        rows = [c for c in contracts if c.get("ticker") == sym]
        calls = sum(_safe_float(c.get("premium")) for c in rows if c.get("type") == "C")
        puts = sum(_safe_float(c.get("premium")) for c in rows if c.get("type") == "P")
        total = calls + puts
        out.append({
            "ticker": sym,
            "spot": _safe_float(t.get("spot")) or None,
            "prev_close": _safe_float(t.get("prev_close")) or None,
            "change_pct": t.get("change_pct"),
            "contracts": len(rows),
            "call_premium": round(calls, 2), "put_premium": round(puts, 2),
            "call_share": round(calls / total, 3) if total else None,
        })
    return out


def _divergence_score(change_pct, call_share) -> dict:
    """One index (or the benchmark): 0-100, our own documented version.

    Milt's exact WARI formula is paid and unpublished, so this is not a
    copy of his score — same idea, stated plainly: divergence is when
    price moves one way and the premium mix leans the other.
    flow_imbalance = how one-sided the premium mix is (0 = even, 1 = all
    one side). price_component = size of today's move, a 1%+ move counting
    full. Score = 100 * (0.6 * flow imbalance + 0.4 * price move), only
    when price and flow disagree; when they agree the score is 0.
    A high score is a reason to question the move, never a prediction —
    hedges can make the flow look defensive when it is not a directional bet.
    """
    if change_pct is None or call_share is None:
        return {"direction": "No read", "score": None}
    price_up = change_pct >= 0
    flow_up = call_share >= 0.5
    if price_up == flow_up:
        return {"direction": "Agreeing", "score": 0}
    direction = "Bearish divergence" if price_up else "Bullish divergence"
    flow_imbalance = abs(call_share - 0.5) * 2
    price_component = min(abs(change_pct) / 1.0, 1.0)
    score = round(100 * (0.6 * flow_imbalance + 0.4 * price_component))
    return {"direction": direction, "score": score}


def wari_divergence(contracts: list[dict], tickers: dict) -> dict:
    """Whale-price divergence, overall (SPY price vs benchmark premium mix)
    plus one read per index. Overall uses SPY's move against the combined
    SPY/QQQ/IWM premium mix, as in the original WARI."""
    tracked = index_price_track(tickers, contracts)
    bench_calls = sum(t.get("call_premium", 0) for t in tracked)
    bench_puts = sum(t.get("put_premium", 0) for t in tracked)
    bench_total = bench_calls + bench_puts
    bench_share = round(bench_calls / bench_total, 3) if bench_total else None
    spy = next((t for t in tracked if t["ticker"] == "SPY"), {})
    overall = _divergence_score(spy.get("change_pct"), bench_share)
    per_index = []
    for t in tracked:
        d = _divergence_score(t.get("change_pct"), t.get("call_share"))
        per_index.append({**t, **d})
    return {
        "direction": overall["direction"], "score": overall["score"],
        "spy_change_pct": spy.get("change_pct"),
        "benchmark_call_share": bench_share,
        "benchmark_call_premium": round(bench_calls, 2),
        "benchmark_put_premium": round(bench_puts, 2),
        "indexes": per_index,
    }


def load_wari_history() -> list[dict]:
    """Daily WARI log: one record per date, updated intraday by refresh.py."""
    try:
        records = json.loads(WARI_HISTORY_FILE.read_text())
        return [r for r in records if isinstance(r, dict) and r.get("date")]
    except Exception:
        return []


def save_wari_history(records: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    WARI_HISTORY_FILE.write_text(json.dumps(records, indent=2))


def log_wari(contracts: list[dict], tickers: dict, as_of: str | None = None) -> list[dict]:
    """Fold today's divergence read into the daily log (one row per date)."""
    as_of = as_of or date.today().isoformat()
    div = wari_divergence(contracts, tickers)
    records = [r for r in load_wari_history() if r.get("date") != as_of]
    records.append({
        "date": as_of, "direction": div["direction"], "score": div["score"],
        "spy_change_pct": div["spy_change_pct"],
        "benchmark_call_share": div["benchmark_call_share"],
        "benchmark_call_premium": div["benchmark_call_premium"],
        "benchmark_put_premium": div["benchmark_put_premium"],
    })
    records.sort(key=lambda r: r["date"])
    save_wari_history(records)
    return records


PRICE_HISTORY_FILE = DATA / "price_history.json"


def load_price_history() -> list[dict]:
    """The board's own price log: one row per refresh, spot prices for the
    indexes. Written by refresh.py so the price chart does not depend on
    Yahoo's history endpoint, which rate-limits (429) from time to time."""
    try:
        rows = json.loads(PRICE_HISTORY_FILE.read_text())
        return [r for r in rows if isinstance(r, dict) and r.get("ts")]
    except Exception:
        return []


def save_price_history(rows: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    PRICE_HISTORY_FILE.write_text(json.dumps(rows, indent=2))


def log_price_history(tickers: dict, ts: str | None = None) -> list[dict]:
    """Append one price row (spot per index) to the board's own log.
    Keeps the last 30 days; one row per refresh at most."""
    ts = ts or datetime.now(timezone.utc).isoformat()
    rows = [r for r in load_price_history()
            if r.get("ts", "") >= (datetime.now(timezone.utc)
                                   - timedelta(days=30)).isoformat()]
    if rows and rows[-1].get("ts", "")[:16] == ts[:16]:
        return rows  # same minute as the last row — do not duplicate
    row = {"ts": ts}
    for sym in WARI_INDEXES:
        spot = _safe_float((tickers.get(sym, {}) or {}).get("spot"))
        if spot:
            row[sym] = round(spot, 2)
    if len(row) > 1:
        rows.append(row)
        save_price_history(rows)
    return rows


def index_history(symbols: list[str] | None = None, period: str = "1mo",
                  interval: str = "1d"):
    """Price history for the index charts: daily closes, normalised so each
    index starts at 0% — that way the lines compare the moves, not the
    very different prices ($757 SPY vs $278 IWM). Returns (DataFrame, source)
    where source is "yahoo", "board log", or "none".
    Yahoo history is the same free source as the rest of the board, so it
    is ~15-min delayed and can be rate-limited (429); when it gives nothing,
    the chart falls back to the board's own price log, which refresh.py
    writes every 15 minutes in market hours. Either way the caller must
    handle an empty result instead of crashing the page."""
    import yfinance as yf
    import pandas as pd

    symbols = symbols or WARI_INDEXES
    frames = {}
    for sym in symbols:
        try:
            hist = yf.Ticker(sym).history(period=period, interval=interval)
            closes = pd.to_numeric(hist["Close"], errors="coerce").dropna()
            if len(closes) < 2:
                continue
            frames[sym] = (closes / float(closes.iloc[0]) - 1) * 100
        except Exception:
            continue
    if frames:
        df = pd.DataFrame(frames).dropna(how="all")
        df.index = pd.to_datetime(df.index).date
        return df, "yahoo"
    rows = load_price_history()
    if len(rows) >= 2:
        df = pd.DataFrame(rows).set_index("ts")
        df.index = pd.to_datetime(df.index)
        df = df[[c for c in symbols if c in df.columns]].dropna(how="all")
        if len(df) >= 2 and len(df.columns):
            df = (df / df.iloc[0] - 1) * 100
            return df, "board log"
    return pd.DataFrame(), "none"


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
