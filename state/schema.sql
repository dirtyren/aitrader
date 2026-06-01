-- aitrader MySQL schema v1
-- Run automatically on container startup via init SQL directory

CREATE TABLE IF NOT EXISTS strategies (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    name        VARCHAR(64) NOT NULL UNIQUE,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS positions (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    strategy_id     INT NOT NULL,
    symbol          VARCHAR(32) NOT NULL,
    asset_class     VARCHAR(16) NOT NULL,        -- 'equity' or 'crypto'
    side            VARCHAR(8) NOT NULL,          -- 'long' or 'short'
    qty             DECIMAL(20,8) NOT NULL,
    entry_px        DECIMAL(20,8) NOT NULL,
    stop_px         DECIMAL(20,8) DEFAULT NULL,
    target_px       DECIMAL(20,8) DEFAULT NULL,
    initial_stop_px DECIMAL(20,8) DEFAULT NULL,
    setup_name      VARCHAR(64) NOT NULL,         -- e.g. 'vwap_bounce', 'adopted'
    order_id        VARCHAR(64) DEFAULT '',
    stop_order_id   VARCHAR(64) DEFAULT NULL,
    client_order_id      VARCHAR(128) DEFAULT NULL,  -- entry COID, set on position_opened
    exit_client_order_id VARCHAR(128) DEFAULT NULL,  -- exit COID, set on position_closed
    legacy_untagged      TINYINT(1) DEFAULT 0,       -- 1 = pre-COID-migration row; reconciler treats as alert-only
    breakeven_moved TINYINT(1) DEFAULT 0,
    bars_held       INT DEFAULT 0,
    adopted         TINYINT(1) DEFAULT 0,
    status          ENUM('open', 'closed') NOT NULL DEFAULT 'open',
    opened_at       TIMESTAMP(3) NOT NULL,
    closed_at       TIMESTAMP(3) DEFAULT NULL,
    close_reason    VARCHAR(32) DEFAULT NULL,     -- 'target', 'stop', 'time_stop', 'breakeven', 'manual', 'drift_adopted'
    exit_px         DECIMAL(20,8) DEFAULT NULL,
    pnl_usd         DECIMAL(20,8) DEFAULT NULL,
    R_realized      DECIMAL(20,8) DEFAULT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_open (strategy_id, status, symbol),
    INDEX idx_closed_time (strategy_id, closed_at),
    INDEX idx_client_order_id (client_order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS trades (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    strategy_id     INT NOT NULL,
    symbol          VARCHAR(32) NOT NULL,
    asset_class     VARCHAR(16) NOT NULL,
    setup_name      VARCHAR(64) NOT NULL,
    side            VARCHAR(8) NOT NULL,
    qty             DECIMAL(20,8) NOT NULL,
    entry_px        DECIMAL(20,8) NOT NULL,
    exit_px         DECIMAL(20,8) NOT NULL,
    stop_px         DECIMAL(20,8) DEFAULT NULL,
    target_px       DECIMAL(20,8) DEFAULT NULL,
    initial_stop_px DECIMAL(20,8) DEFAULT NULL,
    pnl_usd         DECIMAL(20,8) NOT NULL,
    R_realized      DECIMAL(20,8) NOT NULL,
    close_reason    VARCHAR(32) NOT NULL,
    opened_at       TIMESTAMP(3) NOT NULL,
    closed_at       TIMESTAMP(3) NOT NULL,
    bars_held       INT DEFAULT 0,
    reflected       TINYINT(1) DEFAULT 0,     -- 1=already processed by optimizer reflection loop
    client_order_id      VARCHAR(128) DEFAULT NULL,
    exit_client_order_id VARCHAR(128) DEFAULT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_trades_time (strategy_id, closed_at),
    INDEX idx_trades_symbol (strategy_id, symbol),
    INDEX idx_trades_client_order_id (client_order_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS daily_stats (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    strategy_id     INT NOT NULL,
    stat_date       DATE NOT NULL,
    trades_count    INT DEFAULT 0,
    win_count       INT DEFAULT 0,
    loss_count      INT DEFAULT 0,
    total_pnl       DECIMAL(20,8) DEFAULT 0.0,
    avg_R           DECIMAL(20,8) DEFAULT 0.0,
    max_drawdown    DECIMAL(20,8) DEFAULT 0.0,
    sharpe_ratio    DECIMAL(20,8) DEFAULT 0.0,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    UNIQUE KEY uq_strategy_date (strategy_id, stat_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reconciliation_strikes (
    id                   BIGINT AUTO_INCREMENT PRIMARY KEY,
    `key`                VARCHAR(128) NOT NULL,
    direction            ENUM('qty_drift','mysql_only','broker_only') NOT NULL,
    strategy_id          INT DEFAULT NULL,
    symbol               VARCHAR(32) NOT NULL,
    strike_count         INT NOT NULL DEFAULT 0,
    first_seen_at        TIMESTAMP(3) NOT NULL,
    last_seen_at         TIMESTAMP(3) NOT NULL,
    last_observed_state  JSON DEFAULT NULL,
    resolved             TINYINT(1) NOT NULL DEFAULT 0,
    resolved_at          TIMESTAMP(3) DEFAULT NULL,
    resolved_reason      VARCHAR(64) DEFAULT NULL,
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_strikes_key (`key`, resolved),
    INDEX idx_strikes_unresolved (resolved, last_seen_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS reconciliation_events (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    type          VARCHAR(32) NOT NULL,                 -- 'heartbeat','untagged_fill','mysql_only_confirmed','broker_only_confirmed','operator_action','tagged_fill_applied','tagged_entry_inserted'
    strategy_id   INT DEFAULT NULL,
    symbol        VARCHAR(32) DEFAULT NULL,
    payload       JSON DEFAULT NULL,
    created_at    TIMESTAMP(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_events_time (created_at),
    INDEX idx_events_type (type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS broker_credentials (
    asset_class    VARCHAR(16) NOT NULL,
    api_key        VARCHAR(255) NOT NULL,
    secret_key     VARCHAR(255) NOT NULL,
    base_url       VARCHAR(255) NOT NULL,
    account_number VARCHAR(64) DEFAULT NULL,
    updated_at     DATETIME NOT NULL,
    PRIMARY KEY (asset_class)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
