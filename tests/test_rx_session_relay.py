# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An Rx session the AF has ended must not be aborted later (TS 29.214 4.4.4).

Each HSS Diameter node keeps the AF subscriptions of the AARs it handled in its
own Redis. A re-registration's AAR can reach another node, so two nodes may hold
the same session. An STR used to remove only the receiving node's copy; when the
UE's IMS PDN then ended (Gx CCR-T, relayed to every node), the other node sent an
ASR for the ended session. Before P-CSCF 1.14.5 such an ASR could remove every
contact behind the IPsec gateway (KI-84)."""
import json

import pytest

from database import APN, AUC, IMS_SUBSCRIBER, Database, SUBSCRIBER
from diameter import Diameter
from logtool import LogTool
from pyhss_config import config

IMSI = "001010000000001"
REALM = "ims.mnc001.mcc001.3gppnetwork.org"
PCSCF = "cnaas-pcscf-0.pcscf.ims.mnc001.mcc001.3gppnetwork.org"
RX_SESSION = "10.0.1.2;5060;pcscf.ims.mnc001.mcc001.3gppnetwork.org;1794063370.713190;63"


class NodeRedis:
    """The node-local Redis: hash operations on a dict."""

    def __init__(self):
        self.hashes = {}

    def setHashValue(self, name, key, value, keyExpiry=None, **kwargs):
        self.hashes.setdefault(name, {})[key] = value

    def deleteHashKey(self, name, key, **kwargs):
        self.hashes.get(name, {}).pop(key, None)

    def getAllHashData(self, name, **kwargs):
        return {key: json.loads(value) for key, value in self.hashes.get(name, {}).items()}

    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setitem(config["database"], "database", str(tmp_path / "rx.db"))
    database = Database(LogTool(config), main_service=True)
    database.CreateObj(APN, {"apn_id": 1, "apn": "internet", "apn_ambr_dl": 100000000, "apn_ambr_ul": 100000000})
    database.CreateObj(APN, {"apn_id": 2, "apn": "ims", "apn_ambr_dl": 100000000, "apn_ambr_ul": 100000000})
    database.CreateObj(AUC, {"auc_id": 1, "ki": "3c6e0b8a9c15224a8228b9a98ca1531d",
                             "opc": "762a2206fe0b4151ace403c86a11e479", "amf": "8000", "sqn": 0, "imsi": IMSI})
    database.CreateObj(SUBSCRIBER, {"auc_id": 1, "default_apn": 1, "apn_list": "1,2", "imsi": IMSI,
                                    "msisdn": "4915100000001"})
    database.CreateObj(IMS_SUBSCRIBER, {"imsi": IMSI, "msisdn": "4915100000001", "sh_profile": "",
                                        "pcscf_active_session": RX_SESSION})
    yield database
    database.engine.dispose()


def make_node(db, name):
    node = Diameter.__new__(Diameter)
    node.logTool = LogTool(config)
    node.redisMessaging = NodeRedis()
    node.database = db
    node.OriginHost = node.string_to_hex(name)
    node.OriginRealm = node.string_to_hex(REALM)
    node.MNC, node.MCC, node.hostname = "001", "001", name
    node.sent = []
    node.sendDiameterRequest = lambda requestType, hostname, **kwargs: node.sent.append((requestType, kwargs)) or "01"
    return node


STR_HEADER = {"hop-by-hop-identifier": "00000001", "end-to-end-identifier": "00000001", "flags": "c0",
              "flags_bin": "11000000", "command_code": 275, "ApplicationId": 16777236, "packet_version": "01", "length": 0}


def str_avps(node):
    return [{"avp_code": 263, "misc_data": node.string_to_hex(RX_SESSION)}]


def test_session_ended_on_one_node_is_not_aborted_by_another(db):
    node_a, node_b = make_node(db, "hss-0"), make_node(db, "hss-4")
    nodes = [node_a, node_b]
    # The /geored/rx_forget_af_session endpoint of every node, as the relay reaches it.
    for node in nodes:
        node._rx_relay = lambda path, payload, label: [
            other.rx_remove_af_subscription(**payload) for other in nodes] if path == '/geored/rx_forget_af_session' else None
    subscriber_id = db.Get_Subscriber(imsi=IMSI)["subscriber_id"]
    for node in nodes:  # the first AAR on node A, a re-registration's AAR on node B
        node.rx_store_af_subscription(subscriber_id, 2, RX_SESSION, PCSCF, REALM, 7200)

    node_a.Answer_16777236_275(dict(STR_HEADER), str_avps(node_a))      # the P-CSCF ends the session (STR) at node A
    node_b.rx_terminate_local_af_subscriptions(IMSI, "ims")  # then the IMS PDN ends (relayed Gx CCR-T)

    assert [sent for sent in node_b.sent if sent[0] == "ASR"] == []
    assert node_b.rx_get_af_subscriptions(subscriber_id, 2) == {}


def test_live_session_is_still_aborted_when_the_pdn_ends(db):
    # The relay must not remove sessions the AF did not end: the CCR-T of the
    # IMS PDN still makes the holding node send the ASR.
    node = make_node(db, "hss-0")
    subscriber_id = db.Get_Subscriber(imsi=IMSI)["subscriber_id"]
    node.rx_store_af_subscription(subscriber_id, 2, RX_SESSION, PCSCF, REALM, 7200)

    node.rx_terminate_local_af_subscriptions(IMSI, "ims")

    assert [(kind, kwargs["sessionId"]) for kind, kwargs in node.sent] == [("ASR", RX_SESSION)]
