# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cx registration state of an IMS subscriber (TS 29.228 sections 6.1.2 and
6.1.4): Not Registered, Registered, or Unregistered, i.e. an S-CSCF assigned
only to run the services of a subscriber that is not registered.

The HSS used to know one bit, "an S-CSCF is stored". A call to a subscriber
that was not registered was therefore rejected by the I-CSCF although the
subscriber had call forwarding for exactly that case, and a subscriber whose
registration had expired was reported as registered forever."""
import binascii
import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy

import database as database_module
from database import AUC, Base, Database, IFC_TEMPLATE, IMS_SUBSCRIBER, SUBSCRIBER
from database import ims_registration_state, IMS_NOT_REGISTERED, IMS_REGISTERED, IMS_UNREGISTERED
from databaseSchema import DatabaseSchema
from diameter import Diameter
from logtool import LogTool
from pyhss_config import config
from template_cache import IfcTemplateCache, ifc_template_has_unregistered_services

top_dir = Path(Path(__file__) / "../..").resolve()

SCSCF = "sip:scscf.ims.mnc001.mcc001.3gppnetwork.org"
OTHER_SCSCF = "sip:scscf2.ims.mnc001.mcc001.3gppnetwork.org"
IMSI = "001010000000001"
MSISDN = "4915100000001"
IMPU = "sip:+" + MSISDN + "@ims.mnc001.mcc001.3gppnetwork.org"

PROFILE = """<IMSSubscription><PrivateID>{{ iFC_vars['imsi'] }}</PrivateID><ServiceProfile>
<PublicIdentity><Identity>sip:+{{ iFC_vars['msisdn'] }}@ims.example</Identity></PublicIdentity>
%s
</ServiceProfile></IMSSubscription>"""
# What is deployed: the unregistered-state iFC is part of shared set 1, which
# only the S-CSCF knows. Nothing in the profile itself says "SessionCase 2".
SHARED_SET_TEMPLATE = PROFILE % "<Extension><SharedIFCSetID>1</SharedIFCSetID><SharedIFCSetID>3</SharedIFCSetID></Extension>"
INLINE_TEMPLATE = PROFILE % ("<InitialFilterCriteria><Priority>40</Priority><TriggerPoint><SPT><SessionCase>2</SessionCase></SPT>"
                             "</TriggerPoint><ApplicationServer><ServerName>sip:vm.example</ServerName></ApplicationServer></InitialFilterCriteria>")
# Registered-state services only; the unregistered iFC is commented out, as in
# the default_ifc.xml shipped with PyHSS.
NO_UNREG_TEMPLATE = PROFILE % ("<Extension><SharedIFCSetID>3</SharedIFCSetID></Extension>"
                               "<!-- <InitialFilterCriteria><TriggerPoint><SPT><SessionCase>2</SessionCase></SPT></TriggerPoint></InitialFilterCriteria> -->")
TEMPLATES = {1: SHARED_SET_TEMPLATE, 2: INLINE_TEMPLATE, 3: NO_UNREG_TEMPLATE}


class NullMessaging:
    """Logging and metrics go nowhere."""

    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


@pytest.fixture
def hss(tmp_path, monkeypatch):
    """A PyHSS Diameter node on its own database, with one IMS subscriber whose
    iFC template (id 1) references shared iFC set 1."""
    monkeypatch.setitem(config["database"], "database", str(tmp_path / "cx.db"))
    monkeypatch.setitem(config["hss"], "ifc_templates", {"use_database": True, "cache_enabled": True})
    monkeypatch.setitem(config["hss"], "OriginHost", "hss01")
    monkeypatch.delitem(config["hss"], "cx", raising=False)

    db = Database(LogTool(config), main_service=True)
    for template_id, content in TEMPLATES.items():
        db.CreateObj(IFC_TEMPLATE, {"ifc_template_id": template_id, "name": f"t{template_id}", "template_content": content})
    db.CreateObj(AUC, {"auc_id": 1, "ki": "3c6e0b8a9c15224a8228b9a98ca1531d", "opc": "762a2206fe0b4151ace403c86a11e479",
                       "amf": "8000", "sqn": 0, "imsi": IMSI})
    db.CreateObj(SUBSCRIBER, {"auc_id": 1, "default_apn": 1, "apn_list": "1", "imsi": IMSI, "msisdn": MSISDN})
    db.CreateObj(IMS_SUBSCRIBER, {"imsi": IMSI, "msisdn": MSISDN, "ifc_template_id": 1})

    node = Diameter.__new__(Diameter)
    node.logTool = LogTool(config)
    node.redisMessaging = NullMessaging()
    node.database = db
    node.OriginHost = node.string_to_hex("hss01")
    node.OriginRealm = node.string_to_hex("epc.mnc001.mcc001.3gppnetwork.org")
    node.MNC = "001"
    node.MCC = "001"
    node.hostname = "hss01"
    node.ifcTemplateCache = IfcTemplateCache()
    node.ifcCacheEnabled = True
    node.ifcUseDatabase = True
    node.ifcDefaultTemplatePath = "default_ifc.xml"
    yield node
    db.engine.dispose()


def request(node, command, extra_avps):
    avp = node.generate_avp(263, 40, node.string_to_hex("scscf;1;1"))
    avp += node.generate_avp(264, 40, node.string_to_hex("scscf-0.scscf.ims.example"))
    avp += node.generate_avp(296, 40, node.string_to_hex("ims.example"))
    avp += extra_avps
    packet = node.generate_diameter_packet("01", "c0", command, 16777216, "00000001", "00000002", avp)
    return node.decode_diameter_packet(bytes.fromhex(packet))


class Answer:
    def __init__(self, node, packet_hex):
        self.packet_vars, self.avps = node.decode_diameter_packet(bytes.fromhex(packet_hex))
        self.application_id = self.packet_vars["ApplicationId"]
        self.result_code = self._int(node, 268)
        self.experimental_result_code = self._int(node, 298)
        self.server_name = self._str(node, 602)
        self.user_data = self._str(node, 606)

    def _int(self, node, code):
        data = node.get_avp_data(self.avps, code)
        return int(data[0], 16) if data else None

    def _str(self, node, code):
        data = node.get_avp_data(self.avps, code)
        return binascii.unhexlify(data[0]).decode() if data else None


def sar(node, assignment_type, scscf=SCSCF, impu=IMPU, user_data_already_available=0, with_user_name=True):
    avps = node.generate_vendor_avp(601, "c0", 10415, node.string_to_hex(impu))
    avps += node.generate_vendor_avp(602, "c0", 10415, node.string_to_hex(scscf))
    avps += node.generate_vendor_avp(614, "c0", 10415, node.int_to_hex(assignment_type, 4))
    avps += node.generate_vendor_avp(624, "c0", 10415, node.int_to_hex(user_data_already_available, 4))
    if with_user_name:
        avps += node.generate_avp(1, 40, node.string_to_hex(IMSI + "@ims.example"))
    return Answer(node, node.Answer_16777216_301(*request(node, 301, avps)))


def lir(node, impu=IMPU):
    avps = node.generate_vendor_avp(601, "c0", 10415, node.string_to_hex(impu))
    return Answer(node, node.Answer_16777216_302(*request(node, 302, avps)))


def uar(node, authorization_type=None):
    avps = node.generate_avp(1, 40, node.string_to_hex(IMSI + "@ims.example"))
    avps += node.generate_vendor_avp(601, "c0", 10415, node.string_to_hex(IMPU))
    if authorization_type is not None:
        avps += node.generate_vendor_avp(623, "c0", 10415, node.int_to_hex(authorization_type, 4))
    return Answer(node, node.Answer_16777216_300(*request(node, 300, avps)))


def stored(node):
    row = node.database.Get_IMS_Subscriber(imsi=IMSI)
    return ims_registration_state(row), row["scscf"]


def put_in_state(node, state):
    if state == IMS_REGISTERED:
        assert sar(node, 1).result_code == 2001
    elif state == IMS_UNREGISTERED:
        assert sar(node, 3).result_code == 2001
    assert stored(node)[0] == state


# --- SAR: one test per row of the Server-Assignment-Type matrix -----------------

def test_registration_assigns_the_scscf_and_downloads_the_profile(hss):
    answer = sar(hss, 1)
    assert answer.result_code == 2001
    assert "<IMSSubscription>" in answer.user_data
    assert stored(hss) == (IMS_REGISTERED, SCSCF)


@pytest.mark.parametrize("before", [IMS_NOT_REGISTERED, IMS_REGISTERED, IMS_UNREGISTERED])
def test_re_registration_registers_from_any_state(hss, before):
    """A refresh is also what repairs the HSS after an expiry SAR overtook a
    registration: whatever the HSS believed, the subscriber is registered now."""
    put_in_state(hss, before)
    answer = sar(hss, 2, user_data_already_available=1)
    assert answer.result_code == 2001
    assert answer.user_data is None, "the S-CSCF said it holds the profile"
    assert stored(hss) == (IMS_REGISTERED, SCSCF)


@pytest.mark.parametrize("before", [IMS_NOT_REGISTERED, IMS_UNREGISTERED, IMS_REGISTERED])
def test_unregistered_user_assigns_the_scscf_without_registering(hss, before):
    """The S-CSCF got a call for a subscriber it has no binding for and asks for
    the profile to run the unregistered services. It has to stay assigned, or the
    next call starts from scratch, and the subscriber must not count as
    registered: nothing can be delivered to it. From Registered too -- that is the
    repair for an expiry the HSS was never told about."""
    put_in_state(hss, before)
    answer = sar(hss, 3, with_user_name=False)
    assert answer.result_code == 2001
    assert "<IMSSubscription>" in answer.user_data
    assert stored(hss) == (IMS_UNREGISTERED, SCSCF)


@pytest.mark.parametrize("assignment_type", [4, 5, 8, 11])
@pytest.mark.parametrize("before", [IMS_REGISTERED, IMS_UNREGISTERED])
def test_deregistration_clears_the_assignment(hss, assignment_type, before):
    """Timeout, user, administrative and too-much-data de-registration end in
    Not Registered with no S-CSCF, from Registered and from Unregistered. No
    profile is sent: the S-CSCF is about to drop it, and rendering it for every
    expiry would be the most expensive part of the answer."""
    put_in_state(hss, before)
    answer = sar(hss, assignment_type, with_user_name=False)
    assert answer.result_code == 2001
    assert answer.user_data is None
    assert stored(hss) == (IMS_NOT_REGISTERED, None)


@pytest.mark.parametrize("assignment_type", [6, 7])
def test_store_server_name_is_declined_by_default(hss, assignment_type):
    """The S-CSCF offers to stay assigned; by default the HSS declines and says
    so with 2004, which tells the S-CSCF to drop the profile (TS 29.228 6.6.2)."""
    put_in_state(hss, IMS_REGISTERED)
    answer = sar(hss, assignment_type)
    assert (answer.result_code, answer.experimental_result_code) == (None, 2004)
    assert answer.user_data is None
    assert stored(hss) == (IMS_NOT_REGISTERED, None)


@pytest.mark.parametrize("assignment_type", [6, 7])
def test_store_server_name_keeps_the_scscf_when_configured(hss, assignment_type, monkeypatch):
    monkeypatch.setitem(config["hss"], "cx", {"keep_scscf_on_deregistration": True})
    put_in_state(hss, IMS_REGISTERED)
    answer = sar(hss, assignment_type)
    assert (answer.result_code, answer.experimental_result_code) == (2001, None)
    assert answer.user_data is None
    assert stored(hss) == (IMS_UNREGISTERED, SCSCF)


def test_store_server_name_cannot_keep_an_scscf_that_is_not_assigned(hss, monkeypatch):
    monkeypatch.setitem(config["hss"], "cx", {"keep_scscf_on_deregistration": True})
    answer = sar(hss, 6)
    assert answer.experimental_result_code == 2004
    assert stored(hss) == (IMS_NOT_REGISTERED, None)


@pytest.mark.parametrize("before", [IMS_NOT_REGISTERED, IMS_REGISTERED, IMS_UNREGISTERED])
def test_no_assignment_downloads_without_changing_state(hss, before):
    put_in_state(hss, before)
    answer = sar(hss, 0)
    assert answer.result_code == 2001
    assert "<IMSSubscription>" in answer.user_data
    assert stored(hss)[0] == before


@pytest.mark.parametrize("assignment_type", [9, 10])
@pytest.mark.parametrize("before", [IMS_NOT_REGISTERED, IMS_REGISTERED, IMS_UNREGISTERED])
def test_authentication_failure_keeps_the_state(hss, assignment_type, before):
    """A failed or abandoned authentication of a second device must not
    de-register the subscriber's working registration."""
    put_in_state(hss, before)
    answer = sar(hss, assignment_type)
    assert answer.result_code == 2001
    assert answer.user_data is None
    assert stored(hss)[0] == before


