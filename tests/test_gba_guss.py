# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GUSS in the Zh answer (TS 29.109, KI-196).

Why it matters: the Ut authentication proxy may only assert a public identity
the user owns (TS 24.109 clause 7.3). It learns them from the GUSS the HSS
returns over Zh. A barred identity must not be in it (it may not be used),
the identities must be those of the user's own home domain (ADR-0045), and
the document must follow the TS 29.109 schema the BSF parses.
"""
from pathlib import Path
from xml.etree import ElementTree as ET

import jinja2

from gba_guss import GUSS_NAMESPACE, build_guss, public_identities

NS = {"g": GUSS_NAMESPACE}
TEMPLATE = Path(__file__).resolve().parents[1] / "default_ifc.xml"


def render_default(**kw):
    return jinja2.Template(TEMPLATE.read_text()).render(iFC_vars=kw)


def test_identities_of_the_shipped_profile_in_the_users_home_domain():
    xml = render_default(imsi="262259999000001", msisdn="4915799990251",
                         home_domain="ims.mnc025.mcc262.3gppnetwork.org", mcc="262", mnc="024")
    # The IMSI-derived identity is barred in the profile (registration only,
    # TS 23.003 13.4B), so the proxy may never assert it.
    assert public_identities(xml) == [
        "sip:+4915799990251@ims.mnc025.mcc262.3gppnetwork.org",
        "tel:+4915799990251",
    ]


def test_barred_identities_are_left_out_and_duplicates_once():
    xml = ("<IMSSubscription><ServiceProfile>"
           "<PublicIdentity><BarringIndication>1</BarringIndication><Identity>sip:a@x</Identity></PublicIdentity>"
           "<PublicIdentity><Identity>sip:b@x</Identity></PublicIdentity>"
           "<PublicIdentity><BarringIndication>true</BarringIndication><Identity>sip:c@x</Identity></PublicIdentity>"
           "</ServiceProfile><ServiceProfile>"
           "<PublicIdentity><Identity>sip:b@x</Identity></PublicIdentity>"
           "<PublicIdentity><BarringIndication>0</BarringIndication><Identity>tel:+1</Identity></PublicIdentity>"
           "</ServiceProfile></IMSSubscription>")
    assert public_identities(xml) == ["sip:b@x", "tel:+1"]


def test_guss_follows_the_ts_29109_schema():
    root = ET.fromstring(build_guss(["sip:+49@ims.example", "tel:+49", "sip:a&b@x"]))
    assert root.tag == "{%s}guss" % GUSS_NAMESPACE
    assert root.findtext("g:bsfInfo/g:uiccType", namespaces=NS) == "GBA"
    uss = root.findall("g:ussList/g:uss", NS)
    assert len(uss) == 1 and uss[0].get("type") == "2" and uss[0].get("id") == "1"
    assert [u.text for u in uss[0].findall("g:uids/g:uid", NS)] == ["sip:+49@ims.example", "tel:+49", "sip:a&b@x"]
    assert uss[0].find("g:flags", NS) is not None


def test_a_template_comment_with_double_hyphen_does_not_break_the_guss():
    # Profiles stored before 2026-11-17 carry "--" inside a comment, which
    # strict XML rejects; the S-CSCF accepts them, so the GUSS must too.
    xml = ("<IMSSubscription><!-- SPEC-DEVIATION: x -- y --><ServiceProfile>"
           "<PublicIdentity><Identity>sip:b@x</Identity></PublicIdentity>"
           "</ServiceProfile></IMSSubscription>")
    assert public_identities(xml) == ["sip:b@x"]
