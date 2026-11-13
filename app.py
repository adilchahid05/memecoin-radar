import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="MemeCoin Radar", page_icon="📡", layout="wide")
G = "https://api.geckoterminal.com/api/v2"
D = "https://api.dexscreener.com"
HEADERS = {"Accept": "application/json;version=20230203", "User-Agent": "MemeCoinRadar/1.0"}


def request_json(url, params=None, headers=None):
    res = requests.get(url, params=params, headers=headers or {"User-Agent": "MemeCoinRadar/1.0"}, timeout=12)
    res.raise_for_status()
    return res.json()


def number(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except (ValueError, TypeError):
        return default


def epoch_age_minutes(when):
    try:
        ts = dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() / 60)
    except (ValueError, TypeError, AttributeError):
        return None


@st.cache_data(ttl=65, show_spinner=False)
def new_pools(network, pages):
    pools = []
    for page in range(1, pages + 1):
        payload = request_json(f"{G}/networks/{network}/new_pools", {"page": page}, HEADERS)
        pools.extend(payload.get("data") or [])
    return pools


@st.cache_data(ttl=60, show_spinner=False)
def dex_pairs(chain, addresses):
    addresses = tuple(dict.fromkeys(a for a in addresses if a))[:30]
    if not addresses:
        return []
    return request_json(f"{D}/tokens/v1/{chain}/{','.join(addresses)}")


def get_base_address(pool):
    rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
    token_id = rel.get("id", "")
    # GeckoTerminal identifiers are network_contractaddress.
    return token_id.split("_", 1)[1] if "_" in token_id else ""


def score_row(row, min_liq):
    score = 0
    reasons = []
    liq, vol, buys, sells, delta, age = row["Liquidity ($)"], row["5m volume ($)"], row["5m buys"], row["5m sells"], row["5m change (%)"], row["Age (min)"]
    ratio = buys / max(1, sells)
    if liq >= min_liq:
        score += 25
        reasons.append("liquidity threshold passed")
    if vol >= 5000:
        score += 20
        reasons.append("5m volume ≥ $5k")
    if buys >= 20 and ratio >= 1.5:
        score += 25
        reasons.append("buy transaction momentum")
    if 1 <= delta <= 30:
        score += 20
        reasons.append("positive but not extreme 5m change")
    if 2 <= age <= 30:
        score += 10
        reasons.append("young pool")
    # These hard gates are risk-reduction heuristics, NOT safety guarantees.
    gates = age >= 2 and age <= 30 and liq >= min_liq and vol >= 5000 and buys >= 20 and ratio >= 1.5 and 1 <= delta <= 30
    # No security or holder analysis in this MVP: intentionally never claim VERIFIED or safe to buy.
    signal = "SETUP — verify security" if gates else ("WATCH" if score >= 40 else "PASS")
    return score, signal, "; ".join(reasons) or "Thresholds not met"


