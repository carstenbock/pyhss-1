# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A failed call bearer reaches the P-CSCF (KI-115).

When the PGW cannot set up the dedicated bearer for a call (the MME rejects the
Create Bearer Request, or it times out), it reports the call's PCC rule as
INACTIVE in a Gx CCR-U (TS 29.212 4.5.12). The PCRF must pass that on to the AF
of the call's Rx session with an Rx RAR carrying Specific-Action
INDICATION_OF_FAILED_RESOURCES_ALLOCATION (TS 29.214 4.4.6). Before, PyHSS
answered the CCR-U and did nothing, so the P-CSCF kept the call as if its media
bearer existed."""
import pytest

from database import APN, AUC, IMS_SUBSCRIBER, Database, SUBSCRIBER
from diameter import Diameter
from logtool import LogTool
from pyhss_config import config

IMSI = "001010000000001"
UE_IP = "10.45.0.2"
REALM = "ims.mnc001.mcc001.3gppnetwork.org"
PCSCF = "pcscf.ims.mnc001.mcc001.3gppnetwork.org"
CALL_SESSION = f"{PCSCF};1;call"
ACTIVE, INACTIVE = 0, 1
RESOURCE_ALLOCATION_FAILURE = 10
FAILED_RESOURCES_ALLOCATION = 9


class NullMessaging:
    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


@pytest.fixture
def pcrf(tmp_path, monkeypatch):
    monkeypatch.setitem(config["database"], "database", str(tmp_path / "gx.db"))
    db = Database(LogTool(config), main_service=True)
    db.CreateObj(APN, {"apn_id": 1, "apn": "internet", "apn_ambr_dl": 100000000, "apn_ambr_ul": 100000000})
    db.CreateObj(APN, {"apn_id": 2, "apn": "ims", "apn_ambr_dl": 100000000, "apn_ambr_ul": 100000000})
    db.CreateObj(AUC, {"auc_id": 1, "ki": "3c6e0b8a9c15224a8228b9a98ca1531d",
                       "opc": "762a2206fe0b4151ace403c86a11e479", "amf": "8000", "sqn": 0, "imsi": IMSI})
    db.CreateObj(SUBSCRIBER, {"auc_id": 1, "default_apn": 1, "apn_list": "1,2", "imsi": IMSI, "msisdn": "4915100000001"})
    db.CreateObj(IMS_SUBSCRIBER, {"imsi": IMSI, "msisdn": "4915100000001", "sh_profile": ""})
    db.Update_Serving_APN(imsi=IMSI, apn=2, pcrf_session_id="pgw;1;2", serving_pgw="pgw.epc",
                          subscriber_routing=UE_IP, serving_pgw_realm="epc", serving_pgw_peer="pgw.epc;hss01",
                          propagate=False)

    node = Diameter.__new__(Diameter)
    node.logTool = LogTool(config)
    node.redisMessaging = NullMessaging()
    node.database = db
    node.OriginHost = node.string_to_hex("hss01")
    node.OriginRealm = node.string_to_hex(REALM)
    node.MNC, node.MCC, node.hostname = "001", "001", "hss01"
    node.sent = []

    def await_gx_rar(requestType, hostname, **kwargs):
        raa = node.generate_avp(268, 40, node.int_to_hex(2001, 4))
        return node.generate_diameter_packet("01", "00", 258, 16777238, "00000001", "00000001", raa)

    def send(requestType, hostname, via=None, **kwargs):
        node.sent.append((requestType, hostname, kwargs))
        return "01"
    node.awaitDiameterRequestAndResponse = await_gx_rar
    node.sendDiameterRequest = send
    yield node
    db.engine.dispose()


def subscriber_id(node):
    return node.database.Get_Subscriber(imsi=IMSI)["subscriber_id"]


def call_aar(node):
    """The P-CSCF authorizes a voice call; this stores the call's Rx session."""
    rx = lambda code, data: node.generate_vendor_avp(code, "c0", 10415, data)
    component = rx(517, rx(518, node.int_to_hex(1, 4)) + rx(520, node.int_to_hex(0, 4))
                   + rx(511, node.int_to_hex(2, 4)))
    avp = node.generate_avp(263, 40, node.string_to_hex(CALL_SESSION))
    avp += node.generate_avp(264, 40, node.string_to_hex(PCSCF))
    avp += node.generate_avp(296, 40, node.string_to_hex(REALM))
    avp += node.generate_avp(443, 40, node.generate_avp(450, 40, node.int_to_hex(2, 4))
                             + node.generate_avp(444, 40, node.string_to_hex(f"sip:{IMSI}@{REALM}")))
    avp += node.generate_avp(8, 40, "0a2d0002")
    avp += component
    packet = node.generate_diameter_packet("01", "c0", 265, 16777236, "00000001", "00000002", avp)
    packet_vars, avps = node.decode_diameter_packet(bytes.fromhex(packet))
    node.Answer_16777236_265(packet_vars, avps)


