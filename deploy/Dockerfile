# 24/7 SHADOW monitor image (no capital, no orders).
# Build from the repo root:  docker build -f deploy/Dockerfile -t kalshi-arb .
FROM python:3.11-slim

WORKDIR /opt/kalshi

# Install deps first for layer caching. `pip install -e .` installs the
# `kalshi-arb` console script (py-clob-client-v2 + cryptography come from pyproject).
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir -e . || pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e .

ENV PYTHONUNBUFFERED=1
ENV EXECUTION_MODE=paper

ENTRYPOINT ["kalshi-arb"]
# Shadow defaults; override at `docker run` as needed.
CMD ["monitor", \
     "--allowlist", "market_data/matching/match_allowlist.json", \
     "--ledger", "market_data/shadow/ledger.jsonl", \
     "--interval", "20"]
