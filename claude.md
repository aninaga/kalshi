# Kalshi-Polymarket Arbitrage Detection System

## Project Overview
This project implements a comprehensive arbitrage detection system that continuously monitors Kalshi and Polymarket prediction markets to identify and analyze profitable trading opportunities. The system features real-time monitoring, intelligent market matching, and detailed opportunity analysis with multiple completeness levels.

## System Architecture

### Core Components
- **`arbitrage_analyzer.py`**: Main entry point and continuous monitoring system
- **`kalshi_arbitrage/`**: Core package containing all system components
  - `api_clients.py`: API clients for Kalshi and Polymarket with retry logic
  - `market_analyzer.py`: Core analysis engine with arbitrage detection
  - `config.py`: Centralized configuration and settings
  - `utils.py`: Utility functions for text processing and similarity scoring
  - `websocket_client.py`: Real-time WebSocket streaming for Phase 2

### Directory Structure
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

## Key Features

### 1. Lossless Arbitrage Detection (Phase 1)
- **Three Completeness Levels**:
  - `FAST`: 95% completeness, optimized for speed (3 matches/market, 10 trades/opportunity)
  - `BALANCED`: 99% completeness, good speed/accuracy balance (10 matches/market, 25 trades/opportunity)
  - `LOSSLESS`: 100% completeness, no information loss (unlimited matches/trades)

### 2. Real-Time Streaming (Phase 2)
- **WebSocket Integration**: Live price and orderbook updates
- **Cache Staleness Elimination**: Fresh data with sub-second latency
- **Fallback Mechanisms**: Graceful degradation to REST APIs when streams fail
- **Stream Health Monitoring**: Connection quality and data freshness tracking

### 3. Comprehensive Market Coverage
- **Full Market Capture**: Fetches ALL active markets from both platforms using pagination
- **Intelligent Matching**: Advanced fuzzy string matching with similarity scoring
- **Price Analysis**: Real-time bid/ask spreads with slippage calculations
- **Volume Assessment**: Liquidity analysis for tradeable opportunities

### 4. Advanced Configuration
- **Adaptive Limits**: Dynamic thresholds based on market conditions
- **Fee Integration**: Platform-specific fee structures (Kalshi: 0%, Polymarket: ~2%)
- **Risk Management**: Position sizing limits and slippage tolerance
- **Performance Tuning**: Concurrent processing with rate limiting

## Usage Examples

### Basic Continuous Monitoring
```bash
python arbitrage_analyzer.py --mode continuous
```

### Single Comprehensive Scan
```bash
python arbitrage_analyzer.py --mode single --completeness LOSSLESS
```

### Real-Time Streaming Mode
```bash
python arbitrage_analyzer.py --mode continuous --realtime --completeness BALANCED
```

### Custom Parameters
```bash
python arbitrage_analyzer.py \
  --mode continuous \
  --interval 15 \
  --threshold 0.015 \
  --similarity 0.8 \
  --completeness LOSSLESS
```

## Configuration Parameters

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

## Data Management

### Market Data Storage
- **Persistent Caching**: Intelligent caching with TTL-based invalidation
- **Historical Tracking**: Complete audit trail of all opportunities
- **Scan Reports**: Detailed analysis results with metadata
- **Performance Metrics**: Timing, success rates, and completeness statistics

### Logging and Monitoring
- **Multi-Level Logging**: DEBUG, INFO, WARNING, ERROR with file rotation
- **Real-Time Metrics**: Live system performance indicators
- **Error Tracking**: Comprehensive error handling with retry logic
- **Uptime Statistics**: System reliability and availability metrics

## API Integration

### Kalshi API
- **Public Endpoints**: Read-only market data access
- **Pagination Support**: Complete market coverage via cursor-based pagination
- **Rate Limiting**: Respectful API usage with exponential backoff
- **Error Handling**: Robust retry logic for network failures

### Polymarket API
- **Gamma API**: Market metadata and basic pricing
- **CLOB API**: Real-time bid/ask prices and orderbook data
- **Comprehensive Coverage**: All active markets with outcome-level pricing
- **Price Validation**: Cross-validation between different endpoints

## Development and Testing

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

## Performance Characteristics

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

## Risk Management and Compliance

### Operational Safety
- **Analysis Only**: No trading functionality - pure market analysis
- **Rate Limiting**: Respectful API usage within platform terms
- **Error Isolation**: Failures in one component don't affect others
- **Data Validation**: Comprehensive input validation and sanitization

### Legal Compliance
- **Terms of Service**: Full compliance with Kalshi and Polymarket ToS
- **Educational Purpose**: Designed for research and educational use
- **Local Data Storage**: All data stored locally, no external transmission
- **Privacy Protection**: No personal data collection or storage

## Future Enhancement Roadmap

### Phase 3: Advanced Analytics
- **Machine Learning**: Improved market matching using ML models
- **Pattern Recognition**: Historical opportunity pattern analysis
- **Predictive Modeling**: Opportunity likelihood prediction
- **Portfolio Optimization**: Multi-opportunity position sizing

### Phase 4: Extended Integration
- **Additional Platforms**: Integration with more prediction markets
- **Cross-Platform Analysis**: Multi-platform arbitrage detection
- **Advanced Notifications**: Real-time alerts and reporting
- **API Standardization**: Unified interface for multiple platforms

## Support and Maintenance

### Monitoring and Alerts
- **System Health**: Automated monitoring of key system metrics
- **Performance Tracking**: Historical performance trend analysis
- **Error Alerting**: Automatic notification of system issues
- **Capacity Planning**: Resource usage monitoring and optimization

### Maintenance Procedures
- **Regular Updates**: API endpoint and parameter updates
- **Performance Optimization**: Continuous performance improvement
- **Security Updates**: Regular security review and updates
- **Documentation Updates**: Keeping documentation current with system changes

---

# Important Instruction Reminders
- **Focus on Analysis**: This is a market analysis system, not a trading system
- **File Organization**: Maintain clean separation between core system, debug tools, tests, and utilities
- **Configuration Management**: Use centralized configuration for all system parameters
- **Error Handling**: Implement comprehensive error handling with graceful degradation
- **Performance Monitoring**: Track and optimize system performance continuously
- **Documentation**: Keep all documentation current with system changes