def ccr_update(node, rule_name, status=INACTIVE, failure_code=RESOURCE_ALLOCATION_FAILURE):
    """A Gx CCR-U from the PGW with one Charging-Rule-Report."""
    gx = lambda code, data: node.generate_vendor_avp(code, "c0", 10415, data)
    report = gx(1005, node.string_to_hex(rule_name)) + gx(1019, node.int_to_hex(status, 4))
    if failure_code is not None:
        report += gx(1031, node.int_to_hex(failure_code, 4))
    avp = node.generate_avp(263, 40, node.string_to_hex("pgw;1;2"))
    avp += node.generate_avp(264, 40, node.string_to_hex("pgw.epc"))
    avp += node.generate_avp(296, 40, node.string_to_hex("epc"))
    avp += node.generate_avp(258, 40, "01000016")
    avp += node.generate_avp(416, 40, node.int_to_hex(2, 4))
    avp += node.generate_avp(415, 40, node.int_to_hex(1, 4))
    avp += node.generate_avp(443, 40, node.generate_avp(450, 40, node.int_to_hex(1, 4))
                             + node.generate_avp(444, 40, node.string_to_hex(IMSI)))
    avp += node.generate_avp(30, 40, node.string_to_hex("ims"))
    avp += gx(1018, report)
    packet = node.generate_diameter_packet("01", "c0", 272, 16777238, "00000003", "00000004", avp)
    packet_vars, avps = node.decode_diameter_packet(bytes.fromhex(packet))
    answer = node.Answer_16777238_272(packet_vars, avps)
    _, answer_avps = node.decode_diameter_packet(bytes.fromhex(answer))
    return int(node.get_avp_data(answer_avps, 268)[0], 16)


def rx_rars(node):
    return [sent for sent in node.sent if sent[0] == "RX_RAR"]


def test_failed_voice_bearer_is_reported_to_the_pcscf(pcrf):
    call_aar(pcrf)

    result = ccr_update(pcrf, f"GBR-Voice_{subscriber_id(pcrf)}")

    assert result == 2001
    [(_, hostname, rar)] = rx_rars(pcrf)
    assert hostname == PCSCF
    assert rar["sessionId"] == CALL_SESSION
    assert rar["destinationHost"] == PCSCF
    assert rar["destinationRealm"] == REALM
    assert rar["specificActions"] == [FAILED_RESOURCES_ALLOCATION]


def test_failed_video_bearer_is_reported_too(pcrf):
    call_aar(pcrf)

    ccr_update(pcrf, f"GBR-Video_{subscriber_id(pcrf)}")

    assert [rar[2]["sessionId"] for rar in rx_rars(pcrf)] == [CALL_SESSION]


def test_report_without_failure_code_still_counts(pcrf):
    # Rule-Failure-Code is optional in a Charging-Rule-Report; INACTIVE alone
    # already means the rule is not enforced.
    call_aar(pcrf)

    ccr_update(pcrf, f"GBR-Voice_{subscriber_id(pcrf)}", failure_code=None)

    assert len(rx_rars(pcrf)) == 1


def test_active_rule_report_tells_nobody(pcrf):
    call_aar(pcrf)

    ccr_update(pcrf, f"GBR-Voice_{subscriber_id(pcrf)}", status=ACTIVE)

    assert rx_rars(pcrf) == []


def test_failed_rule_of_another_subscriber_is_not_reported(pcrf):
    # Rule names carry the subscriber id; a rule that is not this subscriber's
    # call rule (a predefined APN rule, or another UE's) has no Rx session here.
    call_aar(pcrf)

    ccr_update(pcrf, "GBR-Voice_999")
    ccr_update(pcrf, "internet-default")

    assert rx_rars(pcrf) == []


def test_no_rar_after_the_call_ended(pcrf):
    # After the STR the stored session is cleared; a late report must not reach
    # a session the P-CSCF has already closed.
    call_aar(pcrf)
    avp = pcrf.generate_avp(263, 40, pcrf.string_to_hex(CALL_SESSION))
    avp += pcrf.generate_avp(264, 40, pcrf.string_to_hex(PCSCF))
    avp += pcrf.generate_avp(296, 40, pcrf.string_to_hex(REALM))
    packet = pcrf.generate_diameter_packet("01", "c0", 275, 16777236, "00000005", "00000006", avp)
    pcrf.Answer_16777236_275(*pcrf.decode_diameter_packet(bytes.fromhex(packet)))
    assert not pcrf.database.Get_IMS_Subscriber(imsi=IMSI).get("pcscf_active_session")

    ccr_update(pcrf, f"GBR-Voice_{subscriber_id(pcrf)}")

    assert rx_rars(pcrf) == []


def test_rx_rar_encoding(pcrf):
    packet = pcrf.Request_16777236_258(sessionId=CALL_SESSION, destinationHost=PCSCF,
                                       destinationRealm=REALM, specificActions=[FAILED_RESOURCES_ALLOCATION])

    packet_vars, avps = pcrf.decode_diameter_packet(bytes.fromhex(packet))
    assert packet_vars["command_code"] == 258
    assert packet_vars["ApplicationId"] == 16777236
    assert int(packet_vars["flags"], 16) & 0x80                                    # request
    assert bytes.fromhex(pcrf.get_avp_data(avps, 263)[0]).decode() == CALL_SESSION
    assert bytes.fromhex(pcrf.get_avp_data(avps, 293)[0]).decode() == PCSCF
    assert bytes.fromhex(pcrf.get_avp_data(avps, 283)[0]).decode() == REALM
    assert int(pcrf.get_avp_data(avps, 258)[0], 16) == 16777236
    assert [int(value, 16) for value in pcrf.get_avp_data(avps, 513)] == [FAILED_RESOURCES_ALLOCATION]
