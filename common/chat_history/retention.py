"""CLI entry point for bounded trace retention sweeps."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .principal import HistoryPrincipal
from .repository import AdminHistoryRepository


async def run_retention(*, batch_size: int = 500) -> int:
    # This principal is an internal capability marker. The actual operation is
    # authenticated by the least-privilege TigerGraph runtime credential.
    principal = HistoryPrincipal.create(
        user_id="history-retention",
        accessible_graphs=(),
        global_roles=("superuser",),
    )
    repository = AdminHistoryRepository(principal)
    total = 0
    while True:
        expired = await repository.expire_traces(batch_size=batch_size)
        total += expired
        if expired < batch_size:
            return total


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Expire bounded batches of TigerGraph chat traces"
    )
    parser.add_argument("--batch-size", type=int, default=500)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOGLEVEL", "INFO"))
    print({"expired": asyncio.run(run_retention(batch_size=args.batch_size))})


if __name__ == "__main__":
    main()
