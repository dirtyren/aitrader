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
    INDEX idx_closed_time (strategy_id, closed_at)
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
    FOREIGN KEY (strategy_id) REFERENCES strategies(id),
    INDEX idx_trades_time (strategy_id, closed_at),
    INDEX idx_trades_symbol (strategy_id, symbol)
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