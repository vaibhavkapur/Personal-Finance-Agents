from datetime import datetime, timedelta, timezone
from app.persistence.store import Store

SYMBOLS = [
    ("AAPL", "Apple Inc.", 22845, 22620, 20, 22100),
    ("MSFT", "Microsoft", 42780, 42350, 12, 41100),
    ("NVDA", "NVIDIA", 12485, 12340, 30, 11920),
    ("AMZN", "Amazon", 19265, 19110, 0, 0),
    ("GOOGL", "Alphabet", 16842, 17010, 0, 0),
]
NOW = "2026-09-25T14:00:00Z"


def seed(store):
    with store.tx() as db:
        if Store.all(db, "accounts"):
            return
        positions = {s: {"quantity": q, "cost_minor": q * cost} for s, _, _, _, q, cost in SYMBOLS if q}
        Store.put(db, "accounts", {"id": "account_demo", "customer_id": "cus_demo_6", "currency": "USD", "cash_minor": 8500000, "initial_cash_minor": 8500000, "initial_positions": positions, "positions": positions, "active_mandate_id": "mandate_v1", "reconciliation_status": "matched"})
        Store.put(db, "settings", {"id": "runtime", "now": NOW, "kill_switch": False, "environment": "mock", "fixture_version": "2026.09.25-v1"})
        Store.put(db, "trading_mandates", {"id": "mandate_v1", "version": 1, "name": "Measured momentum", "customer_id": "cus_demo_6", "account_id": "account_demo", "symbols": [s[0] for s in SYMBOLS], "strategy_version": "sma-cross-v1", "parameters": {"lookback": 5, "target_order_minor": 125000}, "limits": {"per_order_minor": 200000, "per_symbol_minor": 1000000, "aggregate_minor": 2500000, "daily_turnover_minor": 1000000, "max_quote_age_seconds": 120, "limit_tolerance_bps": 50}, "expires_at": "2026-10-01T00:00:00Z", "revoked_at": None, "created_at": NOW})
        for symbol, name, ask, close, _, _ in SYMBOLS:
            prices = [close * 100 // 102, close * 101 // 102, close * 100 // 102, close * 99 // 102, close * 98 // 102, close]
            if symbol == "GOOGL":
                prices = [17400, 17320, 17240, 17220, 17130, close]
            dates = ["2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24"]
            bars = [{"at": d + "T20:00:00Z", "close_minor": price} for d, price in zip(dates, prices)]
            Store.put(db, "market_snapshots", {"id": "snapshot_" + symbol, "symbol": symbol, "name": name, "source": "meridian-synthetic-replay", "authority": "simulated", "environment": "mock", "currency": "USD", "observed_at": NOW, "received_at": NOW, "market_session": "regular", "bid_minor": ask - 4, "ask_minor": ask, "previous_close_minor": close, "bars": bars, "information_cutoff": "2026-09-24T20:00:00Z", "eligible_execution_at": "2026-09-25T13:30:00Z", "replay_run_id": "replay_20260925"})
        Store.event(db, "desk.initialized", {"message": "Synthetic account loaded. Cash and positions reconcile with the paper broker.", "source": "fixture:2026.09.25-v1"}, NOW)
