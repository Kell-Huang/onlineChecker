"""Value objects shared by the core layer and the UI.

A ``Feed`` is one Google Merchant Center comparison feed for one country. A
``Selection`` is the whole saved state: the GOS channel, every feed the user
has configured, and the subset they ticked for the next run.

Keeping the feed list and the ticked subset apart is the point of the new
config: the list is "what I have", the subset is "what I want this time".
Before, both lived in one flat list and every launch meant re-choosing.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


def _as_str(value: Any) -> str:
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class GosFeed:
    """The Global Online Status export channel."""
    site_id: str
    channel_id: str
    url: str
    site_name: str = ""
    channel_name: str = ""
    format: str = "csv"
    id_column: str = "id"

    @property
    def key(self) -> str:
        return f"{self.site_id}/{self.channel_id}"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GosFeed":
        return cls(
            site_id=_as_str(data.get("site_id")),
            channel_id=_as_str(data.get("channel_id")),
            url=_as_str(data.get("url")),
            site_name=_as_str(data.get("site_name")),
            channel_name=_as_str(data.get("channel_name")),
            format=_as_str(data.get("format")) or "csv",
            id_column=_as_str(data.get("id_column")) or "id",
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "site_id": self.site_id,
            "channel_id": self.channel_id,
            "site_name": self.site_name,
            "channel_name": self.channel_name,
            "url": self.url,
            "format": self.format,
            "id_column": self.id_column,
        }


@dataclass
class Feed:
    """One comparison feed, keyed by country."""
    country: str
    site_id: str
    channel_id: str
    url: str
    site_name: str = ""
    channel_name: str = ""
    project_id: str = ""
    format: str = "csv"
    id_column: Optional[str] = None
    gos_columns: Dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.site_id}/{self.channel_id}"

    @property
    def display_name(self) -> str:
        return self.channel_name or self.channel_id

    @property
    def is_usable(self) -> bool:
        return bool(self.site_id and self.channel_id and self.url)

    def columns(self) -> Dict[str, str]:
        """GOS column mapping, falling back to the country table."""
        return dict(self.gos_columns or {})

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Feed":
        columns = data.get("gos_columns") or {}
        return cls(
            country=_as_str(data.get("country")),
            site_id=_as_str(data.get("site_id")),
            channel_id=_as_str(data.get("channel_id")),
            url=_as_str(data.get("url")),
            site_name=_as_str(data.get("site_name")),
            channel_name=_as_str(data.get("channel_name")),
            project_id=_as_str(data.get("project_id")),
            format=_as_str(data.get("format")) or "csv",
            id_column=_as_str(data.get("id_column")) or None,
            gos_columns={str(k): _as_str(v) for k, v in columns.items()},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "country": self.country,
            "site_id": self.site_id,
            "channel_id": self.channel_id,
            "site_name": self.site_name,
            "channel_name": self.channel_name,
            "project_id": self.project_id,
            "url": self.url,
            "format": self.format,
            "id_column": self.id_column,
            "gos_columns": dict(self.gos_columns),
        }


@dataclass
class Selection:
    """Everything the app remembers between launches."""
    gos: Optional[GosFeed] = None
    feeds: List[Feed] = field(default_factory=list)
    selected: List[str] = field(default_factory=list)   # feed keys

    def feed_by_key(self, key: str) -> Optional[Feed]:
        for feed in self.feeds:
            if feed.key == key:
                return feed
        return None

    def selected_feeds(self) -> List[Feed]:
        chosen = set(self.selected)
        return [feed for feed in self.feeds if feed.key in chosen]

    def add_feed(self, feed: Feed, select: bool = True) -> None:
        """Insert or update a feed, keeping its position stable."""
        for index, existing in enumerate(self.feeds):
            if existing.key == feed.key:
                self.feeds[index] = feed
                break
        else:
            self.feeds.append(feed)
        if select and feed.key not in self.selected:
            self.selected.append(feed.key)

    def remove_feed(self, key: str) -> None:
        self.feeds = [feed for feed in self.feeds if feed.key != key]
        self.selected = [item for item in self.selected if item != key]

    def countries(self) -> List[str]:
        seen: List[str] = []
        for feed in self.feeds:
            if feed.country and feed.country not in seen:
                seen.append(feed.country)
        return seen
