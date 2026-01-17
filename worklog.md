# Worklog

Date: 2026-01-17

## Summary of Changes
- Fixed timezone handling in risk engine by aligning naive/aware datetimes and normalizing timestamps to UTC.
- Added partial-fill handling in slippage model to treat insufficient liquidity as max slippage.
- Improved Kalshi WebSocket auth handling to use `KALSHI_PRIVATE_KEY_PATH` with repo-local fallback; removed hard-coded key path.
- Skipped Kalshi WebSocket connection when `KALSHI_API_KEY` is not set (REST-only mode) to avoid reconnect loops.
- Normalized Kalshi orderbook levels (cents → dollars) and price conversions.
- Added Polymarket token/market/outcome mapping to correctly resolve orderbook/price updates.
- Normalized Polymarket orderbook levels to `{price, size}`.
- Fixed Polymarket WebSocket parsing to capture `asset_id` / `token_id` and robust market id resolution.
- Updated market subscription to include Polymarket `clob_token_ids` where available.
- Filtered Polymarket opportunities to YES/TRUE outcomes only.
- Normalized orderbook inputs and fixed early-exit logic in overlapping-volume calculation (best bid vs best ask after fees).
- Added synthetic orderbook fallback when live orderbooks are missing to keep scans running.

## Files Touched
- `kalshi_arbitrage/risk_engine.py`
  - timezone-safe datetime math
  - partial-fill slippage handling
- `kalshi_arbitrage/websocket_client.py`
  - Kalshi key path handling
  - Polymarket message parsing for asset/token ids
- `kalshi_arbitrage/api_clients.py`
  - Kalshi REST-only fallback if no API key
  - Kalshi orderbook normalization and cents→dollars conversion
  - Polymarket token→market/outcome mapping
  - Polymarket orderbook normalization
- `kalshi_arbitrage/market_analyzer.py`
  - Polymarket `clob_token_ids` subscription support
  - YES-only outcome filter for arbitrage
  - orderbook normalization helper + corrected early-exit logic
  - synthetic orderbook fallback when orderbooks missing

## Tests
- `pytest -q tests/test_enhanced_infrastructure.py` → **12 passed**

## Notes
- Live scan attempts were aborted by user and not completed.
