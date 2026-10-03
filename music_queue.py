from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import ceil
from typing import Deque


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    requester_id: int
    duration: int | None = None


class TrackQueue:
    def __init__(self) -> None:
        self._items: Deque[Track] = deque()

    def add(self, tracks: list[Track]) -> None:
        self._items.extend(tracks)

    def pop(self) -> Track | None:
        return self._items.popleft() if self._items else None

    def clear(self) -> int:
        count = len(self._items)
        self._items.clear()
        return count

    def snapshot(self) -> list[Track]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)


class SkipVotes:
    def __init__(self) -> None:
        self._voters: set[int] = set()

    def reset(self) -> None:
        self._voters.clear()

    def vote(self, user_id: int, listener_count: int) -> tuple[int, int, bool]:
        self._voters.add(user_id)
        required = max(1, ceil(listener_count / 2))
        return len(self._voters), required, len(self._voters) >= required
