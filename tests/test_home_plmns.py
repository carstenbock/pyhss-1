# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Several home PLMNs on one HSS (ADR-0045).

A UE without ISIM builds its home domain and its identities from its IMSI
(TS 23.003 section 13.2). A subscriber of 262/25 served by a platform whose
own PLMN is 262/24 registers as sip:<IMSI>@ims.mnc025.mcc262.3gppnetwork.org,
so the profile the HSS downloads must list that identity, or the S-CSCF finds
no registered public identity in it. The application servers are nodes of
the platform and keep the platform's names."""
from pathlib import Path

import pytest

from database import AUC, Database, IFC_TEMPLATE, IMS_SUBSCRIBER, SUBSCRIBER
from diameter import Diameter
from logtool import LogTool
from network_control import home_plmn_for_imsi, ims_domain_for_imsi, parse_home_plmns
from pyhss_config import config
from template_cache import IfcTemplateCache

top_dir = Path(Path(__file__) / "../..").resolve()

PLATFORM = ("262", "24")
HOME_24 = "ims.mnc024.mcc262.3gppnetwork.org"
HOME_25 = "ims.mnc025.mcc262.3gppnetwork.org"
IMSI_24 = "262240000000001"
IMSI_25 = "262250000000001"


# --- choosing the home PLMN of an IMSI -------------------------------------------

def test_the_platform_plmn_comes_first_and_malformed_entries_are_ignored():
    plmns = parse_home_plmns("262", "24", "262-25, 26-25, 262-5, x, 262-24, ,310-410")
    assert plmns == [("262", "24"), ("262", "25"), ("310", "410")]


def test_without_additional_plmns_every_subscriber_gets_the_platform_domain():
    """What every deployment did before ADR-0045 must stay unchanged."""
    plmns = parse_home_plmns(*PLATFORM)
    assert ims_domain_for_imsi(IMSI_24, plmns) == HOME_24
    assert ims_domain_for_imsi(IMSI_25, plmns) == HOME_24


def test_a_subscriber_of_an_additional_plmn_gets_its_own_domain():
    plmns = parse_home_plmns("262", "24", "262-25")
    assert ims_domain_for_imsi(IMSI_24, plmns) == HOME_24
    assert ims_domain_for_imsi(IMSI_25, plmns) == HOME_25


def test_the_longest_prefix_wins_because_the_imsi_does_not_show_the_mnc_length():
    """310/41 and 310/410 are different networks; 310410... belongs to the second."""
    plmns = parse_home_plmns("310", "41", "310-410")
    assert home_plmn_for_imsi("310410123456789", plmns) == ("310", "410")
    assert home_plmn_for_imsi("310419123456789", plmns) == ("310", "41")


@pytest.mark.parametrize("imsi", [None, "", "001010000000001"])
def test_an_unknown_or_missing_imsi_falls_back_to_the_platform_plmn(imsi):
    plmns = parse_home_plmns("262", "24", "262-25")
    assert home_plmn_for_imsi(imsi, plmns) == PLATFORM


# --- roaming --------------------------------------------------------------------

def node_with_plmns(additional=""):
    node = Diameter.__new__(Diameter)
    node.MCC, node.MNC = PLATFORM
    node.homePlmns = parse_home_plmns(*PLATFORM, additional)
    return node


def test_a_sister_plmn_is_home_not_roaming():
    """A 262/25 subscriber on a 262/24 radio network must not be checked
    against the roaming rules (and the reverse)."""
    node = node_with_plmns("262-25")
    assert not node.is_foreign_plmn("262", "25")
    assert not node.is_foreign_plmn("262", "025")
    assert not node.is_foreign_plmn("262", "24")
    assert node.is_foreign_plmn("262", "01")
    assert node.is_foreign_plmn("232", "25")


def test_without_additional_plmns_a_sister_network_is_roaming():
    assert node_with_plmns().is_foreign_plmn("262", "25")


# --- the downloaded profile (Cx-User-Data) and Sh ---------------------------------

class NullMessaging:
    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


