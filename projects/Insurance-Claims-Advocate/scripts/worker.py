"""Run the durable worker as a separate process against the shared database."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.context import AppContext  # noqa: E402


def main() -> None:
    ctx = AppContext()
    interval = float(os.environ.get("WORKER_INTERVAL_SECONDS", "2"))
    print(f"worker {ctx.worker.worker_id} polling every {interval}s against {ctx.settings.database_url} (adapter {ctx.adapter.capabilities.name})", flush=True)

    async def loop():
        while True:
            stats = await ctx.worker.run_once()
            if any(stats[k] for k in ("jobs", "provider_events", "replayed_events", "outbox", "recovered_actions")) or stats["errors"]:
                print(stats, flush=True)
            await asyncio.sleep(interval)

    asyncio.run(loop())


if __name__ == "__main__":
    main()
