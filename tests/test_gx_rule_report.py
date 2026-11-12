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
import json
import time

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
    """Redis stand-in: keeps hashes (the node-local Rx call rules), ignores the rest."""

    def __init__(self):
        self.hashes = {}

    def setHashValue(self, name, key, value, keyExpiry=None, **kwargs):
        self.hashes.setdefault(name, {})[key] = json.loads(value)

    def getAllHashData(self, name, **kwargs):
        return dict(self.hashes.get(name, {}))

    def getHashValue(self, name, key, **kwargs):
        value = self.hashes.get(name, {}).get(key)
        return json.dumps(value).encode() if value is not None else ""

    def deleteHashKey(self, name, key, **kwargs):
        self.hashes.get(name, {}).pop(key, None)

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
    node.RX_RAR_MIN_AGE = 0          # the age rule has its own test
    node.sent = []
    node.gx_rars = []
    node.relayed = []
    node._rx_relay = lambda path, payload, label: node.relayed.append((path, payload))

    def await_gx_rar(requestType, hostname, **kwargs):
        node.gx_rars.append(kwargs)
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


# Specific-Actions of the P-CSCF's call AAR (opensips 0.1.49).
PCSCF_ACTIONS = (1, 2, 3, 4, 6, 9, 12)


def call_aar(node, session=CALL_SESSION, media_type=0, actions=PCSCF_ACTIONS, address_avp=None):
    """The P-CSCF authorizes a call; returns the name of the Gx rule installed for it."""
    rx = lambda code, data: node.generate_vendor_avp(code, "c0", 10415, data)
    component = rx(517, rx(518, node.int_to_hex(1, 4)) + rx(520, node.int_to_hex(media_type, 4))
                   + rx(511, node.int_to_hex(2, 4)))
    avp = node.generate_avp(263, 40, node.string_to_hex(session))
    avp += node.generate_avp(264, 40, node.string_to_hex(PCSCF))
    avp += node.generate_avp(296, 40, node.string_to_hex(REALM))
    avp += node.generate_avp(443, 40, node.generate_avp(450, 40, node.int_to_hex(2, 4))
                             + node.generate_avp(444, 40, node.string_to_hex(f"sip:{IMSI}@{REALM}")))
    avp += address_avp if address_avp is not None else node.generate_avp(8, 40, "0a2d0002")
    avp += component
    for action in actions:
        avp += node.generate_vendor_avp(513, "c0", 10415, node.int_to_hex(action, 4))
    packet = node.generate_diameter_packet("01", "c0", 265, 16777236, "00000001", "00000002", avp)
    packet_vars, avps = node.decode_diameter_packet(bytes.fromhex(packet))
    node.Answer_16777236_265(packet_vars, avps)
    return node.gx_rars[-1]["chargingRules"][0]["rule_name"]


def str_request(node, session=CALL_SESSION):
    avp = node.generate_avp(263, 40, node.string_to_hex(session))
    avp += node.generate_avp(264, 40, node.string_to_hex(PCSCF))
    avp += node.generate_avp(296, 40, node.string_to_hex(REALM))
    packet = node.generate_diameter_packet("01", "c0", 275, 16777236, "00000005", "00000006", avp)
    node.Answer_16777236_275(*node.decode_diameter_packet(bytes.fromhex(packet)))


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
    rule = call_aar(pcrf)

    result = ccr_update(pcrf, rule)

    assert result == 2001
    [(_, hostname, rar)] = rx_rars(pcrf)
    assert hostname == PCSCF
    assert rar["sessionId"] == CALL_SESSION
    assert rar["destinationHost"] == PCSCF
    assert rar["destinationRealm"] == REALM
    assert rar["specificActions"] == [FAILED_RESOURCES_ALLOCATION]


def test_failed_video_bearer_is_reported_too(pcrf):
    rule = call_aar(pcrf, media_type=1)

    ccr_update(pcrf, rule)

    assert rule.startswith("GBR-Video_")
    assert [rar[2]["sessionId"] for rar in rx_rars(pcrf)] == [CALL_SESSION]


def test_report_without_failure_code_still_counts(pcrf):
    # Rule-Failure-Code is optional in a Charging-Rule-Report; INACTIVE alone
    # already means the rule is not enforced.
    rule = call_aar(pcrf)

    ccr_update(pcrf, rule, failure_code=None)

    assert len(rx_rars(pcrf)) == 1


def test_active_rule_report_tells_nobody(pcrf):
    rule = call_aar(pcrf)

    ccr_update(pcrf, rule, status=ACTIVE)

    assert rx_rars(pcrf) == []


