"""In-memory stand-ins for the Notion client and the Google Calendar service.

They fake the raw API clients (not our wrappers), so tests cover the real request
payloads, pagination and error handling in notion_api.py and gcal_api.py.
"""

from __future__ import annotations

import copy
import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httplib2
import httpx
from googleapiclient.errors import HttpError
from notion_client import APIErrorCode, APIResponseError

PAGE_SIZE = 2  # Small, so pagination is exercised.

DEFAULT_SCHEMA = {
    "Task name": "title",
    "Due": "date",
    "Status": "status",
    "Priority": "select",
    "Tags": "multi_select",
    "Sync to Calendar": "checkbox",
    "GCal Event ID": "rich_text",
}

EMPTY_VALUES = {"title": [], "rich_text": [], "date": None, "checkbox": False, "status": None, "select": None, "multi_select": []}


class Clock:
    """Each tick moves time forward a minute, so edits have a clear order."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)

    def tick(self) -> str:
        self.now += timedelta(minutes=1)
        return self.now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}, "plain_text": content}] if content else []


class FakeNotion:
    def __init__(self, clock: Clock, schema: dict[str, str] | None = None) -> None:
        self.clock = clock
        self.schema = dict(DEFAULT_SCHEMA if schema is None else schema)
        self.store: dict[str, dict[str, Any]] = {}
        self.api_writes = 0
        self._ids = itertools.count(1)
        self.pages = SimpleNamespace(retrieve=self._retrieve, update=self._update)
        self.data_sources = SimpleNamespace(query=self._query, retrieve=self._retrieve_source, update=self._update_source)

    # Helpers for tests (these act as the user, not the API client).

    def add_task(self, title: str, due: dict | None = None, checked: bool = True, status: str = "🍵",
                 priority: str | None = None, tags: tuple[str, ...] = ()) -> str:
        page_id = f"00000000-0000-0000-0000-{next(self._ids):012d}"
        self.store[page_id] = {
            "object": "page",
            "id": page_id,
            "url": f"https://www.notion.so/{page_id.replace('-', '')}",
            "in_trash": False,
            "last_edited_time": self.clock.tick(),
            "properties": {name: {"type": kind, kind: EMPTY_VALUES[kind]} for name, kind in self.schema.items()},
        }
        self.user_edit(page_id, title=title, due=due, checked=checked, status=status, priority=priority, tags=tags)
        return page_id

    def user_edit(self, page_id: str, **changes: Any) -> None:
        names = {"title": "Task name", "due": "Due", "checked": "Sync to Calendar", "status": "Status",
                 "priority": "Priority", "tags": "Tags", "event_id": "GCal Event ID"}
        props = self.store[page_id]["properties"]
        for key, value in changes.items():
            prop = props[names[key]]
            kind = prop["type"]
            if kind in ("title", "rich_text"):
                prop[kind] = _text(value or "")
            elif kind in ("status", "select"):
                prop[kind] = {"name": value} if value else None
            elif kind == "multi_select":
                prop[kind] = [{"name": tag} for tag in value]
            elif kind == "date":
                prop[kind] = dict(value, time_zone=value.get("time_zone")) if value else None
            else:
                prop[kind] = value
        self.store[page_id]["last_edited_time"] = self.clock.tick()

    def trash(self, page_id: str) -> None:
        self.store[page_id]["in_trash"] = True

    def prop(self, page_id: str, name: str) -> Any:
        prop = self.store[page_id]["properties"][name]
        value = prop[prop["type"]]
        if prop["type"] in ("title", "rich_text"):
            return "".join(part["plain_text"] for part in value)
        return value

    # The API surface used by NotionTasks.

    def _not_found(self) -> APIResponseError:
        return APIResponseError(APIErrorCode.ObjectNotFound, 404, "Could not find page.", httpx.Headers(), "")

    def _retrieve(self, page_id: str) -> dict[str, Any]:
        if page_id not in self.store:
            raise self._not_found()
        return copy.deepcopy(self.store[page_id])

    def _update(self, page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        if page_id not in self.store:
            raise self._not_found()
        page = self.store[page_id]
        for name, value in properties.items():
            prop = page["properties"][name]
            kind = prop["type"]
            if kind in ("title", "rich_text"):
                prop[kind] = [part for item in value[kind] for part in _text(item["text"]["content"])]
            elif kind == "date":
                prop[kind] = dict(value[kind], time_zone=None) if value[kind] else None
            else:
                prop[kind] = value[kind]
        page["last_edited_time"] = self.clock.tick()
        self.api_writes += 1
        return copy.deepcopy(page)

    def _matches(self, page: dict[str, Any], flt: dict[str, Any]) -> bool:
        if "or" in flt:
            return any(self._matches(page, sub) for sub in flt["or"])
        prop = page["properties"][flt["property"]]
        if "checkbox" in flt:
            return prop["checkbox"] == flt["checkbox"]["equals"]
        if "rich_text" in flt and flt["rich_text"].get("is_not_empty"):
            return bool(prop["rich_text"])
        raise NotImplementedError(flt)

    def _query(self, data_source_id: str, filter: dict[str, Any], start_cursor: str | None = None) -> dict[str, Any]:
        matches = [p for p in self.store.values() if not p["in_trash"] and self._matches(p, filter)]
        offset = int(start_cursor or 0)
        chunk = matches[offset : offset + PAGE_SIZE]
        more = offset + PAGE_SIZE < len(matches)
        return {"results": copy.deepcopy(chunk), "has_more": more, "next_cursor": str(offset + PAGE_SIZE) if more else None}

    def _retrieve_source(self, data_source_id: str) -> dict[str, Any]:
        return {"object": "data_source", "properties": {name: {"type": kind} for name, kind in self.schema.items()}}

    def _update_source(self, data_source_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        for name, definition in properties.items():
            (kind,) = definition
            self.schema[name] = kind
            for page in self.store.values():
                page["properties"][name] = {"type": kind, kind: EMPTY_VALUES[kind]}
        return self._retrieve_source(data_source_id)


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    """Google's patch semantics: nested objects merge, null clears a field."""
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


