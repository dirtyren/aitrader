#!/usr/bin/env python3
"""Run CMF equity WFO sweep — sources Alpaca creds from .env."""
import os, sys, subprocess

env_path = os.path.join(os.path.dirname(__file__), '..', 'config', '.env')
env = os.environ.copy()
with open(env_path) as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        k, v = line.split('=', 1)
        env[k] = v.strip().strip('"').strip("'")

env['ALPACA_API_KEY'] = env['ALPACA_EQUITY_API_KEY']
env['ALPACA_SECRET_KEY'] = env['ALPACA_EQUITY_SECRET_KEY']
env['ALPACA_BASE_URL'] = env.get('ALPACA_EQUITY_BASE_URL', 'https://paper-api.alpaca.markets')
env['PYTHONPATH'] = os.path.join(os.path.dirname(__file__), '..')

project_dir = os.path.join(os.path.dirname(__file__), '..')
sys.exit(subprocess.call([
    sys.executable,
    os.path.join(project_dir, 'scripts', 'run_wfo.py'),
    '--config', 'config/wfo_cmf_equities.yaml',
    '--settings', 'config/settings_cmf_equity.yaml',
    '--verbose',
], cwd=project_dir, env=env))
