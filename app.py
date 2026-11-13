"""MemeCoin Radar v3: informational scanner; no wallets or orders."""
import datetime as dt
import json
import time

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="MemeCoin Radar v3", page_icon="📡", layout="wide")
GECKO = "https://api.geckoterminal.com/api/v2"
DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1/tokens"
JUP = "https://api.jup.ag/swap/v1/quote"
SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
HEADERS = {"User-Agent": "MemeCoinRadar/3.0"}


def fetch(url, params=None, headers=None):
    response = requests.get(url, params=params, headers=headers or HEADERS, timeout=12)
    response.raise_for_status()
    return response.json()


def num(value, default=0.0):
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError, OverflowError):
        return default


def age_minutes(iso):
    try:
        return max(0, (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 60)
    except (ValueError, TypeError, AttributeError):
        return None


@st.cache_data(ttl=65, show_spinner=False)
def new_pools(chain, pages):
    result = []
    for page in range(1, pages + 1):
        result.extend(fetch(f"{GECKO}/networks/{chain}/new_pools", {"page": page}, {**HEADERS, "Accept": "application/json;version=20230203"}).get("data") or [])
    return result


@st.cache_data(ttl=60, show_spinner=False)
def pairs_for_tokens(chain, token_addresses):
    result = []
    for start in range(0, min(90, len(token_addresses)), 30):
        batch = token_addresses[start:start + 30]
        data = fetch(f"{DEX}/tokens/v1/{chain}/{','.join(batch)}")
        if isinstance(data, list):
            result.extend(data)
    return result


def base_address(pool):
    ref = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id") or ""
    return ref.split("_", 1)[1] if "_" in ref else ""


def momentum(row, min_liq):
    score = 0
    score += 25 if row["Liquidity ($)"] >= min_liq else 0
    score += 20 if row["5m volume ($)"] >= 5000 else 0
    score += 25 if row["5m buys"] >= 20 and row["5m buys"] / max(1, row["5m sells"]) >= 1.5 else 0
    score += 20 if 1 <= row["5m change (%)"] <= 30 else 0
    score += 10 if 2 <= row["Age (min)"] <= 30 else 0
    return score


def scan(chain, pages, min_liq, max_age, min_volume):
    pools = new_pools(chain, pages)
    pool_rows = []
    for pool in pools:
        attrs = pool.get("attributes") or {}
        addr = base_address(pool)
        pool_addr = str(attrs.get("address") or "")
        age = age_minutes(attrs.get("pool_created_at"))
        if addr and pool_addr and age is not None and age <= max_age:
            pool_rows.append((pool, addr, pool_addr, age))
    token_addresses = list(dict.fromkeys(item[1] for item in pool_rows))[:90]
    pairs = pairs_for_tokens(chain, token_addresses) if token_addresses else []
    # Match the EXACT discovered pool; never silently use the most-liquid pool of the token.
    by_pool = {str(p.get("pairAddress") or "").lower(): p for p in pairs if p.get("pairAddress")}
    entries = []
    for pool, addr, pool_addr, age in pool_rows:
        if addr not in token_addresses:
            continue
        p = by_pool.get(pool_addr.lower())
        if not p:
            continue  # Unknown liquidity is never represented as liquid.
        tx = ((p.get("txns") or {}).get("m5") or {})
        volumes = p.get("volume") or {}
        changes = p.get("priceChange") or {}
        liquidity = num((p.get("liquidity") or {}).get("usd"))
        price = num(p.get("priceUsd"))
        vol = num(volumes.get("m5"))
        if liquidity < min_liq or price <= 0 or vol < min_volume or "buys" not in tx or "m5" not in changes:
            continue  # Hard gates, not merely scoring bonuses.
        row = {
            "Token": str((p.get("baseToken") or {}).get("symbol") or "Unknown")[:24],
            "Address": addr, "Network": chain, "Pool address": pool_addr,
            "Age (min)": round(age, 1), "Price ($)": price,
            "Liquidity ($)": liquidity, "5m volume ($)": vol,
            "5m buys": int(num(tx.get("buys"))), "5m sells": int(num(tx.get("sells"))),
            "5m change (%)": num(changes.get("m5")),
            "Market cap ($)": num(p.get("marketCap")) or num(p.get("fdv")),
            "Chart": p.get("url") or f"https://dexscreener.com/{chain}/{pool_addr}",
            "DEX": p.get("dexId") or "unknown",
        }
        row["Score"] = momentum(row, min_liq)
        row["Status"] = "WATCH — needs checks"
        entries.append(row)
    return sorted(entries, key=lambda r: (r["Score"], r["5m volume ($)"]), reverse=True)


@st.cache_data(ttl=120, show_spinner=False)
def rug_report(mint):
    return fetch(f"{RUG}/{mint}/report")


def assess_rug(report, max_top10, min_locked):
    """Fail closed on missing data. RugCheck and FOMO use DIFFERENT analyses."""
    flags, unknown = [], []
    if not isinstance(report, dict) or not report:
        return "UNKNOWN — no security report", ["No security data returned"]
    token = report.get("token") or {}
    if "mintAuthority" not in token:
        unknown.append("mint authority not reported")
    elif token["mintAuthority"]:
        flags.append("Mint authority remains active")
    if "freezeAuthority" not in token:
        unknown.append("freeze authority not reported")
    elif token["freezeAuthority"]:
        flags.append("Freeze authority remains active")
    holders = report.get("topHolders")
    if not isinstance(holders, list) or not holders:
        unknown.append("holder concentration unavailable")
    else:
        # RugCheck report pct may include pool custody addresses: not an insider score.
        values = [num(h.get("pct"), num(h.get("percentage"), -1)) for h in holders[:10]]
        if any(v < 0 for v in values):
            unknown.append("holder percentages missing")
        elif sum(values) > max_top10:
            flags.append(f"Top 10 reported holder addresses: {sum(values):.1f}% (may include pool accounts)")
        if any(h.get("insider") is True or h.get("isInsider") is True for h in holders):
            flags.append("One or more holders flagged as insiders by RugCheck")
    risk_list = report.get("risks")
    if not isinstance(risk_list, list):
        unknown.append("risk flags unavailable")
    else:
        for item in risk_list:
            level = str(item.get("level") or "").lower()
            name = str(item.get("name") or "unnamed risk")
            if level in {"danger", "critical", "error"}:
                flags.append(f"RugCheck risk: {name}")
    # LP locking on the summary endpoint is not always supplied on the full report.
    if "lpLockedPct" in report:
        pct = num(report["lpLockedPct"])
        if pct < min_locked:
            flags.append(f"Only {pct:.1f}% of LP reported locked")
    else:
        unknown.append("LP lock coverage not independently confirmed")
    if flags:
        return "REJECT — risk detected", flags + [f"Unverified: {x}" for x in unknown]
    if unknown:
        return "WATCH — security incomplete", [f"Unverified: {x}" for x in unknown]
    return "REVIEW — independent checks passed", ["No risk gates failed in available RugCheck data; this is NOT FOMO approval"]


@st.cache_data(ttl=35, show_spinner=False)
def jupiter_quote(input_mint, output_mint, raw_amount, api_key):
    headers = dict(HEADERS)
    if api_key:
        headers["x-api-key"] = api_key
    return fetch(JUP, {"inputMint": input_mint, "outputMint": output_mint,
                       "amount": int(raw_amount), "slippageBps": 100}, headers)


def route_probe(mint, report, usdc_amount, api_key):
    # 1 USDC = 1e6 base units; always use token mint DECIMALS for the reverse test.
    buy = jupiter_quote(USDC, mint, int(usdc_amount * 1_000_000), api_key)
    output_amount = int(buy.get("outAmount") or 0)
    if output_amount <= 0 or not buy.get("routePlan"):
        return "NO ROUTE", "No usable forward Jupiter route returned"
    sell = jupiter_quote(mint, USDC, output_amount, api_key)
    reverse_out = int(sell.get("outAmount") or 0)
    if reverse_out <= 0 or not sell.get("routePlan"):
        return "NO SELL ROUTE", "Jupiter could not quote a return route"
    recovered = reverse_out / 1_000_000
    loss_pct = (1 - recovered / usdc_amount) * 100
    return "JUPITER TWO-WAY QUOTE", f"Simulated ${usdc_amount:.0f} USDC buy then immediate sell ≈ ${recovered:.2f} USDC ({loss_pct:+.1f}% round-trip friction before FOMO fees). This is NOT a FOMO route or an execution test."


def latest_token_price(chain, token_addr, exact_pool):
    pairs = fetch(f"{DEX}/token-pairs/v1/{chain}/{token_addr}")
    exact = next((p for p in pairs if str(p.get("pairAddress") or "").lower() == exact_pool.lower()), None)
    if not exact or num((exact.get("liquidity") or {}).get("usd")) <= 0:
        raise ValueError("Discovered pool missing or illiquid — price unavailable")
    val = num(exact.get("priceUsd"))
    if val <= 0:
        raise ValueError("No valid price")
    return val


st.title("📡 MemeCoin Radar — v3")
st.caption("FOMO-oriented screening • exact-pool data • independent Solana security checks • PAPER trading only")
st.error("NO AUTOMATED BUY SIGNALS. FOMO has no verified integration in this app. A public DEX price, RugCheck report, or Jupiter route does NOT prove FOMO can buy or sell the token. If FOMO shows zero liquidity, serious warnings, or no trade quote: SKIP.")
with st.sidebar:
    st.header("V3 screening")
    chain = st.selectbox("Network", ["solana", "base"])
    pages = st.slider("Pages of newly created pools", 1, 3, 2)
    min_liq = st.number_input("Minimum exact-pool liquidity ($)", min_value=5000, max_value=1000000, value=30000, step=5000)
    min_vol = st.number_input("Minimum 5-minute volume ($)", min_value=1000, max_value=1000000, value=5000, step=1000)
    max_age = st.slider("Maximum pool age (minutes)", 5, 240, 60)
    st.divider()
    st.subheader("Independent Solana checks")
    max_top10 = st.slider("Flag top-10 holder share above (%)", 10, 90, 50)
    min_locked = st.slider("Minimum reported LP lock if available (%)", 0, 100, 80)
    st.caption("Holder % may include DEX pool custody. These thresholds are user-defined heuristics, not FOMO rules.")
    use_jup = st.checkbox("Enable optional Jupiter buy AND sell quote probe", value=False)
    api_key = st.text_input("Jupiter API key (optional; not a wallet key)", value="", type="password") if use_jup else ""
    test_amount = st.slider("Quote amount (USDC)", 5, 200, 50, step=5) if use_jup else 50
    st.caption("Jupiter may rate-limit requests; its routes are NOT FOMO routes.")
    st.divider()
    st.subheader("Paper trading")
    stake = st.number_input("Paper stake ($)", 5.0, 10000.0, 50.0, 5.0)
    stop = st.slider("Paper stop (%)", 5, 80, 20)
    target = st.slider("Paper target (%)", 10, 500, 50)
    if st.button("Force refresh of market data"):
        new_pools.clear()
        pairs_for_tokens.clear()
        st.rerun()

if "v3_positions" not in st.session_state:
    st.session_state.v3_positions = []
if "v3_history" not in st.session_state:
    st.session_state.v3_history = []
if "v3_reviews" not in st.session_state:
    st.session_state.v3_reviews = {}

try:
    with st.spinner("Finding exact-pool matches…"):
        rows = scan(chain, pages, min_liq, max_age, min_vol)
except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
    rows = []
    st.error(f"Market data unavailable: {exc}")

st.caption(f"Scan generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M:%S} UTC. Discovery and prices may be cached/delayed. Not a complete blockchain feed.")
a, b, c = st.columns(3)
a.metric("Pools meeting liquidity/volume gates", len(rows))
b.metric("FOMO-confirmed tokens", "0 (not connected)")
c.metric("Top momentum score", f"{max((r['Score'] for r in rows), default=0)}/100")
if rows:
    frame = pd.DataFrame(rows)
    st.dataframe(frame[["Token", "Network", "Liquidity ($)", "5m volume ($)", "5m buys", "5m sells", "5m change (%)", "Age (min)", "Score", "Status", "Chart"]],
                 hide_index=True, use_container_width=True, column_config={"Chart": st.column_config.LinkColumn("DEX chart")})
else:
    st.info("No tokens meet the minimum exact-pool liquidity and volume requirements right now. Do not lower the minimum just to generate trades.")

st.subheader("Inspect one exact token")
if rows:
    token_options = {f"{r['Token']} | {r['Address'][:8]}… | {r['Network']}": r for r in rows}
    label = st.selectbox("Select token", list(token_options))
    token = token_options[label]
    st.code(token["Address"], language=None)
    st.caption(f"Exact pool: {token['Pool address']} | DEX: {token['DEX']} | Momentum score: {token['Score']}/100")
    st.link_button("Open exact-pool DEX chart", token["Chart"])
    st.link_button("Open FOMO web to check the EXACT contract (manual)", "https://fomo.family")
    st.caption("In FOMO search, paste the contract (not only the ticker). Check token warnings, liquidity and a small buy/sell quote. This app CANNOT read FOMO warning labels or confirm FOMO routes.")
    fingerprint = f"{token['Network']}:{token['Address']}"
    audit = st.session_state.v3_reviews.get(fingerprint)
    if token["Network"] == "solana":
        if st.button("Run independent RugCheck security audit for selected token", type="primary"):
            try:
                with st.spinner("Requesting token authority / concentration / risk data…"):
                    data = rug_report(token["Address"])
                    status, notes = assess_rug(data, max_top10, min_locked)
                audit = {"status": status, "notes": notes, "at": time.time()}
                st.session_state.v3_reviews[fingerprint] = audit
            except (requests.RequestException, ValueError, TypeError) as exc:
                audit = {"status": "UNKNOWN — RugCheck unavailable", "notes": [str(exc)], "at": time.time()}
                st.session_state.v3_reviews[fingerprint] = audit
        if audit and audit.get("status"):
            st.write("**Independent security result:**", audit["status"])
            for note in audit["notes"]:
                st.write("•", note)
            st.caption("Audit may become stale as holders/liquidity change. Check freshness; RugCheck is not FOMO's security system.")
        if use_jup and st.button("Test external Jupiter USDC buy AND sell quotes"):
            try:
                status, note = route_probe(token["Address"], None, test_amount, api_key)
                st.session_state.v3_reviews.setdefault(fingerprint, {})["quote"] = [status, note]
            except (requests.RequestException, ValueError, TypeError) as exc:
                st.session_state.v3_reviews.setdefault(fingerprint, {})["quote"] = ["UNKNOWN — quote failed", str(exc)]
        stored = st.session_state.v3_reviews.get(fingerprint, {})
        if stored.get("quote"):
            st.write("**External Jupiter route check:**", stored["quote"][0])
            st.caption(stored["quote"][1])
    else:
        st.warning("Base support: market/liquidity screen only. Solana RugCheck and Jupiter checks are NOT applicable. No independent Base security audit is implemented; do not interpret WATCH as safe.")

    st.divider()
    st.subheader("Manual FOMO checkpoint")
    st.caption("Record what YOU see in FOMO. Nothing below is collected from FOMO automatically.")
    fomo_liq = st.selectbox("FOMO liquidity", ["Not checked", "Positive displayed liquidity", "Zero liquidity / unavailable"], key=f"liq_{fingerprint}")
    fomo_warn = st.selectbox("FOMO risk labels", ["Not checked", "No warning labels seen", "One or more warning labels"], key=f"warn_{fingerprint}")
    fomo_route = st.selectbox("FOMO trade preview", ["Not checked", "Buy and sell quote screens both appear", "Buy/sell quote unavailable"], key=f"route_{fingerprint}")
    severe = fomo_liq == "Zero liquidity / unavailable" or fomo_warn == "One or more warning labels" or fomo_route == "Buy/sell quote unavailable"
    if severe:
        st.error("REJECT / SKIP — FOMO reported a risk or route problem. Never override this because momentum score is high.")
    elif all([fomo_liq == "Positive displayed liquidity", fomo_warn == "No warning labels seen", fomo_route == "Buy and sell quote screens both appear"]):
        st.warning("MANUALLY SCREENED — FOMO checks reported by YOU, not independently verified. Quotes can disappear, and no real sell was tested.")
    else:
        st.info("WATCH ONLY — FOMO liquidity, warnings and route are not fully checked.")

    if st.button("Record simulated buy (NO real transaction)", disabled=severe):
        st.session_state.v3_positions.append({"network": chain, "address": token["Address"], "pool": token["Pool address"],
                                               "name": token["Token"], "entry": token["Price ($)"], "stake": stake,
                                               "stop": stop, "target": target, "time": dt.datetime.now(dt.timezone.utc).isoformat()})
        st.success("Paper entry recorded in this browser session. No FOMO account or real funds used.")

st.divider()
st.subheader("Paper positions — exit alerts on refresh only")
st.caption("Not exchange stop orders. Thin pools can move faster than these updates; a displayed exit value may be impossible to realize.")
if st.button("Refresh paper position prices"):
    st.rerun()
for i, pos in enumerate(st.session_state.v3_positions.copy()):
    with st.container(border=True):
        st.write(f"**{pos['name']}** — {pos['network']}")
        try:
            current = latest_token_price(pos["network"], pos["address"], pos["pool"])
            pct = (current / pos["entry"] - 1) * 100
            pnl = pos["stake"] * pct / 100
            status = "EXIT ALERT: stop threshold" if pct <= -pos["stop"] else "EXIT ALERT: profit target" if pct >= pos["target"] else "MONITOR"
            st.write(f"{status} | price ${current:.9g} | move {pct:+.2f}% | indicative P/L ${pnl:+.2f}")
            if st.button("Close paper position", key=f"close_{i}"):
                st.session_state.v3_history.append({**pos, "exit_price": current, "gross_pnl": pnl, "exit_time": dt.datetime.now(dt.timezone.utc).isoformat()})
                st.session_state.v3_positions.pop(i)
                st.rerun()
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            st.error(f"PRICE / LIQUIDITY UNKNOWN. Do not assume you can exit. {exc}")
if st.session_state.v3_history:
    st.download_button("Download paper-trade journal CSV", pd.DataFrame(st.session_state.v3_history).to_csv(index=False), "paper_journal.csv", "text/csv")

with st.expander("What FOMO guidelines this app follows and what it cannot know"):
    st.markdown("""**Public FOMO guidance:** avoid zero/removed liquidity, concentrated token holdings, suspicious token controls, low volume and warning labels. Verified means FOMO performed a review; it does **not** guarantee safety, profits, or continued liquidity. Freshly graduated tokens may be unverified.\n\n**Independent data:** GeckoTerminal discovers pools, DEX Screener supplies exact-pool metrics, RugCheck performs Solana-specific security analysis on demand, and optional Jupiter checks quotes in both directions. None of these is an official FOMO API. There is no automatic FOMO verification, direct FOMO routing test, confirmed real sell, or inside-wallet access.\n\n**Always skip** a token with a FOMO zero-liquidity / route warning even if its DEX score is high. Scores are momentum heuristics, not a prediction or a buy recommendation.\n\n**Public sources:** https://help.fomo.family/en/articles/14436396-how-tokens-are-labeled-and-verified-on-fomo ; https://help.fomo.family/en/articles/14728938-why-can-t-i-buy-or-sell-this-token ; https://help.fomo.family/en/articles/14436622-protecting-yourself-from-scams""")