class _Request:
    def __init__(self, run: Any) -> None:
        self.execute = run


class FakeCalendar:
    """Stands in for googleapiclient's `build("calendar", "v3")` service."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.store: dict[str, dict[str, Any]] = {}
        self.api_writes = 0
        self._ids = itertools.count(1)

    def events(self) -> FakeCalendar:
        return self

    # Helpers for tests (these act as the user in the Google Calendar UI).

    def live(self) -> list[dict[str, Any]]:
        return [e for e in self.store.values() if e["status"] != "cancelled"]

    def user_edit(self, event_id: str, **fields: Any) -> None:
        self.store[event_id].update(copy.deepcopy(fields))
        self.store[event_id]["updated"] = self.clock.tick()

    def user_delete(self, event_id: str) -> None:
        self.store[event_id]["status"] = "cancelled"

    # The API surface used by Calendar.

    @staticmethod
    def _validate(event: dict[str, Any]) -> None:
        for edge in ("start", "end"):
            if len({"date", "dateTime"} & set(event[edge])) != 1:
                raise _http_error(400)  # Google rejects an event with both or neither.

    def list(self, calendarId: str, privateExtendedProperty: str, maxResults: int, pageToken: str | None = None) -> _Request:
        key, _, value = privateExtendedProperty.partition("=")

        def run() -> dict[str, Any]:
            items = [e for e in self.live() if e.get("extendedProperties", {}).get("private", {}).get(key) == value]
            offset = int(pageToken or 0)
            response: dict[str, Any] = {"items": copy.deepcopy(items[offset : offset + PAGE_SIZE])}
            if offset + PAGE_SIZE < len(items):
                response["nextPageToken"] = str(offset + PAGE_SIZE)
            return response

        return _Request(run)

    def get(self, calendarId: str, eventId: str) -> _Request:
        def run() -> dict[str, Any]:
            if eventId not in self.store:
                raise _http_error(404)
            return copy.deepcopy(self.store[eventId])

        return _Request(run)

    def insert(self, calendarId: str, body: dict[str, Any]) -> _Request:
        def run() -> dict[str, Any]:
            event = copy.deepcopy(body)
            event.update(id=f"evt{next(self._ids)}", status="confirmed", updated=self.clock.tick())
            self._validate(event)
            self.store[event["id"]] = event
            self.api_writes += 1
            return copy.deepcopy(event)

        return _Request(run)

    def patch(self, calendarId: str, eventId: str, body: dict[str, Any]) -> _Request:
        def run() -> dict[str, Any]:
            if eventId not in self.store:
                raise _http_error(404)
            event = copy.deepcopy(self.store[eventId])
            _merge(event, body)
            event["updated"] = self.clock.tick()
            self._validate(event)
            self.store[eventId] = event
            self.api_writes += 1
            return copy.deepcopy(event)

        return _Request(run)

    def delete(self, calendarId: str, eventId: str) -> _Request:
        def run() -> None:
            if eventId not in self.store or self.store[eventId]["status"] == "cancelled":
                raise _http_error(410)
            self.store[eventId]["status"] = "cancelled"
            self.api_writes += 1

        return _Request(run)
