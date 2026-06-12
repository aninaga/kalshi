# Kalshi-Polymarket Arbitrage Detection System

A comprehensive Python system for detecting and analyzing arbitrage opportunities between Kalshi and Polymarket prediction markets with real-time streaming and multiple completeness levels.

## 🎯 Overview

This system continuously monitors **ALL** markets on both Kalshi and Polymarket, identifies equivalent markets, and detects arbitrage opportunities with detailed analysis and historical tracking. Features two-phase architecture with lossless analysis and real-time streaming capabilities.

## ✨ Key Features

### Phase 1: Lossless Arbitrage Detection
- **Three Completeness Levels**:
  - `FAST`: 95% completeness, optimized for speed (3 matches/market, 10 trades/opportunity)
  - `BALANCED`: 99% completeness, good speed/accuracy balance (10 matches/market, 25 trades/opportunity)
  - `LOSSLESS`: 100% completeness, no information loss (unlimited matches/trades)

### Phase 2: Real-Time Streaming
- **WebSocket Integration**: Live price and orderbook updates
- **Cache Staleness Elimination**: Fresh data with sub-second latency
- **Fallback Mechanisms**: Graceful degradation to REST APIs when streams fail
- **Stream Health Monitoring**: Connection quality and data freshness tracking

### Core Capabilities
- **Complete Market Coverage**: Captures ALL active markets from both platforms using pagination
- **Intelligent Matching**: Advanced fuzzy string matching with similarity scoring
- **Comprehensive Pricing**: Real-time bid/ask spreads with slippage calculations
- **Volume Assessment**: Liquidity analysis for tradeable opportunities
- **Historical Tracking**: Persistent storage of all opportunities and market data
- **Professional Logging**: Multi-level logging with file rotation
- **Authentication Support**: Full Kalshi API access with proper authentication

## 🚀 Installation (local)

```bash
git clone <repo> && cd kalshi
git checkout claude/codebase-overview-QycoJ      # the working branch

python -m venv .venv && source .venv/bin/activate   # recommended
pip install -e .                                  # installs the `kalshi-arb` command
```

`pip install -e .` is all you need to run a paper scan. Optional extras:
`pip install -e ".[dev]"` (tests), `".[viz]"` (plots), `".[research]"` (parquet/duckdb).

## ⚡ Quick Start — one command for everything

Everything runs through a single `kalshi-arb` CLI:

```bash
kalshi-arb doctor            # 1. confirm both venues are reachable (catches the
                             #    Polymarket Cloudflare/User-Agent gotcha)
kalshi-arb scan              # 2. full paper pipeline: detect → match → verify →
                             #    price → simulated fills → PnL  (NO real orders)
kalshi-arb analyze-paper     # 3. summarize the captured paper run (drift, hedges)
kalshi-arb diagnose matches  #    inspect why matches are/aren't found
kalshi-arb backtest          #    matching precision/recall gate (needs labels)
kalshi-arb readiness         # 4. live-pilot go/no-go checklist
kalshi-arb live status       #    show the live-trading lock state
```

> **Safe by default.** `kalshi-arb scan` runs the *entire* pipeline including the
> execution engine, but fills are **simulated** — no real order is ever placed.
> Real trading additionally requires `EXECUTION_MODE="live"` **and** an armed
> live-trading lock (see *Auto-Execution* below). It is the mechanism saved for
> after you've validated.

### Common scan variations
```bash
kalshi-arb scan --mode single --completeness LOSSLESS   # one thorough pass
kalshi-arb scan --mode continuous --interval 30         # keep monitoring
kalshi-arb scan --threshold 0.015 --similarity 0.80     # custom thresholds
```

(The original `python arbitrage_analyzer.py ...` entry point still works; the CLI
just wraps it plus the validation tools.)

### Going live (only after validation)
```bash
kalshi-arb readiness                 # must print PASS
kalshi-arb live arm                  # writes the arm token (deliberate, explicit)
EXECUTION_MODE=live kalshi-arb scan  # real orders now permitted, hard-capped
kalshi-arb live disarm               # lock it back down
```
See [`docs/EXECUTION.md`](docs/EXECUTION.md) for the full safety model.

## 📊 Sample Output