def test_failed_rule_of_another_subscriber_is_not_reported(pcrf):
    # Rule names carry the subscriber id; a rule that is not this subscriber's
    # call rule (a predefined APN rule, or another UE's) has no Rx session here.
    call_aar(pcrf)

    ccr_update(pcrf, "GBR-Voice_999_0000abcd")
    ccr_update(pcrf, "internet-default")

    assert rx_rars(pcrf) == [] and pcrf.relayed == []


def test_no_rar_after_the_call_ended(pcrf):
    # The STR removes the call's rule and its record; a late report must not
    # reach a session the P-CSCF has already closed.
    rule = call_aar(pcrf)
    str_request(pcrf)
    assert pcrf.gx_rars[-1]["chargingRuleName"] == [rule]
    assert pcrf.gx_rars[-1]["chargingRuleAction"] == "remove"

    ccr_update(pcrf, rule)

    assert rx_rars(pcrf) == []


def test_two_calls_each_get_their_own_rule_and_report(pcrf):
    # KI-151: one stored call session per subscriber, so with two calls at
    # once a RAR reached only the latest one. Each call now has its own rule.
    first = call_aar(pcrf, session=f"{PCSCF};1;first")
    second = call_aar(pcrf, session=f"{PCSCF};1;second")
    assert first != second

    ccr_update(pcrf, first)
    assert [rar[2]["sessionId"] for rar in rx_rars(pcrf)] == [f"{PCSCF};1;first"]

    str_request(pcrf, session=f"{PCSCF};1;first")          # the first call ends
    assert pcrf.gx_rars[-1]["chargingRuleName"] == [first]  # only its rule goes
    # ... and the second call's stored session is left alone
    assert pcrf.database.Get_IMS_Subscriber(imsi=IMSI)["pcscf_active_session"] == f"{PCSCF};1;second"

    ccr_update(pcrf, second)
    assert [rar[2]["sessionId"] for rar in rx_rars(pcrf)] == [f"{PCSCF};1;first", f"{PCSCF};1;second"]


def test_rule_held_by_another_node_is_relayed(pcrf):
    # The CCR-U can reach a node other than the one that handled the AAR, before
    # anything about the call has reached it; that node relays the report.
    rule = call_aar(pcrf)
    pcrf.redisMessaging.hashes.clear()                      # this node holds nothing

    ccr_update(pcrf, rule)

    assert rx_rars(pcrf) == []
    assert pcrf.relayed == [("/geored/rx_failed_call_rules",
                             {"subscriber_id": subscriber_id(pcrf), "rule_names": [rule]})]


def test_rule_of_a_call_from_before_the_upgrade(pcrf):
    # A call set up by HSS <= 1.7.33 has the per-subscriber rule; which
    # Specific-Actions its AF subscribed to is not known, so it gets no RAR.
    call_aar(pcrf)

    ccr_update(pcrf, f"GBR-Voice_{subscriber_id(pcrf)}")

    assert rx_rars(pcrf) == []


def test_af_that_did_not_subscribe_is_not_told(pcrf):
    # TS 29.214 4.4.6: INDICATION_OF_FAILED_RESOURCES_ALLOCATION goes only to an
    # AF that subscribed to it. The P-CSCF before opensips 0.1.49 subscribed to
    # 1-6 and 12; PyHSS used to send the RAR anyway (a SPEC-DEVIATION).
    rule = call_aar(pcrf, actions=(1, 2, 3, 4, 5, 6, 12))

    ccr_update(pcrf, rule)

    assert rx_rars(pcrf) == []


def test_later_aar_without_specific_actions_keeps_the_subscription(pcrf):
    # The subscription is made in the session's first AAR; an update AAR
    # (183, re-INVITE) that names no Specific-Action leaves it in place.
    rule = call_aar(pcrf)
    assert call_aar(pcrf, actions=()) == rule

    ccr_update(pcrf, rule)

    assert len(rx_rars(pcrf)) == 1


def test_rar_names_the_failed_media_component(pcrf):
    # TS 29.214 4.4.6: the RAR carries the affected flows (Flows with the
    # Media-Component-Number of the call's component).
    rule = call_aar(pcrf)

    ccr_update(pcrf, rule)

    assert rx_rars(pcrf)[0][2]["mediaComponents"] == [1]
    packet = pcrf.Request_16777236_258(sessionId=CALL_SESSION, destinationHost=PCSCF, destinationRealm=REALM,
                                       specificActions=[FAILED_RESOURCES_ALLOCATION], mediaComponents=[1])
    _, avps = pcrf.decode_diameter_packet(bytes.fromhex(packet))
    assert [int(value, 16) for value in pcrf.get_avp_data(avps, 518)] == [1]


