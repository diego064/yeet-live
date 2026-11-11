"""Diego's AI Money Magnet — friendly UI redesign (Nov 10, 2026).

Same data engine as before (live_data.py, Yahoo Finance, free, no key),
same RAMBO matching rule, same pages underneath. Only the presentation
changed, at Diego's request:

- Today is the home page: one plain-English read first — direction,
  the one contract that matters, and why — then cards, not tables.
- Plain words instead of jargon: "betting up / betting down",
  "fresh opening / closing out", "leans bought / leans sold (a guess)".
- Tabs instead of a sidebar maze. Full tables live behind
  "Show the details" on each tab, with friendly column names.
- No emojis in headings. Direction is shown with words and colour.

Honest limits, unchanged: Yahoo US data is ~15-min delayed, there is
no buy/sell side in the feed, and spread position is a lean, not a
fill. A setup is a research lead, never a trade signal.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from live_data import (
    get_snapshot, load_events, load_snapshot, load_watchlist,
    match_events, pari_percentile, save_events, wari_score,
)

ROOT = Path(__file__).parent
st.set_page_config(page_title="Diego's AI Money Magnet", page_icon="M", layout="wide")

st.markdown("""
<style>
.stApp{background:linear-gradient(145deg,#0a1a2b,#0b2233);color:#eef2ee}
h1{color:#5eead4;font-size:2.2rem!important}
h2{color:#7dd3fc;font-size:1.4rem!important}
h3{color:#d6e8f0;font-size:1.15rem!important}
.read-card{padding:24px 26px;border-radius:20px;margin:10px 0 18px;
  background:linear-gradient(130deg,#16324a,#0f2436);
  border:1px solid #ffffff22;border-left:6px solid #2dd4bf}
.read-card.down{border-left-color:#ff7a7a}
.read-card.mixed{border-left-color:#f4d47c}
.read-card h2{margin:0 0 6px;font-size:1.6rem!important;color:#eef2ee!important}
.read-card p{color:#c3d4e2;font-size:15px;line-height:1.6;margin:6px 0}
.setup-card{padding:18px 20px;border-radius:16px;margin:10px 0;
  background:linear-gradient(130deg,#122c3f,#0d1f2e);
  border:1px solid #ffffff18}
.setup-card h3{margin:2px 0 6px}
.badge{display:inline-block;border-radius:20px;padding:4px 12px;font-size:12px;
  font-weight:700;margin:4px 6px 4px 0}
.badge-up{background:#0f3d4a;color:#67e8f9}
.badge-down{background:#5a2b2b;color:#ffb3b3}
.badge-mixed{background:#54491f;color:#ffe9a8}
.badge-plain{background:#1c2f45;color:#cfe8f5}
.small{color:#9db8c8;font-size:13px;line-height:1.5}
.big{font-size:26px;font-weight:800;color:#7dd3fc}
[data-testid="stMetric"]{background:#10283a;border:1px solid #ffffff16;
  padding:14px;border-radius:14px}
</style>
""", unsafe_allow_html=True)

# ---------- data (cached 5 min, same as before) ----------
if "watchlist" not in st.session_state:
    st.session_state.watchlist = load_watchlist()
if "added_tickers" not in st.session_state:
    st.session_state.added_tickers = []

# A newly added ticker must be scanned right away, not served the
# cached snapshot: get_snapshot() returns the saved snapshot while it
# is fresh (<5 min), which used to swallow the new ticker entirely.
force_refresh = st.session_state.pop("force_refresh_once", False)

with st.spinner("Getting the latest flow…"):
    try:
        snap = get_snapshot(st.session_state.watchlist, force_refresh=force_refresh)
    except Exception as exc:
        snap = load_snapshot() or {"contracts": [], "tickers": {}, "fetched_at": "never",
                                   "total_call_premium": 0, "total_put_premium": 0,
                                   "earnings_events": []}
        st.warning(f"Live fetch failed ({exc}). Showing the last saved snapshot — it may be stale.")

contracts = snap.get("contracts", [])
tickers = snap.get("tickers", {})
events = load_events()
all_events = events + snap.get("earnings_events", [])
today = date.today().isoformat()
today_dt = date.today()


def money(v: float) -> str:
    return f"${v:,.0f}"


def friendly_date(iso: str) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
    except Exception:
        return str(iso)
    days = (d - today_dt).days
    when = "Today" if days == 0 else "Tomorrow" if days == 1 else f"In {days} days" if days > 0 else f"{-days} days ago"
    return f"{d.strftime('%b %d, %Y')} ({when})"


def bet_word(c: dict) -> str:
    return "Betting up" if c.get("type") == "C" else "Betting down"


def fresh_word(c: dict) -> str:
    ratio = c.get("vol_oi") or 0
    if ratio >= 3:
        return "Fresh opening — volume is well above open interest, so new positions are likely being opened"
    if ratio >= 1:
        return "Likely fresh opening — volume is above open interest"
    if ratio >= 0.5:
        return "Mixed — some opening, some closing"
    return "Likely closing out — volume is well below open interest"


def spread_word(c: dict) -> str:
    pos = c.get("spread_position")
    if pos is None:
        return "No spread read available"
    if pos >= 0.75:
        return "Leans bought — last price near the ask (a guess, not a fill)"
    if pos <= 0.25:
        return "Leans sold — last price near the bid (a guess, not a fill)"
    return "Spread read is mixed — no clear lean"


def contract_sentence(c: dict) -> str:
    kind = "call" if c.get("type") == "C" else "put"
    return (f"{c['ticker']} {c['strike']:g} {kind}, expiring {friendly_date(c['expiry'])} — "
            f"{money(c['premium'])} in premium. {fresh_word(c)}. {spread_word(c)}.")


def setup_card(c: dict):
    up = c.get("type") == "C"
    badge = "badge-up" if up else "badge-down"
    st.markdown(f"""<div class="setup-card">
    <span class="badge {badge}">{bet_word(c)}</span>
    <span class="badge badge-plain">{money(c['premium'])}</span>
    <span class="badge badge-plain">{friendly_date(c['expiry'])}</span>
    <h3>{c['ticker']} ${c['strike']:g} {'call' if up else 'put'}</h3>
    <p class="small">{fresh_word(c)}.<br>{spread_word(c)}.<br>
    Volume {c.get('volume', 0):,} vs open interest {c.get('open_interest', 0):,}.
    Last price ${c.get('last_price', 0):.2f}.</p></div>""", unsafe_allow_html=True)


def details_table(rows: list[dict], label: str = "Show the details"):
    if not rows:
        st.info("Nothing here right now.")
        return
    with st.expander(label):
        df = pd.DataFrame(rows)
        rename = {"ticker": "Ticker", "expiry": "Expires", "strike": "Strike",
                  "type": "Bet", "moneyness": "In or out of the money",
                  "volume": "Volume", "open_interest": "Open interest",
                  "vol_oi": "Volume vs open interest", "last_price": "Last price",
                  "bid": "Bid", "ask": "Ask", "spread_position": "Spread position (0 = bid, 1 = ask)",
                  "premium": "Premium ($)", "implied_volatility": "Implied volatility",
                  "days_to_expiry": "Days to expiry"}
        df = df.rename(columns=rename)
        df["Bet"] = df["Bet"].replace({"C": "Up (call)", "P": "Down (put)"})
        cols = [c for c in ["Ticker", "Expires", "Strike", "Bet", "In or out of the money",
                            "Volume", "Open interest", "Volume vs open interest",
                            "Last price", "Bid", "Ask", "Spread position (0 = bid, 1 = ask)",
                            "Premium ($)", "Implied volatility", "Days to expiry"]
                if c in df.columns]
        st.dataframe(df[cols], hide_index=True, width="stretch")
        st.caption("Premium = volume x last price x 100. Yahoo does not publish whether a "
                   "trade was bought or sold, so that is never invented. Spread position "
                   "is a lean only — near the ask leans bought, near the bid leans sold.")


# ---------- settings (sidebar, always available) ----------
with st.sidebar:
    st.markdown("### Settings")
    st.caption(f"{len(st.session_state.watchlist)} tickers scanned each refresh.")
    new_ticker = st.text_input("Add a ticker", placeholder="e.g. NVDA").strip().upper().lstrip("$")
    if st.button("Add ticker") and new_ticker:
        if new_ticker in st.session_state.watchlist:
            st.session_state.add_ticker_msg = f"{new_ticker} is already on the list."
        else:
            st.session_state.watchlist.append(new_ticker)
            st.session_state.added_tickers.append(new_ticker)
            try:
                (ROOT / "data" / "watchlist.json").write_text(
                    json.dumps(st.session_state.watchlist, indent=2))
            except Exception:
                pass  # Streamlit Cloud disk is ephemeral; session list still works
            st.session_state.force_refresh_once = True
            st.session_state.add_ticker_msg = f"Added {new_ticker} — scanning it now."
        st.rerun()
    if st.session_state.get("add_ticker_msg"):
        st.success(st.session_state.add_ticker_msg)
    if st.session_state.added_tickers:
        st.caption("Added this visit: " + ", ".join(st.session_state.added_tickers) +
                   " — these stay until you close or reload the app. "
                   "To keep one permanently, tell me and I'll add it to the repo list.")
        last = st.session_state.added_tickers[-1]
        n = len([c for c in contracts if c.get("ticker") == last])
        if last in tickers:
            st.caption(f"{last}: {n} unusual contract(s) in the latest scan." if n
                       else f"{last}: no unusual flow in the latest scan — "
                            "it will show up when it has some.")
    if st.button("Refresh now"):
        get_snapshot(st.session_state.watchlist, force_refresh=True)
        st.rerun()
    st.caption("The app caches data for 5 minutes, then pulls fresh data on the next load. "
               "A scheduled refresh also runs every 15 minutes on weekdays.")

# ---------- header: the read, first ----------
st.title("Diego's AI Money Magnet")
st.caption("Options flow, in plain English. The flow pulls you toward the big money. Free data from Yahoo Finance — about 15 minutes delayed.")

fetched = snap.get("fetched_at", "?")
try:
    fetched_dt = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
    age_min = max(0, int((datetime.now(fetched_dt.tzinfo) - fetched_dt).total_seconds() // 60))
    fresh_line = f"Updated {age_min} min ago" if age_min else "Updated just now"
except Exception:
    fresh_line = f"Last update: {fetched}"
st.caption(f"{fresh_line} · {snap.get('tickers_scanned', '?')} tickers scanned · "
           f"{len(contracts)} unusual contracts found")

# Today's read: direction from call/put premium, lead contract, why
call_p = snap.get("total_call_premium", 0) or 0
put_p = snap.get("total_put_premium", 0) or 0
total_p = call_p + put_p
call_share = (call_p / total_p * 100) if total_p else None
top = sorted(contracts, key=lambda c: c["premium"], reverse=True)
lead = top[0] if top else None
spy = tickers.get("SPY", {})
spy_chg = spy.get("change_pct")

if lead and call_share is not None:
    if call_share >= 60:
        direction, cls = "Betting up today", ""
        why = (f"Most of the big money is in calls — {call_share:.0f}% of the unusual premium "
               f"is betting up, vs {100 - call_share:.0f}% betting down.")
    elif call_share <= 40:
        direction, cls = "Betting down today", "down"
        why = (f"Most of the big money is in puts — {100 - call_share:.0f}% of the unusual premium "
               f"is betting down, vs {call_share:.0f}% betting up.")
    else:
        direction, cls = "Split today — no clear direction", "mixed"
        why = (f"The big money is split — {call_share:.0f}% betting up, "
               f"{100 - call_share:.0f}% betting down. No side is dominating.")
    spy_line = f" SPY is {spy_chg:+.1f}% today." if spy_chg is not None else ""
    st.markdown(f"""<div class="read-card {cls}"><h2>{direction}</h2>
    <p><strong>The one that matters:</strong> {contract_sentence(lead)}</p>
    <p>{why}{spy_line}</p>
    <p class="small">Research lead, not a trade signal. Data is about 15 minutes delayed.</p>
    </div>""", unsafe_allow_html=True)
else:
    st.info("No unusual flow in the latest snapshot yet. Check back after the next refresh.")

# ---------- tabs ----------
tab_today, tab_flow, tab_events, tab_mood, tab_semis, tab_memes, tab_about = st.tabs(
    ["Today", "Big flow", "Events", "Market mood", "Semis & China", "Memes", "About"])

with tab_today:
    st.subheader("Today at a glance")
    c1, c2, c3 = st.columns(3)
    c1.metric("Money betting up", money(call_p))
    c2.metric("Money betting down", money(put_p))
    c3.metric("SPY today", f"{spy_chg:+.1f}%" if spy_chg is not None else "—")
    st.subheader("The biggest setups")
    for c in top[:3]:
        setup_card(c)
    details_table(top[:30], "Show the full list (top 30)")
    st.subheader("Events coming up")
    results = match_events(events, contracts, today)
    if results:
        for ev in results[:5]:
            up = ev["direction"] == "Call-side"
            badge = "badge-up" if up else "badge-down" if ev["direction"] == "Put-side" else "badge-mixed"
            label = {"Call-side": "Betting up", "Put-side": "Betting down",
                     "Mixed": "Betting both ways"}.get(ev["direction"], ev["direction"])
            st.markdown(f"""<div class="setup-card">
            <span class="badge {badge}">{label}</span>
            <span class="badge badge-plain">{friendly_date(ev['date'])}</span>
            <h3>{ev['ticker']} — {ev['title']}</h3>
            <p class="small">{ev.get('summary', '')}</p></div>""", unsafe_allow_html=True)
    else:
        st.info("No company event lines up with live flow right now.")

with tab_flow:
    st.subheader("Big flow")
    st.write("The biggest unusual contracts right now, ranked by premium. "
             "Unusual means volume is high compared with open interest — "
             "in plain terms, a lot of new activity in that contract today.")
    for c in top[:6]:
        setup_card(c)
    details_table(top[:30], "Show the full table (top 30)")

with tab_events:
    st.subheader("Events")
    st.write("Company events — analyst days, conferences, meetings — lined up with "
             "options that cover the event date. Earnings are listed separately and "
             "are not counted here, as in the original.")
    results = match_events(events, contracts, today)
    query = st.text_input("Find a ticker", placeholder="e.g. NVDA").strip().upper().lstrip("$")
    if query:
        results = [r for r in results if query in r["ticker"]]
    if not results:
        st.info("Nothing lines up here yet — no live unusual contract covers an upcoming confirmed event.")
    for ev in results:
        label = {"Call-side": "Betting up", "Put-side": "Betting down",
                 "Mixed": "Betting both ways"}.get(ev["direction"], ev["direction"])
        badge = "badge-up" if ev["direction"] == "Call-side" else "badge-down" if ev["direction"] == "Put-side" else "badge-mixed"
        st.markdown(f"""<div class="setup-card">
        <span class="badge {badge}">{label}</span>
        <span class="badge badge-plain">{friendly_date(ev['date'])}</span>
        <h3>{ev['ticker']} — {ev['title']}</h3>
        <p class="small">{ev.get('summary', '')}</p></div>""", unsafe_allow_html=True)
        with st.expander(f"{ev['ticker']}: contracts and source"):
            st.write(ev.get("note", ""))
            st.link_button("Official company source", ev["url"])
            details_table(ev["prints"], "Show the contracts for this event")
    st.divider()
    st.subheader("Add an event")
    st.caption("Conference and analyst-day dates are not in any free feed, so this is the one manual piece. Add one and it joins this list instantly.")
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
                            "summary": summary, "note": "Added in Diego's AI Money Magnet."})
                save_events(evs)
                st.success("Event added.")
                st.rerun()
            else:
                st.error("Need a ticker, a title, and an https:// source URL.")

with tab_mood:
    st.subheader("Market mood")
    st.write("Two simple reads on the market, in plain words.")
    frames = [("Last 5 minutes", "5d", "5m"), ("Last hour", "1mo", "60m"),
              ("Today", "1y", "1d")]
    st.markdown("**SPY momentum — where is the price compared with its recent range?**")
    for label, period, interval in frames:
        r = pari_percentile("SPY", period, interval)
        if r:
            st.write(f"{label}: **{r['posture']}** — {r['percentile']:.0f} out of 100. "
                     f"SPY ${r['latest']:,.2f}, {r['change_pct']:+.1f}% over the period.")
            st.progress(min(100, max(0, int(r["percentile"]))) / 100)
        else:
            st.write(f"{label}: no data right now.")
    st.caption("Above 65 leans bullish, below 35 leans bearish, in between is neutral. "
               "Calculated from Yahoo closing prices only.")
    w = wari_score(contracts)
    st.markdown("**Whale mood — are the big contracts betting up or down?**")
    if w["call_share"] is not None:
        st.write(f"{w['call_share'] * 100:.0f}% of the unusual premium is betting up, "
                 f"{w['put_share'] * 100:.0f}% betting down. "
                 f"Total scanned: {money(w['total_premium'])}.")
        st.progress(w["call_share"])
        if spy_chg is not None:
            agree = (w["call_share"] >= 0.5 and spy_chg >= 0) or (w["call_share"] < 0.5 and spy_chg < 0)
            st.write("The flow and the market agree today." if agree else
                     "The flow and the market disagree today — the big money is betting one way while the market moves the other.")
    else:
        st.write("No flow to read right now.")

with tab_semis:
    st.subheader("Semis & China")
    st.write("Semiconductor names and China, against the market.")
    for label, group in [("Semis", ["SMH", "NVDA", "AMD", "AVGO", "TSM", "MU"]),
                         ("China", ["BABA"]),
                         ("Market", ["SPY", "QQQ"])]:
        rows = [c for c in contracts if c["ticker"] in group]
        calls = sum(c["premium"] for c in rows if c["type"] == "C")
        puts = sum(c["premium"] for c in rows if c["type"] == "P")
        st.markdown(f"**{label}** — {money(calls)} betting up / {money(puts)} betting down")
        for c in sorted(rows, key=lambda x: x["premium"], reverse=True)[:3]:
            setup_card(c)
        details_table(sorted(rows, key=lambda x: x["premium"], reverse=True)[:15],
                      f"Show all {label} contracts")

with tab_memes:
    st.subheader("Memes")
    st.write("The high-energy names: GME, AMC and the high-beta crowd.")
    cols = st.columns(6)
    for col, s in zip(cols, ["GME", "AMC", "PLTR", "COIN", "HOOD", "TSLA"]):
        with col:
            r = tickers.get(s, {})
            spot, chg = r.get("spot"), r.get("change_pct")
            st.metric(s, f"${spot:,.2f}" if spot else "—",
                      f"{chg:+.1f}%" if chg is not None else None)
    rows = [c for c in contracts if c["ticker"] in {"GME", "AMC", "PLTR", "COIN", "HOOD", "TSLA"}]
    for c in sorted(rows, key=lambda x: x["premium"], reverse=True)[:6]:
        setup_card(c)
    details_table(sorted(rows, key=lambda x: x["premium"], reverse=True)[:30],
                  "Show the full meme table")

with tab_about:
    st.subheader("About this board")
    st.write("Diego's AI Money Magnet is a free recreation of the YEET Terminal. The original was fed by "
             "paid options-flow uploads and a hand-updated snapshot. This version pulls live "
             "options data from Yahoo Finance — free, no key — and refreshes every few minutes.")
    st.markdown("**How to read it**")
    st.write("- **Betting up** means calls, **betting down** means puts. A call usually pays if the stock rises; a put usually pays if it falls or as protection.")
    st.write("- **Fresh opening** means volume is well above open interest, so new positions are likely being opened. **Closing out** means the opposite.")
    st.write("- **Leans bought / sold** is a guess from where the last price sits between the bid and the ask. It is never a confirmed fill.")
    st.write("- **Events** are company meetings and conferences lined up with options that cover the event date. Earnings are listed in the calendar, not counted as event matches.")
    st.markdown("**The limits, honestly**")
    st.write("- Data is about 15 minutes delayed. There is no free, real-time options-flow feed.")
    st.write("- Yahoo does not show whether a trade was bought or sold, so that is never invented.")
    st.write("- A big contract is a research lead, not a trade signal.")
    st.markdown("**Calendar**")
    upcoming = sorted([e for e in all_events if e["date"] >= today],
                      key=lambda e: (e["date"], e["ticker"]))
    for ev in upcoming[:15]:
        st.write(f"{ev['date']} — {ev['ticker']}: {ev['title']} "
                 f"({'Earnings' if ev.get('earnings_related') else ev['category']})")
    st.divider()
    st.caption("Changelog: Nov 10 — friendly redesign. Same data and matching, new Today-first layout with plain-English cards.")