```
📊 SCAN #42 COMPLETED (14:23:15)
Duration: 8.2s | Uptime: 0:21:18
Markets: Kalshi(234) + Polymarket(567) = 801
Matches: 89 | Opportunities: 3

🚨 3 ARBITRAGE OPPORTUNITIES DETECTED!

🎯 TOP 3 OPPORTUNITIES:
────────────────────────────────────────────────────────────────────────────────
1. Buy Kalshi YES → Sell Polymarket
   Profit: 4.2% | Similarity: 87.3%
   Kalshi: Trump wins 2024 presidential election
   Polymarket: Donald Trump to win 2024 US Presidential Election
   Buy: $0.456 → Sell: $0.485

2. Buy Polymarket → Sell Kalshi YES
   Profit: 2.8% | Similarity: 91.2%
   Kalshi: Federal Reserve raises rates in December 2024
   Polymarket: Fed to hike rates December 2024
   Buy: $0.234 → Sell: $0.248
```

## 🔧 Configuration

### Analysis Settings
- `MIN_PROFIT_THRESHOLD`: Minimum profit margin (default: 2%)
- `SIMILARITY_THRESHOLD`: Market matching sensitivity (default: 55%)
- `SCAN_INTERVAL_SECONDS`: Analysis frequency (default: 30s)

### Performance Optimization
- `MAX_CONCURRENT_API_CALLS`: Parallel request limit (default: 20)
- `BATCH_PROCESSING_SIZE`: Processing batch size (default: 50)
- `CACHE_CLEANUP_INTERVAL`: Cache maintenance frequency (default: 5min)

### Real-Time Streaming
- `REALTIME_ENABLED`: Enable WebSocket streaming (default: true)
- `STREAM_BUFFER_SIZE`: Message buffer capacity (default: 1000)
- `STREAM_FRESHNESS_THRESHOLD`: Data staleness limit (default: 10s)

## 📁 Project Structure

This repo holds **two separate projects** that share market-data plumbing but
are otherwise independent — keep contributions on the correct side of the line:

1. **Kalshi–Polymarket arbitrage bot** — the `kalshi_arbitrage/` package and the
   root `arbitrage_analyzer.py` entry point. Never imports from `research/`.
2. **Automated hedge-fund research** (NBA quant studies, strategy backtesting) —
   everything under `research/`. Self-contained.

```
kalshi/
├── arbitrage_analyzer.py          # ARB BOT — main entry point
├── analyze_price_discrepancies.py # ARB BOT — helper scripts
├── simple_price_check.py
├── kalshi_arbitrage/              # ARB BOT — core package
│   ├── api_clients.py             # API integration layer
│   ├── market_analyzer.py         # detection / matching engine
│   ├── matching/                  # cross-venue match verification (polarity, criteria, allowlist)
│   ├── execution/                 # general-purpose order execution (gateways, engine, kill switch)
│   ├── arbitrage_executor.py      # arb orchestration (two-leg + confirmed-unwind hedge)
│   ├── validation/                # arb validation tooling
│   │   ├── matching/              #   matcher precision/recall backtest + gate
│   │   ├── paper/                 #   paper-run analysis
│   │   └── pilot/                 #   live-pilot readiness checklist
│   ├── risk_engine.py · monitoring.py · config.py · utils.py · websocket_client.py
│   └── ...
├── research/                      # HEDGE FUND — NBA quant research (independent project)
│   ├── nba_odds_study/            #   NBA data/analysis package
│   ├── harness/                   #   strategy backtester (replay, fills, cost profiles)
│   ├── scorer/ · promotion/ · registry/ · lake/ · agents/
│   └── scripts/                   #   NBA study CLIs (analyze_nba_game.py, study_*.py)
├── tests/                         # ARB BOT test suite (pytest; async via pytest-asyncio)
├── tools/                         # misc analysis utilities
├── docs/EXECUTION.md              # auto-execution platform guide
└── market_data/                   # data storage (logs, opportunities, captures)
```


## 🔍 How It Works