@pytest.mark.parametrize("assignment_type", [12, 13, 14, 99])
def test_unsupported_assignment_type_is_rejected_without_side_effects(hss, assignment_type):
    put_in_state(hss, IMS_REGISTERED)
    answer = sar(hss, assignment_type)
    assert (answer.result_code, answer.experimental_result_code) == (None, 5007)
    assert stored(hss) == (IMS_REGISTERED, SCSCF)


@pytest.mark.parametrize("assignment_type", [1, 2, 3, 4, 5, 6, 8])
@pytest.mark.parametrize("before", [IMS_REGISTERED, IMS_UNREGISTERED])
def test_another_scscf_cannot_take_over_or_clear_the_assignment(hss, assignment_type, before):
    """TS 29.228 8.1.2. A late de-registration from a former S-CSCF must not
    remove the current one, and a second S-CSCF is told where the subscriber is
    served instead of silently splitting it over two."""
    put_in_state(hss, before)
    answer = sar(hss, assignment_type, scscf=OTHER_SCSCF)
    assert (answer.result_code, answer.experimental_result_code) == (None, 5005)
    assert answer.server_name == SCSCF
    assert answer.user_data is None
    assert stored(hss) == (before, SCSCF)


def test_no_assignment_from_another_scscf_is_unable_to_comply(hss):
    put_in_state(hss, IMS_REGISTERED)
    answer = sar(hss, 0, scscf=OTHER_SCSCF)
    assert (answer.result_code, answer.experimental_result_code) == (5012, None)
    assert answer.user_data is None


