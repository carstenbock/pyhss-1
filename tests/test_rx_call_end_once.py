# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Gx rules of a call are removed once when the call ends (KI-296).

The AAR of a call and its STR can reach different HSS Diameter nodes. The node
that gets the STR removes the voice rule itself, because it can name it without
the call's record, and asks the node that holds the record to remove the call's
rules. That node used to remove the voice rule again: two Re-Auth-Requests with
the same Charging-Rule-Remove reached the PGW within a millisecond, and a PGW
that had started to deactivate the bearer aborted on the second (KI-250).

The second removal is still wanted when the first did not happen: the receiving
node has no serving APN, or the PGW did not answer or answered with an error."""
import json

import pytest

from database import APN, AUC, IMS_SUBSCRIBER, Database, SUBSCRIBER
from diameter import Diameter
from logtool import LogTool
from pyhss_config import config

IMSI = "001010000000001"
REALM = "ims.mnc001.mcc001.3gppnetwork.org"
PCSCF = "cnaas-pcscf-0.pcscf.ims.mnc001.mcc001.3gppnetwork.org"
RX_SESSION = "10.0.1.2;5060;pcscf.ims.mnc001.mcc001.3gppnetwork.org;1794063370.713190;64"

STR_HEADER = {"hop-by-hop-identifier": "00000001", "end-to-end-identifier": "00000001", "flags": "c0",
              "flags_bin": "11000000", "command_code": 275, "ApplicationId": 16777236, "packet_version": "01", "length": 0}


class NodeRedis:
    """The node-local Redis: hash operations on a dict."""

    def __init__(self):
        self.hashes = {}

    def setHashValue(self, name, key, value, keyExpiry=None, **kwargs):
        self.hashes.setdefault(name, {})[key] = value

    def getHashValue(self, name, key, **kwargs):
        return self.hashes.get(name, {}).get(key)

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


def attach_ims_pdn(db):
    db.Update_Serving_APN(imsi=IMSI, apn=2, pcrf_session_id="pgw;1;2", serving_pgw="pgw.epc",
                          subscriber_routing="10.46.0.5", serving_pgw_realm="epc", serving_pgw_peer="pgw.epc;hss-4",
                          propagate=False)


def make_node(db, name, pgw_answers=True, pgw_result=2001):
    node = Diameter.__new__(Diameter)
    node.logTool = LogTool(config)
    node.redisMessaging = NodeRedis()
    node.database = db
    node.OriginHost = node.string_to_hex(name)
    node.OriginRealm = node.string_to_hex(REALM)
    node.MNC, node.MCC, node.hostname = "001", "001", name
    node.sent = []
    node.sendDiameterRequest = lambda requestType, hostname, **kwargs: node.sent.append((requestType, kwargs)) or "01"
    raa = node.generate_diameter_packet("01", "00", 258, 16777238, "00000001", "00000001",
                                        node.generate_avp(268, 40, node.int_to_hex(pgw_result, 4)))

    def await_answer(requestType, hostname, **kwargs):
        node.sent.append((requestType, kwargs))
        return raa if pgw_answers else ""

    node.awaitDiameterRequestAndResponse = await_answer
    return node


def a_call_set_up_on(node, db):
    """The AAR of a voice and video call was handled by this node: it holds
    the call's record with one rule per media component (KI-168)."""
    subscriber_id = db.Get_Subscriber(imsi=IMSI)["subscriber_id"]
    names = [Diameter.rx_call_rule_name(kind, subscriber_id, RX_SESSION) for kind in ("Voice", "Video")]
    for name in names:
        node.rx_store_call_rule(subscriber_id, name, RX_SESSION, PCSCF, REALM, imsi=IMSI)
    return names


def connect(receiver, holder):
    """The receiver's relay reaches the holder's /geored/rx_call_ended."""
    def relay(path, payload, label):
        if path == '/geored/rx_call_ended':
            holder.rx_end_call_locally(payload['af_session_id'], payload.get('removed_rules'))
    receiver._rx_relay = relay


def removed_rules(*nodes):
    """Every rule name in a Charging-Rule-Remove sent to the PGW, in order."""
    names = []
    for node in nodes:
        for request, kwargs in node.sent:
            if request == 'RAR' and kwargs.get('chargingRuleAction') == 'remove':
                rule = kwargs['chargingRuleName']
                names += [rule] if isinstance(rule, str) else list(rule)
    return names


def end_call_at(node):
    node.Answer_16777236_275(dict(STR_HEADER), [{"avp_code": 263, "misc_data": node.string_to_hex(RX_SESSION)}])


def test_str_on_another_node_removes_each_rule_once(db):
    attach_ims_pdn(db)
    holder, receiver = make_node(db, "hss-4"), make_node(db, "hss-5")
    voice, video = a_call_set_up_on(holder, db)
    connect(receiver, holder)

    end_call_at(receiver)

    assert sorted(removed_rules(receiver, holder)) == sorted([voice, video])
    assert removed_rules(receiver) == [voice]       # the node with the STR can name the voice rule
    assert removed_rules(holder) == [video]         # the holder of the record removes the rest
    assert holder.rx_local_call_session(RX_SESSION) is None


def test_holder_removes_all_rules_when_the_pgw_did_not_answer_the_receiver(db):
    attach_ims_pdn(db)
    holder, receiver = make_node(db, "hss-4"), make_node(db, "hss-5", pgw_answers=False)
    voice, video = a_call_set_up_on(holder, db)
    connect(receiver, holder)

    end_call_at(receiver)

    assert sorted(removed_rules(holder)) == sorted([voice, video])


def test_holder_removes_all_rules_when_the_pgw_refused_the_receivers_removal(db):
    # DIAMETER_UNABLE_TO_COMPLY: the voice rule may still be installed.
    attach_ims_pdn(db)
    holder, receiver = make_node(db, "hss-4"), make_node(db, "hss-5", pgw_result=5012)
    voice, video = a_call_set_up_on(holder, db)
    connect(receiver, holder)

    end_call_at(receiver)

    assert sorted(removed_rules(holder)) == sorted([voice, video])


def test_holder_removes_all_rules_when_the_receiver_sends_no_removal(db):
    # The receiver finds no IMS PDN of the subscriber (the holder's own lookup
    # is stubbed to succeed): nobody else removes the voice rule.
    holder, receiver = make_node(db, "hss-4"), make_node(db, "hss-5")
    voice, video = a_call_set_up_on(holder, db)
    connect(receiver, holder)
    receiver.database = type("NoServingApn", (), {
        "__getattr__": lambda self, name: getattr(db, name),
        "Get_Serving_APN": lambda self, **kwargs: None})()
    holder.database = type("ServingApn", (), {
        "__getattr__": lambda self, name: getattr(db, name),
        "Get_Serving_APN": lambda self, **kwargs: {"serving_pgw": "pgw.epc", "serving_pgw_realm": "epc",
                                                   "serving_pgw_peer": "pgw.epc;hss-4", "pcrf_session_id": "pgw;1;2"}})()

    end_call_at(receiver)

    assert removed_rules(receiver) == []
    assert sorted(removed_rules(holder)) == sorted([voice, video])


def test_str_on_the_node_that_set_the_call_up_removes_each_rule_once(db):
    attach_ims_pdn(db)
    holder, other = make_node(db, "hss-4"), make_node(db, "hss-5")
    voice, video = a_call_set_up_on(holder, db)
    connect(holder, other)

    end_call_at(holder)

    assert sorted(removed_rules(holder, other)) == sorted([voice, video])
