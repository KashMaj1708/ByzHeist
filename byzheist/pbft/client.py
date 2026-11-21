"""PBFT bank client: signs requests, waits for f+1 matching replies, broadcasts on timeout."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from nacl.signing import SigningKey, VerifyKey

from byzheist import config
from byzheist.pbft.crypto import generate_keypair
from byzheist.pbft.messages import Reply, Request
from byzheist.pbft.network import InMemoryNetwork
from byzheist.pbft.replica import primary_for_view


class PBFTClient:
    """Signed PBFT client waiting for f+1 matching verified replica replies."""

    def __init__(
        self,
        client_id: str,
        network: InMemoryNetwork,
        replica_keys: dict[str, VerifyKey],
        replica_ids: tuple[str, ...] = config.REPLICA_IDS,
        client_timeout_ms: float = config.CLIENT_TIMEOUT_MS,
        signing_key: SigningKey | None = None,
    ) -> None:
        self.client_id = client_id
        self.network = network
        self.replica_keys = replica_keys
        self.replica_ids = replica_ids
        self.client_timeout_s = client_timeout_ms / 1000.0
        sk, vk = (signing_key, signing_key.verify_key) if signing_key else generate_keypair()
        self.signing_key = sk
        self.verify_key = vk

        self._next_ts: int = 1
        self._view_hint: int = 0
        self._waiters: dict[int, asyncio.Future[Reply]] = {}
        self._replies: dict[tuple[int, str], dict[str, Reply]] = defaultdict(dict)
        self.network.register(self.client_id, self._on_message)

    def next_timestamp(self) -> int:
        ts = self._next_ts
        self._next_ts += 1
        return ts

    def create_request(
        self,
        sender: str,
        recipient: str,
        amount: int,
        timestamp: int | None = None,
    ) -> Request:
        ts = self.next_timestamp() if timestamp is None else timestamp
        if ts >= self._next_ts:
            self._next_ts = ts + 1
        return Request(
            client_id=self.client_id,
            timestamp=ts,
            sender=sender,
            recipient=recipient,
            amount=amount,
            noop=False,
        ).signed(self.signing_key)

    async def _on_message(self, src: str, msg: Any, send_ts: float) -> None:
        if not isinstance(msg, Reply):
            return
        if msg.client_id != self.client_id:
            return
        vk = self.replica_keys.get(msg.signer)
        if vk is None or not msg.verify(vk):
            return

        bucket = self._replies[(msg.timestamp, msg.result)]
        bucket[msg.signer] = msg
        if len(bucket) >= config.CLIENT_QUORUM:
            if msg.view > self._view_hint:
                self._view_hint = msg.view
            fut = self._waiters.get(msg.timestamp)
            if fut is not None and not fut.done():
                fut.set_result(msg)

    async def submit_request(
        self,
        request: Request,
        timeout_s: float | None = None,
        broadcast_first: bool = False,
    ) -> Reply:
        """Submit a pre-constructed Request; retry via broadcast on per-attempt timeout."""
        total_timeout = timeout_s if timeout_s is not None else max(self.client_timeout_s * 5.0, 2.0)
        loop = asyncio.get_running_loop()

        # Clear any prior reply state for this timestamp so replay tests observe fresh quorum
        for key in [k for k in self._replies if k[0] == request.timestamp]:
            del self._replies[key]

        fut: asyncio.Future[Reply] = loop.create_future()
        self._waiters[request.timestamp] = fut

        deadline = loop.time() + total_timeout
        first_attempt = True
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Client {self.client_id} timed out waiting for f+1 replies on ts={request.timestamp}"
                    )

                if first_attempt and not broadcast_first:
                    primary = primary_for_view(self._view_hint, self.replica_ids)
                    self.network.send(self.client_id, primary, request)
                    first_attempt = False
                else:
                    self.network.broadcast(self.client_id, request, dsts=self.replica_ids)

                step_wait = min(self.client_timeout_s, remaining)
                try:
                    return await asyncio.wait_for(asyncio.shield(fut), timeout=step_wait)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._waiters.pop(request.timestamp, None)

    async def submit(
        self,
        sender: str,
        recipient: str,
        amount: int,
        timeout_s: float | None = None,
    ) -> Reply:
        """Sign and submit a transfer, returning the f+1 quorum Reply."""
        req = self.create_request(sender=sender, recipient=recipient, amount=amount)
        return await self.submit_request(req, timeout_s=timeout_s)

    def close(self) -> None:
        self.network.unregister(self.client_id)
        for fut in self._waiters.values():
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
