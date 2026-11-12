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
from typing import Iterable, List, Optional, Tuple

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