def test_unknown_user_is_a_cx_error_of_the_cx_application(hss):
    """It was answered as 5005 (which on Cx means "served by another S-CSCF")
    inside a packet of application 16777217 (Sh)."""
    answer = sar(hss, 3, impu="sip:+4999999@ims.example", with_user_name=False)
    assert (answer.result_code, answer.experimental_result_code) == (None, 5001)
    assert answer.application_id == 16777216


@pytest.mark.parametrize("assignment_type", [1, 2, 3])
def test_disabled_subscriber_gets_no_profile(hss, assignment_type):
    """UNREGISTERED_USER downloads the profile like a registration does, so a
    blocked subscriber must not get it that way either."""
    subscriber = hss.database.Get_Subscriber(imsi=IMSI)
    hss.database.UpdateObj(SUBSCRIBER, {"enabled": False}, subscriber["subscriber_id"])
    answer = sar(hss, assignment_type)
    assert answer.result_code == 5003
    assert answer.user_data is None
    assert stored(hss) == (IMS_NOT_REGISTERED, None)


def test_disabled_subscriber_can_still_be_deregistered(hss):
    put_in_state(hss, IMS_REGISTERED)
    subscriber = hss.database.Get_Subscriber(imsi=IMSI)
    hss.database.UpdateObj(SUBSCRIBER, {"enabled": False}, subscriber["subscriber_id"])
    assert sar(hss, 4).result_code == 2001
    assert stored(hss) == (IMS_NOT_REGISTERED, None)


