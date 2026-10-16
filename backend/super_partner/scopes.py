"""Steps working in one folder at the same time: those whose files do not overlap run together;
a step without a scope (planner.package) has the folder to itself."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Optional


class Scope:
    """What a step changes and reads; ``None`` for a step that may touch anything."""

    def __init__(self, files: Optional[list[str]] = None, reads: Optional[list[str]] = None):
        self.everything = files is None and reads is None
        self.writes, self.reads = set(files or ()), set(reads or ())
        # Files that steps alongside this one changed, so they are not counted as its changes.
        self.others: set[str] = set()

    def own(self, changed: list[str]) -> list[str]:
        """Of the files changed in the folder while this step ran, those that are its own."""
        return [path for path in changed if path not in self.others]

    def clashes(self, other: Scope) -> bool:
        if self.everything or other.everything:
            return True
        return bool(self.writes & (other.writes | other.reads) or self.reads & other.writes)


def of(node) -> Scope:
    """A step's scope: its package's files, or everything when the planner gave it none."""
    if not node.files and not node.reads:
        return Scope()
    return Scope(node.files, node.reads)


class FolderLock:
    """Lets steps into one folder while their scopes do not clash with those already in it."""

    def __init__(self):
        self.held: list[Scope] = []
        self.changed = asyncio.Condition()

    @contextlib.asynccontextmanager
    async def hold(self, scope: Scope):
        async with self.changed:
            await self.changed.wait_for(lambda: not any(scope.clashes(other) for other in self.held))
            for other in self.held:
                other.others |= scope.writes
                scope.others |= other.writes
            self.held.append(scope)
        try:
            yield
        finally:
            async with self.changed:
                self.held.remove(scope)
                self.changed.notify_all()
