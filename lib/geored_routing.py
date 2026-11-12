# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Where a Diameter node pushes a runtime state change (ADR-0040 step 1, KI-19).

The provisioning node writes a pushed change to the MariaDB master, and
replication carries it to every Diameter node's replica. A change to a
replicated table therefore only needs to reach the provisioning node; pushing
it to every other node as well made each node apply it twice. The direct push
to the other nodes stays as the fallback while the provisioning node does not
accept it.

Changes that replication does not carry still go to every node:
`serving_apn`, `emergency_subscriber` and `eir_history` are in the replicas'
replicate-ignore-table list, and an `xcap_profile` push makes each receiving
node send the Sh PNRs for the subscriptions held in its own Redis.
"""
import socket
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

# Body keys of the pushes whose rows replication carries (subscriber,
# ims_subscriber, auc).
REPLICATED_MARKERS = ("serving_mme", "last_seen_mcc", "scscf", "pcscf", "auc_id")
# Body keys of pushes that every node must receive itself.
EVERY_NODE_MARKERS = ("serving_apn", "emergency_subscriber_ip", "imei", "xcap_profile")


def push_targets(body, peers: Iterable[str], primary: Optional[str]) -> Tuple[List[str], List[str]]:
    """Return (targets, fallback): push to targets; if that fails, to fallback."""
    peers = list(peers or [])
    if not primary or primary not in peers or not isinstance(body, dict):
        return peers, []
    if any(key in body for key in EVERY_NODE_MARKERS):
        return peers, []
    if not any(key in body for key in REPLICATED_MARKERS):
        return peers, []
    return [primary], [peer for peer in peers if peer != primary]


# Seconds a resolved endpoint list is reused; pods added or removed by the HPA
# are seen within this time (KI-139).
RESOLVE_TTL = 5.0
_resolved: Dict[str, Tuple[float, List[str]]] = {}


def _addresses(host: str, port: int) -> List[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


def local_addresses() -> set:
    try:
        return set(_addresses(socket.gethostname(), 0))
    except OSError:
        return set()


def expand_endpoints(urls: Iterable[str], own: Optional[set] = None,
                     resolve: Callable[[str, int], List[str]] = _addresses,
                     now: Callable[[], float] = time.monotonic) -> List[str]:
    """One URL per address each endpoint's host name resolves to, without this
    node's own addresses (KI-139).

    The chart names the Diameter pods by their headless Service, which resolves
    to every pod of the StatefulSet, so pods added or removed later are pushed
    to as well; a list fixed at pod start missed them. A host name that does
    not resolve is kept as it is, so the push fails and is logged as before.
    """
    own = local_addresses() if own is None else own
    out: List[str] = []
    for url in urls:
        parsed = urlparse(url)
        host = parsed.hostname
        if not host:
            out.append(url)
            continue
        cached = _resolved.get(url)
        if cached and now() - cached[0] < RESOLVE_TTL:
            expanded = cached[1]
        else:
            try:
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                addresses = resolve(host, port)
            except OSError:
                addresses = []
            if not addresses:
                expanded = [url]
            else:
                netloc_port = f":{parsed.port}" if parsed.port else ""
                expanded = []
                for address in addresses:
                    shown = f"[{address}]" if ":" in address else address
                    expanded.append(parsed._replace(netloc=f"{shown}{netloc_port}").geturl())
            _resolved[url] = (now(), expanded)
        for candidate in expanded:
            if urlparse(candidate).hostname not in own and candidate not in out:
                out.append(candidate)
    return out