def test_ipv6_ue_address_from_framed_ipv6_prefix(pcrf):
    # KI-72: the P-CSCF (opensips 0.1.49) names an IPv6 UE with
    # Framed-IPv6-Prefix (AVP 97) /128 instead of a 16-byte Framed-IP-Address.
    prefix = pcrf.generate_avp(97, 40, "0080" + "cafe00000046000412340000000000aa")
    packet = pcrf.generate_diameter_packet("01", "c0", 265, 16777236, "00000001", "00000002", prefix)
    _, avps = pcrf.decode_diameter_packet(bytes.fromhex(packet))
    assert pcrf.ue_address(avps) == "cafe:0:46:4:1234::aa"

    legacy = pcrf.generate_avp(8, 40, "cafe00000046000412340000000000aa")
    packet = pcrf.generate_diameter_packet("01", "c0", 265, 16777236, "00000001", "00000002", legacy)
    _, avps = pcrf.decode_diameter_packet(bytes.fromhex(packet))
    assert pcrf.ue_address(avps) == "cafe:0:46:4:1234::aa"           # the old 16-byte AVP 8 still works


def test_ipv6_formatting():
    # The old formatter stripped each group's zeros: "cafe:0000:0046:..." became
    # "cafe::46:..." and runs of zero groups became ":::".
    node = Diameter.__new__(Diameter)
    assert node.hex_to_ip("cafe00000046000400000000000000aa") == "cafe:0:46:4::aa"
    assert node.hex_to_ip("0a2d0002") == "10.45.0.2"


def test_ipv6_only_pdn_session_is_found_by_the_ue_address(pcrf):
    # A CCR-I of an IPv6-only PDN connection carries only Framed-IPv6-Prefix
    # (/64). It was stored as "Failed to Decode / Get UE IP", the same value for
    # every such session; the AAR's /128 address is now matched to the /64.
    avp = pcrf.generate_avp(263, 40, pcrf.string_to_hex("pgw;6;6"))
    avp += pcrf.generate_avp(264, 40, pcrf.string_to_hex("pgw.epc"))
    avp += pcrf.generate_avp(296, 40, pcrf.string_to_hex("epc"))
    avp += pcrf.generate_avp(258, 40, "01000016")
    avp += pcrf.generate_avp(416, 40, pcrf.int_to_hex(1, 4))
    avp += pcrf.generate_avp(415, 40, pcrf.int_to_hex(0, 4))
    avp += pcrf.generate_avp(443, 40, pcrf.generate_avp(450, 40, pcrf.int_to_hex(1, 4))
                             + pcrf.generate_avp(444, 40, pcrf.string_to_hex(IMSI)))
    avp += pcrf.generate_avp(30, 40, pcrf.string_to_hex("internet"))
    avp += pcrf.generate_avp(97, 40, "0040" + "cafe0000004600040000000000000000")
    packet = pcrf.generate_diameter_packet("01", "c0", 272, 16777238, "00000009", "0000000a", avp)
    pcrf.Answer_16777238_272(*pcrf.decode_diameter_packet(bytes.fromhex(packet)))

    found = pcrf.serving_apn_for_address("cafe:0:46:4:1234::aa")

    assert found and found["subscriber_routing"] == "cafe:0:46:4::/64"


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


def ccr_terminate(node, imsi, apn):
    avp = node.generate_avp(263, 40, node.string_to_hex("pgw;9;9"))
    avp += node.generate_avp(264, 40, node.string_to_hex("pgw.epc"))
    avp += node.generate_avp(296, 40, node.string_to_hex("epc"))
    avp += node.generate_avp(258, 40, "01000016")
    avp += node.generate_avp(416, 40, node.int_to_hex(3, 4))
    avp += node.generate_avp(415, 40, node.int_to_hex(2, 4))
    avp += node.generate_avp(443, 40, node.generate_avp(450, 40, node.int_to_hex(1, 4))
                             + node.generate_avp(444, 40, node.string_to_hex(imsi)))
    avp += node.generate_avp(30, 40, node.string_to_hex(apn))
    packet = node.generate_diameter_packet("01", "c0", 272, 16777238, "00000007", "00000008", avp)
    answer = node.Answer_16777238_272(*node.decode_diameter_packet(bytes.fromhex(packet)))
    _, answer_avps = node.decode_diameter_packet(bytes.fromhex(answer))
    return int(node.get_avp_data(answer_avps, 268)[0], 16)


