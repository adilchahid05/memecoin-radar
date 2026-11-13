# MemeCoin Radar — live scanner / paper trader

An actual Streamlit app that retrieves recent Solana or Base pools from GeckoTerminal and market data from DEX Screener, calculates transparent setup/watch/pass labels, and tracks paper entries/exits.

## Run

Install Python 3.10+ and run in a terminal:

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

Open the localhost URL Streamlit prints (usually http://localhost:8501).

## IMPORTANT

- **No live trading and no wallet connected.** All buys/sells are hypothetical.
- **Not a prediction engine.** The model has no validated predictive edge; no guaranteed timing.
- **NOT a security checker.** This MVP does not validate token mint/freeze authorities, LP ownership, rugs, holder concentration, insiders, or sellability. An app signal is NEVER a claim that a token is safe. Verify independently before risking money.
- Pool discovery is limited to GeckoTerminal's public `/networks/{network}/new_pools` pages and is not an exhaustive launch firehose. API data is cached and may be delayed.
- A price-based paper exit is evaluated on refresh; it is not an executable stop order. Gaps and slippage are not included.
- Session-based paper positions disappear if Streamlit resets. Export closed trades if desired.
- Do not put real money into memecoins unless you are comfortable losing all of it.

## API docs

https://api.geckoterminal.com/docs/index.html
https://docs.dexscreener.com/api/reference

## Engineering status

Source checked for Python syntax. Live API calls were not executed in the build environment (no internet access), so production connectivity and API shape should be verified on your machine. For real trading, add authenticated broker/wallet integration, transaction simulation, anti-rug checks, market-impact controls, audit logs, testing, paper performance, and explicit human approval.
