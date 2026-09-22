from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from notion_gcal_sync.config import Config, ConfigError
from notion_gcal_sync.gcal_api import Calendar
from notion_gcal_sync.model import PAGE_KEY
from notion_gcal_sync.notion_api import NotionTasks
from notion_gcal_sync.sync import Syncer

from .fakes import Clock, FakeCalendar, FakeNotion

TIMED = {"start": "2026-09-22T10:00:00.000-04:00", "end": None}


def make_env(**overrides) -> SimpleNamespace:
    cfg = replace(Config(notion_token="test", data_source_id="ds1", timezone_name="America/New_York"), **overrides)
    clock = Clock()
    notion, gcal = FakeNotion(clock), FakeCalendar(clock)
    syncer = Syncer(cfg, NotionTasks(notion, cfg), Calendar(gcal, "primary"))
    return SimpleNamespace(cfg=cfg, notion=notion, gcal=gcal, sync=syncer.run_once)


@pytest.fixture
def env() -> SimpleNamespace:
    return make_env()


def only_event(env) -> dict:
    (event,) = env.gcal.live()
    return event


def assert_settled(env) -> None:
    """A further run finds nothing to do and writes nothing."""
    writes = env.notion.api_writes, env.gcal.api_writes
    stats = env.sync()
    assert not stats.changed, stats
    assert (env.notion.api_writes, env.gcal.api_writes) == writes


def test_ticked_task_creates_an_all_day_event_and_links_it(env):
    page = env.notion.add_task("Pay rent", due={"start": "2026-09-22"}, priority="High", tags=("Bills",))

    assert env.sync().created == 1

    event = only_event(env)
    assert event["summary"] == "Pay rent"
    assert event["start"] == {"date": "2026-09-22"}
    assert event["end"] == {"date": "2026-09-23"}
    assert event["colorId"] == "11"
    assert "Tags: Bills" in event["description"]
    assert event["extendedProperties"]["private"][PAGE_KEY] == page
    assert env.notion.prop(page, "GCal Event ID") == event["id"]
    assert_settled(env)


def test_unticked_and_undated_tasks_are_left_alone(env):
    env.notion.add_task("Not synced", due={"start": "2026-09-22"}, checked=False)
    env.notion.add_task("No date yet")

    stats = env.sync()

    assert stats.skipped == 1 and not stats.changed
    assert env.gcal.live() == []


def test_timed_task_without_end_gets_default_duration(env):
    env.notion.add_task("Dentist", due=TIMED)
    env.sync()

    event = only_event(env)
    assert event["start"] == {"dateTime": "2026-09-22T10:00:00-04:00"}
    assert event["end"] == {"dateTime": "2026-09-22T11:00:00-04:00"}


def test_date_range_becomes_multi_day_event(env):
    env.notion.add_task("Trip", due={"start": "2026-09-22", "end": "2026-09-24"})
    env.sync()

    assert only_event(env)["end"] == {"date": "2026-09-25"}


def test_notion_edits_are_pushed(env):
    page = env.notion.add_task("Draft", due={"start": "2026-09-22"})
    env.sync()

    env.notion.user_edit(page, title="Final", priority="Low", due=TIMED)
    stats = env.sync()

    assert stats.pushed == 1
    event = only_event(env)
    assert event["summary"] == "Final"
    assert event["colorId"] == "2"
    assert event["start"] == {"dateTime": "2026-09-22T10:00:00-04:00"}  # all-day -> timed clears "date"
    assert_settled(env)


def test_google_edits_are_pulled_into_notion(env):
    page = env.notion.add_task("Gym", due=TIMED)
    env.sync()
    event_id = only_event(env)["id"]

    env.gcal.user_edit(
        event_id,
        summary="Gym (legs)",
        start={"dateTime": "2026-09-23T18:00:00-04:00"},
        end={"dateTime": "2026-09-23T19:30:00-04:00"},
    )
    stats = env.sync()

    assert stats.pulled == 1
    assert env.notion.prop(page, "Task name") == "Gym (legs)"
    assert env.notion.prop(page, "Due")["start"] == "2026-09-23T18:00:00-04:00"
    assert env.notion.prop(page, "Due")["end"] == "2026-09-23T19:30:00-04:00"
    assert_settled(env)


