-- Slice 1 schema. Owned by hyx/slice1.py.
--
-- Tables:
--   ohlcv_daily       -- daily OHLCV bars per ticker
--   news              -- one row per article (tickers flattened to news_tickers)
--   news_tickers      -- many-to-many article <-> ticker tags
--   news_sentiment    -- sentiment scores per (article, model); lets FinBERT +
--                        Qwen zero-shot (slice 5) coexist without migration
--   fetch_state       -- per-source incremental-pull cursors
--   audit_log         -- append-only structured log
--
-- PK choices follow architecture.md §3.3. `news_sentiment` is keyed by
-- (news_id, model) so the same article can be scored by multiple models.
-- `fetch_state.ticker` uses '' for non-ticker-scoped sources (macro etc.).

CREATE TABLE IF NOT EXISTS ohlcv_daily (
    ticker      TEXT      NOT NULL,
    date        DATE      NOT NULL,
    open        DOUBLE    NOT NULL,
    high        DOUBLE    NOT NULL,
    low         DOUBLE    NOT NULL,
    close       DOUBLE    NOT NULL,
    adj_close   DOUBLE    NOT NULL,    -- yfinance split/dividend-adjusted close
    volume      BIGINT    NOT NULL,
    fetched_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS news (
    news_id       TEXT      PRIMARY KEY,
    published_at  TIMESTAMP NOT NULL,
    headline      TEXT      NOT NULL,
    summary       TEXT,
    url           TEXT,
    source        TEXT,
    fetched_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS news_tickers (
    news_id  TEXT NOT NULL,
    ticker   TEXT NOT NULL,
    PRIMARY KEY (news_id, ticker)
);

CREATE TABLE IF NOT EXISTS news_sentiment (
    news_id     TEXT      NOT NULL,
    model       TEXT      NOT NULL,
    label       TEXT      NOT NULL,      -- positive | negative | neutral
    score       DOUBLE    NOT NULL,      -- argmax softmax probability
    score_pos   DOUBLE    NOT NULL,
    score_neg   DOUBLE    NOT NULL,
    score_neu   DOUBLE    NOT NULL,
    scored_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (news_id, model)
);

CREATE TABLE IF NOT EXISTS fetch_state (
    source            TEXT      NOT NULL,
    ticker            TEXT      NOT NULL DEFAULT '',
    last_fetched_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (source, ticker)
);

CREATE SEQUENCE IF NOT EXISTS audit_log_id_seq;

CREATE TABLE IF NOT EXISTS audit_log (
    id       BIGINT    PRIMARY KEY DEFAULT nextval('audit_log_id_seq'),
    ts       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    slice    TEXT      NOT NULL,
    level    TEXT      NOT NULL,      -- debug | info | warn | error
    event    TEXT      NOT NULL,
    payload  JSON
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_date       ON ohlcv_daily (date);
CREATE INDEX IF NOT EXISTS idx_news_published   ON news (published_at);
CREATE INDEX IF NOT EXISTS idx_news_tickers_tkr ON news_tickers (ticker);
CREATE INDEX IF NOT EXISTS idx_audit_ts         ON audit_log (ts);
