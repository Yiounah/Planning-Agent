"""Asynchronous Event Bus implementation for decoupled domain orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from app.models.domain_events import DomainEvent, EventType

logger = logging.getLogger(__name__)

EventHandler = Callable[[DomainEvent], Awaitable[None]]


class AsyncEventBus:
    """Async Pub/Sub event bus with queued dispatch semantics.

    The bus guarantees FIFO dequeue order from a single queue and fan-out
    delivery to all handlers subscribed to each event type.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[DomainEvent] = asyncio.Queue()
        self._subscribers: dict[EventType, list[EventHandler]] = defaultdict(list)
        self._wildcard_subscribers: list[EventHandler] = []

        self._dispatch_task: asyncio.Task[None] | None = None
        self._running = False

    def subscribe(self, event_type: EventType | None, handler: EventHandler) -> None:
        """Register an async handler for a concrete event or wildcard stream."""
        if event_type is None:
            self._wildcard_subscribers.append(handler)
            return

        self._subscribers[event_type].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        """Publish an event into the dispatch queue."""
        await self._queue.put(event)

    async def start(self) -> None:
        """Start the background dispatcher loop."""
        if self._running:
            return

        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop(), name="event-bus")

    async def stop(self) -> None:
        """Stop dispatcher loop and drain in-flight tasks."""
        if not self._running:
            return

        self._running = False

        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            finally:
                self._dispatch_task = None

    async def join(self) -> None:
        """Wait until all queued events have been processed."""
        await self._queue.join()

    async def _dispatch_loop(self) -> None:
        """Consume events and dispatch fan-out handler execution."""
        while self._running:
            event = await self._queue.get()
            try:
                handlers = [
                    *self._subscribers.get(event.event_type, []),
                    *self._wildcard_subscribers,
                ]
                if handlers:
                    await asyncio.gather(
                        *(self._safe_invoke(handler, event) for handler in handlers)
                    )
            finally:
                self._queue.task_done()

    async def _safe_invoke(self, handler: EventHandler, event: DomainEvent) -> None:
        """Invoke a handler with exception shielding to keep the bus alive."""
        try:
            await handler(event)
        except Exception:
            logger.exception(
                "Event handler failed",
                extra={"event_type": event.event_type.value, "event_id": event.event_id},
            )
