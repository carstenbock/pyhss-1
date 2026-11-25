# Tests for the subscriber self-care API for supplementary services.
# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The self-care API lets the Self-Care Portal change a few supplementary
services for the subscriber who is logged in (ADR-0052). The portal faces
the internet, so the API must never let it change anything else: barring,
operator settings or another forwarding rule set by the operator.
"""
import json
import xml.etree.ElementTree as ET

import pytest
import requests

import simservs_selfcare as ss

base_url = 'http://localhost:8080'
SELFCARE_KEY = "selfcareTestKey"          # tests/config.yaml
PROVISIONING_KEY = "changeThisKeyInProduction"
NS = {"ss": ss.NS_SS, "cp": ss.NS_CP}

OPERATOR_PROFILE = """<?xml version="1.0"?>
<Sh-Data>
  <RepositoryData>
    <ServiceIndication>MMTEL-Services</ServiceIndication>
    <SequenceNumber>7</SequenceNumber>
    <ServiceData>
      <ss:MMTelServices xmlns:ss="http://uri.etsi.org/ngn/params/xml/simservs/xcap"
                        xmlns:cp="urn:ietf:params:xml:ns:common-policy">
        <ss:complete-communication-barring>
          <ss:outgoing-communication-barring active="true"/>
        </ss:complete-communication-barring>
        <ss:complete-communication-diversion>
          <ss:communication-diversion active="true">
            <ss:NoReplyTimer>20</ss:NoReplyTimer>
            <cp:ruleset>
              <cp:rule id="op-cfnrc">
                <cp:conditions><cp:not-reachable/></cp:conditions>
                <cp:actions><cp:forward-to><cp:target>tel:+4930111</cp:target>
                  <cp:notify-caller>true</cp:notify-caller></cp:forward-to></cp:actions>
              </cp:rule>
              <cp:rule id="op-cfb">
                <cp:conditions><cp:busy/></cp:conditions>
                <cp:actions><cp:forward-to><cp:target>tel:+4930222</cp:target>
                  <cp:notify-caller>true</cp:notify-caller></cp:forward-to></cp:actions>
              </cp:rule>
            </cp:ruleset>
          </ss:communication-diversion>
        </ss:complete-communication-diversion>
        <ss:complete-customized-alerting-tone>
          <ss:operator-customized-alerting-tone authorized="true"/>
        </ss:complete-customized-alerting-tone>
      </ss:MMTelServices>
    </ServiceData>
  </RepositoryData>
  <RepositoryData>
    <ServiceIndication>Other-AS</ServiceIndication>
    <SequenceNumber>3</SequenceNumber>
  </RepositoryData>