# --- LIR -------------------------------------------------------------------------

@pytest.mark.parametrize("state", [IMS_REGISTERED, IMS_UNREGISTERED])
def test_lia_names_the_assigned_scscf(hss, state):
    """Unregistered included: the S-CSCF that already holds the profile gets the
    next call, with no second SAR."""
    put_in_state(hss, state)
    answer = lir(hss)
    assert (answer.result_code, answer.experimental_result_code) == (2001, None)
    assert answer.server_name == SCSCF


@pytest.mark.parametrize("template_id", [1, 2])
def test_lia_for_not_registered_with_unregistered_services(hss, template_id):
    """The headline bug: with the unregistered iFC in a shared iFC set (template
    1) the HSS found no "SessionCase 2" in the profile and answered 5003, so the
    I-CSCF rejected every call to a subscriber that was not registered. The
    answer is 2003 and carries no Server-Name (TS 29.228 6.1.4.1): none is
    assigned, the I-CSCF selects one."""
    hss.database.UpdateObj(IMS_SUBSCRIBER, {"ifc_template_id": template_id},
                           hss.database.Get_IMS_Subscriber(imsi=IMSI)["ims_subscriber_id"])
    answer = lir(hss)
    assert (answer.result_code, answer.experimental_result_code) == (None, 2003)
    assert answer.server_name is None
    assert stored(hss) == (IMS_NOT_REGISTERED, None), "a LIR assigns nothing"


