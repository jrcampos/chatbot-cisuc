"""
Subprocess entry point for the kill -9 / SIGTERM tests: runs the real Runner + real Loader
against the test database with a fake 30-record source and a slow enricher.

    python tests/integration/run_fake_store.py <work_dir> <delay_seconds>

Database settings come from the EXTSRC_DB_* environment of the test container.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from external_sources.store.db import connect  # noqa: E402
from external_sources.store.runner import RunOptions, Runner, StopFlag  # noqa: E402
from external_sources.store.settings import DBSettings, load_store_policy  # noqa: E402
from tests.integration.store_fakes import FakePlugin, SlowEnricher, make_record  # noqa: E402

KEYS = [f"HORIZON-KILL-{i:02d}" for i in range(30)]


def main() -> int:
    work, delay = Path(sys.argv[1]), float(sys.argv[2])
    stop = StopFlag()
    stop.install()
    runner = Runner(work, RunOptions(["ft_portal"]), load_store_policy(),
                    lambda: connect(DBSettings.from_env("loader"), attempts=1),
                    plugin_factory=lambda source, r: FakePlugin([make_record(k) for k in KEYS]),
                    enricher_factory=lambda source, r: SlowEnricher(delay), stop=stop)
    return runner.run()


if __name__ == "__main__":
    sys.exit(main())
