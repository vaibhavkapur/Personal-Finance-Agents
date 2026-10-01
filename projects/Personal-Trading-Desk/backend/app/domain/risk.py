from app.domain.types import instant
from app.domain.market import is_session


def check_risk(mandate, account, runtime, signal, snapshot, snapshots, reservations, fills, exclude_order=None):
    now = instant(runtime["now"])
    limits = mandate["limits"]
    quantity = mandate["parameters"]["target_order_minor"] // snapshot["ask_minor"]
    limit = snapshot["ask_minor"]
    notional = quantity * limit
    required = notional + 100
    quotes = {s["symbol"]: s for s in snapshots}
    active = [r for r in reservations if r["status"] == "active" and r["order_id"] != exclude_order]
    reserved = sum(r["cash_minor"] for r in active)
    symbol_reserved = sum(r["exposure_minor"] for r in active if r["symbol"] == signal["symbol"])
    exposure = {s: p["quantity"] * quotes[s]["ask_minor"] for s, p in account["positions"].items() if s in quotes}
    current_symbol = exposure.get(signal["symbol"], 0)
    aggregate = sum(exposure.values())
    today = runtime["now"][:10]
    turnover = sum(f["quantity"] * f["price_minor"] + f["fee_minor"] for f in fills if f["executed_at"][:10] == today) + reserved
    age = (now - instant(snapshot["observed_at"])).total_seconds()
    stale_position = any(s not in quotes or (now - instant(quotes[s]["observed_at"])).total_seconds() > limits["max_quote_age_seconds"] or instant(quotes[s]["observed_at"]) > now for s in account["positions"])
    checks = []

    def add(code, passed, detail):
        checks.append({"code": code, "passed": bool(passed), "detail": detail})

    add("mandate_active", not mandate["revoked_at"] and instant(mandate["expires_at"]) > now and account["active_mandate_id"] == mandate["id"], "Mandate must be current, unrevoked, and unexpired")
    add("symbol_allowed", signal["symbol"] in mandate["symbols"], f"{signal['symbol']} must be on the explicit allowlist")
    add("buy_signal", signal["direction"] == "buy", signal["rationale"])
    add("kill_switch", not runtime["kill_switch"], "New submissions are paused" if runtime["kill_switch"] else "Submissions are enabled")
    add("reconciled", account["reconciliation_status"] == "matched", "Account and broker ledgers must reconcile")
    add("regular_session", is_session(runtime["now"]) and snapshot["market_session"] == "regular", "Execution is restricted to the fixture's regular US market session")
    add("no_lookahead", instant(signal["information_cutoff"]) < instant(signal["eligible_execution_at"]) <= now, "Execute only after the signal's historical data cutoff")
    add("fresh_quote", 0 <= age <= limits["max_quote_age_seconds"], f"Quote age {int(age)}s; maximum {limits['max_quote_age_seconds']}s")
    add("valuation_fresh", not stale_position, "All held symbols require current valuation quotes")
    add("whole_shares", quantity > 0, "Order must contain at least one whole share")
    add("limit_tolerance", abs(limit - snapshot["ask_minor"]) * 10000 <= snapshot["ask_minor"] * limits["limit_tolerance_bps"], "Limit must remain within the approved quote tolerance")
    latest_quote = quotes.get(signal["symbol"])
    add("quote_unchanged", latest_quote and latest_quote["ask_minor"] == snapshot["ask_minor"] and latest_quote["bid_minor"] == snapshot["bid_minor"], "Quote prices must match the signal snapshot; changed terms require new review")
    add("per_order", required <= limits["per_order_minor"], f"${required / 100:,.2f} including fee reserve / ${limits['per_order_minor'] / 100:,.2f} per order")
    add("per_symbol", current_symbol + symbol_reserved + required <= limits["per_symbol_minor"], f"${max(0, limits['per_symbol_minor'] - current_symbol - symbol_reserved) / 100:,.2f} remaining in {signal['symbol']}; ${required / 100:,.2f} requested")
    add("aggregate", aggregate + reserved + required <= limits["aggregate_minor"], f"${max(0, limits['aggregate_minor'] - aggregate - reserved) / 100:,.2f} aggregate capacity remains")
    add("buying_power", required <= account["cash_minor"] - reserved, f"${(account['cash_minor'] - reserved) / 100:,.2f} unreserved cash")
    add("daily_turnover", turnover + required <= limits["daily_turnover_minor"], f"${turnover / 100:,.2f} used or reserved / ${limits['daily_turnover_minor'] / 100:,.2f} daily turnover")
    return {"allowed": all(c["passed"] for c in checks), "checks": checks, "reasons": [c for c in checks if not c["passed"]], "quantity": quantity, "limit_price_minor": limit, "notional_minor": notional, "fee_buffer_minor": 100, "cash_reservation_minor": required, "valuation": {"as_of": runtime["now"], "position_exposure_minor": aggregate, "symbol_exposure_minor": current_symbol, "reserved_minor": reserved, "daily_turnover_minor": turnover, "quote_id": snapshot["id"], "quote_observed_at": snapshot["observed_at"]}, "currency": "USD"}