def test_lia_for_not_registered_without_unregistered_services(hss):
    hss.database.UpdateObj(IMS_SUBSCRIBER, {"ifc_template_id": 3},
                           hss.database.Get_IMS_Subscriber(imsi=IMSI)["ims_subscriber_id"])
    answer = lir(hss)
    assert (answer.result_code, answer.experimental_result_code) == (None, 5003)
    assert answer.server_name is None


def test_lia_after_deregistration_is_the_not_registered_answer(hss):
    put_in_state(hss, IMS_REGISTERED)
    sar(hss, 5)
    assert lir(hss).experimental_result_code == 2003


def test_lia_for_unknown_user(hss):
    assert lir(hss, impu="sip:+4999999@ims.example").experimental_result_code == 5001


def test_lia_does_not_render_the_profile(hss, monkeypatch):
    """A LIR for a subscriber that is not registered used to render the whole
    Jinja profile only to search it; the answer is known per template."""
    def render(*args, **kwargs):
        raise AssertionError("profile rendered for a LIR")
    monkeypatch.setattr(Diameter, "build_cx_user_data", render)
    monkeypatch.setattr("jinja2.Template.render", render)
    assert lir(hss).experimental_result_code == 2003
    assert lir(hss).experimental_result_code == 2003


def test_which_shared_ifc_sets_count_is_configuration(hss, monkeypatch):
    """The HSS cannot look into a shared iFC set; the operator says which ones
    carry an unregistered-state iFC."""
    monkeypatch.setitem(config["hss"], "ifc_templates",
                        {"use_database": True, "cache_enabled": True, "unregistered_shared_ifc_sets": [7]})
    assert lir(hss).experimental_result_code == 5003


