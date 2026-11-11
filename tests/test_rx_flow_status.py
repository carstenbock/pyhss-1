# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Rx Flow-Status reaches the Gx rule (TS 29.214 5.3.11, TS 29.213 6.2/6.3).

The P-CSCF derives a media component's Flow-Status from the SDP direction: a
held call is ENABLED-UPLINK, ENABLED-DOWNLINK or DISABLED, a stream removed with
port 0 is REMOVED. PyHSS used to install every voice rule ENABLED, so a held
call's bearer stayed open both ways and a removed stream stayed authorized until
the call ended (KI-70)."""
import pytest

from database import APN, AUC, IMS_SUBSCRIBER, Database, SUBSCRIBER
from diameter import Diameter
from logtool import LogTool
from pyhss_config import config

IMSI = "001010000000001"
UE_IP = "10.45.0.2"
NET_IP = "10.46.0.9"
REALM = "ims.mnc001.mcc001.3gppnetwork.org"
PCSCF = "pcscf.ims.mnc001.mcc001.3gppnetwork.org"
ENABLED_UPLINK, ENABLED, DISABLED, REMOVED = 0, 2, 3, 4


class NullMessaging:
    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


@pytest.fixture
def pcrf(tmp_path, monkeypatch):
    monkeypatch.setitem(config["database"], "database", str(tmp_path / "rx.db"))
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
    node.rars = []

    def await_rar(requestType, hostname, **kwargs):
        node.rars.append(kwargs)
        raa = node.generate_avp(268, 40, node.int_to_hex(2001, 4))
        return node.generate_diameter_packet("01", "00", 258, 16777238, "00000001", "00000001", raa)
    node.awaitDiameterRequestAndResponse = await_rar
    yield node
    db.engine.dispose()


def media_component(node, flow_status, media_type=0, port=50000):
    rx = lambda code, data: node.generate_vendor_avp(code, "c0", 10415, data)
    text = lambda s: s.encode().hex()
    rtp = rx(519, rx(509, node.int_to_hex(1, 4))
             + rx(507, text(f"permit in 17 from {UE_IP} {port} to {NET_IP} 60000"))
             + rx(507, text(f"permit out 17 from {NET_IP} 60000 to {UE_IP} {port}"))
             + rx(512, node.int_to_hex(0, 4)))
    rtcp = rx(519, rx(509, node.int_to_hex(2, 4))
              + rx(507, text(f"permit in 17 from {UE_IP} {port + 1} to {NET_IP} 60001"))
              + rx(507, text(f"permit out 17 from {NET_IP} 60001 to {UE_IP} {port + 1}"))
              + rx(511, node.int_to_hex(ENABLED, 4))
              + rx(512, node.int_to_hex(1, 4)))
    flows = "" if flow_status == REMOVED else rtp + rtcp
    return rx(517, rx(518, node.int_to_hex(1, 4)) + flows + rx(520, node.int_to_hex(media_type, 4))
              + rx(515, node.int_to_hex(41000, 4)) + rx(516, node.int_to_hex(41000, 4))
              + rx(511, node.int_to_hex(flow_status, 4)))


def aar(node, flow_status, media_type=0):
    avp = node.generate_avp(263, 40, node.string_to_hex(f"{PCSCF};1;call"))
    avp += node.generate_avp(264, 40, node.string_to_hex(PCSCF))
    avp += node.generate_avp(296, 40, node.string_to_hex(REALM))
    avp += node.generate_avp(443, 40, node.generate_avp(450, 40, node.int_to_hex(2, 4))
                             + node.generate_avp(444, 40, node.string_to_hex(f"sip:{IMSI}@{REALM}")))
    avp += node.generate_avp(8, 40, "0a2d0002")                                          # Framed-IP-Address 10.45.0.2
    avp += media_component(node, flow_status, media_type)
    packet = node.generate_diameter_packet("01", "c0", 265, 16777236, "00000001", "00000002", avp)
    packet_vars, avps = node.decode_diameter_packet(bytes.fromhex(packet))
    return node.Answer_16777236_265(packet_vars, avps)


def test_held_call_gates_the_voice_rule(pcrf):
    aar(pcrf, ENABLED_UPLINK)

    rar = pcrf.rars[-1]
    assert rar["chargingRules"]["flow_status"] == ENABLED_UPLINK


def test_inactive_stream_disables_the_voice_rule(pcrf):
    aar(pcrf, DISABLED)

    assert pcrf.rars[-1]["chargingRules"]["flow_status"] == DISABLED


def test_resumed_call_enables_the_voice_rule_again(pcrf):
    aar(pcrf, DISABLED)
    aar(pcrf, ENABLED)

    assert pcrf.rars[-1]["chargingRules"]["flow_status"] == ENABLED


def test_removed_stream_removes_the_voice_rule(pcrf):
    aar(pcrf, REMOVED)

    rar = pcrf.rars[-1]
    assert rar["chargingRuleAction"] == "remove"
    assert rar["chargingRuleName"].startswith("GBR-Voice_")
    assert "chargingRules" not in rar


def test_video_component_gets_a_video_rule(pcrf):
    # Media-Type VIDEO is 1 (TS 29.214 5.3.19); the handler tested 4 (CONTROL),
    # so a video AAR got no rule at all.
    aar(pcrf, ENABLED, media_type=1)

    assert pcrf.rars[-1]["chargingRules"]["rule_name"].startswith("GBR-Video_")


def test_gx_rule_carries_the_flow_status(pcrf):
    rule = {"rule_name": "GBR-Voice_1", "tft": [], "qci": 1, "arp_priority": 14,
            "arp_preemption_capability": False, "arp_preemption_vulnerability": True,
            "mbr_ul": 41000, "mbr_dl": 41000, "gbr_ul": 41000, "gbr_dl": 41000,
            "precedence": 40, "rating_group": None, "flow_status": ENABLED_UPLINK}

    encoded = pcrf.Charging_Rule_Generator(ChargingRules=rule, ue_ip=UE_IP)

    flow_status = pcrf.generate_vendor_avp(511, "c0", 10415, pcrf.int_to_hex(ENABLED_UPLINK, 4))
    assert flow_status in encoded
