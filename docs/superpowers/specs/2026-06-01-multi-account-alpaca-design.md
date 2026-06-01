# Per-Asset-Class Alpaca Accounts + Dashboard Split

**Status:** Approved design — ready for implementation plan
**Date:** 2026-06-01

## Summary

Split the single shared Alpaca account into two: one for all equity strategies, one for all crypto. Credentials live in MySQL with `.env` bootstrap. Dashboard exposes a Settings page to edit credentials with a required test-connection step. The Strategies tab splits equity and crypto into two sub-tabs, each showing its account info and a colorized P&L (red negative, green positive).

## Goals

- Trade equity and crypto from independent Alpaca paper/live accounts to isolate risk and buying power.
- Operators can rotate keys from the dashboard without editing files on the host.
- Dashboard shows clearly which account is in use per asset class and surfaces account state at a glance.
- Negative P&L is visually obvious in the strategies list.

## Non-Goals

- Per-strategy credentials (10 accounts). Possible future extension; out of scope here.
- Encrypted-at-rest secrets in MySQL. Same trust model as today's `.env`.
- Auto-restart of trader containers from the dashboard.
- Migration of historical positions across accounts.

## Architecture

### New module — `broker/credentials.py`

Single resolver responsible for credential lookup and dashboard write-path. The only place that knows where credentials live.

```python
@dataclass(frozen=True)
class AlpacaCreds:
    asset_class: Literal["equity", "crypto"]
    api_key: str
    secret_key: str
    base_url: str
    source: Literal["db", "env_bootstrap", "env_legacy"]

class MissingCredentialsError(Exception): ...

def resolve(asset_class: Literal["equity", "crypto"]) -> AlpacaCreds: ...
def upsert(asset_class, api_key, secret_key, base_url) -> None: ...
def test_connection(creds: AlpacaCreds) -> tuple[bool, str]: ...
```

**`resolve` precedence** (first hit wins):
1. `broker_credentials` row for the asset class with non-empty `api_key` and `secret_key`.
2. Asset-class env vars: `ALPACA_{EQUITY,CRYPTO}_API_KEY` / `_SECRET_KEY` / `_BASE_URL`. On hit, the row is upserted into MySQL (the bootstrap) and returned.
3. Legacy `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` / `ALPACA_BASE_URL`. Used for *both* asset classes; logs a deprecation warning. Does NOT seed the DB (operator should explicitly split).
4. Raise `MissingCredentialsError`.

If MySQL is unreachable, the resolver logs and falls through to env-only — so a DB outage does not stop a trader that has its creds in `.env`.

### New table — `broker_credentials` (in `state/schema.sql`)

```sql
CREATE TABLE IF NOT EXISTS broker_credentials (
  asset_class    VARCHAR(16)  NOT NULL,
  api_key        VARCHAR(255) NOT NULL,
  secret_key     VARCHAR(255) NOT NULL,
  base_url       VARCHAR(255) NOT NULL,
  account_number VARCHAR(64)  NULL,
  updated_at     DATETIME     NOT NULL,
  PRIMARY KEY (asset_class)
);
```

Plaintext. Trader DB user (`trader`) is the only role with access. Same trust model as the existing `.env` mounted into containers.

`account_number` is cached from the most recent successful `test_connection`; used by the dashboard header to display "acct: ABC1***" without an Alpaca round-trip on every render.

### Changed — `broker/alpaca_client.py`

```python
class AlpacaClient:
    def __init__(self, asset_class: Literal["equity", "crypto"] | None = None):
        load_dotenv()
        if asset_class is not None:
            creds = credentials.resolve(asset_class)
            self.api_key = creds.api_key
            self.secret_key = creds.secret_key
            self.base_url = creds.base_url.rstrip("/")
            self.asset_class = asset_class
        else:
            # Backwards-compat path: env-only, used by tests and scripts.
            self.api_key = os.environ["ALPACA_API_KEY"]
            self.secret_key = os.environ["ALPACA_SECRET_KEY"]
            self.base_url = os.environ.get(
                "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"
            ).rstrip("/")
            self.asset_class = None
        self._session = requests.Session()
        self._session.headers.update(self._get_headers())
```

