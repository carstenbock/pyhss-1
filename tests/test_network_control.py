# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
from network_control import push_clr_and_relay

IMSI = "001010000000001"
CLR = {
    "diameterPeer": "mme01.epc.mnc001.mcc001.3gppnetwork.org",
    "DestinationHost": "mme01.epc.mnc001.mcc001.3gppnetwork.org",
    "DestinationRealm": "epc.mnc001.mcc001.3gppnetwork.org",
    "cancellationType": 2,
    "immediateReattach": False,
}


class FakeDiameter:
    """Stands in for a node whose peer table may or may not hold the MME."""

    def __init__(self, connected_peers=()):
        self.connected_peers = set(connected_peers)
        self.sent = []

    def sendDiameterRequest(self, requestType, hostname, **kwargs):
        if hostname not in self.connected_peers:
            return ""
        self.sent.append((requestType, hostname, kwargs))
        return "01000000"


class FakeRelay:
    def __init__(self, node_results):
        self.node_results = node_results
        self.calls = []

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        return self.node_results


def test_clr_reaches_mme_connected_to_another_node():
    # The provisioning API holds no Diameter peers; the MME is attached to a
    # Diameter node. The CLR must still be reported as sent, via the relay.
    local = FakeDiameter()
    relay = FakeRelay([{"result": "NotSent"}, {"result": "OK"}])

    outcome = push_clr_and_relay(local, IMSI, CLR, relay)

    assert outcome == {"sent": True, "sent_locally": False, "relayed": True, "nodes_sent": 1}
    assert relay.calls == [("/geored/push_clr", dict(CLR, imsi=IMSI))]


def test_clr_fails_loudly_when_no_node_holds_the_peer():
    # Nodes answered but none had the MME connected: the operator must see a
    # failure, not a success, since no MME was told to cancel the location.
    outcome = push_clr_and_relay(FakeDiameter(), IMSI, CLR, FakeRelay([{"result": "NotSent"}, {"result": "NotSent"}]))

    assert outcome["sent"] is False
    assert outcome["relayed"] is True


def test_clr_without_relay_endpoints_keeps_local_behaviour():
    # Single-node deployments (no sh_notify_endpoints) send from the local peer
    # table only, as before the relay existed.
    local = FakeDiameter(connected_peers=[CLR["diameterPeer"]])

    outcome = push_clr_and_relay(local, IMSI, CLR, FakeRelay([]))

    assert outcome == {"sent": True, "sent_locally": True, "relayed": False, "nodes_sent": 0}
    assert local.sent[0][0] == "CLR"
    assert local.sent[0][2]["CancellationType"] == 2
    assert local.sent[0][2]["immediateReattach"] is False
