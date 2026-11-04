"""Steps working in one folder at the same time: those whose files do not overlap run together;
a step without a scope (planner.package) has the folder to itself."""
from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from .planner import writes_files


class Scope:
    """What a step changes and reads; ``None`` for a step that may touch anything."""

    def __init__(self, files: Optional[list[str]] = None, reads: Optional[list[str]] = None, title: str = ""):
        self.everything = files is None and reads is None
        self.writes, self.reads = set(files or ()), set(reads or ())
        self.title = title
        # Files that steps alongside this one own, and those they changed beyond them (told when they
        # leave), so neither is counted as its changes.
        self.others: set[str] = set()
        self.left: set[str] = set()
        # Steps that were in the folder at some time while this one was, and the files this one
        # changed (told to the steps still in the folder when it leaves).
        self.alongside: list[Scope] = []
        self.changed: list[str] = []

    def own(self, changed: list[str]) -> list[str]:
        """Of the files changed in the folder while this step ran, those that are its own."""
        return [path for path in changed if not within(path, self.others | self.left)]

    def clashes(self, other: Scope) -> bool:
        if self.everything or other.everything:
            return True
        def overlap(left, right):
            return any(a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")
                       for a in left for b in right)
        return overlap(self.writes, other.writes | other.reads) or overlap(self.reads, other.writes)

    def intrusions(self, changed: list[str]) -> list[tuple[str, str]]:
        """Of the files changed in the folder while this step ran, those outside its own that a step
        alongside it reads, with that step's title: blamed only when every other step alongside it
        is such a reader (one that does not write the file), so no one else can have changed it.
        What steps that left reported changing does not clear it: that report may hold this change."""
        if self.everything:
            return []
        found = []
        for path in changed:
            if within(path, self.writes | self.others):
                continue
            readers = [other for other in self.alongside if within(path, other.reads) and not within(path, other.writes)]
            suspects = [other for other in self.alongside if other not in readers]
            if readers and not suspects:
                found.append((path, readers[0].title))
        return found


def within(path: str, scope: set[str]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in scope)


def of(node) -> Scope:
    """A step's scope: its package's files, else the folders it owns; nothing for a step that writes no files (a review or
    summary); everything for one that writes files the planner did not name."""
    if node.artifact_contract:
        return Scope(list(set(node.files) | {item.path for item in node.artifact_contract.outputs}),
                     list(set(node.reads) | {item.path for item in node.artifact_contract.inputs}), node.title)
    if node.files or node.reads:
        return Scope(node.files, node.reads, node.title)
    if node.folders:
        return Scope(node.folders, [], node.title)
    return Scope(title=node.title) if writes_files(node) else Scope([], [], node.title)


class FolderLock:
    """Lets steps into one folder while their scopes do not clash with those already in it."""

    def __init__(self):
        self.held: list[Scope] = []
        self.changed = asyncio.Condition()

    def busy(self, scope: Scope) -> bool:
        """Whether a step with ``scope`` would have to wait for steps already in the folder."""
        return any(scope.clashes(other) for other in self.held)

    @contextlib.asynccontextmanager
    async def hold(self, scope: Scope):
        async with self.changed:
            await self.changed.wait_for(lambda: not any(scope.clashes(other) for other in self.held))
            for other in self.held:
                other.others |= scope.writes
                scope.others |= other.writes
                other.alongside.append(scope)
                scope.alongside.append(other)
            self.held.append(scope)
        try:
            yield
        finally:
            async with self.changed:
                self.held.remove(scope)
                for other in self.held:
                    # What it actually changed, declared or not, is not the changes of those still in.
                    other.left |= set(scope.changed)
                self.changed.notify_all()
