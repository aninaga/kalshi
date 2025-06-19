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

## 🚀 Installation

```bash
# Navigate to project directory
cd kalshi

# Install dependencies
pip install -r requirements.txt

# Configure authentication (optional but recommended)
cp .env.example .env
# Edit .env with your Kalshi credentials
```

## ⚡ Quick Start

### Basic Continuous Monitoring
Monitor markets continuously with default settings:
```bash
python arbitrage_analyzer.py --mode continuous
```

### Single Comprehensive Scan
Run one complete analysis with lossless completeness:
```bash
python arbitrage_analyzer.py --mode single --completeness LOSSLESS
```

### Real-Time Streaming Mode
Enable WebSocket streaming for sub-second latency:
```bash
python arbitrage_analyzer.py --mode continuous --realtime --completeness BALANCED
```

### Custom Parameters
Fine-tune analysis with custom thresholds and completeness:
```bash
python arbitrage_analyzer.py \
  --mode continuous \
  --interval 15 \
  --threshold 0.015 \
  --similarity 0.8 \
  --completeness LOSSLESS
```

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

```
kalshi/
├── arbitrage_analyzer.py          # Main system entry point
├── kalshi_arbitrage/              # Core system package
│   ├── api_clients.py             # API integration layer
│   ├── market_analyzer.py         # Analysis engine
│   ├── config.py                  # Configuration management
│   ├── utils.py                   # Utility functions
│   └── websocket_client.py        # Real-time streaming
├── debug/                         # Debugging utilities
│   ├── debug_arbitrage.py         # Comprehensive debugging tool
│   ├── check_specific_prices.py   # Price verification utilities
│   ├── check_polymarket_fields.py # API field inspection
│   └── test_polymarket_api.py     # API exploration
├── tests/                         # Test suites
│   ├── test_all_methods.py        # Comprehensive system tests
│   ├── test_lossless.py           # Phase 1 completeness testing
│   └── test_phase2_realtime.py    # Phase 2 WebSocket testing
├── demos/                         # Demonstration scripts
│   └── demo_lossless_complete.py  # Full system demo
├── tools/                         # Analysis utilities
│   ├── detailed_search.py         # Market search tool
│   └── search_markets.py          # Data analysis utility
└── market_data/                   # Data storage
    ├── arbitrage_analysis.log     # System logs
    ├── arbitrage_opportunities.json # Historical opportunities
    └── scan_report_*.json         # Individual scan reports
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

2. Add your Kalshi credentials:
```
KALSHI_EMAIL=your_email@example.com
KALSHI_PASSWORD=your_password
```

3. Without authentication, the system uses public endpoints (limited data)

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