def test_template_scan():
    assert ifc_template_has_unregistered_services(SHARED_SET_TEMPLATE, [1])
    assert not ifc_template_has_unregistered_services(SHARED_SET_TEMPLATE, [])
    assert not ifc_template_has_unregistered_services(SHARED_SET_TEMPLATE, [13])
    assert ifc_template_has_unregistered_services(INLINE_TEMPLATE, [])
    assert not ifc_template_has_unregistered_services(NO_UNREG_TEMPLATE, [1])
    assert not ifc_template_has_unregistered_services("<x>{# <SessionCase>2</SessionCase> #}</x>", [1])


def test_template_change_is_picked_up_after_invalidation(hss):
    assert lir(hss).experimental_result_code == 2003
    hss.database.UpdateObj(IFC_TEMPLATE, {"template_content": NO_UNREG_TEMPLATE}, 1)
    hss.ifcTemplateCache.invalidate_db_template(1)
    assert lir(hss).experimental_result_code == 5003


# --- UAR -------------------------------------------------------------------------

def test_uar_in_unregistered_state_returns_the_assigned_scscf(hss):
    """The REGISTER has to reach the S-CSCF that is already assigned; any other
    one would be refused by the SAR (TS 29.228 6.1.1.1)."""
    put_in_state(hss, IMS_UNREGISTERED)
    answer = uar(hss)
    assert answer.experimental_result_code == 2002
    assert answer.server_name == SCSCF


def test_registration_after_unregistered_state(hss):
    put_in_state(hss, IMS_UNREGISTERED)
    assert sar(hss, 1).result_code == 2001
    assert stored(hss) == (IMS_REGISTERED, SCSCF)


def test_uar_for_deregistration_does_not_clear_the_assignment(hss):
    """A UAR is a query. Clearing here left the S-CSCF with bindings the HSS no
    longer knew about if the de-REGISTER then failed."""
    put_in_state(hss, IMS_REGISTERED)
    answer = uar(hss, authorization_type=1)
    assert answer.server_name == SCSCF
    assert stored(hss) == (IMS_REGISTERED, SCSCF)


# --- Sh --------------------------------------------------------------------------

@pytest.mark.parametrize("state,ims_user_state,legacy_value,voice_over_ps", [
    (IMS_NOT_REGISTERED, "NOT_REGISTERED", 0, "false"),
    (IMS_REGISTERED, "REGISTERED", 1, "true"),
    (IMS_UNREGISTERED, "REGISTERED_UNREG_SERVICES", 2, "false"),
])
def test_sh_reports_the_state(hss, state, ims_user_state, legacy_value, voice_over_ps):
    """An AS deciding where to deliver (T-ADS) must not be told that voice over
    PS is available for a subscriber that merely has an S-CSCF assigned for its
    unregistered services (TS 29.328 7.6.4, 7.6.18)."""
    put_in_state(hss, state)
    row = hss.database.Get_IMS_Subscriber(imsi=IMSI)
    row["serving_mme"] = "mme01"
    assert hss._sh_ims_user_state(row) == f"<IMSUserState>{ims_user_state}</IMSUserState>"
    assert f"<VoiceOverPS-SessionSupported>{voice_over_ps}</VoiceOverPS-SessionSupported>" in hss._sh_tads_information(row)
    assert ims_registration_state(row) == legacy_value


def test_served_subscriber_count_is_registered_subscribers_only(hss):
    put_in_state(hss, IMS_UNREGISTERED)
    assert hss.database.Count_Served_IMS_Subscribers() == 0
    assert hss.database.Count_Unregistered_IMS_Subscribers() == 1
    put_in_state(hss, IMS_REGISTERED)
    assert hss.database.Count_Served_IMS_Subscribers() == 1
    assert hss.database.Count_Unregistered_IMS_Subscribers() == 0


# --- rows written before the state column existed, or by an older node ------------

