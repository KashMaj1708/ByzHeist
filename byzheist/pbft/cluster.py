"""PBFT 4-replica cluster manager and asynchronous load generator."""

from __future__ import annotations

import asyncio
import random
from typing import Any

from nacl.signing import SigningKey, VerifyKey

from byzheist import config
from byzheist.pbft.client import PBFTClient
from byzheist.pbft.crypto import generate_keypair
from byzheist.pbft.messages import Reply
from byzheist.pbft.network import InMemoryNetwork
from byzheist.pbft.replica import Replica


class Cluster:
    """Builds and runs n=4 PBFT replicas over an in-memory network with a signed client."""

    def __init__(
        self,
        latency_range_ms: tuple[float, float] = config.LATENCY_RANGE_MS,
        view_change_timeout_ms: float = config.VIEW_CHANGE_TIMEOUT_MS,
        client_timeout_ms: float = config.CLIENT_TIMEOUT_MS,
        seed: int | None = None,
    ) -> None:
        self._rng = random.Random(seed)
        self.latency_range_ms = latency_range_ms
        self.view_change_timeout_ms = view_change_timeout_ms
        self.client_timeout_ms = client_timeout_ms

        self.network = InMemoryNetwork(
            latency_range_ms=latency_range_ms,
            rng=random.Random(self._rng.randint(0, 2**31 - 1)),
        )

        self.replica_ids: tuple[str, ...] = config.REPLICA_IDS
        self._signing_keys: dict[str, SigningKey] = {}
        self.peer_keys: dict[str, VerifyKey] = {}
        for rid in self.replica_ids:
            sk, vk = generate_keypair()
            self._signing_keys[rid] = sk
            self.peer_keys[rid] = vk

        self.client_keys: dict[str, VerifyKey] = {}
        self.client = PBFTClient(
            client_id="client-0",
            network=self.network,
            replica_keys=self.peer_keys,
            replica_ids=self.replica_ids,
            client_timeout_ms=client_timeout_ms,
        )
        self.client_keys[self.client.client_id] = self.client.verify_key

        self.replicas: dict[str, Replica] = {
            rid: Replica(
                replica_id=rid,
                signing_key=self._signing_keys[rid],
                peer_keys=self.peer_keys,
                client_keys=self.client_keys,
                network=self.network,
                replica_ids=self.replica_ids,
                view_change_timeout_ms=view_change_timeout_ms,
            )
            for rid in self.replica_ids
        }

        self._load_tasks: set[asyncio.Task[Any]] = set()
        self._running: bool = False

    async def start(self) -> None:
        self._running = True
        for replica in self.replicas.values():
            await replica.start()

    async def stop(self) -> None:
        self._running = False
        load_tasks = list(self._load_tasks)
        for task in load_tasks:
            task.cancel()
        if load_tasks:
            await asyncio.gather(*load_tasks, return_exceptions=True)
        self._load_tasks.clear()

        for replica in self.replicas.values():
            await replica.stop()
        self.client.close()
        await self.network.close()

    async def submit(
        self,
        sender: str,
        recipient: str,
        amount: int,
        timeout_s: float | None = None,
    ) -> Reply:
        return await self.client.submit(
            sender=sender,
            recipient=recipient,
            amount=amount,
            timeout_s=timeout_s,
        )

    def start_load_generator(self, rate_hz: float = 5.0) -> asyncio.Task[None]:
        task = asyncio.create_task(self.load_generator(rate_hz=rate_hz))
        self._load_tasks.add(task)
        task.add_done_callback(self._load_tasks.discard)
        return task

    async def load_generator(self, rate_hz: float = 5.0) -> None:
        """Continuously submit random transfers across A0..A99 at rate_hz."""
        interval = 1.0 / max(rate_hz, 0.1)
        in_flight: set[asyncio.Task[Any]] = set()

        async def _one_transfer(src_acc: str, dst_acc: str, amt: int) -> None:
            try:
                await self.submit(src_acc, dst_acc, amt)
            except (TimeoutError, asyncio.CancelledError):
                pass

        try:
            while self._running:
                src_acc, dst_acc = self._rng.sample(config.ACCOUNT_IDS, 2)
                amt = self._rng.randint(1, 15)
                t = asyncio.create_task(_one_transfer(src_acc, dst_acc, amt))
                in_flight.add(t)
                t.add_done_callback(in_flight.discard)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        finally:
            for t in list(in_flight):
                t.cancel()
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)

    def state_hashes(self) -> dict[str, str]:
        return {rid: replica.state_hash() for rid, replica in self.replicas.items()}

    def balances(self, replica_id: str) -> dict[str, int]:
        return dict(self.replicas[replica_id].balances)
