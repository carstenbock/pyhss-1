# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
from network_control import _send_cx_rtr, push_clr_and_relay, push_ims_profile, serving_state_snapshot, teardown_subscriber

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


# Values as stored by the SAR handler on Helsinki: the SIP Server-Name every
# S-CSCF pod shares, and the pod's own Diameter identity plus the HSS identity.
SCSCF_POD = "cnaas-scscf-2.scscf.ims.mnc001.mcc001.3gppnetwork.org"
IMS_SUBSCRIBER = {
    "imsi": IMSI,
    "msisdn": "4911112345",
    "scscf": "sip:scscf.ims.mnc001.mcc001.3gppnetwork.org",
    "scscf_peer": SCSCF_POD + ";hss-0.mnc001.mcc001.3gppnetwork.org",
    "scscf_realm": "mnc001.mcc001.3gppnetwork.org",
}
IMS_DOMAIN = "ims.mnc001.mcc001.3gppnetwork.org"


class FakeCxDiameter(FakeDiameter):
    def build_cx_user_data(self, ims_subscriber):
        return "<IMSSubscription/>"


def test_rtr_is_addressed_to_the_scscf_pod_not_the_server_name():
    # KI-38: with the shared Server-Name as Destination-Host no S-CSCF matched,
    # the DRA routed the RTR back to the HSS and the user stayed registered.
    node = FakeCxDiameter(connected_peers=[SCSCF_POD])
    warnings = []

    _send_cx_rtr(node, IMS_SUBSCRIBER, IMSI, IMS_DOMAIN, warnings)

    assert warnings == []
    assert len(node.sent) == 1
    request_type, hostname, kwargs = node.sent[0]
    assert request_type == "RTR"
    assert kwargs["destinationHost"] == SCSCF_POD
    assert kwargs["destinationRealm"] == IMS_SUBSCRIBER["scscf_realm"]


def test_rtr_that_could_not_be_sent_is_reported():
    # The operator's deregistration must not look successful when no RTR left
    # this node (the old code logged "Sent" regardless).
    warnings = []

    _send_cx_rtr(FakeCxDiameter(), IMS_SUBSCRIBER, IMSI, IMS_DOMAIN, warnings)

    assert len(warnings) == 1
    assert "not sent" in warnings[0]


def test_rtr_without_scscf_identity_sends_nothing():
    # Without the S-CSCF's Diameter identity any RTR would loop back to the HSS
    # through the DRA's Cx route, so none is sent and the gap is reported.
    node = FakeCxDiameter(connected_peers=[SCSCF_POD])
    subscriber = dict(IMS_SUBSCRIBER, scscf_peer=None)
    warnings = []

    _send_cx_rtr(node, subscriber, IMSI, IMS_DOMAIN, warnings)

    assert node.sent == []
    assert len(warnings) == 1


def test_rtr_for_unregistered_subscriber_is_silent():
    # Nothing is registered, so there is nothing to deregister and nothing to warn about.
    node = FakeCxDiameter(connected_peers=[SCSCF_POD])
    subscriber = dict(IMS_SUBSCRIBER, scscf=None, scscf_peer=None, scscf_realm=None)
    warnings = []

    _send_cx_rtr(node, subscriber, IMSI, IMS_DOMAIN, warnings)

    assert node.sent == []
    assert warnings == []


def test_ppr_is_addressed_to_the_scscf_pod_not_the_server_name():
    # Same addressing as the RTR: a PPR to the Server-Name never reached the
    # S-CSCF, which kept serving the iFC it cached at registration.
    node = FakeCxDiameter(connected_peers=[SCSCF_POD])

    warnings = push_ims_profile(node, IMS_SUBSCRIBER, mcc="001", mnc="01")

    assert warnings == []
    assert node.sent[0][0] == "PPR"
    assert node.sent[0][2]["destinationHost"] == SCSCF_POD


class FakeImsDatabase:
    def __init__(self, ims_subscriber):
        self.ims_subscriber = ims_subscriber
        self.cleared = []

    def Get_IMS_Subscriber(self, **kwargs):
        key, value = next(iter(kwargs.items()))
        if str(self.ims_subscriber.get(key)) != str(value):
            raise ValueError("not found")
        return dict(self.ims_subscriber)

    def Update_Serving_CSCF(self, imsi, serving_cscf, **kwargs):
        self.cleared.append((imsi, serving_cscf))


