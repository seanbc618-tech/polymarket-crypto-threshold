---
name: performance-analyzer
description: Analyze and optimize crypto threshold performance
tools: ["Read", "Bash", "Grep"]
---

# Performance Analyzer Agent

## Purpose
Optimize crypto threshold market analysis speed and efficiency.

## Performance Targets

| Metric | Current | Target | Status |
|--------|---------|--------|--------|
| Analysis Speed | ~714ms | < 500ms | ⚠️ Needs improvement |
| Price Fetch | ~300ms | < 200ms | ⚠️ Needs improvement |
| Probability Calc | ~50ms | < 30ms | ✅ Acceptable |
| Database Write | ~10ms | < 5ms | ✅ Acceptable |
| Test Suite | 0.10s | < 0.15s | ✅ Excellent |

## Analysis Areas

### 1. API Calls
**Current Issues:**
- Sequential API calls to Binance and Coinbase
- No connection pooling
- No caching

**Optimization Strategies:**
- Parallel API requests
- Connection pooling
- Price caching (30 seconds)
- Batch requests

**Implementation:**
```python
import asyncio
import httpx
from functools import lru_cache
from datetime import datetime, timedelta

# Connection pooling
client = httpx.AsyncClient(
    timeout=10.0,
    limits=httpx.Limits(
        max_connections=100,
        max_keepalive_connections=20
    )
)

# Price caching
@lru_cache(maxsize=100)
def get_cached_price(asset: str, cache_time: int):
    """Cache price for 30 seconds."""
    return fetch_price(asset)

# Parallel requests
async def fetch_prices_parallel(assets: list[str]):
    """Fetch prices for multiple assets in parallel."""
    tasks = [fetch_price(asset) for asset in assets]
    return await asyncio.gather(*tasks)
```

### 2. Database Operations
**Current Issues:**
- Individual writes
- No indexing on frequent queries
- Synchronous operations

**Optimization Strategies:**
- Batch writes
- Add indexes
- Async operations
- Connection pooling

**Implementation:**
```python
import sqlite3
from contextlib import contextmanager

# Connection pooling
@contextmanager
def get_db_connection():
    """Get database connection from pool."""
    conn = sqlite3.connect(
        "crypto_threshold.db",
        check_same_thread=False
    )
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
    finally:
        conn.close()

# Batch writes
def batch_insert_analyses(analyses: list[dict]):
    """Insert multiple analyses in single transaction."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            """
            INSERT INTO analyses 
            (market_question, asset, threshold, deadline, 
             market_probability, model_probability, edge, 
             confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(
                a["market_question"],
                a["asset"],
                a["threshold"],
                a["deadline"],
                a["market_probability"],
                a["model_probability"],
                a["edge"],
                a["confidence"],
                a["created_at"]
            ) for a in analyses]
        )
        conn.commit()

# Indexes
def add_indexes():
    """Add indexes for frequent queries."""
    with get_db_connection() as conn:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analyses_asset 
            ON analyses(asset)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analyses_deadline 
            ON analyses(deadline)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analyses_created_at 
            ON analyses(created_at)
        """)
        conn.commit()
```

### 3. Computation
**Current Issues:**
- Redundant calculations
- No memoization
- Inefficient algorithms

**Optimization Strategies:**
- Memoization
- Caching intermediate results
- Optimized algorithms
- Lazy evaluation

**Implementation:**
```python
from functools import lru_cache
from decimal import Decimal

# Memoization
@lru_cache(maxsize=1000)
def calculate_probability_cached(
    spot_price: float,
    threshold: float,
    days_to_expiry: float,
    volatility: float
) -> float:
    """Cached probability calculation."""
    return calculate_probability(
        spot_price, threshold, 
        days_to_expiry, volatility
    )

# Lazy evaluation
class ProbabilityCalculator:
    """Lazy probability calculator."""
    
    def __init__(self, spot_price, threshold, deadline):
        self.spot_price = spot_price
        self.threshold = threshold
        self.deadline = deadline
        self._probability = None
    
    @property
    def probability(self):
        """Lazy calculation of probability."""
        if self._probability is None:
            self._probability = self._calculate()
        return self._probability
    
    def _calculate(self):
        """Actual calculation."""
        # Implementation here
        pass
```

## Profiling Tools

### 1. cProfile
```bash
# Profile analysis function
python -m cProfile -s cumulative src/crypto_threshold/services/probability_service.py

# Profile with output
python -m cProfile -o profile.prof src/crypto_threshold/cli.py analyze 'Will Bitcoin be above $100,000 on June 30?'
```

### 2. line_profiler
```bash
# Install
pip install line_profiler

# Profile specific function
kernprof -l -v src/crypto_threshold/services/probability_service.py
```

### 3. memory_profiler
```bash
# Install
pip install memory_profiler

# Profile memory usage
python -m memory_profiler src/crypto_threshold/cli.py analyze 'Will Bitcoin be above $100,000 on June 30?'
```

