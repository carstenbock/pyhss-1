# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cancel Location on MME change (TS 29.272 section 5.2.1.1.3).

When an Update Location arrives from a new MME, the HSS shall send a Cancel
Location to the MME that served the subscriber before, so that the old MME
releases the UE's context and bearers. Without it the old MME kept the UE
attached and the old PDN sessions stayed in the core (KI-3)."""
import pytest

from database import APN, AUC, Database, SUBSCRIBER
from diameter import Diameter
from logtool import LogTool
from pyhss_config import config

IMSI = "001010000000001"
MME1 = "mmec01.mmegi0001.mme.epc.mnc001.mcc001.3gppnetwork.org"
MME2 = "mmec02.mmegi0001.mme.epc.mnc001.mcc001.3gppnetwork.org"
REALM = "epc.mnc001.mcc001.3gppnetwork.org"
INITIAL_ATTACH_INDICATOR = 1 << 5


class NullMessaging:
    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


@pytest.fixture
def hss(tmp_path, monkeypatch):
    monkeypatch.setitem(config["database"], "database", str(tmp_path / "s6a.db"))
    monkeypatch.setitem(config["hss"], "OriginHost", "hss01")

    db = Database(LogTool(config), main_service=True)
    db.CreateObj(APN, {"apn_id": 1, "apn": "internet", "apn_ambr_dl": 100000000, "apn_ambr_ul": 100000000})
    db.CreateObj(AUC, {"auc_id": 1, "ki": "3c6e0b8a9c15224a8228b9a98ca1531d", "opc": "762a2206fe0b4151ace403c86a11e479",
                       "amf": "8000", "sqn": 0, "imsi": IMSI})
    db.CreateObj(SUBSCRIBER, {"auc_id": 1, "default_apn": 1, "apn_list": "1", "imsi": IMSI, "msisdn": "4915100000001"})

    node = Diameter.__new__(Diameter)
    node.logTool = LogTool(config)
    node.redisMessaging = NullMessaging()
    node.database = db
    node.OriginHost = node.string_to_hex("hss01")
    node.OriginRealm = node.string_to_hex(REALM)
    node.MNC = "001"
    node.MCC = "001"
    node.hostname = "hss01"
    node.sendDsrOnMmeChange = False
    node.cancelLocationOnMmeChange = True
    node.sent = []
    node.sendDiameterRequest = lambda requestType, hostname, **kwargs: node.sent.append((requestType, hostname, kwargs)) or "01"
    yield node
    db.engine.dispose()


def ulr(node, mme, ulr_flags=0):
    avp = node.generate_avp(263, 40, node.string_to_hex(mme + ";1;1"))
    avp += node.generate_avp(264, 40, node.string_to_hex(mme))
    avp += node.generate_avp(296, 40, node.string_to_hex(REALM))
    avp += node.generate_avp(1, 40, node.string_to_hex(IMSI))
    avp += node.generate_vendor_avp(1032, "80", 10415, node.int_to_hex(1004, 4))          # RAT-Type EUTRAN
    avp += node.generate_vendor_avp(1405, "c0", 10415, node.int_to_hex(ulr_flags, 4))     # ULR-Flags
    avp += node.generate_vendor_avp(1407, "c0", 10415, "00f110")                          # Visited-PLMN-Id 001/01
    packet = node.generate_diameter_packet("01", "c0", 316, 16777251, "00000001", "00000002", avp)
    packet_vars, avps = node.decode_diameter_packet(bytes.fromhex(packet))
    return node.Answer_16777251_316(packet_vars, avps)


def clrs(node):
    return [(hostname, kwargs) for request_type, hostname, kwargs in node.sent if request_type == "CLR"]


def test_new_mme_cancels_the_location_at_the_previous_mme(hss):
    ulr(hss, MME1)
    assert clrs(hss) == []

    ulr(hss, MME2)

    sent = clrs(hss)
    assert len(sent) == 1
    hostname, kwargs = sent[0]
    assert kwargs["DestinationHost"] == MME1
    assert kwargs["DestinationRealm"] == REALM
    assert kwargs["CancellationType"] == 0          # MME_UPDATE_PROCEDURE
    assert kwargs["immediateReattach"] is False     # the UE is already attached to the new MME


def test_initial_attach_at_new_mme_uses_initial_attach_cancellation(hss):
    ulr(hss, MME1)

    ulr(hss, MME2, ulr_flags=INITIAL_ATTACH_INDICATOR)

    assert clrs(hss)[0][1]["CancellationType"] == 4  # INITIAL_ATTACH_PROCEDURE


def test_update_from_the_same_mme_cancels_nothing(hss):
    ulr(hss, MME1)
    ulr(hss, MME1)

    assert clrs(hss) == []


def test_cancel_location_is_off_unless_configured(hss):
    hss.cancelLocationOnMmeChange = False
    ulr(hss, MME1)
    ulr(hss, MME2)

    assert clrs(hss) == []
