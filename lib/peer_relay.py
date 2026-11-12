# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTTP relays from a Diameter node to the other HSS nodes, off the worker's path.

The Diameter worker is serial: every request it handles waits for the one
before. The Rx relays (CCR-T -> ASR on the node holding the AF subscription,
STR -> forget, CCR-U -> failed-resources RAR, call end) were plain
requests.post calls inside the worker, one node after the other, with a 2 s
timeout. When the HPA removed a pod at peak load (r16, 2026-11-12 18:35 UTC),
the workers posted to its address until it was gone from DNS; the Cx, S6a and
Rx requests queued behind them timed out at the DRA on every node (KI-171).

Relays are now queued to one background thread per process, in order (an STR's
forget still reaches the nodes before a later CCR-T's terminate), with a short
connect timeout, and an address that refused or timed out is skipped for a
while instead of being tried for every relay.
"""
import queue
import socket
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests

CONNECT_TIMEOUT = 0.5
READ_TIMEOUT = 2.0
DEAD_SECONDS = 30.0


def _addresses(host: str, port: int) -> List[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


class PeerRelay:
    def __init__(self, post: Callable = requests.post, resolve: Callable = _addresses,
                 now: Callable[[], float] = time.monotonic, log: Optional[Callable[[str, str], None]] = None,
                 dead_seconds: float = DEAD_SECONDS, maxsize: int = 10000, start: bool = True):
        self.post, self.resolve, self.now, self.log = post, resolve, now, log or (lambda level, message: None)
        self.dead_seconds = dead_seconds
        self.dead: Dict[str, float] = {}
        self.queue: "queue.Queue" = queue.Queue(maxsize=maxsize)
        self.thread = None
        if start:
            self.thread = threading.Thread(target=self._run, name="peer-relay", daemon=True)
            self.thread.start()

    def submit(self, endpoints: Iterable[str], path: str, payload: dict, label: str) -> bool:
        """Queue a relay; never blocks. False when the queue is full (logged)."""
        try:
            self.queue.put_nowait((list(endpoints), path, payload, label))
            return True
        except queue.Full:
            self.log("warning", f"[{label}] relay queue full, dropped: {payload}")
            return False

    def _run(self):
        while True:
            endpoints, path, payload, label = self.queue.get()
            try:
                self.deliver(endpoints, path, payload, label)
            except Exception as error:
                self.log("warning", f"[{label}] relay failed: {error}")

    def deliver(self, endpoints: Iterable[str], path: str, payload: dict, label: str) -> List[str]:
        """Post to every address of every endpoint, skipping addresses that
        failed within dead_seconds. Returns the URLs that accepted the post."""
        delivered = []
        for endpoint in endpoints:
            parsed = urlparse(endpoint)
            host, port = parsed.hostname, parsed.port or 8080
            if not host:
                continue
            try:
                addresses = self.resolve(host, port)
            except OSError as error:
                self.log("warning", f"[{label}] failed to resolve {host}: {error}")
                continue
            # Forget addresses DNS no longer shows; a pod that comes back with
            # the same address is tried again at once.
            for address in [address for address in self.dead if address not in addresses]:
                self.dead.pop(address, None)
            for address in addresses:
                if self.dead.get(address, 0) > self.now():
                    continue
                urlHost = f"[{address}]" if ":" in address else address
                url = f"{parsed.scheme}://{urlHost}:{port}{path}"
                try:
                    self.post(url, json=payload, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
                    self.dead.pop(address, None)
                    delivered.append(url)
                except (requests.ConnectionError, requests.Timeout) as error:
                    self.dead[address] = self.now() + self.dead_seconds
                    self.log("warning", f"[{label}] relay to {url} failed, skipping it for {self.dead_seconds:.0f} s: {error}")
                except Exception as error:
                    self.log("warning", f"[{label}] relay to {url} failed: {error}")
        return delivered
