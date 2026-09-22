"""Command line: python -m notion_gcal_sync [--setup | --watch] [-v]."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from logging.handlers import RotatingFileHandler

from notion_client import Client

from .config import PROJECT_ROOT, Config, ConfigError
from .gcal_api import Calendar, build_service
from .notion_api import NotionTasks
from .sync import Syncer

log = logging.getLogger("notion_gcal_sync")


def _configure_logging(verbose: bool) -> None:
    handlers: list[logging.Handler] = [
        RotatingFileHandler(PROJECT_ROOT / "sync.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    ]
    for stream in (sys.stdout, sys.stderr):
        # Windows consoles default to cp1252, which can't print the emoji statuses.
        # Both streams are None under pythonw.exe.
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # otherwise logs every request
    if verbose:
        log.setLevel(logging.DEBUG)


def _setup(cfg: Config, notion: NotionTasks) -> int:
    for name in notion.check_schema(create_missing=True):
        log.info("Added the '%s' property to the Notion database", name)
    log.info("Notion: OK")
    Calendar(build_service(cfg), cfg.calendar_id).check_access(cfg)
    log.info("Google Calendar: OK (calendar %r)", cfg.calendar_id)
    log.info(
        "Setup complete. Tick '%s' on a task that has a '%s' date, then run: python -m notion_gcal_sync",
        cfg.prop_checkbox,
        cfg.prop_date,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notion-gcal-sync", description="Sync ticked Notion tasks with Google Calendar.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--setup", action="store_true", help="add the sync properties to Notion and check Google Calendar access")
    mode.add_argument("--watch", action="store_true", help="keep running, syncing every SYNC_INTERVAL_SECONDS")
    parser.add_argument("-v", "--verbose", action="store_true", help="also log unchanged and skipped tasks")
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)

    try:
        cfg = Config.from_env()
        # Passing our own logger stops notion-client adding a second console handler.
        client = Client(auth=cfg.notion_token, logger=logging.getLogger("notion_client"), log_level=logging.ERROR)
        notion = NotionTasks(client, cfg)
        if args.setup:
            return _setup(cfg, notion)
        notion.check_schema()
        calendar = Calendar(build_service(cfg), cfg.calendar_id)
        calendar.check_access(cfg)
        syncer = Syncer(cfg, notion, calendar)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if not args.watch:
        try:
            stats = syncer.run_once()
        except Exception:
            log.exception("Sync failed")
            return 1
        log.info("Sync finished: %s", stats)
        return 1 if stats.errors or stats.blocked else 0

    log.info("Syncing every %s seconds. Press Ctrl+C to stop.", cfg.sync_interval_seconds)
    try:
        while True:
            try:
                stats = syncer.run_once()
                log.log(logging.INFO if stats.changed else logging.DEBUG, "Sync finished: %s", stats)
            except Exception:
                log.exception("Sync failed; retrying next cycle")
            time.sleep(cfg.sync_interval_seconds)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
