"""The sync engine: reconciles ticked Notion tasks with their Google Calendar events."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .gcal_api import Calendar, Event, NotEventOwner
from .model import (
    EVENT_HASH_KEY,
    NOTION_HASH_KEY,
    PAGE_KEY,
    Task,
    When,
    build_event,
    current_event_hash,
    parse_timestamp,
    private_props,
    title_from_summary,
)
from .notion_api import NotionTasks

log = logging.getLogger(__name__)


@dataclass
class Stats:
    created: int = 0
    pushed: int = 0
    pulled: int = 0
    deleted: int = 0
    unlinked: int = 0
    unchanged: int = 0
    skipped: int = 0
    blocked: int = 0
    errors: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.created or self.pushed or self.pulled or self.deleted or self.unlinked or self.blocked or self.errors
        )

    def __str__(self) -> str:
        return ", ".join(f"{count} {name}" for name, count in vars(self).items() if count) or "nothing to sync"


class Syncer:
    def __init__(self, cfg: Config, notion: NotionTasks, calendar: Calendar) -> None:
        self.cfg = cfg
        self.notion = notion
        self.calendar = calendar

    def run_once(self) -> Stats:
        stats = Stats()
        events = {event["id"]: event for event in self.calendar.list_managed(self.cfg.data_source_id)}
        by_page = {private_props(event).get(PAGE_KEY): event for event in events.values()}
        seen_pages: set[str] = set()
        claimed: set[str] = set()

        for page in self.notion.query_candidates():
            task = Task.from_page(page, self.cfg)
            seen_pages.add(task.page_id)
            try:
                self._sync_task(task, events, by_page, claimed, stats)
            except Exception:
                stats.errors += 1
                log.exception("Failed to sync task %r", task.title)

        # Managed events no task claimed: their task was deleted, unticked with the
        # event ID cleared by hand, or they are duplicates.
        for event_id, event in events.items():
            if event_id in claimed:
                continue
            try:
                self._remove_orphan(event, seen_pages, stats)
            except Exception:
                stats.errors += 1
                log.exception("Failed to clean up event %r", event.get("summary"))
        return stats

    def _sync_task(
        self,
        task: Task,
        events: dict[str, Event],
        by_page: dict[str | None, Event],
        claimed: set[str],
        stats: Stats,
    ) -> None:
        linked_id = task.event_id
        event = (events.get(linked_id) or self.calendar.get(linked_id)) if linked_id else None
        if event is not None and private_props(event).get(PAGE_KEY, task.page_id) != task.page_id:
            # This page was duplicated from another task and copied its event ID.
            self.notion.set_event_id(task.page_id, None)
            linked_id, event = "", None
        if not linked_id and task.page_id in by_page:
            # The event exists but its ID never made it back to Notion (e.g. an interrupted run).
            event = by_page[task.page_id]
            linked_id = event["id"]
            self.notion.set_event_id(task.page_id, linked_id)
            log.info("Relinked %r to its existing event", task.title)
        if event is not None:
            claimed.add(event["id"])
            if event.get("status") == "cancelled":
                event = None

        if not task.checked or task.when is None:
            if event is not None:
                try:
                    self.calendar.delete(event["id"])
                except NotEventOwner:
                    stats.blocked += 1
                    log.warning(
                        "Can't remove the event for %r: it predates the service account and Google won't let it "
                        "delete an event it didn't create. Delete it by hand in Google Calendar.",
                        task.title,
                    )
                    return
                stats.deleted += 1
                reason = "unticked" if not task.checked else f"no {self.cfg.prop_date} date"
                log.info("Removed event for %r (%s)", task.title, reason)
            if linked_id:
                self.notion.set_event_id(task.page_id, None)
            if event is None and task.checked:
                stats.skipped += 1
                log.debug("Skipping %r: no %s date", task.title, self.cfg.prop_date)
            return

        if event is None:
            if linked_id and self.cfg.two_way:
                self.notion.unlink(task.page_id)
                stats.unlinked += 1
                log.info("Event for %r was deleted in Google Calendar; unticked the task", task.title)
                return
            created = self.calendar.insert(build_event(task, self.cfg))
            self.notion.set_event_id(task.page_id, created["id"])
            stats.created += 1
            log.info("Created event for %r", task.title)
            return

        self._reconcile(task, event, stats)

    def _reconcile(self, task: Task, event: Event, stats: Stats) -> None:
        stored = private_props(event)
        body = build_event(task, self.cfg)
        notion_changed = body["extendedProperties"]["private"][NOTION_HASH_KEY] != stored.get(NOTION_HASH_KEY)
        google_changed = current_event_hash(event) != stored.get(EVENT_HASH_KEY)
        if not (notion_changed or google_changed):
            stats.unchanged += 1
            log.debug("Unchanged: %r", task.title)
            return

        # When both sides changed since the last run, the most recent edit wins.
        google_wins = not notion_changed or parse_timestamp(event["updated"]) > task.last_edited
        if self.cfg.two_way and google_changed and google_wins:
            task = self._pull(task, event)
            body = build_event(task, self.cfg)
            stats.pulled += 1
            log.info("Updated %r from Google Calendar", task.title)
        else:
            stats.pushed += 1
            log.info("Updated event for %r", task.title)
        # Always patch: it writes the new fingerprints, and pushes any Notion-only fields.
        try:
            self.calendar.patch(event["id"], body)
        except NotEventOwner:
            # Predates the service account (created under the old OAuth sign-in). Google
            # won't let it edit that event in place, so replace it with a fresh one the
            # service account owns; this won't happen again for events it creates itself.
            try:
                recreated = self.calendar.replace(event["id"], body)
            except NotEventOwner:
                stats.blocked += 1
                log.warning(
                    "Can't update the event for %r: it predates the service account and Google won't even let "
                    "it delete an event it didn't create. Delete it by hand in Google Calendar.",
                    task.title,
                )
                return
            self.notion.set_event_id(task.page_id, recreated["id"])
            log.info("Recreated the event for %r under the service account", task.title)

    def _pull(self, task: Task, event: Event) -> Task:
        """Copy the event's title and time into the Notion task."""
        when = When.from_gcal(event["start"], event["end"])
        title = title_from_summary(event.get("summary", ""), self.cfg)
        new_title = title if title and title != task.title else None
        new_due = None if task.when and when.key() == task.when.key() else when.to_notion(self.cfg.default_duration)
        if new_title is None and new_due is None:
            return task
        page = self.notion.update_task(task.page_id, title=new_title, due=new_due)
        return Task.from_page(page, self.cfg)

    def _remove_orphan(self, event: Event, seen_pages: set[str], stats: Stats) -> None:
        page_id = private_props(event).get(PAGE_KEY)
        if page_id not in seen_pages:
            page = self.notion.get_page(page_id) if page_id else None
            if page and not (page.get("in_trash") or page.get("archived")) and Task.from_page(page, self.cfg).checked:
                return  # Still wanted; the next query will pick it up.
        try:
            self.calendar.delete(event["id"])
        except NotEventOwner:
            stats.blocked += 1
            log.warning(
                "Can't remove event %r: it predates the service account and Google won't let it delete an "
                "event it didn't create. Delete it by hand in Google Calendar.",
                event.get("summary"),
            )
            return
        stats.deleted += 1
        log.info("Removed event %r: its Notion task was deleted, unticked or already has an event", event.get("summary"))