def test_ccr_t_for_a_deleted_subscriber_or_unprovisioned_apn_succeeds(pcrf):
    # The CCR-I of such a session is accepted; its CCR-T was answered 5030
    # (DIAMETER_USER_UNKNOWN) because the lookups for the state to clear raised,
    # e.g. for a subscriber deleted while attached. There is nothing to clear.
    assert ccr_terminate(pcrf, "001010000009999", "ims") == 2001
    assert ccr_terminate(pcrf, IMSI, "temporary-apn") == 2001
    assert ccr_terminate(pcrf, IMSI, "ims") == 2001


def test_call_end_removes_every_rule_of_the_call(pcrf):
    # KI-168: a video call has a voice and a video rule; the STR removed only
    # the voice rule, so the video bearer stayed until the PDN connection ended.
    rx = lambda code, data: pcrf.generate_vendor_avp(code, "c0", 10415, data)
    components = "".join(rx(517, rx(518, pcrf.int_to_hex(number, 4)) + rx(520, pcrf.int_to_hex(media, 4))
                            + rx(511, pcrf.int_to_hex(2, 4))) for number, media in ((1, 0), (2, 1)))
    avp = pcrf.generate_avp(263, 40, pcrf.string_to_hex(CALL_SESSION))
    avp += pcrf.generate_avp(264, 40, pcrf.string_to_hex(PCSCF))
    avp += pcrf.generate_avp(296, 40, pcrf.string_to_hex(REALM))
    avp += pcrf.generate_avp(443, 40, pcrf.generate_avp(450, 40, pcrf.int_to_hex(2, 4))
                             + pcrf.generate_avp(444, 40, pcrf.string_to_hex(f"sip:{IMSI}@{REALM}")))
    avp += pcrf.generate_avp(8, 40, "0a2d0002") + components
    packet = pcrf.generate_diameter_packet("01", "c0", 265, 16777236, "00000001", "00000002", avp)
    pcrf.Answer_16777236_265(*pcrf.decode_diameter_packet(bytes.fromhex(packet)))
    installed = sorted(rule["rule_name"] for rule in pcrf.gx_rars[-1]["chargingRules"])

    str_request(pcrf)

    assert sorted(pcrf.gx_rars[-1]["chargingRuleName"]) == installed and len(installed) == 2


def test_report_right_after_the_aar_waits_for_the_aaa(pcrf):
    # Live 2026-11-12 21:01:17 UTC: the bearer was refused at once, and the RAR
    # reached the P-CSCF 10 ms before the AAA, so it did not know the session
    # yet (answer 5001). The worker hands a fresh rule to the relay; the relay
    # endpoint sends once the rule is RX_RAR_MIN_AGE old.
    pcrf.RX_RAR_MIN_AGE = 0.3
    rule = call_aar(pcrf)

    ccr_update(pcrf, rule)
    assert rx_rars(pcrf) == []
    assert pcrf.relayed[-1] == ("/geored/rx_failed_call_rules", {"subscriber_id": subscriber_id(pcrf), "rule_names": [rule]})

    started = time.monotonic()
    sent, missing = pcrf.rx_send_failed_resources(subscriber_id(pcrf), [rule], wait_fresh=True)
    assert sent == [CALL_SESSION] and missing == []
    assert time.monotonic() - started > 0.1


def test_str_succeeds_when_the_pgw_no_longer_has_the_rule(pcrf):
    # After a reported failure the PGW has dropped the rule and answers its
    # removal with an error; the STR was answered 5001.
    rule = call_aar(pcrf)

    def await_failed(requestType, hostname, **kwargs):
        pcrf.gx_rars.append(kwargs)
        raa = pcrf.generate_avp(268, 40, pcrf.int_to_hex(5012, 4))
        return pcrf.generate_diameter_packet("01", "00", 258, 16777238, "00000001", "00000001", raa)
    pcrf.awaitDiameterRequestAndResponse = await_failed
    avp = pcrf.generate_avp(263, 40, pcrf.string_to_hex(CALL_SESSION))
    avp += pcrf.generate_avp(264, 40, pcrf.string_to_hex(PCSCF))
    avp += pcrf.generate_avp(296, 40, pcrf.string_to_hex(REALM))
    packet = pcrf.generate_diameter_packet("01", "c0", 275, 16777236, "00000005", "00000006", avp)
    answer = pcrf.Answer_16777236_275(*pcrf.decode_diameter_packet(bytes.fromhex(packet)))
    _, avps = pcrf.decode_diameter_packet(bytes.fromhex(answer))

    assert int(pcrf.get_avp_data(avps, 268)[0], 16) == 2001
