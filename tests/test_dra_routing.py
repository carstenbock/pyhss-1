# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Choice of the DRA for a request to a peer the HSS is not connected to (KI-74).

The DRAs do not forward to each other, so a request reaches a peer only through
the DRA that peer is attached to. The HSS prefers the DRA the peer's own
requests came through, and uses another DRA only when that one is down."""
import json

from diameter import Diameter
from logtool import LogTool
from pyhss_config import config

DRA0 = "dra-0.mnc001.mcc001.3gppnetwork.org"
DRA1 = "dra-1.mnc001.mcc001.3gppnetwork.org"
MME = "mme.mnc022.mcc262.3gppnetwork.org"


def peer(hostname, ip, connected=True):
    return {"IpAddress": ip, "Port": "3868", "Hostname": hostname, "Connected": connected,
            "TransportProtocol": "TCP", "PeerType": "dra", "LastConnectTimestamp": "",
            "LastDisconnectTimestamp": "", "ReconnectionCount": 0,
            "Metadata": json.dumps({"DiameterPeerType": "dra"})}


class PeerTable:
    def __init__(self, peers):
        self.peers = {f"{p['IpAddress']}-{p['Port']}": p for p in peers}

    def getAllHashData(self, name, **kwargs):
        return self.peers

    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


def node_with(peers):
    node = Diameter.__new__(Diameter)
    node.logTool = LogTool(config)
    node.redisMessaging = PeerTable(peers)
    node.diameterPeerKey = "diameterPeers"
    node.diameterServiceHostname = "hss-0"
    node.useDraFallback = True
    return node


def test_request_goes_through_the_dra_the_peer_is_attached_to():
    node = node_with([peer(DRA0, "10.0.0.1"), peer(DRA1, "10.0.0.2")])

    assert node.getPeerByHostname(MME, via=DRA1).Hostname == DRA1


def test_another_dra_is_used_when_the_peers_dra_is_down():
    node = node_with([peer(DRA0, "10.0.0.1"), peer(DRA1, "10.0.0.2", connected=False)])

    assert node.getPeerByHostname(MME, via=DRA1).Hostname == DRA0


def test_without_a_known_dra_the_first_connected_dra_is_used():
    node = node_with([peer(DRA0, "10.0.0.1"), peer(DRA1, "10.0.0.2")])

    assert node.getPeerByHostname(MME).Hostname == DRA0


def test_peer_field_names_the_dra_only_in_its_third_part():
    assert Diameter.peerFieldVia(f"{MME};hss-0;{DRA1}") == DRA1
    assert Diameter.peerFieldVia(f"{MME};hss-0") is None
    assert Diameter.peerFieldVia(None) is None