Lifetime: constructed once at process start (today's behavior). Credential edits in the dashboard therefore require a trader-container restart to apply. The dashboard surfaces this explicitly on save.

### Changed — `main.py` and `main_gap_and_go.py`

```python
asset_class = next(iter(cfg["asset_classes"].keys()))  # "equity" or "crypto"
alpaca = AlpacaClient(asset_class=asset_class)
```

`asset_classes:` blocks already contain exactly one key after commits 612–620.

### Changed — `ui/tabs/strategies_tab.py`

Two Streamlit sub-tabs: **Equity** | **Crypto**. Each renders the existing landing layout (cards + admin table) but filtered to that asset class.

```python
@st.cache_resource
def _get_alpaca(asset_class: str) -> AlpacaClient:
    return AlpacaClient(asset_class=asset_class)

def render():
    equity_tab, crypto_tab = st.tabs(["Equity", "Crypto"])
    with equity_tab:
        _render_asset_class("equity")
    with crypto_tab:
        _render_asset_class("crypto")

def _render_asset_class(asset_class: str):
    client = _get_alpaca(asset_class)
    account = client.get_account()  # cached for the rerun
    st.caption(
        f"acct: `{_mask(account['account_number'])}`  "
        f"BP: `${float(account['buying_power']):,.0f}`"
    )
    strategies = list_by_asset_class(asset_class)
    # existing landing render — cards + admin table — restricted to `strategies`.
```

P&L cells (Today, Period, Avg R) and card P&L use a new helper:

```python
def _pnl_html(value: float | None, fmt: str = "{:+.2f}") -> str:
    if value is None or pd.isna(value):
        return "—"
    color = "#10b981" if value >= 0 else "#ef4444"
    return (
        f'<span style="color:{color};font-family:monospace">'
        f'{fmt.format(value)}</span>'
    )
```

Rendered with `st.markdown(html, unsafe_allow_html=True)`. Same helper used in `ui/components/strategy_card.py`.

### New helper — `ui/data/strategy_configs.list_by_asset_class`

```python
def list_by_asset_class(asset_class: Literal["equity", "crypto"]) -> list[str]:
    """Return strategy names whose YAML's `asset_classes` block contains
    the given key. Strategies with both keys appear in both lists with
    a logged warning."""
```

### New tab — `ui/tabs/settings_tab.py`

Two cards (Equity, Crypto). Each card:
1. Display: `acct: ABC1***`, `base_url: https://paper-api.alpaca.markets`, `updated 2 min ago`. Edit button.
2. Edit reveals: api_key (masked input), secret_key (masked), base_url (text). "Test connection" button.
3. On test → `credentials.test_connection(...)` → `GET /v2/account`. UI shows ✓ green with account number on success, ✗ red with reason on failure. Save button stays disabled until a successful test on the current input.
4. On save → `credentials.upsert(...)`, cached `account_number` updated, success banner: *"Saved. Restart these containers to apply: trader-orb-equity, trader-rsi-equity, …"* (list computed from YAMLs matching the asset class).
5. `@st.cache_resource` for that asset class is cleared on next rerun.

Wired into the dashboard nav alongside the existing tabs. Position: after Config, before Logs (operator workflow: configure once, monitor often).

### Changed — `docker-compose.yml`

No structural change required — containers still load `config/.env`. The split credentials live in that same file; resolver picks them up. Document the new variables in `.env.example`.

### Changed — `config/.env.example`

```bash
# Per-asset-class Alpaca credentials. Either set these OR keep the legacy
# ALPACA_API_KEY/SECRET_KEY (which serves both asset classes with a deprecation warning).
ALPACA_EQUITY_API_KEY=
ALPACA_EQUITY_SECRET_KEY=
ALPACA_EQUITY_BASE_URL=https://paper-api.alpaca.markets

ALPACA_CRYPTO_API_KEY=
ALPACA_CRYPTO_SECRET_KEY=
ALPACA_CRYPTO_BASE_URL=https://paper-api.alpaca.markets
```

## Data Flow

### Trader startup
1. Load YAML → extract sole key under `asset_classes:`.
2. `AlpacaClient(asset_class=...)` → `credentials.resolve(...)`.
3. Resolver: DB row → env split vars → legacy env. On split-env hit, seed DB.
4. Client retains creds for process lifetime.

### Dashboard view
1. Sub-tab opens → `_get_alpaca(asset_class)` (cached per class).
2. Header shows masked account number + buying power.
3. Strategy list filtered via `list_by_asset_class(asset_class)`.
4. Cards and admin table render with colorized P&L.

### Dashboard credential edit
1. Settings → Edit on an asset-class card.
2. Test connection → `GET /v2/account`. Required before save.
3. Save → `credentials.upsert(...)`; `account_number` cached.
4. Banner lists trader containers needing restart.
5. Cache cleared on next rerun; dashboard immediately uses new creds.

## Error Handling

| Condition                                       | Behavior                                                                  |
| ----------------------------------------------- | ------------------------------------------------------------------------- |
| DB unreachable at trader startup                | Log + fall through to env. No regression vs today.                         |
| DB unreachable in dashboard Settings            | "DB error, cannot edit" red toast. Trader keeps running on its cached creds. |
| Neither DB nor env has creds                    | `MissingCredentialsError`. Trader: fatal log + exit. Dashboard: "Not configured — set credentials in Settings." |
| DB row has empty `api_key` or `secret_key`     | Treated as missing; falls through to env.                                 |
| Test-connection 401                             | "Invalid API key or secret"; save blocked.                                |
| Test-connection timeout (5s)                    | "Cannot reach Alpaca — check base_url"; save blocked.                     |
| Test-connection 200 but `account.status` ≠ ACTIVE | Yellow warn "Account inactive: {status}"; save still allowed.            |
| Invalid asset class (`"options"`, `""`, None)  | `ValueError` at construction. Trader: fail-fast. Dashboard: red toast.    |
| YAML has both `equity` and `crypto` keys        | Strategy appears in both tabs; warning logged. Better than silently dropping. |
| Legacy `ALPACA_API_KEY` only (no split vars)    | Resolver uses legacy for both classes; deprecation warning logged once.   |

Two operators editing the same credential simultaneously: last write wins. `updated_at` is shown on the edit form ("last modified 2 min ago") so a stale view is noticeable.

## Testing

### Unit — `tests/test_credentials.py` (new)
- `resolve` returns DB row when present.
- `resolve` falls back to env split vars when DB row missing AND seeds DB.
- `resolve` raises `MissingCredentialsError` when both missing.
- `resolve` treats empty-string columns as missing.
- Legacy `ALPACA_API_KEY` fallback serves both asset classes when split vars absent.
- Legacy fallback NOT used when split vars present (precedence test).
- `upsert` writes/updates row; `updated_at` advances.
- `test_connection` parses 200 / 401 / timeout responses correctly (mocked `requests`).
- Invalid asset class raises `ValueError`.

### Schema — `tests/test_schema_broker_credentials.py` (new)
- Apply schema to fresh DB; verify table exists with expected columns and PK.
- Insert + select roundtrip.

### Modified — `tests/test_alpaca_client_*.py`
- Existing tests keep passing (backwards-compat path).
- New: `AlpacaClient(asset_class="equity")` reads creds via resolver; `"crypto"` gets different creds.

### Dashboard helpers — `tests/test_strategies_tab_split.py` (new)
- `list_by_asset_class("equity")` returns only equity YAMLs; same for crypto.
- `_pnl_html(+5.0)` contains green hex; `_pnl_html(-5.0)` red; `_pnl_html(None)` returns `—`.

### Settings tab — `tests/test_settings_tab.py` (new)
- Save flow with test_connection mocked OK → `upsert` called once.
- Save flow with test_connection mocked 401 → `upsert` NOT called; error surfaced.

### Manual verification (run before merge)
1. Fresh DB + `.env` with split vars → start one equity + one crypto trader → `broker_credentials` has 2 rows; each trader hit its own account (verify in Alpaca dashboard).
2. Dashboard → Strategies → Equity tab shows only the 5 equity strategies + equity account; Crypto tab shows 5 crypto + crypto account.
3. Negative P&L cells red, positive green, in admin table and cards.
4. Settings → Equity → Edit → bad secret → Test → red error → Save disabled.
5. Settings → Equity → Edit → valid secret → Test → green OK → Save → banner lists 5 containers to restart → restart one → it picks up new key.
6. Restart with `.env` containing only legacy `ALPACA_API_KEY` → both asset classes work, deprecation warning logged once.

## Open Questions

None at design time. Decisions captured above:
- Asset-class granularity (not per-strategy).
- DB-backed with `.env` bootstrap.
- Test-connection required before save.
- Plaintext storage in restricted table.
- Two-sub-tab layout.
- All four P&L surfaces colorized (Today, Period, Avg R, card).
- Per-account `base_url` (independent paper/live per asset class).

## Out of Scope (deferred)

- Per-strategy credentials.
- Encrypted-at-rest credentials.
- Auto-restart of trader containers.
- A "switch all to live" master toggle.