class FakeHss(FakeCxDiameter):
    """Runs the real Diameter.deregisterIms against fake peers and database."""

    from diameter import Diameter as _Diameter
    deregisterIms = _Diameter.deregisterIms
    MCC, MNC = "001", "01"
    logTool = None
    redisMessaging = None

    def __init__(self, database, connected_peers=()):
        super().__init__(connected_peers)
        self.database = database


def test_deregister_ims_by_msisdn_clears_the_right_subscriber():
    # KI-55: the MSISDN branch passed msisdn= to Update_Serving_CSCF, which takes
    # only imsi, so a deregistration by number raised TypeError and did nothing.
    database = FakeImsDatabase(IMS_SUBSCRIBER)
    hss = FakeHss(database, connected_peers=[SCSCF_POD])

    assert hss.deregisterIms(msisdn=IMS_SUBSCRIBER["msisdn"]) is True

    assert database.cleared == [(IMSI, None)]
    assert hss.sent[0][0] == "RTR"
    assert hss.sent[0][2]["destinationHost"] == SCSCF_POD


def test_deregister_ims_reports_an_rtr_that_was_not_sent():
    # The state is still cleared, but the caller learns that the S-CSCF was not told.
    database = FakeImsDatabase(IMS_SUBSCRIBER)
    hss = FakeHss(database)

    assert hss.deregisterIms(imsi=IMSI) is False
    assert database.cleared == [(IMSI, None)]


class FakeReplica:
    """A Diameter node's own database copy, which may lack the registration."""

    def __init__(self, subscriber=None, ims_subscriber=None):
        self.subscriber = subscriber or {}
        self.ims_subscriber = ims_subscriber or {}

    def Get_Subscriber(self, **kwargs):
        return dict(self.subscriber)

    def Get_IMS_Subscriber(self, **kwargs):
        return dict(self.ims_subscriber)


UNREGISTERED_COPY = {key: value for key, value in IMS_SUBSCRIBER.items()
                     if key not in ("scscf", "scscf_realm", "scscf_peer")}


def test_teardown_uses_relayed_scscf_when_the_node_copy_lacks_it():
    # KI-38: /oam/deregister cleared the HSS, but no Diameter node sent the RTR,
    # because each node read its own database copy, which had no S-CSCF. The
    # API relays what it read from the primary, so the S-CSCF is still told.
    primary = FakeReplica(ims_subscriber=IMS_SUBSCRIBER)
    node = FakeCxDiameter(connected_peers=[SCSCF_POD])

    warnings = teardown_subscriber(
        node, FakeReplica(ims_subscriber=UNREGISTERED_COPY), IMSI, domains=["ims"],
        mcc="001", mnc="01", clear_serving_state=False,
        serving_state=serving_state_snapshot(primary, IMSI),
    )

    assert warnings == []
    assert [(sent[0], sent[2]["destinationHost"]) for sent in node.sent] == [("RTR", SCSCF_POD)]


def test_teardown_without_relayed_state_cannot_address_the_rtr():
    # The failure the relayed state fixes: from its own copy alone the node
    # does not know an S-CSCF and sends nothing.
    node = FakeCxDiameter(connected_peers=[SCSCF_POD])

    teardown_subscriber(node, FakeReplica(ims_subscriber=UNREGISTERED_COPY), IMSI, domains=["ims"],
                        mcc="001", mnc="01", clear_serving_state=False)

    assert node.sent == []


def test_relayed_state_does_not_override_the_node_copy():
    # A node that has the registration itself (it may have handled a newer SAR)
    # keeps its own S-CSCF; the relayed state only fills gaps.
    other_pod = "cnaas-scscf-9.scscf.ims.mnc001.mcc001.3gppnetwork.org"
    stale = dict(IMS_SUBSCRIBER, scscf_peer=other_pod)
    node = FakeCxDiameter(connected_peers=[SCSCF_POD, other_pod])

    teardown_subscriber(node, FakeReplica(ims_subscriber=IMS_SUBSCRIBER), IMSI, domains=["ims"],
                        mcc="001", mnc="01", clear_serving_state=False,
                        serving_state=serving_state_snapshot(FakeReplica(ims_subscriber=stale), IMSI))

    assert [sent[2]["destinationHost"] for sent in node.sent] == [SCSCF_POD]