</Sh-Data>"""


def _canon(elem):
    return ET.canonicalize(ET.tostring(elem, encoding="unicode"), strip_text=True)


def _apply(profile, body, prefixes=("+49",)):
    return ss.apply_changes(profile, ss.validate_changes(body, list(prefixes)))


# ---------------------------------------------------------------------------
# Profile editing (lib/simservs_selfcare.py)
# ---------------------------------------------------------------------------

def test_a_change_leaves_operator_settings_untouched():
    """Barring, operator authorizations, the operator's CFNRc rule and other
    ASs' repository data are not the subscriber's to change; a self-care
    write must carry them over unchanged."""
    before = ET.fromstring(OPERATOR_PROFILE)
    after = ET.fromstring(_apply(OPERATOR_PROFILE, {
        "call_forwarding": {"cfu": {"active": True, "target": "+4930999"}},
        "acr": True, "cw": True, "oir": True, "language": "de-DE", "no_reply_timer": 25}))

    for path in (".//ss:complete-communication-barring",
                 ".//ss:complete-customized-alerting-tone",
                 ".//cp:rule[@id='op-cfnrc']",
                 ".//cp:rule[@id='op-cfb']"):
        assert _canon(after.find(path, NS)) == _canon(before.find(path, NS)), path
    other = [r for r in after.findall("RepositoryData")
             if r.findtext("ServiceIndication") == "Other-AS"]
    assert len(other) == 1 and other[0].findtext("SequenceNumber") == "3"


def test_the_mmtel_as_sees_the_change():
    """The sh-daemon's simplify_user_data reads exactly these paths for the
    MMTel-AS; a value written anywhere else would have no effect on calls."""
    root = ET.fromstring(_apply("", {
        "call_forwarding": {"cfna": {"active": True, "target": "+4930999"}},
        "acr": True, "cw": True, "oir": True, "language": "fr", "no_reply_timer": 15}))
    mmtel = root.find("RepositoryData/ServiceData/ss:MMTelServices", NS)
    assert mmtel.find("ss:anonymous-communication-rejection", NS).get("active") == "true"
    assert mmtel.find("ss:complete-communication-waiting/ss:communication-waiting", NS).get("active") == "true"
    assert mmtel.find("ss:complete-originating-identity-restriction/"
                      "ss:originating-identity-presentation-restriction", NS).get("active") == "true"
    assert mmtel.findtext("ss:language", namespaces=NS) == "fr"
    cd = mmtel.find("ss:complete-communication-diversion/ss:communication-diversion", NS)
    assert cd.findtext("ss:NoReplyTimer", namespaces=NS) == "15"
    rule = cd.find("cp:ruleset/cp:rule", NS)
    assert rule.find("cp:conditions/cp:no-answer", NS) is not None
    assert rule.findtext("cp:actions/cp:forward-to/cp:target", namespaces=NS) == "tel:+4930999"


def test_switching_forwarding_off_removes_only_that_rule():
    """Switching CFB off must not take the operator's CFNRc with it; the
    diversion service stays active while a forwarding rule is left."""
    root = ET.fromstring(_apply(OPERATOR_PROFILE, {"call_forwarding": {"cfb": {"active": False}}}))
    cd = root.find(".//ss:communication-diversion", NS)
    assert [r.get("id") for r in cd.iter(f"{{{ss.NS_CP}}}rule")] == ["op-cfnrc"]
    assert cd.get("active") == "true"
    assert ss.read_settings(ET.tostring(root, encoding="unicode"))["call_forwarding"]["cfb"]["active"] is False


def test_replacing_a_target_keeps_the_notify_caller_choice():
    """The portal does not expose notify-caller; changing the target must
    not silently reset what the operator set."""
    root = ET.fromstring(_apply(OPERATOR_PROFILE, {
        "call_forwarding": {"cfb": {"active": True, "target": "+4930333"}}}))
    rules = [r for r in root.iter(f"{{{ss.NS_CP}}}rule") if r.find("cp:conditions/cp:busy", NS) is not None]
    assert len(rules) == 1
    assert rules[0].findtext("cp:actions/cp:forward-to/cp:target", namespaces=NS) == "tel:+4930333"
    assert rules[0].findtext("cp:actions/cp:forward-to/cp:notify-caller", namespaces=NS) == "true"


def test_read_reports_what_the_mmtel_as_applies():
    settings = ss.read_settings(OPERATOR_PROFILE)
    assert settings["call_forwarding"]["cfb"] == {"active": True, "target": "tel:+4930222"}
    assert settings["call_forwarding"]["cfu"] == {"active": False, "target": ""}
    assert "cfnrc" not in settings["call_forwarding"]
    assert settings["no_reply_timer"] == 20
    # Absent elements: the MMTel-AS defaults (flags off, 30 s timer).
    empty = ss.read_settings("")
    assert (empty["acr"], empty["cw"], empty["oir"], empty["no_reply_timer"]) == (False, False, False, 30)


def test_every_write_bumps_the_sequence_number():
    """TS 29.328 clause 7.6.1: an AS compares sequence numbers; an unchanged
    number would make the change look stale. After 65535 comes 1."""
    assert ET.fromstring(_apply(OPERATOR_PROFILE, {"cw": True})).findtext(
        "RepositoryData/SequenceNumber") == "8"
    assert ET.fromstring(_apply("", {"cw": True})).findtext("RepositoryData/SequenceNumber") == "1"
    wrapped = OPERATOR_PROFILE.replace("<SequenceNumber>7<", "<SequenceNumber>65535<")
    assert ET.fromstring(_apply(wrapped, {"cw": True})).findtext("RepositoryData/SequenceNumber") == "1"


def test_a_bare_simservs_document_is_moved_into_an_envelope():
    """The HSS serves only enveloped RepositoryData over Sh, so a bare
    document would never reach the MMTel-AS."""
    bare = ('<ss:simservs xmlns:ss="http://uri.etsi.org/ngn/params/xml/simservs/xcap">'
            '<ss:complete-communication-barring><ss:incoming-communication-barring active="true"/>'
            '</ss:complete-communication-barring></ss:simservs>')
    root = ET.fromstring(_apply(bare, {"acr": True}))
    assert root.tag == "Sh-Data"
    assert root.find(".//ss:incoming-communication-barring", NS).get("active") == "true"


@pytest.mark.parametrize("body", [
    {"bar_outgoing": False},                                     # barring is the operator's
    {"call_forwarding": {"cfnrc": {"active": False}}},            # not applied by the MMTel-AS
    {"cat": True},                                                # not implemented
    {"tir": True},
])
def test_settings_outside_the_allowlist_are_rejected(body):
    with pytest.raises(ss.SelfcareError):
        ss.validate_changes(body, ["+49"])


@pytest.mark.parametrize("target", ["+4412345678", "+90012345678", "+4930", "4930999", "sip:x@evil", ""])
def test_forwarding_targets_outside_the_allowed_prefixes_are_rejected(target):
    """Forwarding to an international or premium number costs the operator
    money; only numbers with an allowed prefix may be set."""
    with pytest.raises(ss.SelfcareError):
        ss.validate_changes({"call_forwarding": {"cfu": {"active": True, "target": target}}}, ["+49"])


def test_without_allowed_prefixes_forwarding_can_only_be_switched_off():
    """Fail safe: an environment that has not configured prefixes allows no
    forwarding target at all, but subscribers can still switch it off."""
    with pytest.raises(ss.SelfcareError):
        ss.validate_changes({"call_forwarding": {"cfu": {"active": True, "target": "+4930999"}}}, [])
    assert ss.validate_changes({"call_forwarding": {"cfu": {"active": False}}}, []) == \
        {"call_forwarding": {"cfu": {"active": False}}}


@pytest.mark.parametrize("timer,ok", [(4, False), (5, True), (180, True), (181, False), (True, False), ("20", False)])
def test_no_reply_timer_range(timer, ok):
    """TS 24.604 XML schema: 5 to 180 seconds."""
    if ok:
        assert ss.validate_changes({"no_reply_timer": timer}, [])["no_reply_timer"] == timer
    else:
        with pytest.raises(ss.SelfcareError):
            ss.validate_changes({"no_reply_timer": timer}, [])


def test_targets_are_stored_as_tel_uris():
    changes = ss.validate_changes({"call_forwarding": {"cfu": {"active": True, "target": "+49 30 999-000"}}}, ["+49"])
    assert changes["call_forwarding"]["cfu"]["target"] == "tel:+4930999000"


# ---------------------------------------------------------------------------
# REST API (services/apiService.py)
# ---------------------------------------------------------------------------

IMSI = "001019990000555"
MSISDN = "4930555000"
SELFCARE = {"Selfcare-Key": SELFCARE_KEY, "Content-Type": "application/json"}


@pytest.fixture(scope="module")
def ims_subscriber(create_test_db, run_pyhss_api):
    r = requests.put(base_url + '/ims_subscriber/', headers={"Content-Type": "application/json"},
                     data=json.dumps({"msisdn": "+" + MSISDN, "msisdn_list": "+" + MSISDN, "imsi": IMSI,
                                      "ifc_path": "default_ifc.xml", "xcap_profile": OPERATOR_PROFILE}))
    assert r.status_code == 200, r.text
    ims_subscriber_id = r.json()["ims_subscriber_id"]
    yield ims_subscriber_id
    requests.delete(base_url + '/ims_subscriber/' + str(ims_subscriber_id))


def _stored_profile(ims_subscriber_id):
    return requests.get(base_url + '/ims_subscriber/' + str(ims_subscriber_id)).json()


@pytest.mark.parametrize("headers", [
    {},
    {"Selfcare-Key": "wrong"},
    {"Provisioning-Key": PROVISIONING_KEY},          # the admin key is not a self-care key
    {"Selfcare-Key": PROVISIONING_KEY},
])
def test_the_api_requires_the_selfcare_key(ims_subscriber, headers):
    """Even with lock_provisioning off (tests/config.yaml) the self-care API
    stays closed without its own key."""
    r = requests.get(base_url + '/selfcare/supplementary_services/imsi/' + IMSI, headers=headers)
    assert r.status_code == 401


def test_get_by_imsi_and_msisdn(ims_subscriber):
    for ref in ("imsi/" + IMSI, "msisdn/" + MSISDN, "msisdn/+" + MSISDN):
        r = requests.get(base_url + '/selfcare/supplementary_services/' + ref, headers=SELFCARE)
        assert r.status_code == 200, (ref, r.text)
        assert r.json()["call_forwarding"]["cfb"] == {"active": True, "target": "tel:+4930222"}
        assert r.json()["allowed_target_prefixes"] == ["+49"]


def test_put_changes_the_stored_profile(ims_subscriber):
    r = requests.put(base_url + '/selfcare/supplementary_services/msisdn/' + MSISDN, headers=SELFCARE,
                     data=json.dumps({"call_forwarding": {"cfu": {"active": True, "target": "+4930999"}}, "acr": True}))
    assert r.status_code == 200, r.text
    assert r.json()["call_forwarding"]["cfu"] == {"active": True, "target": "tel:+4930999"}
    stored = _stored_profile(ims_subscriber)
    assert not stored.get("sh_profile")
    root = ET.fromstring(stored["xcap_profile"])
    assert root.find(".//ss:outgoing-communication-barring", NS).get("active") == "true"
    assert root.find(".//ss:anonymous-communication-rejection", NS).get("active") == "true"


@pytest.mark.parametrize("body", [
    {"bar_outgoing": False},
    {"call_forwarding": {"cfu": {"active": True, "target": "+4412345678"}}},
    {"no_reply_timer": 300},
    None,
])
def test_a_rejected_put_changes_nothing(ims_subscriber, body):
    before = _stored_profile(ims_subscriber)["xcap_profile"]
    r = requests.put(base_url + '/selfcare/supplementary_services/imsi/' + IMSI, headers=SELFCARE,
                     data=json.dumps(body))
    assert r.status_code == 400
    assert _stored_profile(ims_subscriber)["xcap_profile"] == before


@pytest.mark.parametrize("ref,status", [
    ("imsi/001019990000999", 404),
    ("msisdn/4930555999", 404),
    ("ims_subscriber_id/1", 400),
    ("imsi/abc", 400),
])
def test_unknown_or_malformed_subscribers(ims_subscriber, ref, status):
    r = requests.get(base_url + '/selfcare/supplementary_services/' + ref, headers=SELFCARE)
    assert r.status_code == status
