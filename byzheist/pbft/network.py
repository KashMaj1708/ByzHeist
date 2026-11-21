"""Asyncio in-memory network bus with latency, drop rules, quarantine, and event log."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import random
import time
from typing import Any, Awaitable, Callable, Iterable

from byzheist import config


MessageHandler = Callable[[str, Any, float], Awaitable[None]]


def _extract_event_fields(msg: Any) -> tuple[str, int | None, int | None, str | None]:
    msg_type = getattr(msg, "TYPE", type(msg).__name__)
    view = getattr(msg, "view", getattr(msg, "new_view", None))
    seq = getattr(msg, "seq", None)
    digest = getattr(msg, "digest", None)
    if digest is None and hasattr(msg, "digest") and callable(msg.digest):
        digest = msg.digest()
    return str(msg_type), view, seq, digest


class InMemoryNetwork:
    """Single-process simulated async network with per-link latency, drop, and quarantine."""

    def __init__(
        self,
        latency_range_ms: tuple[float, float] = config.LATENCY_RANGE_MS,
        event_log_maxlen: int = config.EVENT_LOG_MAXLEN,
        rng: random.Random | None = None,
    ) -> None:
        self.latency_min_ms, self.latency_max_ms = latency_range_ms
        self._rng = rng or random.Random()
        self._handlers: dict[str, MessageHandler] = {}
        self.drop_links: set[tuple[str, str]] = set()
        self.dropped_nodes: set[str] = set()
        self.blocked_senders: dict[str, set[str]] = defaultdict(set)
        self.event_log: deque[dict[str, Any]] = deque(maxlen=event_log_maxlen)
        self._in_flight: set[asyncio.Task[None]] = set()
        self._closed: bool = False

    def register(self, node_id: str, handler: MessageHandler) -> None:
        """Register async delivery callback `(src, msg, send_ts) -> None` for node_id."""
        self._handlers[node_id] = handler

    def unregister(self, node_id: str) -> None:
        self._handlers.pop(node_id, None)

    def set_link_drop(self, src: str, dst: str, drop: bool = True) -> None:
        """Drop or restore traffic on directed link (src -> dst)."""
        if drop:
            self.drop_links.add((src, dst))
        else:
            self.drop_links.discard((src, dst))

    def drop_node(self, node_id: str) -> None:
        """Simulate crash/partition by dropping all inbound and outbound traffic for node_id."""
        self.dropped_nodes.add(node_id)

    def restore_node(self, node_id: str) -> None:
        """Restore traffic for a previously crashed/dropped node_id."""
        self.dropped_nodes.discard(node_id)

    def block_sender(self, receiver_id: str, sender_id: str) -> None:
        """Quarantine filter: receiver_id ignores all messages from sender_id."""
        self.blocked_senders[receiver_id].add(sender_id)

    def unblock_sender(self, receiver_id: str, sender_id: str) -> None:
        """Remove sender_id from receiver_id's quarantine filter."""
        self.blocked_senders[receiver_id].discard(sender_id)

    def send(
        self,
        src: str,
        dst: str,
        msg: Any,
        extra_delay_ms: float = 0.0,
    ) -> None:
        """Schedule asynchronous delivery of msg from src to dst."""
        if self._closed or dst not in self._handlers:
            return

        now = time.monotonic()
        msg_type, view, seq, digest = _extract_event_fields(msg)
        dropped = (
            src in self.dropped_nodes
            or dst in self.dropped_nodes
            or (src, dst) in self.drop_links
            or src in self.blocked_senders.get(dst, set())
        )

        self.event_log.append(
            {
                "t": time.time(),
                "mono_t": now,
                "src": src,
                "dst": dst,
                "type": msg_type,
                "view": view,
                "seq": seq,
                "digest": digest,
                "dropped": dropped,
            }
        )

        if dropped:
            return

        base_ms = (
            0.0
            if src == dst
            else self._rng.uniform(self.latency_min_ms, self.latency_max_ms)
        )
        delay_s = max(0.0, (base_ms + extra_delay_ms) / 1000.0)
        task = asyncio.create_task(self._deliver(src, dst, msg, now, delay_s))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    def broadcast(
        self,
        src: str,
        msg: Any,
        dsts: Iterable[str] | None = None,
        extra_delay_ms: float = 0.0,
    ) -> None:
        """Send msg from src to every destination in dsts (defaults to all registered nodes)."""
        targets = list(dsts) if dsts is not None else list(self._handlers.keys())
        for dst in targets:
            self.send(src, dst, msg, extra_delay_ms=extra_delay_ms)

    async def _deliver(
        self,
        src: str,
        dst: str,
        msg: Any,
        send_ts: float,
        delay_s: float,
    ) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        if self._closed:
            return
        # Re-check dynamic drop/quarantine rules on arrival
        if (
            src in self.dropped_nodes
            or dst in self.dropped_nodes
            or (src, dst) in self.drop_links
            or src in self.blocked_senders.get(dst, set())
        ):
            return
        handler = self._handlers.get(dst)
        if handler is not None:
            await handler(src, msg, send_ts)

    async def close(self) -> None:
        """Cancel all pending in-flight message deliveries."""
        self._closed = True
        pending = list(self._in_flight)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._in_flight.clear()
