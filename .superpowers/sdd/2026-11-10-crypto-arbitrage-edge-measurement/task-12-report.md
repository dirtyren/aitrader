# Task 12 report: read-only Streamlit dashboard

Status: DONE
Files: arbitrage/ui/__init__.py, arbitrage/ui/app.py, arbitrage/tests/test_dashboard.py

- Pure loaders `load_report(path)` / `load_live_state(path)` return dict, or None if the file is missing or corrupt. They never write.
- `report_rows(report)` flattens the report into one row per pair/slippage/leg_risk cell. `recorder_coverage(dir)` returns per venue/symbol rows, first/last ts and span from `<dir>/<venue>/<SYM>.parquet`.
- `main()` imports streamlit lazily and renders three tabs: Measurement (cells table + best cell per pair), Live (test_mode banner, spread/P&L/round-trip metrics, resource meter table), Recorder (coverage table + rows-per-venue chart). Paths are sidebar inputs.
- `app.py` imports nothing from `arbitrage.*`, so `streamlit run arbitrage/ui/app.py` works without the repo root on sys.path.
- Read-only: no writes, no ccxt, no orders, no test_mode changes. A test asserts the loaders create no files.

Tests: 4 passed in test_dashboard.py. I also ran the app headless via streamlit AppTest with real report/state/parquet files: no exceptions, 4 dataframes and 3 metrics rendered.

Notes:
- Default paths (arbitrage/reports/report.json, arbitrage/live_state.json, arbitrage/data/recorder) are guesses and are editable in the sidebar.
- Tabs show a "no data" info message instead of failing when a file is absent.
- The Measurement tab does not show the go/no-go verdict. The JSON has none; it lives only in the markdown. It shows the raw cells and best cell per pair instead.

## Fix: Measurement tab go/no-go verdict (073e8d1)

- `arbitrage/ui/app.py`: added pure `pair_verdicts(report, config)` (real iff any cell with slippage_bps >= `go_nogo_min_slippage_bps` is "real" via `arbitrage.report.go_nogo`, mirrors `build_report` headline). Measurement tab now loads `load_config()` and renders "Pair X -> real/not real" (green/red) plus the rule caption above the existing Cells/Best-cell tables (unchanged). app.py now imports from `arbitrage.config`/`arbitrage.report` (overrides earlier "import nothing").
- `arbitrage/tests/test_dashboard.py`: added `test_pair_verdicts` (real, below-min-slippage, short persistence, negative edge, JSON string-key roundtrip).
- Command: `.venv/bin/python -m pytest arbitrage/tests/test_dashboard.py -v`
- Output: 5 passed in 0.49s
