import sys
import os
import json
import yaml
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from main import load_config, apply_overrides, build_asset_class_configs
from backtest.intraday_replay import IntradayReplay

def run():
    cfg = load_config()
    cfg = apply_overrides(cfg, cfg.get("overrides", {}).get("path"), enabled=cfg.get("overrides", {}).get("enabled", True))
    
    asset_classes = build_asset_class_configs(cfg)
    symbols_with_class = []
    
    for ac_name, ac_cfg in cfg.get("asset_classes", {}).items():
        for sym in ac_cfg.get("symbols", []):
            symbols_with_class.append((sym, ac_name))
            
    print(f"Running backtest for {len(symbols_with_class)} symbols: {[s[0] for s in symbols_with_class]}...")
    replay = IntradayReplay(symbols=symbols_with_class, asset_class_configs=asset_classes,
                            initial_equity=cfg.get("backtest", {}).get("initial_equity", 100000),
                            config=cfg)
    result = replay.run()
    
    print("\n--- BACKTEST RESULTS ---")
    print(json.dumps(result.metrics, indent=2))
    
if __name__ == "__main__":
    run()
