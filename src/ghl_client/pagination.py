"""Cursor pagination that refuses to lie.

The failure mode being fixed is common and expensive: a page fails, the caller
catches the exception, returns what it has, and a downstream automation treats
"page 1 of 5" as the complete contact list. Then it deletes the other 80%.

So: exhaustion is always explicit. You get a ``Collection`` whose ``complete``
flag is honest, or you get an exception. There is no third option.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from .errors import GhlError, GhlPaginationError, GhlPartialResultError

T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None = None


@dataclass
class Collection(Generic[T]):
    items: list[T] = field(default_factory=list)
    pages_fetched: int = 0
    complete: bool = True
    errors: list[GhlError] = field(default_factory=list)
    truncated_reason: str | None = None

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    @property
    def partial(self) -> bool:
        return not self.complete


def paginate(
    fetch_page: Callable[[str | None], Page[T]],
    *,
    max_pages: int = 200,
    max_items: int | None = None,
    strict: bool = True,
) -> Collection[T]:
    """Walk a cursor until exhaustion.

    ``strict=True`` (default) raises ``GhlPartialResultError`` after any page
    failure, carrying what was already collected. ``strict=False`` returns a
    Collection with ``complete=False`` and the errors attached, for callers that
    genuinely want best-effort behaviour and will check the flag.
    """

    collection: Collection[T] = Collection()
    cursor: str | None = None
    seen_cursors: set[str] = set()

    while True:
        try:
            page = fetch_page(cursor)
        except GhlError as exc:
            if strict:
                raise GhlPartialResultError(
                    f"pagination stopped after {collection.pages_fetched} page(s): {exc}",
                    collected=collection.items,
                    errors=[exc],
                ) from exc
            collection.errors.append(exc)
            collection.complete = False
            collection.truncated_reason = "page_failure"
            return collection

        collection.pages_fetched += 1
        collection.items.extend(page.items)

        if max_items is not None and len(collection.items) >= max_items:
            del collection.items[max_items:]
            collection.complete = False
            collection.truncated_reason = "max_items"
            return collection

        cursor = page.next_cursor
        if not cursor:
            return collection

        if cursor in seen_cursors:
            raise GhlPaginationError(
                f"cursor repeated after {collection.pages_fetched} page(s): "
                f"{cursor!r} — refusing to loop"
            )
        seen_cursors.add(cursor)

        if collection.pages_fetched >= max_pages:
            collection.complete = False
            collection.truncated_reason = "max_pages"
            return collection


def extract_cursor(payload: dict[str, Any]) -> str | None:
    """HighLevel v2 returns the next cursor under ``meta`` in several shapes."""

    meta = payload.get("meta") or {}
    for key in ("nextPageUrl", "nextCursor", "startAfterId", "startAfter"):
        value = meta.get(key)
        if value:
            return str(value)
    return None
