# macOS installation — Trading-Agent 0.3.3rc3

## Scope and prerequisites

macOS Terminal, Python **3.11+** with `venv`/pip, a writable new installation
folder, and an unused localhost port (example: 8877). The offline dashboard needs
no CoinSwitch keys or network connection. Genuine market collection needs network
access and separately verified venue metadata. Historical signing has an optional
cryptography dependency; it is not needed for this installation check.

Use a **new extracted release folder**, never the current Trading app/account
folder. Do not overwrite `.venv`, the existing SQLite account or backups. This guide
installs the wheel; it does not clone history, download research data or enable live
orders. Keep the existing account's Emergency Stop engaged.

## Verify and install

From the extracted `Trading-Agent-0.3.3rc3` folder:

```sh
shasum -a 256 -c SHA256SUMS
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --no-deps \
  wheels/safety_first_trading_app-0.3.3rc3-py3-none-any.whl
.venv/bin/python -c 'import trading; print(trading.__version__)'
.venv/bin/trading-paper --help
```

Stop on any checksum failure. Expected version: `0.3.3rc3`. Installation is offline;
no dependency upgrade, optional extra or API credential is required. Preserve the
release ZIP and checksums for provenance.

## Isolated dashboard check — PAUSED, zero orders

In the same terminal, create a new temporary account explicitly separate from the
existing genuine-market account:

```sh
RUN_DIR="$(mktemp -d "${TMPDIR:-/tmp}/trading-agent-check.XXXXXX")"
printf 'Temporary check folder: %s\n' "$RUN_DIR"
.venv/bin/trading-paper-account create --state-db "$RUN_DIR/inspection.sqlite3" \
  --cash 1234.56 --currency USD
.venv/bin/trading-paper --dashboard --host 127.0.0.1 --port 8877 \
  --state-db "$RUN_DIR/inspection.sqlite3"
```

This is a labeled **SYNTHETIC DEMO**, used only to check installation/UI. It is
created PAUSED. Do not press Start/resume or pass `--resume-paper`. No orders are
needed for installation validation; demo prices/P&L cannot qualify a strategy.

In another terminal:

```sh
open http://127.0.0.1:8877/
curl --fail http://127.0.0.1:8877/readyz
curl --fail http://127.0.0.1:8877/api/snapshot
curl -i http://127.0.0.1:8877/healthz
```

Expect PAUSED, cash 1234.56 USD, no positions/orders, `live_trading=false`, synthetic
origin, and `/readyz` HTTP 200 after the first collection pass. `/healthz` HTTP 503
is expected while paused; collection readiness is not trading permission.

## Safe shutdown and isolated restart

Press **Ctrl-C in the application terminal**, then wait for clean exit:

```sh
lsof -nP -iTCP:8877 -sTCP:LISTEN
.venv/bin/python -m trading.app.maintenance inspect "$RUN_DIR/inspection.sqlite3"
```

No listener should remain. In that original terminal, restart using the same
`trading-paper --dashboard ... --state-db "$RUN_DIR/inspection.sqlite3"` command.
Verify STOPPED and unchanged balance/inventory/orders, then Ctrl-C again. Clean
shutdown persists STOPPED; a crash from RUNNING restores PAUSED. Neither resumes
trading automatically.
Never remove an active lock or use `kill -9` as the routine shutdown procedure.

## Troubleshooting

| Symptom | Action |
|---|---|
| Python version below 3.11 or missing pip/venv | Obtain a suitable Python installation; do not alter the working account environment. |
| Checksum mismatch | Stop. Re-obtain the release; do not install or edit checksums to pass. |
| Address already in use | Identify the listener with `lsof`; choose another unused port for this isolated check. Never stop the genuine app to free its port. |
| `/readyz` unavailable | Inspect foreground startup errors; confirm the process is alive and the selected port matches. Do not start learning manually to bypass readiness. |
| HTTP 503 on `/healthz` | Expected while paused; inspect snapshot blocked reasons. It is not permission to resume. |
| Locked database / inconsistent recovery | Stop; verify writer ownership, preserve files and use the operator recovery checklist. Do not delete locks, balances or history. |
| CoinSwitch metadata rejected | Keep execution blocked. `precision.limit=2` is unresolved; manual guesses/another exchange's rules are unacceptable. |
| Learning awaiting experience | Valid state; insufficient contiguous genuine candles must not trigger fake training or deployment. |

## Genuine paper account — separate, gated workflow

**Do not execute the following workflow until authoritative rules are implemented,
fresh metadata is accepted, the account has a verified backup/reconciliation, and
explicit operator recovery/startup authorization has been given.**

The packaged Mac launcher assumes `.venv`, `config` and `artifacts` under its own
installation root. Moving an existing account between roots is not authorized by
this guide. Use the reviewed original account path/profile and the
[exact operator recovery procedure](PAPER_OPERATOR_RECOVERY_CHECKLIST.md); never
let a new empty path silently substitute for the original account.

After those gates, start the approved application from its reviewed root with:

```sh
./scripts/start-paper.command --coinswitch-market-info-file "$REVIEWED_CAPTURE"
```

`REVIEWED_CAPTURE` must be the actual accepted, current capture path. The genuine
dashboard is at `http://127.0.0.1:8766/`. Do not pass `--resume-paper`. Verify restored
PAUSED inventory/cash/orders, genuine advancing timestamps, accepted metadata,
startup reconciliation and scheduler status. Resume needs **separate authorization**.
Ctrl-C in the launcher terminal stops its owned paper/scheduler processes.

Before any existing-account recovery: stop and verify writer ownership, make a
new non-overwriting backup with `trading.app.maintenance backup`, inspect it and
record SHA-256. Retain Emergency Stop until authorized recovery **into PAUSED**.
Any duplicate fill, oversell, unknown order, broken accounting/persistence, invalid
metadata or unauthorized execution requires stopping and preserving evidence.

Real exchange orders remain disabled. Neither successful installation nor seven
observation days establish ML predictive value or profitability.