### 4. Custom Timing
```python
import time
from contextlib import contextmanager

@contextmanager
def timer(name: str):
    """Time a block of code."""
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    print(f"{name}: {elapsed*1000:.2f}ms")

# Usage
with timer("Price fetch"):
    price = fetch_price("BTC")

with timer("Probability calculation"):
    prob = calculate_probability(price, threshold, days, vol)
```

## Performance Monitoring

### Metrics Collection
```python
import time
from dataclasses import dataclass
from datetime import datetime

@dataclass
class PerformanceMetrics:
    """Performance metrics for analysis."""
    start_time: float
    end_time: float
    api_calls: int
    db_writes: int
    cache_hits: int
    cache_misses: int
    
    @property
    def duration_ms(self) -> float:
        """Duration in milliseconds."""
        return (self.end_time - self.start_time) * 1000
    
    @property
    def cache_hit_rate(self) -> float:
        """Cache hit rate percentage."""
        total = self.cache_hits + self.cache_misses
        if total == 0:
            return 0.0
        return (self.cache_hits / total) * 100

class PerformanceMonitor:
    """Monitor performance metrics."""
    
    def __init__(self):
        self.metrics = []
    
    def record(self, metric: PerformanceMetrics):
        """Record a metric."""
        self.metrics.append(metric)
        self._check_thresholds(metric)
    
    def _check_thresholds(self, metric: PerformanceMetrics):
        """Check if metrics exceed thresholds."""
        if metric.duration_ms > 500:
            print(f"⚠️ Slow analysis: {metric.duration_ms:.2f}ms")
        
        if metric.cache_hit_rate < 50:
            print(f"⚠️ Low cache hit rate: {metric.cache_hit_rate:.1f}%")
```

## Optimization Checklist

### API Optimization
- [ ] Implement parallel API requests
- [ ] Add connection pooling
- [ ] Implement price caching (30s)
- [ ] Add retry logic with backoff
- [ ] Monitor API response times

### Database Optimization
- [ ] Add indexes for frequent queries
- [ ] Implement batch writes
- [ ] Use WAL mode
- [ ] Add connection pooling
- [ ] Monitor query performance

### Computation Optimization
- [ ] Add memoization for calculations
- [ ] Implement lazy evaluation
- [ ] Optimize algorithms
- [ ] Cache intermediate results
- [ ] Monitor calculation times

### Memory Optimization
- [ ] Implement object pooling
- [ ] Add garbage collection hints
- [ ] Monitor memory usage
- [ ] Optimize data structures
- [ ] Implement streaming for large datasets

## Performance Testing

### Load Testing
```bash
# Install locust
pip install locust

# Create load test
cat > locustfile.py << 'EOF'
from locust import HttpUser, task, between

class CryptoThresholdUser(HttpUser):
    wait_time = between(1, 3)
    
    @task
    def analyze_market(self):
        self.client.post("/analyze", json={
            "question": "Will Bitcoin be above $100,000 on June 30?",
            "market_prob": 0.02
        })
EOF

# Run load test
locust -f locustfile.py --host=http://localhost:8000
```

### Benchmarking
```python
import time
import statistics

def benchmark(func, iterations=100):
    """Benchmark a function."""
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        func()
        elapsed = time.perf_counter() - start
        times.append(elapsed)
    
    return {
        "mean": statistics.mean(times) * 1000,
        "median": statistics.median(times) * 1000,
        "stdev": statistics.stdev(times) * 1000,
        "min": min(times) * 1000,
        "max": max(times) * 1000,
    }

# Usage
results = benchmark(lambda: analyze_market("Will Bitcoin be above $100,000?"))
print(f"Mean: {results['mean']:.2f}ms")
print(f"Median: {results['median']:.2f}ms")
```

## Output Format

When analyzing performance, provide:

### Summary
- **Current Performance:** [metrics]
- **Target Performance:** [metrics]
- **Bottlenecks:** [identified issues]
- **Optimizations:** [recommendations]

### Detailed Analysis

#### API Calls
- **Current:** [details]
- **Issues:** [problems]
- **Recommendations:** [solutions]

#### Database
- **Current:** [details]
- **Issues:** [problems]
- **Recommendations:** [solutions]

#### Computation
- **Current:** [details]
- **Issues:** [problems]
- **Recommendations:** [solutions]

### Implementation Plan
1. [Priority 1 optimization]
2. [Priority 2 optimization]
3. [Priority 3 optimization]

## Resources

- Python Performance: https://wiki.python.org/moin/PythonSpeed/PerformanceTips
- httpx Performance: https://www.python-httpx.org/advanced/
- SQLite Optimization: https://www.sqlite.org/optoverview.html
- Profiling Python: https://docs.python.org/3/library/profile.html

## Integration with CI/CD

Performance checks run on:
- Every pull request (benchmark tests)
- Main branch merges (performance regression)
- Weekly scheduled runs (trend analysis)
- Before releases (performance validation)