def test_google_edit_keeping_default_duration_leaves_notion_without_end(env):
    page = env.notion.add_task("Call", due=TIMED)
    env.sync()
    event_id = only_event(env)["id"]

    # Same instant written in UTC: not a change.
    env.gcal.user_edit(event_id, start={"dateTime": "2026-09-22T14:00:00Z"}, end={"dateTime": "2026-09-22T15:00:00Z"})
    assert env.sync().unchanged == 1

    env.gcal.user_edit(event_id, start={"dateTime": "2026-09-22T15:00:00Z"}, end={"dateTime": "2026-09-22T16:00:00Z"})
    env.sync()

    assert env.notion.prop(page, "Due") == {"start": "2026-09-22T15:00:00+00:00", "end": None, "time_zone": None}
    assert_settled(env)


def test_google_all_day_edit_is_pulled_as_date(env):
    page = env.notion.add_task("Move", due=TIMED)
    env.sync()

    env.gcal.user_edit(only_event(env)["id"], start={"date": "2026-10-01"}, end={"date": "2026-10-03"})
    env.sync()

    assert env.notion.prop(page, "Due") == {"start": "2026-10-01", "end": "2026-10-02", "time_zone": None}
    assert_settled(env)


def test_completed_task_is_prefixed_and_prefix_is_not_pulled_back(env):
    page = env.notion.add_task("Laundry", due={"start": "2026-09-22"})
    env.sync()

    env.notion.user_edit(page, status="✅")
    env.sync()
    assert only_event(env)["summary"] == "✅ Laundry"

    env.gcal.user_edit(only_event(env)["id"], summary="✅ Laundry + ironing")
    env.sync()
    assert env.notion.prop(page, "Task name") == "Laundry + ironing"
    assert only_event(env)["summary"] == "✅ Laundry + ironing"
    assert_settled(env)


def test_when_both_sides_change_the_latest_edit_wins(env):
    page = env.notion.add_task("Plan", due={"start": "2026-09-22"})
    env.sync()

    env.gcal.user_edit(only_event(env)["id"], summary="Plan (google)")
    env.notion.user_edit(page, title="Plan (notion)")  # later on the fake clock
    env.sync()

    assert only_event(env)["summary"] == "Plan (notion)"
    assert env.notion.prop(page, "Task name") == "Plan (notion)"


def test_unticking_deletes_the_event(env):
    page = env.notion.add_task("Maybe", due={"start": "2026-09-22"})
    env.sync()

    env.notion.user_edit(page, checked=False)
    assert env.sync().deleted == 1

    assert env.gcal.live() == []
    assert env.notion.prop(page, "GCal Event ID") == ""
    assert_settled(env)


def test_clearing_the_date_deletes_the_event(env):
    page = env.notion.add_task("Someday", due={"start": "2026-09-22"})
    env.sync()

    env.notion.user_edit(page, due=None)
    assert env.sync().deleted == 1
    assert env.gcal.live() == []

    env.notion.user_edit(page, due={"start": "2026-09-30"})
    assert env.sync().created == 1


def test_deleting_the_event_in_google_unticks_the_task(env):
    page = env.notion.add_task("Cancelled plan", due={"start": "2026-09-22"})
    env.sync()

    env.gcal.user_delete(only_event(env)["id"])
    assert env.sync().unlinked == 1

    assert env.notion.prop(page, "Sync to Calendar") is False
    assert env.notion.prop(page, "GCal Event ID") == ""
    assert_settled(env)


def test_one_way_mode_recreates_deleted_events_and_overwrites_google_edits():
    env = make_env(two_way=False)
    page = env.notion.add_task("Source of truth", due={"start": "2026-09-22"})
    env.sync()

    env.gcal.user_edit(only_event(env)["id"], summary="Edited in Google")
    assert env.sync().pushed == 1
    assert only_event(env)["summary"] == "Source of truth"

    env.gcal.user_delete(only_event(env)["id"])
    assert env.sync().created == 1
    assert env.notion.prop(page, "GCal Event ID") == only_event(env)["id"]


def test_trashing_the_notion_page_deletes_the_event(env):
    page = env.notion.add_task("Deleted task", due={"start": "2026-09-22"})
    env.sync()

    env.notion.trash(page)
    assert env.sync().deleted == 1
    assert env.gcal.live() == []


def test_lost_event_id_is_relinked_instead_of_duplicated(env):
    page = env.notion.add_task("Interrupted", due={"start": "2026-09-22"})
    env.sync()
    event_id = only_event(env)["id"]

    env.notion.user_edit(page, event_id="")
    env.sync()

    assert only_event(env)["id"] == event_id
    assert env.notion.prop(page, "GCal Event ID") == event_id
    assert_settled(env)


