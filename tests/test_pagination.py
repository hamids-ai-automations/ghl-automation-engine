from __future__ import annotations

import pytest

from ghl_client.errors import GhlPaginationError, GhlPartialResultError, GhlServerError
from ghl_client.pagination import Page, paginate


def test_walks_every_page_until_exhausted():
    pages = {
        None: Page(items=[1, 2], next_cursor="c1"),
        "c1": Page(items=[3, 4], next_cursor="c2"),
        "c2": Page(items=[5], next_cursor=None),
    }
    seen = []

    def fetch(cursor):
        seen.append(cursor)
        return pages[cursor]

    result = paginate(fetch)

    assert result.items == [1, 2, 3, 4, 5]
    assert result.complete is True
    assert result.pages_fetched == 3
    assert seen == [None, "c1", "c2"]


def test_mid_pagination_failure_raises_with_partial_data():
    """The collected rows must travel with the exception, never as a clean result."""

    def fetch(cursor):
        if cursor is None:
            return Page(items=[1, 2], next_cursor="c1")
        raise GhlServerError("boom", status=502)

    with pytest.raises(GhlPartialResultError) as excinfo:
        paginate(fetch)

    err = excinfo.value
    assert err.collected == [1, 2]
    assert len(err.errors) == 1
    assert "after 1 page" in str(err)


def test_non_strict_mode_returns_flagged_incomplete_collection():
    def fetch(cursor):
        if cursor is None:
            return Page(items=[1, 2], next_cursor="c1")
        raise GhlServerError("boom", status=502)

    result = paginate(fetch, strict=False)

    assert result.items == [1, 2]
    assert result.complete is False
    assert result.partial is True
    assert result.truncated_reason == "page_failure"


def test_repeated_cursor_is_refused_instead_of_looping():
    def fetch(cursor):
        return Page(items=[1], next_cursor="same")

    with pytest.raises(GhlPaginationError):
        paginate(fetch)


def test_max_pages_marks_result_incomplete():
    counter = {"n": 0}

    def fetch(cursor):
        counter["n"] += 1
        return Page(items=[counter["n"]], next_cursor=f"c{counter['n']}")

    result = paginate(fetch, max_pages=3)

    assert result.items == [1, 2, 3]
    assert result.complete is False
    assert result.truncated_reason == "max_pages"


def test_max_items_trims_and_marks_incomplete():
    def fetch(cursor):
        return Page(items=[1, 2, 3], next_cursor=f"c-{cursor or 'start'}")

    result = paginate(fetch, max_items=4)

    assert result.items == [1, 2, 3, 1]
    assert result.complete is False
    assert result.truncated_reason == "max_items"