def test_row_without_state_reads_as_registered(hss):
    """No backfill at the upgrade: a million rows with an S-CSCF and no state are
    registered subscribers, and are treated as such everywhere."""
    put_in_state(hss, IMS_REGISTERED)
    with hss.database.engine.connect() as conn:
        conn.execute(sqlalchemy.text("UPDATE ims_subscriber SET scscf_state = NULL"))
        conn.commit()
    assert stored(hss) == (IMS_REGISTERED, SCSCF)
    assert hss.database.Count_Served_IMS_Subscribers() == 1
    assert lir(hss).server_name == SCSCF


def test_stale_state_without_scscf_reads_as_not_registered(hss):
    """A node of the previous version clears the S-CSCF and knows nothing of the
    state column, so the state it leaves behind must not count."""
    put_in_state(hss, IMS_UNREGISTERED)
    with hss.database.engine.connect() as conn:
        conn.execute(sqlalchemy.text("UPDATE ims_subscriber SET scscf = NULL"))
        conn.commit()
    assert stored(hss) == (IMS_NOT_REGISTERED, None)
    assert hss.database.Count_Unregistered_IMS_Subscribers() == 0


# --- geored ----------------------------------------------------------------------

@pytest.fixture
def geored(hss, monkeypatch):
    sent = []
    monkeypatch.setattr(hss.database, "georedEnabled", True)
    monkeypatch.setattr(hss.database, "handleGeored", lambda body, **kwargs: sent.append(body))
    return sent


def test_geored_carries_the_state(hss, geored):
    sar(hss, 3)
    assert geored[-1]["scscf"] == SCSCF and geored[-1]["scscf_state"] == IMS_UNREGISTERED
    sar(hss, 4)
    assert geored[-1]["scscf"] is None and geored[-1]["scscf_state"] == IMS_NOT_REGISTERED


def apply_geored(node, body):
    """What POST /geored does with an S-CSCF update."""
    return node.database.Update_Serving_CSCF(
        imsi=body["imsi"], serving_cscf=body["scscf"], scscf_realm=body.get("scscf_realm"),
        scscf_peer=body.get("scscf_peer"), scscf_timestamp=body.get("scscf_timestamp"),
        propagate=False, scscf_state=body.get("scscf_state"))


def test_geored_last_writer_wins(hss):
    """Two Diameter nodes handle the expiry SAR and the new registration of one
    subscriber and both tell the provisioning node. The messages arrive in no
    particular order; applying an old de-registration after a newer registration
    would leave the subscriber de-registered on every node that replicates from
    there."""
    registration = {"imsi": IMSI, "scscf": SCSCF, "scscf_realm": "ims.example", "scscf_peer": "scscf-0;hss02",
                    "scscf_timestamp": "2026-10-07T10:00:05Z", "scscf_state": IMS_REGISTERED}
    expiry = {"imsi": IMSI, "scscf": None, "scscf_timestamp": "2026-10-07T10:00:03Z", "scscf_state": IMS_NOT_REGISTERED}

    assert apply_geored(hss, registration) is True
    assert apply_geored(hss, expiry) is False, "older than what is stored"
    assert stored(hss) == (IMS_REGISTERED, SCSCF)

    # The other way round both apply, and the result is the same.
    with hss.database.engine.connect() as conn:
        conn.execute(sqlalchemy.text("UPDATE ims_subscriber SET scscf = NULL, scscf_state = NULL, scscf_timestamp = NULL"))
        conn.commit()
    assert apply_geored(hss, expiry) is True
    assert stored(hss) == (IMS_NOT_REGISTERED, None)
    assert apply_geored(hss, registration) is True
    assert stored(hss) == (IMS_REGISTERED, SCSCF)


def test_time_of_a_deregistration_is_kept(hss):
    """Clearing the S-CSCF used to clear the timestamp too, which left nothing
    to order a late registration update against."""
    expiry = {"imsi": IMSI, "scscf": None, "scscf_timestamp": "2026-10-07T10:00:05Z", "scscf_state": IMS_NOT_REGISTERED}
    late_registration = {"imsi": IMSI, "scscf": SCSCF, "scscf_timestamp": "2026-10-07T10:00:01Z", "scscf_state": IMS_REGISTERED}
    assert apply_geored(hss, expiry) is True
    assert apply_geored(hss, late_registration) is False
    assert stored(hss) == (IMS_NOT_REGISTERED, None)


