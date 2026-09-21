"""The Notion side: reading and updating pages in the tasks data source."""

from __future__ import annotations

from typing import Any

from notion_client import APIErrorCode, APIResponseError, Client
from notion_client.helpers import collect_paginated_api

from .config import Config, ConfigError

Page = dict[str, Any]


def _rich_text(value: str | None) -> dict[str, Any]:
    return {"rich_text": [{"type": "text", "text": {"content": value}}] if value else []}


class NotionTasks:
    def __init__(self, client: Client, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg

    def query_candidates(self) -> list[Page]:
        """Tasks that are ticked, plus unticked ones that still have an event linked."""
        return collect_paginated_api(
            self.client.data_sources.query,
            data_source_id=self.cfg.data_source_id,
            filter={
                "or": [
                    {"property": self.cfg.prop_checkbox, "checkbox": {"equals": True}},
                    {"property": self.cfg.prop_event_id, "rich_text": {"is_not_empty": True}},
                ]
            },
        )

    def get_page(self, page_id: str) -> Page | None:
        try:
            return self.client.pages.retrieve(page_id=page_id)
        except APIResponseError as exc:
            if exc.code == APIErrorCode.ObjectNotFound:
                return None
            raise

    def set_event_id(self, page_id: str, event_id: str | None) -> None:
        self.client.pages.update(page_id=page_id, properties={self.cfg.prop_event_id: _rich_text(event_id)})

    def unlink(self, page_id: str) -> None:
        """Untick the task and forget its event."""
        self.client.pages.update(
            page_id=page_id,
            properties={self.cfg.prop_checkbox: {"checkbox": False}, self.cfg.prop_event_id: _rich_text(None)},
        )

    def update_task(self, page_id: str, title: str | None = None, due: dict[str, Any] | None = None) -> Page:
        properties: dict[str, Any] = {}
        if title is not None:
            properties[self.cfg.prop_title] = {"title": [{"type": "text", "text": {"content": title}}]}
        if due is not None:
            properties[self.cfg.prop_date] = {"date": due}
        return self.client.pages.update(page_id=page_id, properties=properties)

    def check_schema(self, create_missing: bool = False) -> list[str]:
        """Check the configured properties exist with the right types.

        With create_missing, adds the checkbox and event ID properties if they are
        absent. Returns the names of any properties it created.
        """
        try:
            source = self.client.data_sources.retrieve(data_source_id=self.cfg.data_source_id)
        except APIResponseError as exc:
            if exc.code == APIErrorCode.Unauthorized:
                raise ConfigError("Notion rejected NOTION_TOKEN. Check the token in .env.") from exc
            if exc.code in (APIErrorCode.ObjectNotFound, APIErrorCode.RestrictedResource):
                raise ConfigError(
                    "Notion can't find the tasks database. Connect your integration to it: open the "
                    "database, click ... > Connections, and add the integration (see README)."
                ) from exc
            raise

        existing = {name: prop["type"] for name, prop in source["properties"].items()}
        creatable = {self.cfg.prop_checkbox: "checkbox", self.cfg.prop_event_id: "rich_text"}
        required = {self.cfg.prop_title: "title", self.cfg.prop_date: "date", **creatable}
        to_create: dict[str, Any] = {}
        for name, kind in required.items():
            if name not in existing:
                if create_missing and name in creatable:
                    to_create[name] = {kind: {}}
                    continue
                hint = " Run `python -m notion_gcal_sync --setup` to add it." if name in creatable else ""
                raise ConfigError(f"The Notion database has no '{name}' property.{hint}")
            if existing[name] != kind:
                raise ConfigError(f"Notion property '{name}' is a {existing[name]} property; expected {kind}.")

        if to_create:
            self.client.data_sources.update(data_source_id=self.cfg.data_source_id, properties=to_create)
        return list(to_create)
