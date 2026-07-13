"""WebSocket synchronization service for incremental task-tree updates."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect

from app.core.event_bus import AsyncEventBus
from app.models.domain_events import DomainEvent, EventType


@dataclass(slots=True)
class ClientSubscription:
    """Connection metadata for selective event broadcasting."""

    scope_node_id: str | None = None


class WebSocketSyncService:
    """Broadcast task tree mutations and status updates to connected clients."""

    def __init__(self, *, event_bus: AsyncEventBus) -> None:
        self._event_bus = event_bus
        self._clients: dict[WebSocket, ClientSubscription] = {}
        self._clients_lock = asyncio.Lock()
        self._registered = False

    def register(self) -> None:
        """Subscribe transport handlers to domain event streams."""
        if self._registered:
            return

        self._event_bus.subscribe(EventType.NEW_TASK, self._relay_event)
        self._event_bus.subscribe(EventType.TASK_STATUS_UPDATED, self._relay_event)
        self._event_bus.subscribe(EventType.TREE_MUTATED, self._relay_event)
        self._registered = True

    async def serve(self, websocket: WebSocket) -> None:
        """Handle websocket lifecycle and lightweight ping/pong control."""

        scope = websocket.query_params.get("scope")
        await self._connect(websocket, ClientSubscription(scope_node_id=scope))

        try:
            while True:
                message = await websocket.receive_text()
                if message.strip().lower() == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            await self._disconnect(websocket)

    async def _relay_event(self, event: DomainEvent) -> None:
        """Forward selected domain events to websocket clients."""

        payload = event.to_transport_dict()
        target_node_id = None
        if isinstance(event.payload, dict):
            raw_target = event.payload.get("target_node_id")
            target_node_id = raw_target if isinstance(raw_target, str) else None

        await self._broadcast(payload, target_node_id=target_node_id)

    async def _connect(self, websocket: WebSocket, subscription: ClientSubscription) -> None:
        """Accept and register a websocket connection."""

        await websocket.accept()
        async with self._clients_lock:
            self._clients[websocket] = subscription

    async def _disconnect(self, websocket: WebSocket) -> None:
        """Remove websocket connection from active registry."""

        async with self._clients_lock:
            self._clients.pop(websocket, None)

    async def _broadcast(self, payload: dict, target_node_id: str | None) -> None:
        """Broadcast payload with optional scope-based filtering."""

        async with self._clients_lock:
            connections = list(self._clients.items())

        stale: list[WebSocket] = []
        for websocket, subscription in connections:
            if not self._is_subscribed(subscription, target_node_id):
                continue

            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)

        if stale:
            async with self._clients_lock:
                for websocket in stale:
                    self._clients.pop(websocket, None)

    def _is_subscribed(
        self,
        subscription: ClientSubscription,
        target_node_id: str | None,
    ) -> bool:
        """Evaluate if a client should receive an event for a node scope."""

        if subscription.scope_node_id in (None, "", "all"):
            return True
        if target_node_id is None:
            return True
        return subscription.scope_node_id == target_node_id
