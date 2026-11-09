"""YEET Live — free, automated recreation of YEET Terminal.

Original: Streamlit dashboard fed by paid Unusual Whales CSV uploads
and a hand-committed snapshot.json (Oct 2026). Pages: Prestige, RAMBO,
PARI, RAMBO Calendar, Index WARI, Orange Swan, MEME LAB, Changelog.

This version: same pages, same RAMBO matching rule, but the flow is
pulled live from Yahoo Finance (free, no key) on every load / refresh,
with a 5-minute cache. No uploads, no manual snapshot commits.
See live_data.py for the honest limits: Yahoo US data is ~15-min
delayed, options "side" is not available (so it is never invented),
and "unusual" means volume-vs-open-interest on the public chain.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from live_data import (
    get_snapshot, load_events, load_snapshot, load_watchlist,
    match_events, pari_percentile, save_events, wari_score,
)

ROOT = Path(__file__).parent
st.set_page_config(page_title="YEET Live | Free Terminal", page_icon="🚀", layout="wide")

st.markdown("""
<style>
.stApp{background:linear-gradient(145deg,#0f212f,#0d242b);color:#eee9dd}
[data-testid="stSidebar"]{background:linear-gradient(180deg,#102632,#0c1e28)}
h1{color:#6ee947} h2{color:#73ed49} h3{color:#1cdfa0}
.card{padding:20px;border:1px solid #ffffff16;border-top:2px solid #13dca077;border-radius:18px;background:linear-gradient(130deg,#142f39,#102630);margin:8px 0 16px}
.pill{display:inline-block;border-radius:20px;background:#293f36;color:#c1f4d6;padding:5px 12px;font-size:12px;margin:6px 5px 6px 0}
.ticker{font:900 28px Arial,sans-serif;color:#89ed35}
.muted{color:#b5bcc8;font-size:14px;line-height:1.6}
.money{font-size:28px;color:#f4e4a7;font-weight:700}
</style>
""", unsafe_allow_html=True)

PAGES = ["Prestige", "RAMBO", "PARI", "RAMBO Calendar", "Index WARI",
         "Orange Swan", "MEME LAB", "Changelog"]

if "page" not in st.session_state:
    st.session_state.page = "RAMBO"
if "watchlist" not in st.session_state:
    st.session_state.watchlist = load_watchlist()

# ---------- sidebar: automation controls ----------
with st.sidebar:
    st.markdown("## 🚀 YEET Live")
    st.caption("Free + automated. Yahoo Finance, no key, no uploads.")
    for p in PAGES:
        if st.button(p, key=f"nav_{p}", use_container_width=True,
                     type="primary" if st.session_state.page == p else "secondary"):
            st.session_state.page = p
    st.divider()
    st.markdown("### Watchlist")
    st.caption(f"{len(st.session_state.watchlist)} tickers scanned each refresh.")
    new_ticker = st.text_input("Add ticker", placeholder="e.g. NVDA").strip().upper().lstrip("$")
    if st.button("Add ticker") and new_ticker:
        if new_ticker not in st.session_state.watchlist:
            st.session_state.watchlist.append(new_ticker)
            (ROOT / "data" / "watchlist.json").write_text(
                json.dumps(st.session_state.watchlist, indent=2))
        st.rerun()
    if st.button("Refresh live data now"):
        get_snapshot(st.session_state.watchlist, force_refresh=True)
        st.rerun()
    st.caption("Auto-refresh: the app caches for 5 min, then re-pulls on the next load. "
               "For hands-off updates, run refresh.py on a schedule — see README.")

# ---------- live data (cached 5 min) ----------
with st.spinner("Pulling live options data from Yahoo Finance…"):
    try:
        snap = get_snapshot(st.session_state.watchlist)
    except Exception as exc:
        snap = load_snapshot() or {"contracts": [], "tickers": {}, "fetched_at": "never",
                                   "total_call_premium": 0, "total_put_premium": 0,
                                   "earnings_events": []}
        st.warning(f"Live fetch failed ({exc}). Showing last saved snapshot — prices may be stale.")

fetched = snap.get("fetched_at", "?")
age = snap.get("_age_seconds", 0)
st.caption(f"Live data: {snap.get('source', 'Yahoo Finance')} · fetched {fetched} "
           f"· {snap.get('tickers_scanned', '?')} tickers, "
           f"{snap.get('tickers_ok', '?')} OK · "
           f"{len(snap.get('contracts', []))} unusual contracts · "
           f"age {int(age)}s {'(stale — refresh)' if snap.get('_stale') else ''}")
st.caption("Free data is ~15-min delayed for US stocks. This is not tick-by-tick "
           "whale flow — no free feed offers that. Unusual = volume vs open interest "
           "on the public options chain.")

contracts = snap.get("contracts", [])
tickers = snap.get("tickers", {})
events = load_events()
all_events = events + snap.get("earnings_events", [])
today = date.today().isoformat()
page = st.session_state.page


def money(v: float) -> str:
    return f"${v:,.0f}"


def contract_table(rows: list[dict]):
    if not rows:
        st.info("No unusual contracts in this view right now.")
        return
    df = pd.DataFrame(rows)
    cols = [c for c in ["ticker", "expiry", "strike", "type", "moneyness",
                        "volume", "open_interest", "vol_oi", "last_price",
                        "premium", "implied_volatility", "days_to_expiry"]
            if c in df.columns]
    st.dataframe(df[cols], hide_index=True, use_container_width=True)
    st.caption("Side is not shown: Yahoo does not publish buy/sell side, so it is never invented. "
               "Premium = volume × last price × 100.")


def quote_row(symbol: str):
    r = tickers.get(symbol, {})
    spot = r.get("spot")
    chg = r.get("change_pct")
    if spot:
        st.metric(symbol, f"${spot:,.2f}", f"{chg:+.2f}%" if chg is not None else None)
    else:
        st.metric(symbol, "—", "no quote")


# ---------- pages ----------
if page == "Prestige":
    st.markdown("# 🥇 Prestige")
    st.markdown("The biggest live unusual contracts, ranked by premium — "
                "computed fresh from the public chain, not a hand-published list.")
    c1, c2, c3 = st.columns(3)
    c1.metric("Unusual contracts", len(contracts))
    c2.metric("Call premium", money(snap.get("total_call_premium", 0)))
    c3.metric("Put premium", money(snap.get("total_put_premium", 0)))
    top = sorted(contracts, key=lambda c: c["premium"], reverse=True)[:30]
    contract_table(top)

elif page == "RAMBO":
    st.markdown("# 🪃 RAMBO")
    st.markdown("**Relations and Meeting Buy Opportunities** — company events lined up "
                "with options that cover the event date. Same rule as the original: "
                "confirmed, non-earnings events only; contract must expire on/after the event.")
    results = match_events(events, contracts, today)
    c1, c2, c3 = st.columns(3)
    c1.metric("Events to watch", len(results))
    c2.metric("Companies in live flow", len({c["ticker"] for c in contracts}))
    c3.metric("Unusual contracts", len(contracts))
    query = st.text_input("Find a ticker", placeholder="$SMTC, $NVTS…").strip().upper().lstrip("$")
    if query:
        results = [r for r in results if query in r["ticker"]]
    if not results:
        st.info("Nothing lines up here yet — no live unusual contract covers an upcoming "
                "confirmed event. Try the Prestige board, or add the ticker in the sidebar.")
    for ev in results:
        st.markdown(f"""<div class="card"><span class="ticker">${ev['ticker']}</span>
        <h3>{ev['title']}</h3>
        <span class="pill">✅ Confirmed event</span>
        <span class="pill">{ev['direction']}</span>
        <div class="muted">📅 <strong>{ev['date']}</strong> · {ev.get('time', '')} ·
        {ev['days_until']} days away</div>
        <p class="muted">{ev.get('summary', '')}</p></div>""", unsafe_allow_html=True)
        with st.expander(f"{ev['ticker']}: contracts and source"):
            st.write(ev.get("note", ""))
            st.link_button("Official company source", ev["url"])
            contract_table(ev["prints"])
    st.divider()
    st.markdown("### Add an event")
    st.caption("Events are the one manual piece left — conference / analyst-day dates "
               "aren't in any free structured feed. Add one and it joins RAMBO instantly.")
    with st.form("add_event"):
        t = st.text_input("Ticker").strip().upper()
        title = st.text_input("Event name")
        d = st.date_input("Date", value=date.today())
        url = st.text_input("Official source URL (https://…)")
        summary = st.text_area("What happens at the event")
        if st.form_submit_button("Add event"):
            if t and title and url.startswith("https://"):
                evs = load_events()
                evs.append({"ticker": t, "title": title, "date": d.isoformat(),
                            "end_date": d.isoformat(), "time": "Time not announced",
                            "category": "Investor / analyst", "status": "Confirmed",
                            "earnings_related": False, "url": url,
                            "summary": summary, "note": "Added in YEET Live."})
                save_events(evs)
                st.success("Event added.")
                st.rerun()
            else:
                st.error("Need a ticker, a title, and an https:// source URL.")

elif page == "PARI":
    st.markdown("# 🧭 PARI")
    st.markdown("SPY momentum percentile from Yahoo closes only — where is the latest "
                "close in its own recent range? 65+ = bullish, 35− = bearish.")
    frames = [("5-minute", "5d", "5m"), ("10-minute", "5d", "15m"),
              ("1-hour", "1mo", "60m"), ("4-hour", "3mo", "60m"),
              ("Daily", "1y", "1d")]
    cols = st.columns(len(frames))
    for col, (label, period, interval) in zip(cols, frames):
        r = pari_percentile("SPY", period, interval)
        with col:
            if r:
                st.metric(label, f"{r['percentile']:.0f}/100",
                          f"{r['posture']} · ${r['latest']:,.2f} · {r['change_pct']:+.1f}%")
            else:
                st.metric(label, "—", "no data")
    st.caption("Intraday frames use the last 5 days / 1 month of Yahoo candles; "
               "daily uses 1 year. No synthetic scores — empty means the fetch failed.")

elif page == "RAMBO Calendar":
    st.markdown("# 📅 RAMBO Calendar")
    st.markdown("Upcoming confirmed events + Yahoo earnings dates, by month.")
    upcoming = sorted([e for e in all_events if e["date"] >= today],
                      key=lambda e: (e["date"], e["ticker"]))
    if not upcoming:
        st.info("No upcoming events in the list.")
    month = None
    for ev in upcoming:
        m = ev["date"][:7]
        if m != month:
            st.markdown(f"## {datetime.fromisoformat(ev['date']).strftime('%B %Y')}")
            month = m
        st.markdown(f"""<div class="card"><span class="ticker">${ev['ticker']}</span>
        <h3>{ev['title']}</h3><div class="muted">📅 {ev['date']} · {ev['category']} ·
        {'Earnings — excluded from RAMBO' if ev.get('earnings_related') else 'RAMBO-eligible'}</div></div>""",
                    unsafe_allow_html=True)

elif page == "Index WARI":
    st.markdown("# 🐋 Index WARI")
    st.markdown("Whale-vs-index disagreement, from live public flow: call-premium share "
                "of all scanned flow vs the SPY/QQQ benchmark, set against SPY's move.")
    w = wari_score(contracts)
    spy = tickers.get("SPY", {})
    st.metric("Call share (all flow)", f"{w['call_share']*100:.0f}%" if w["call_share"] is not None else "—")
    st.metric("Call share (SPY/QQQ)", f"{w['benchmark_call_share']*100:.0f}%" if w["benchmark_call_share"] is not None else "—")
    st.metric("SPY today", f"{spy.get('change_pct', 0):+.2f}%" if spy.get("change_pct") is not None else "—")
    st.write(f"Total unusual premium scanned: **{money(w['total_premium'])}** "
             f"({money(w['call_premium'])} calls / {money(w['put_premium'])} puts).")
    st.caption("Original WARI compared proprietary flow against index price history. "
               "This free version uses the same idea on public-chain premium — "
               "a call-heavy flow on a falling SPY is disagreement; call-heavy on a "
               "rising SPY is agreement. No 0–100 score is invented when the inputs are thin.")

elif page == "Orange Swan":
    st.markdown("# 🍊 Orange Swan")
    st.markdown("Semis + China vs the market — live call/put premium for SMH names and BABA.")
    semis = ["SMH", "NVDA", "AMD", "AVGO", "TSM", "MU"]
    china = ["BABA"]
    for label, group in [("Semis", semis), ("China", china), ("Market", ["SPY", "QQQ"])]:
        rows = [c for c in contracts if c["ticker"] in group]
        calls = sum(c["premium"] for c in rows if c["type"] == "C")
        puts = sum(c["premium"] for c in rows if c["type"] == "P")
        st.markdown(f"### {label} — {money(calls)} calls / {money(puts)} puts")
        contract_table(sorted(rows, key=lambda c: c["premium"], reverse=True)[:15])

elif page == "MEME LAB":
    st.markdown("# 🚀 MEME LAB")
    st.markdown("Meme names, live: GME, AMC and the high-beta crowd.")
    for s in ["GME", "AMC", "PLTR", "COIN", "HOOD", "MSTR"]:
        pass
    cols = st.columns(6)
    for col, s in zip(cols, ["GME", "AMC", "PLTR", "COIN", "HOOD", "TSLA"]):
        with col:
            quote_row(s)
    rows = [c for c in contracts if c["ticker"] in {"GME", "AMC", "PLTR", "COIN", "HOOD", "TSLA"}]
    contract_table(sorted(rows, key=lambda c: c["premium"], reverse=True)[:30])

else:  # Changelog
    st.markdown("# 📝 Changelog")
    st.markdown("""
    **Nov 8, 2026 — YEET Live (free recreation)**
    - Replaced paid Unusual Whales CSV uploads + hand-committed snapshot.json with a live Yahoo Finance pull (free, no key), cached 5 minutes.
    - RAMBO matching kept identical; events seed carried over from the original (53 confirmed events) + Yahoo earnings dates auto-added.
    - PARI, WARI, Orange Swan and MEME LAB now compute from the same live public chain instead of saved paid-flow files.
    - Automation: `refresh.py` + GitHub Actions (every 15 min, weekdays) refresh `data/live_snapshot.json` with no manual step.
    - Honest limits: ~15-min delayed US data, no buy/sell side, no tick flow. See README.
    """)
    st.caption("Original YEET Terminal: RAMBO dashboard, Oct 14 2026 snapshot, UW CSV import, "
               "GitHub Actions source-page checks. This is an independent free recreation, not the original repo.")
