# Kalshi-Polymarket Arbitrage Scanner

Real-time WebSocket-powered arbitrage detection for prediction markets. Implements strategies that have extracted **$40M+** from these markets.

## Arbitrage Strategies

| Strategy | Description | Typical Profit |
|----------|-------------|----------------|
| **Intra-Market** | YES + NO < $1 on same market | 2-6% |
| **Multi-Outcome** | All outcomes sum < $1 | 0.5-5% |
| **Cross-Platform** | Same market, different prices | 2-7% |

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the scanner
python main.py

# With custom minimum profit threshold (1%)
python main.py --min-profit 1.0

# With sound alerts
python main.py --alert-sound
```

## How It Works

1. **Bootstrap**: Fetches all active markets from Polymarket and Kalshi via REST
2. **Connect**: Opens WebSocket connections to both platforms
3. **Monitor**: Receives real-time price updates
4. **Detect**: On each price change, scans for arbitrage opportunities
5. **Alert**: Notifies when profitable opportunities are found

## Configuration

Set credentials in `.env` (optional, for authenticated access):

```
KALSHI_API_KEY=your_api_key
KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem
```

## Project Structure

```
kalshi/
├── main.py                         # Entry point
├── kalshi_arbitrage/
│   ├── realtime_scanner.py         # WebSocket scanner
│   ├── arbitrage_engine.py         # Detection logic
│   ├── config.py                   # Configuration
│   └── utils.py                    # Utilities
└── tests/
    └── test_arbitrage_engine.py    # Test suite
```

## CLI Options

```
python main.py --help

Options:
  --min-profit FLOAT     Minimum profit percentage (default: 0.5)
  --alert-sound          Play sound on opportunity detection
  --full-scan-interval   Seconds between full scans (default: 60)
```

## Requirements

- Python 3.9+
- aiohttp
- fuzzywuzzy
- python-dotenv

## Performance

- **Detection Speed**: Sub-second on price updates
- **Market Coverage**: 200+ Kalshi, 500+ Polymarket markets
- **Memory**: ~50MB during scanning
