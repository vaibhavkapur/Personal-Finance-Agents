from datetime import timedelta
from zoneinfo import ZoneInfo
from app.domain.types import DomainError, instant

# Fixture trading calendar is intentionally bounded; no claim of a live exchange calendar.
TRADING_DAYS = {"2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"}


def is_session(now):
    local = instant(now).astimezone(ZoneInfo("America/New_York"))
    minutes = local.hour * 60 + local.minute
    return local.date().isoformat() in TRADING_DAYS and 570 <= minutes < 960


def market_snapshot(snapshot, as_of):
    if instant(snapshot["observed_at"]) > instant(as_of):
        raise DomainError("Snapshot is beyond the permitted information cutoff")
    return {**snapshot, "bars": [b for b in snapshot["bars"] if instant(b["at"]) <= instant(as_of)]}


def evaluate(snapshot, mandate):
    cutoff = snapshot["information_cutoff"]
    bars = [b for b in snapshot["bars"] if instant(b["at"]) <= instant(cutoff)]
    n = mandate["parameters"]["lookback"]
    if len(bars) < n + 1:
        raise DomainError(f"Need {n + 1} historical closing bars", reasons=["missing_history"])
    bars.sort(key=lambda b: b["at"])
    last = bars[-1]["close_minor"]
    previous = bars[-2]["close_minor"]
    moving_sum = sum(b["close_minor"] for b in bars[-n:])
    previous_sum = sum(b["close_minor"] for b in bars[-n-1:-1])
    buy = last * n > moving_sum and previous * n <= previous_sum
    if instant(snapshot["eligible_execution_at"]) <= instant(bars[-1]["at"]):
        raise DomainError("A close signal can only execute at a later eligible session")
    return {
        "direction": "buy" if buy else "hold", "rule_version": "sma-cross-v1",
        "information_cutoff": cutoff, "eligible_execution_at": snapshot["eligible_execution_at"],
        "rationale": f"Close ${last / 100:.2f} {'crossed above' if buy else 'did not cross above'} the {n}-day moving average (${moving_sum / n / 100:.2f}).",
        "inputs": {"close_minor": last, "sma_minor": moving_sum // n, "previous_close_minor": previous, "previous_sma_minor": previous_sum // n, "bar_timestamps": [b["at"] for b in bars[-n-1:]]},
    }


def add_seconds(now, seconds):
    return (instant(now) + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")