1. **Full Market Capture**: Fetches ALL active markets from both platforms using pagination
2. **Data Processing**: Standardizes and cleans market data for comparison
3. **Intelligent Matching**: Uses fuzzy string matching to identify equivalent markets
4. **Price Analysis**: Retrieves real-time pricing data for matched markets
5. **Arbitrage Detection**: Calculates profit margins accounting for platform fees
6. **Opportunity Ranking**: Sorts opportunities by profit potential and confidence
7. **Data Persistence**: Saves all results for historical analysis and trending

## 🌐 API Integration

### Kalshi
- **Authentication**: Email/password login for full access
- **Market Data**: Complete market information with pricing
- **Rate Limiting**: Respectful API usage with retry logic

### Polymarket
- **Public Access**: No authentication required for market data
- **Comprehensive Coverage**: All active markets with token pricing
- **Real-time Pricing**: Live bid/ask data via CLOB API

## 📈 Data & Analytics

- **Historical Tracking**: All opportunities saved with timestamps
- **Market Coverage Reports**: Complete visibility into market scanning
- **Performance Metrics**: Scan duration, success rates, opportunity trends
- **Exportable Data**: JSON format for further analysis

## ⚙️ Command Line Options

```bash
python arbitrage_analyzer.py --help

options:
  --mode {single,continuous}    Analysis mode (default: continuous)
  --interval SECONDS           Scan interval in seconds (default: 30)
  --threshold DECIMAL          Min profit threshold (default: 0.02)
  --similarity DECIMAL         Market similarity threshold (default: 0.55)
  --completeness {FAST,BALANCED,LOSSLESS}  Analysis completeness level
  --realtime                   Enable real-time WebSocket streaming
```

## 🔐 Authentication Setup

1. Create `.env` file from template:
```bash
cp .env.example .env
```

2. (Optional) Add Kalshi API credentials for the live WebSocket feed:
```
KALSHI_API_KEY=your_access_key_id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi_private_key.pem
```
The bot signs WebSocket requests with an API key id + an RSA private key
(RSA-PSS), **not** an email/password. The key may also live at the repo root
as `kalshi_private_key.pem`.

3. Without these, the client runs **REST-only**: public REST market data still
   works, but there is no live Kalshi WebSocket feed.

## 📜 Legal & Compliance

- **Analysis Only**: No trading functionality - pure market analysis
- **Educational Purpose**: For research and educational use
- **Terms Compliance**: Respects API terms of service
- **Data Privacy**: All data stored locally

## 🚀 Performance

### Typical Performance Metrics
- **Scan Duration**: 8-15 seconds for complete market analysis
- **Market Coverage**: 200+ Kalshi markets, 500+ Polymarket markets
- **Match Detection**: 50-100 potential market pairs per scan
- **Opportunity Detection**: 1-5 arbitrage opportunities per scan (varies by market conditions)
- **Memory Usage**: ~50-100MB during active scanning
- **API Efficiency**: 95%+ success rate with retry logic

### Scalability Features
- **Concurrent Processing**: Parallel market analysis
- **Efficient Caching**: Reduces redundant API calls by 60-80%
- **Adaptive Throttling**: Dynamic rate limiting based on API response times
- **Memory Management**: Automatic cache cleanup and optimization

## 🎯 Use Cases

- **Market Research**: Identify pricing inefficiencies across platforms
- **Academic Study**: Research prediction market dynamics
- **Strategy Development**: Analyze arbitrage opportunity patterns
- **Platform Comparison**: Compare market offerings and pricing

## 🔬 Development & Testing

### Debug Tools (`debug/`)
- **Comprehensive Debugging**: Full arbitrage detection pipeline analysis
- **Price Verification**: Market-specific price checking utilities
- **API Exploration**: Field inspection and endpoint testing
- **Issue Diagnosis**: Targeted debugging for specific market pairs

### Test Suite (`tests/`)
- **System Integration**: End-to-end testing of complete workflows
- **Completeness Validation**: Phase 1 lossless feature verification
- **Real-Time Testing**: Phase 2 WebSocket functionality validation
- **Performance Testing**: Load testing and optimization validation

### Utility Tools (`tools/`)
- **Market Search**: Advanced search through historical and live data
- **Data Analysis**: Flexible analysis of stored market information
- **Report Generation**: Custom analysis and reporting utilities
- **Data Exploration**: Interactive market data investigation