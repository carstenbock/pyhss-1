# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ENUM entries of subscribers of an additional home PLMN (ADR-0045, KI-305).

An ENUM entry translates a called number into the SIP URI the call is routed
to. That URI has to be the public identity the subscriber registered, and a
subscriber of 262/25 on a platform whose own PLMN is 262/24 registers as
sip:+<MSISDN>@ims.mnc025.mcc262.3gppnetwork.org. An entry that names the
platform's domain for every number points calls to such a subscriber at an
identity nobody registered."""
from enum_management import ENUMClient

HOME_24 = "ims.mnc024.mcc262.3gppnetwork.org"
HOME_25 = "ims.mnc025.mcc262.3gppnetwork.org"
IMSI_24 = "262240000000001"
IMSI_25 = "262250000000001"


def client(sip_domain=HOME_24, additional="262-25"):
    enum = ENUMClient({
        "hss": {"MCC": "262", "MNC": "24", "AdditionalPLMNs": additional},
        "enum": {"enabled": True, "endpoints": [
            {"name": "internal", "url": "http://pdns", "api_key": "k",
             "sip_domain": sip_domain, "domains": [HOME_24]}]},
    })
    enum.sent = []
    enum._make_pdns_request = lambda endpoint, zone, rrsets: (enum.sent.extend(rrsets), (True, None))[1]
    return enum


def uris(enum):
    return [rec["content"].split("!")[2] for rrset in enum.sent for rec in rrset["records"]]


def test_a_subscriber_of_an_additional_plmn_gets_its_own_domain_in_the_enum_uri():
    enum = client()
    enum.create_enum_entries("+4915799990251", None, imsi=IMSI_25)
    assert uris(enum) == [f"sip:+4915799990251@{HOME_25}"]


def test_a_subscriber_of_the_platform_plmn_keeps_the_platform_domain():
    enum = client()
    enum.create_enum_entries("+4915799990241", "4915799990242", imsi=IMSI_24)
    assert uris(enum) == [f"sip:+4915799990241@{HOME_24}", f"sip:+4915799990242@{HOME_24}"]


def test_the_entry_stays_in_the_zone_the_cscf_queries():
    """Only the URI follows the subscriber; the S-CSCF looks every number up in one zone."""
    enum = client()
    enum.create_enum_entries("+4915799990251", None, imsi=IMSI_25)
    assert enum.sent[0]["name"] == "1.5.2.0.9.9.9.9.7.5.1.9.4." + HOME_24 + "."


def test_an_endpoint_with_its_own_sip_domain_keeps_it_for_every_subscriber():
    """An operator who points an ENUM server at another name (interconnect) chose that name."""
    enum = client(sip_domain="sbc.example.net")
    enum.create_enum_entries("+4915799990251", None, imsi=IMSI_25)
    assert uris(enum) == ["sip:+4915799990251@sbc.example.net"]


def test_a_number_added_later_gets_the_subscribers_domain_too():
    enum = client()
    enum.update_enum_entries("+4915799990251", None, "+4915799990251", "4915799990252", imsi=IMSI_25)
    assert uris(enum) == [f"sip:+4915799990252@{HOME_25}"]


def test_reconciliation_repairs_entries_written_with_the_platform_domain():
    """The repair path for zones written before the fix: reconcile rewrites every entry."""
    class Db:
        def getAllPaginated(self, table, page, page_size, **kwargs):
            return [{"ims_subscriber_id": 1, "imsi": IMSI_25, "msisdn": "4915799990251", "msisdn_list": None},
                    {"ims_subscriber_id": 2, "imsi": IMSI_24, "msisdn": "4915799990241", "msisdn_list": None}
                    ] if page == 0 else []

    enum = client()
    enum._find_orphans = lambda provisioned, results: []
    enum.reconcile_all(Db())
    assert uris(enum) == [f"sip:+4915799990251@{HOME_25}", f"sip:+4915799990241@{HOME_24}"]