@pytest.fixture
def hss(tmp_path, monkeypatch):
    """An HSS of platform PLMN 262/24 that also serves 262/25, with the shipped
    default_ifc.xml as iFC template 1 and one subscriber of each PLMN."""
    monkeypatch.setitem(config["database"], "database", str(tmp_path / "plmn.db"))
    db = Database(LogTool(config), main_service=True)
    template = (top_dir / "default_ifc.xml").read_text()
    db.CreateObj(IFC_TEMPLATE, {"ifc_template_id": 1, "name": "default", "template_content": template})
    for auc_id, imsi, msisdn in ((1, IMSI_24, "4915100000024"), (2, IMSI_25, "4915100000025")):
        db.CreateObj(AUC, {"auc_id": auc_id, "ki": "3c6e0b8a9c15224a8228b9a98ca1531d",
                           "opc": "762a2206fe0b4151ace403c86a11e479", "amf": "8000", "sqn": 0, "imsi": imsi})
        db.CreateObj(SUBSCRIBER, {"auc_id": auc_id, "default_apn": 1, "apn_list": "1", "imsi": imsi, "msisdn": msisdn})
        db.CreateObj(IMS_SUBSCRIBER, {"imsi": imsi, "msisdn": msisdn, "ifc_template_id": 1})

    node = node_with_plmns("262-25")
    node.logTool = LogTool(config)
    node.redisMessaging = NullMessaging()
    node.database = db
    node.ifcTemplateCache = IfcTemplateCache()
    node.ifcCacheEnabled = False
    node.ifcUseDatabase = True
    node.ifcDefaultTemplatePath = "default_ifc.xml"
    yield node
    db.engine.dispose()


def test_the_profile_lists_the_identities_of_the_subscribers_own_plmn(hss):
    user_data = hss.build_cx_user_data(hss.database.Get_IMS_Subscriber(imsi=IMSI_25))
    assert f"<PrivateID>{IMSI_25}@{HOME_25}</PrivateID>" in user_data
    assert f"sip:+4915100000025@{HOME_25}" in user_data
    assert f"sip:{IMSI_25}@{HOME_25}" in user_data
    assert "mnc024" not in user_data.split("<ServiceProfile>")[0]


def test_application_servers_keep_the_platform_names(hss):
    """AS URIs are node names (ADR-0045); a 262/25 subscriber's services run
    on the same application servers."""
    user_data = hss.build_cx_user_data(hss.database.Get_IMS_Subscriber(imsi=IMSI_25))
    assert "smsc.ims.mnc024.mcc262.3gppnetwork.org" in user_data
    assert "smsc.ims.mnc025" not in user_data


def test_a_platform_subscriber_renders_as_before(hss):
    user_data = hss.build_cx_user_data(hss.database.Get_IMS_Subscriber(imsi=IMSI_24))
    assert f"<PrivateID>{IMSI_24}@{HOME_24}</PrivateID>" in user_data
    assert "mnc025" not in user_data


def test_a_template_written_for_one_plmn_still_renders_the_platform_plmn(hss):
    """Templates in existing databases build identities from mcc/mnc; those keep
    meaning the platform PLMN, so nothing changes until the template is updated."""
    old_style = "<PrivateID>{{ iFC_vars['imsi'] }}@ims.mnc{{ iFC_vars['mnc'] }}.mcc{{ iFC_vars['mcc'] }}.3gppnetwork.org</PrivateID>"
    hss.database.UpdateObj(IFC_TEMPLATE, {"template_content": old_style}, 1)
    user_data = hss.build_cx_user_data(hss.database.Get_IMS_Subscriber(imsi=IMSI_24))
    assert user_data == f"<PrivateID>{IMSI_24}@{HOME_24}</PrivateID>"


def test_sh_public_identities_use_the_subscribers_own_domain(hss):
    xml = hss._sh_ims_public_identity({"imsi": IMSI_25, "msisdn": "4915100000025"})
    assert f"sip:4915100000025@{HOME_25}" in xml
    assert f"sip:{IMSI_25}@{HOME_25}" in xml
    assert "mnc024" not in xml
