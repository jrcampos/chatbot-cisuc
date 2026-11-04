"""
CLI of the external-sources store.

    python -m external_sources.store migrate
    python -m external_sources.store run --all [--fresh] [--force-publish] [--no-documents]
    python -m external_sources.store run --source ft_portal ft_cascade
    python -m external_sources.store status [--json]
    python -m external_sources.store coverage [--json]
    python -m external_sources.store dead-letters list [--source S]
    python -m external_sources.store dead-letters retry [--source S]
    python -m external_sources.store prune
    python -m external_sources.store embed [--limit N] [--records-only]

Exit codes (run): 0 ok, 1 degraded (data served, flagged), 2 usage/config error, 3 database
unreachable (batches spooled), 4 another run in progress, 75 interrupted (resumable).
Connection settings come from the environment (config/chatbot.env + secrets/chatbot.env).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from external_sources.store.settings import (
    ConfigError,
    DBSettings,
    load_store_policy,
    work_dir,
)


def _connect(role: str = "loader", attempts: int | None = None) -> Any:
    from external_sources.store.db import connect
    policy = load_store_policy()
    return connect(DBSettings.from_env(role), read_only=role == "reader",
                   attempts=attempts or int(policy["runner"].get("db_connect_attempts", 4)))


def cmd_migrate(args: argparse.Namespace) -> int:
    from external_sources.store.migrate import migrate
    conn = _connect()
    applied = migrate(conn)
    print(f"applied: {applied or 'nothing (up to date)'}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from external_sources.quality import notifier_from_env
    from external_sources.store.runner import Runner, RunOptions, StopFlag
    policy = load_store_policy()
    sources = list(policy["runner"]["sources"]) if args.all else list(args.source or [])
    if not sources:
        print("[ERROR] give --all or --source NAME ...", file=sys.stderr)
        return 2
    # Fail fast on missing configuration (names only), before any network traffic.
    DBSettings.from_env("loader")
    work = work_dir()
    options = RunOptions(sources, fresh=args.fresh, force_publish=args.force_publish,
                         documents=not args.no_documents, force_refresh=args.force_refresh, limit=args.limit,
                         trigger=args.trigger)
    stop = StopFlag()
    stop.install()
    attempts = int(policy["runner"].get("db_connect_attempts", 4))
    runner = Runner(work, options, policy, lambda: _connect(attempts=attempts),
                    notifier=notifier_from_env(work), stop=stop)
    return runner.run()


def cmd_status(args: argparse.Namespace) -> int:
    from external_sources.store.db import DatabaseUnavailable
    from external_sources.store.status import collect_status, health_code, render
    try:
        conn = _connect(attempts=1)
    except DatabaseUnavailable as exc:
        print(f"[WARN] {exc}", file=sys.stderr)
        conn = None
    status = collect_status(conn, work_dir())
    print(json.dumps(status, indent=2, default=str) if args.json else render(status))
    return health_code(status)


def cmd_coverage(args: argparse.Namespace) -> int:
    from external_sources.store.status import coverage, render_coverage
    rows = coverage(_connect(attempts=1))
    print(json.dumps(rows, indent=2, default=str) if args.json else render_coverage(rows))
    return 0


def cmd_dead_letters(args: argparse.Namespace) -> int:
    from external_sources.store import deadletters
    conn = _connect()
    if args.action == "list":
        letters = deadletters.list_open(conn, args.source, args.limit)
        for letter in letters:
            print(f"#{letter['id']} {letter['source']} {letter['stage']:<9} x{letter['attempts']} "
                  f"{letter['item_key'][:90]}\n      {letter['error'][:200]}")
        print(f"{len(letters)} open dead letter(s)")
        return 0
    from external_sources.http_cache import USER_AGENT, CachedFetcher
    from external_sources.resilience import ResilientSession
    fetcher = CachedFetcher(work_dir() / "cache" / "http", session=ResilientSession(user_agent=USER_AGENT),
                            respect_robots=True, force_refresh=True)
    outcomes = deadletters.retry(conn, fetcher, args.source, args.limit)
    for outcome in outcomes:
        mark = {True: "resolved", False: "still failing", None: "next run"}[outcome["ok"]]
        print(f"#{outcome['id']} {outcome['stage']:<9} {mark:<13} {outcome['item_key'][:80]}  {outcome['detail'][:120]}")
    return 0 if all(o["ok"] is not False for o in outcomes) else 1


def cmd_prune(args: argparse.Namespace) -> int:
    from external_sources.store.checkpoint import prune_run_dirs
    from external_sources.store.retention import prune
    policy = load_store_policy()
    counts = prune(_connect(), policy["retention"])
    counts["run_dirs"] = prune_run_dirs(work_dir(), float(policy["retention"].get("run_dir_days", 14)))
    print(json.dumps(counts))
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    from external_sources.store.embed import run_embedding
    return run_embedding(_connect(), limit=args.limit, records_only=args.records_only)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m external_sources.store",
                                     description="External-sources store: load, inspect and maintain.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply pending SQL migrations").set_defaults(func=cmd_migrate)
    run = sub.add_parser("run", help="collect, enrich and load sources")
    run.add_argument("--all", action="store_true", help="every source in store_policy.yaml runner.sources")
    run.add_argument("--source", nargs="+", help="named sources only")
    run.add_argument("--fresh", action="store_true", help="do not resume an unfinished run")
    run.add_argument("--force-publish", action="store_true", help="publish even when validation fails")
    run.add_argument("--no-documents", action="store_true", help="skip the document layer")
    run.add_argument("--force-refresh", action="store_true", help="ignore HTTP cache validators")
    run.add_argument("--limit", type=int, default=None, help="first N records per source (smoke tests)")
    run.add_argument("--trigger", default="manual", help="free text stored with the run (cron, manual, ...)")
    run.set_defaults(func=cmd_run)
    status = sub.add_parser("status", help="per-source freshness and failures; exit 0/1/3")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)
    cov = sub.add_parser("coverage", help="per-source coverage of the live calls (Markdown table)")
    cov.add_argument("--json", action="store_true")
    cov.set_defaults(func=cmd_coverage)
    letters = sub.add_parser("dead-letters", help="list or retry failed items")
    letters.add_argument("action", choices=["list", "retry"])
    letters.add_argument("--source")
    letters.add_argument("--limit", type=int, default=100)
    letters.set_defaults(func=cmd_dead_letters)
    sub.add_parser("prune", help="apply the retention policy now").set_defaults(func=cmd_prune)
    embed = sub.add_parser("embed", help="embed chunks/records with MODEL_EMBEDDINGS when OLLAMA_URL answers")
    embed.add_argument("--limit", type=int, default=None)
    embed.add_argument("--records-only", action="store_true", help="embed only call records, not document chunks")
    embed.set_defaults(func=cmd_embed)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"[ERROR] configuration: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        from external_sources.store.db import DatabaseUnavailable
        if isinstance(exc, DatabaseUnavailable):
            print(f"[ERROR] {exc}", file=sys.stderr)
            return 3
        raise


if __name__ == "__main__":
    sys.exit(main())
