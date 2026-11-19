"""Short state writes that a cancellation must not cut in half.

A write to SQLite runs on aiosqlite's thread. If the task awaiting it is cancelled
mid-write, the commit may still land after the task has moved on, and nothing rolls that
row back: a graceful stop would leave a ``downloading`` or ``extracting`` row behind. So the
writes that change a row's status run to completion under ``asyncio.shield``; the
cancellation continues once their outcome is known.
"""

import asyncio
from collections.abc import Awaitable, Callable


async def write_through[T](write: Awaitable[T]) -> T:
    """Await ``write`` to the end even if the caller is cancelled meanwhile, then let the
    cancellation continue."""
    task = asyncio.ensure_future(write)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        if not task.cancelled():
            task.exception()  # retrieved: the cancellation is what the caller sees
        raise


async def claim(write: Awaitable[None], undo: Callable[[], Awaitable[None]]) -> None:
    """Run a claim (``x -> in progress``). If the caller is cancelled while it runs and the
    claim landed anyway, ``undo`` puts the row back before the cancellation continues.
    The claim's own errors (``InvalidTransition``) reach the caller as usual."""
    task = asyncio.ensure_future(write)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        if not task.cancelled() and task.exception() is None:
            await undo()
        raise