def test_geored_update_from_a_node_without_the_state_column(hss):
    """During the rollout a node of the previous version sends no scscf_state."""
    assert apply_geored(hss, {"imsi": IMSI, "scscf": SCSCF, "scscf_timestamp": "2026-10-07T10:00:05Z"}) is True
    assert stored(hss) == (IMS_REGISTERED, SCSCF)
    assert apply_geored(hss, {"imsi": IMSI, "scscf": None, "scscf_timestamp": "2026-10-07T10:00:06Z"}) is True
    assert stored(hss) == (IMS_NOT_REGISTERED, None)


# --- schema ----------------------------------------------------------------------

def test_upgrade_adds_the_state_column_without_touching_rows(tmp_path):
    """Schema version 6. The upgrade must not rewrite ims_subscriber: on a
    replicated setup that would be a million-row transaction in the binlog."""
    path = tmp_path / "v5.db"
    conn = sqlite3.connect(path)
    with open(os.path.join(top_dir, "tests/db_schema/20240125_release_1.0.1.sql")) as f:
        conn.executescript(f.read())
    conn.execute("INSERT INTO ims_subscriber (ims_subscriber_id, msisdn, imsi, scscf) VALUES (1, '100', '001010000000009', ?)", (SCSCF,))
    conn.commit()
    conn.close()

    engine = sqlalchemy.create_engine(f"sqlite:///{path}")
    schema = DatabaseSchema(LogTool(config), Base, engine, main_service=True)

    assert schema.get_version() == DatabaseSchema.latest == 6
    assert schema.column_exists("ims_subscriber", "scscf_state")
    with engine.connect() as conn:
        row = conn.execute(sqlalchemy.text("SELECT scscf, scscf_state FROM ims_subscriber")).fetchone()
    assert tuple(row) == (SCSCF, None)
    assert ims_registration_state({"scscf": row[0], "scscf_state": row[1]}) == IMS_REGISTERED

    # A second start, or the replicated statement on a replica that upgraded
    # itself, must not fail on the existing column.
    schema.add_column("ims_subscriber", "scscf_state", "SMALLINT")
    engine.dispose()


def test_upgrade_indexes_the_pcscf_session_lookup(tmp_path):
    """Every Rx STR finds the IMS subscriber by pcscf_active_session. Without an
    index that is a full table scan per de-registration, which brought a
    100k-subscriber de-registration run down. The index may exist already (it
    was created by hand on a live master), and the upgrade must not fail on it."""
    def indexes(engine):
        return [index["name"] for index in sqlalchemy.inspect(engine).get_indexes("ims_subscriber")]

    for created_by_hand in (False, True):
        engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path}/v5-{created_by_hand}.db")
        Base.metadata.create_all(engine)
        with engine.connect() as conn:
            conn.execute(sqlalchemy.text("DROP INDEX ix_ims_subscriber_pcscf_active_session"))
            conn.execute(sqlalchemy.text("ALTER TABLE ims_subscriber DROP COLUMN scscf_state"))
            if created_by_hand:
                conn.execute(sqlalchemy.text(
                    "CREATE INDEX ix_ims_subscriber_pcscf_active_session ON ims_subscriber (pcscf_active_session)"))
            conn.execute(sqlalchemy.text("INSERT INTO database_schema_version (upgrade_id, comment) VALUES (5, 'test')"))
            conn.commit()

        schema = DatabaseSchema(LogTool(config), Base, engine, main_service=True)

        assert schema.get_version() == 6
        assert "ix_ims_subscriber_pcscf_active_session" in indexes(engine)
        assert schema.column_exists("ims_subscriber", "scscf_state")
        engine.dispose()