def scan(network, pages, min_liq, max_age):
    pools = new_pools(network, pages)
    token_addresses = []
    pool_by_token = {}
    for pool in pools:
        addr = get_base_address(pool)
        if addr:
            pool_by_token.setdefault(addr, []).append(pool)
            token_addresses.append(addr)
    pair_data = []
    unique = list(dict.fromkeys(token_addresses))
    for i in range(0, min(90, len(unique)), 30):
        pair_data.extend(dex_pairs(network, tuple(unique[i:i+30])))
    by_base = {}
    for pair in pair_data:
        addr = (pair.get("baseToken") or {}).get("address", "")
        if addr in pool_by_token:
            if addr not in by_base or number((pair.get("liquidity") or {}).get("usd")) > number((by_base[addr].get("liquidity") or {}).get("usd")):
                by_base[addr] = pair
    rows = []
    for addr, pool_list in pool_by_token.items():
        pool = min(pool_list, key=lambda x: (x.get("attributes") or {}).get("pool_created_at") or "9999")
        att = pool.get("attributes") or {}
        age = epoch_age_minutes(att.get("pool_created_at"))
        if age is None or age > max_age:
            continue
        pair = by_base.get(addr, {})
        tx = pair.get("txns") or {}
        change = pair.get("priceChange") or {}
        vols = pair.get("volume") or {}
        liq = pair.get("liquidity") or {}
        # Missing data stays missing; never substitute an unverified signal.
        if not pair or "m5" not in tx or "m5" not in change:
            continue
        m5 = tx.get("m5") or {}
        row = {
            "Token": ((pair.get("baseToken") or {}).get("symbol") or att.get("name") or "Unknown")[:24],
            "Address": addr,
            "Age (min)": round(age, 1),
            "Price ($)": number(pair.get("priceUsd")),
            "Liquidity ($)": number(liq.get("usd")),
            "5m volume ($)": number(vols.get("m5")),
            "5m buys": int(number(m5.get("buys"))),
            "5m sells": int(number(m5.get("sells"))),
            "5m change (%)": number(change.get("m5")),
            "Market cap ($)": number(pair.get("marketCap")) or number(pair.get("fdv")),
            "Chart": pair.get("url") or f"https://dexscreener.com/{network}/{pair.get('pairAddress', '')}",
        }
        if row["Price ($)"] <= 0:
            continue
        row["Score"], row["Signal"], row["Reasons"] = score_row(row, min_liq)
        rows.append(row)
    return sorted(rows, key=lambda x: (x["Score"], x["5m volume ($)"]), reverse=True)


def price_for_token(chain, addr):
    # Fetch directly, rather than relying on the discovery cache for position monitoring.
    pairs = request_json(f"{D}/token-pairs/v1/{chain}/{addr}")
    matching = [p for p in pairs if (p.get("baseToken") or {}).get("address") == addr]
    if not matching:
        raise ValueError("No liquid base-token pair available")
    pair = max(matching, key=lambda p: number((p.get("liquidity") or {}).get("usd")))
    value = number(pair.get("priceUsd"))
    if not value:
        raise ValueError("No price returned")
    return value


st.title("📡 MemeCoin Radar")
st.caption("Live Solana/Base pool discovery • transparent momentum rules • paper trading only")
st.warning("A SETUP is **not** a buy recommendation or a safety certification. Holder concentration, mint/freeze authorities, LP control, insider wallets and ability to sell are NOT verified in this version. Token markets can be manipulated and you can lose 100%.")
with st.sidebar:
    st.header("Scanner settings")
    network = st.selectbox("Network", ["solana", "base"], index=0)
    pages = st.slider("New-pool pages", 1, 3, 2, help="API limits apply; each page is approximately one request.")
    min_liq = st.number_input("Minimum liquidity ($)", 1000, 1000000, 20000, 1000)
    max_age = st.slider("Maximum pool age (minutes)", 5, 240, 60)
    st.divider()
    st.subheader("Paper trading")
    stake = st.number_input("Paper entry amount ($)", 5.0, 10000.0, 50.0, 5.0)
    stop = st.slider("Stop-loss (%)", 5, 80, 20)
    take = st.slider("Take-profit (%)", 10, 500, 50)
    st.caption("The stop/take alerts rely on refreshes. They are NOT actual exchange orders and can miss gaps.")
    scan_clicked = st.button("Refresh scanner", type="primary", use_container_width=True)

if "positions" not in st.session_state:
    st.session_state.positions = []
if "history" not in st.session_state:
    st.session_state.history = []

st.subheader("New pools — live scan")
try:
    with st.spinner("Loading live pools and token pairs…"):
        rows = scan(network, pages, min_liq, max_age)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    st.caption(f"Latest app refresh: {now}. GeckoTerminal pools may be cached for 1 minute. Only recently created pools surfaced by its API are considered; not an exhaustive real-time blockchain feed.")
except Exception as exc:
    rows = []
    st.error(f"Live API unavailable: {exc}. Check internet access, API rate limits, or retry after a minute.")

