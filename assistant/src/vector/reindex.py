"""Cold-start backfill / reindex CLI for the vector store.

Runs the same sweep code as the background indexer, in the foreground, until
a clean pass. Uses the *effective* runtime config (Redis overrides included),
so it needs access to the same Redis as the assistant — run it next to a
configured deployment, not on a blank machine.

    PYTHONPATH=src uv run python -m vector.reindex [--full]

(In the Docker image PYTHONPATH is already set.) `--full` resets the sweep
cursors first, turning the run into a complete backfill; without it the run
just catches up from the current cursors.
"""

import argparse
import asyncio
import sys

from config import get_settings
from deps import build_deps
from vector.db import run_migrations
from vector.indexer import SweepReport, VectorIndexer

_MAX_ATTEMPTS = 3


def _print_report(report: SweepReport) -> None:
    print(f"{report.kind}: {report.status}" + (f" ({report.skip_reason})" if report.skip_reason else ""))
    print(
        f"  objects seen: {report.objects_seen}, chunks embedded: {report.chunks_embedded}, "
        f"chunks deleted: {report.chunks_deleted}"
    )
    for error in report.errors:
        print(f"  error: {error}")


async def _run(full: bool) -> int:
    settings = get_settings()
    if not settings.database_url:
        print("database_url is not set — the vector store is unavailable", file=sys.stderr)
        return 1
    await asyncio.to_thread(run_migrations, settings.database_url)
    deps = build_deps(settings)
    try:
        indexer = VectorIndexer(deps)
        if full:
            indexer.request_reindex()
        # Retry until a clean pass: per-class errors don't advance that class's
        # cursor, and the hash-guard makes re-reading already-indexed pages cheap
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            report = await indexer.sweep_once()
            _print_report(report)
            if report.status == "ok":
                return 0
            if report.status == "skipped":
                return 1
            if attempt < _MAX_ATTEMPTS:
                print(f"retrying ({attempt + 1}/{_MAX_ATTEMPTS}) …", file=sys.stderr)
        return 1
    finally:
        await deps.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill or reindex the vector store from iTop.")
    parser.add_argument("--full", action="store_true", help="reset sweep cursors first (complete backfill)")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.full))


if __name__ == "__main__":
    sys.exit(main())