def test_duplicated_page_gets_its_own_event(env):
    original = env.notion.add_task("Weekly review", due={"start": "2026-09-22"})
    env.sync()
    original_event = only_event(env)["id"]

    # Duplicating a page in Notion copies the event ID along with everything else.
    copy_ = env.notion.add_task("Weekly review", due={"start": "2026-09-29"})
    env.notion.user_edit(copy_, event_id=original_event)
    env.sync()

    events = {e["extendedProperties"]["private"][PAGE_KEY]: e for e in env.gcal.live()}
    assert events[original]["id"] == original_event
    assert events[original]["start"] == {"date": "2026-09-22"}
    assert events[copy_]["start"] == {"date": "2026-09-29"}
    assert env.notion.prop(copy_, "GCal Event ID") == events[copy_]["id"]
    assert_settled(env)


def test_many_tasks_across_pages_of_results(env):
    pages = [env.notion.add_task(f"Task {i}", due={"start": f"2026-10-{i + 1:02d}"}) for i in range(5)]

    assert env.sync().created == 5
    assert len(env.gcal.live()) == 5
    env.notion.user_edit(pages[4], checked=False)
    assert env.sync().deleted == 1
    assert_settled(env)


def test_editing_a_pre_service_account_event_recreates_it(env):
    """An event created before the switch to a service account can't be patched in
    place (Google's forbiddenForNonCreator); the sync should replace it instead."""
    page = env.notion.add_task("Legacy event", due={"start": "2026-09-22"})
    env.sync()
    old_id = only_event(env)["id"]
    env.gcal.mark_foreign(old_id)

    env.notion.user_edit(page, title="Legacy event, renamed")
    stats = env.sync()

    assert stats.pushed == 1 and stats.blocked == 0
    new_event = only_event(env)
    assert new_event["id"] != old_id
    assert new_event["summary"] == "Legacy event, renamed"
    assert env.notion.prop(page, "GCal Event ID") == new_event["id"]
    assert_settled(env)


def test_fully_locked_event_is_reported_but_does_not_crash(env):
    """If Google won't even let the service account delete the old event, the sync
    should report it (stats.blocked) rather than raise or spin in a loop."""
    page = env.notion.add_task("Stuck event", due={"start": "2026-09-22"})
    env.sync()
    old_id = only_event(env)["id"]
    env.gcal.mark_foreign(old_id, deletable=False)

    env.notion.user_edit(page, title="Stuck event, renamed")
    stats = env.sync()

    assert stats.blocked == 1
    assert only_event(env)["id"] == old_id  # untouched
    assert env.notion.prop(page, "GCal Event ID") == old_id  # still linked, not orphaned

    # Retrying doesn't crash or duplicate anything either.
    stats = env.sync()
    assert stats.blocked == 1
    assert len(env.gcal.live()) == 1


def test_unticking_a_task_with_an_undeletable_event_is_reported(env):
    page = env.notion.add_task("Can't unsync this one", due={"start": "2026-09-22"})
    env.sync()
    event_id = only_event(env)["id"]
    env.gcal.mark_foreign(event_id, deletable=False)

    env.notion.user_edit(page, checked=False)
    stats = env.sync()

    assert stats.blocked == 1 and stats.deleted == 0
    assert only_event(env)["id"] == event_id  # still there
    assert env.notion.prop(page, "GCal Event ID") == event_id  # left linked, not orphaned


def test_setup_adds_missing_properties():
    clock = Clock()
    notion = FakeNotion(clock, schema={"Task name": "title", "Due": "date"})
    cfg = Config(notion_token="test", data_source_id="ds1")
    tasks = NotionTasks(notion, cfg)

    with pytest.raises(ConfigError, match="Sync to Calendar"):
        tasks.check_schema()
    assert tasks.check_schema(create_missing=True) == ["Sync to Calendar", "GCal Event ID"]
    assert tasks.check_schema() == []


def test_wrong_property_type_is_reported():
    notion = FakeNotion(Clock(), schema={"Task name": "title", "Due": "rich_text"})
    with pytest.raises(ConfigError, match="'Due' is a rich_text property; expected date"):
        NotionTasks(notion, Config(notion_token="test", data_source_id="ds1")).check_schema(create_missing=True)
