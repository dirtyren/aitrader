#!/usr/bin/env python3
"""Run WFO for CMF strategy — sets up Alpaca credentials from .env."""
import os, sys, subprocess

# Read .env and set AlpacaClient env vars
env_path = os.path.join(os.path.dirname(__file__), '..', 'config', '.env')
env = os.environ.copy()
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        k, v = line.split('=', 1)
        v = v.strip().strip('"').strip("'")
        env[k] = v

env['ALPACA_API_KEY'] = env['ALPACA_EQUITY_API_KEY']
env['ALPACA_SECRET_KEY'] = env['ALPACA_EQUITY_SECRET_KEY']
env['ALPACA_BASE_URL'] = env.get('ALPACA_EQUITY_BASE_URL', 'https://paper-api.alpaca.markets')
env['PYTHONPATH'] = os.path.join(os.path.dirname(__file__), '..')

# Run the WFO
project_dir = os.path.join(os.path.dirname(__file__), '..')
sys.exit(subprocess.call([
    sys.executable,
    os.path.join(project_dir, 'scripts', 'run_wfo.py'),
    '--config', 'config/wfo_cmf.yaml',
    '--settings', 'config/settings_cmf_equity.yaml',
    '--verbose',
], cwd=project_dir, env=env))