if rows:
    count = sum(x["Signal"].startswith("SETUP") for x in rows)
    a,b,c = st.columns(3)
    a.metric("Pools evaluated", len(rows))
    b.metric("Unverified setups", count)
    c.metric("Highest heuristic score", f"{rows[0]['Score']}/100")
    shown = pd.DataFrame(rows)[["Token","Age (min)","Price ($)","Liquidity ($)","5m volume ($)","5m buys","5m sells","5m change (%)","Score","Signal","Chart"]]
    st.dataframe(shown, use_container_width=True, hide_index=True, column_config={"Chart": st.column_config.LinkColumn("DEX chart")})
    st.subheader("Review candidate")
    choices = {f"{r['Token']} • {r['Address'][:8]}… • {r['Signal']}": r for r in rows}
    selected = st.selectbox("Token", list(choices))
    token = choices[selected]
    x,y = st.columns([2,1])
    with x:
        st.write(f"**Signal:** {token['Signal']} | **Score:** {token['Score']}/100")
        st.write("**Observed factors:** " + token["Reasons"])
        st.code(token["Address"])
        st.link_button("Open chart and inspect token", token["Chart"])
    with y:
        st.error("Not security verified")
        st.caption("Before any real trade: check token authority settings, top holders, LP control, executable sell route, and independent security reports. This app does not perform these checks.")
        if st.button("Record PAPER buy", use_container_width=True):
            st.session_state.positions.append({"network":network,"address":token["Address"],"name":token["Token"],"entry":token["Price ($)"],"amount":stake,"stop":stop,"take":take,"entry_time":now})
            st.success(f"Recorded simulated buy of {token['Token']}; no real funds used.")
else:
    st.info("No evaluable pools found for these settings. Try increasing maximum age, or retry after API rate limits reset.")

st.divider()
st.subheader("Paper positions and rule-based EXIT alerts")
if st.button("Update all paper positions", use_container_width=False):
    st.session_state.last_update = time.time()
if st.session_state.positions:
    for i, pos in enumerate(st.session_state.positions.copy()):
        try:
            current = price_for_token(pos["network"], pos["address"])
            pct = (current / pos["entry"] - 1) * 100
            pnl = pos["amount"] * pct / 100
            status = "EXIT SIGNAL — stop loss" if pct <= -pos["stop"] else "EXIT SIGNAL — take profit" if pct >= pos["take"] else "HOLD / monitor"
            with st.container(border=True):
                st.write(f"**{pos['name']}** | {status}")
                aa,bb,cc,dd = st.columns(4)
                aa.metric("Entry",f"${pos['entry']:.10g}")
                bb.metric("Current",f"${current:.10g}")
                cc.metric("P/L %",f"{pct:+.1f}%")
                dd.metric("Gross P/L",f"${pnl:+.2f}")
                st.caption(f"Stop: -{pos['stop']}% | Target: +{pos['take']}% | Prices are indicative and may not be executable.")
                if st.button("Close paper position", key=f"close_{i}"):
                    record = dict(pos, exit_price=current, gross_pnl=pnl, exit_time=dt.datetime.now(dt.timezone.utc).isoformat())
                    st.session_state.history.append(record)
                    st.session_state.positions.pop(i)
                    st.rerun()
        except Exception as exc:
            st.info(f"{pos['name']}: price temporarily unavailable ({exc}).")
else:
    st.caption("No simulated positions yet. Select a token above to open one.")

if st.session_state.history:
    st.subheader("Closed paper trades")
    h = pd.DataFrame(st.session_state.history)
    st.dataframe(h[["name","amount","entry","exit_price","gross_pnl","entry_time","exit_time"]], use_container_width=True, hide_index=True)
    st.download_button("Export paper trades CSV", h.to_csv(index=False), "paper_trades.csv", "text/csv")

st.divider()
st.markdown("**What the score means:** +25 liquidity ≥ your threshold, +20 5-minute volume ≥ $5k, +25 at least 20 buys and buy/sell count ratio ≥ 1.5, +20 five-minute price change 1–30%, +10 pool age 2–30 minutes. A SETUP requires **all** gates. These values are illustrative, unvalidated heuristics, not predictions. No trade should be assumed safe or profitable because of a score.")
st.caption("Data providers: GeckoTerminal and DEX Screener. Refresh via the sidebar. No wallet connection, private key handling, automatic orders, or server-side monitoring. Paper positions remain in this browser session; data does not persist after session reset.")
