# GBA User Security Settings (GUSS) for the Zh interface.
# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""GUSS for a Zh Multimedia-Auth-Answer (TS 29.109 clause 6.1.1, Annex A).

The BSF keeps the GUSS with the bootstrapping session and gives it to the
NAF. An authentication proxy (the Ut NAF, TS 33.222) needs the user's public
identities from it to assert one of them to the application server
(X-3GPP-Asserted-Identity, TS 24.109 clause 7.3); without them it only knows
the private identity, which no XCAP document is addressed to.

The identities are the non-barred public identities of the user's Cx profile,
the same set the S-CSCF registers, so Ut and IMS agree on who the user is
(including the home domain of an additional home PLMN, ADR-0045).
"""
import re
from typing import Iterable, List
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

GUSS_NAMESPACE = "urn:3gpp:gba:GBAGUSSSchema-R9:2010-02"
# GAA service type code 2: Authentication Proxy (TS 29.109 Annex B).
USS_TYPE_AUTHENTICATION_PROXY = 2


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def public_identities(user_data_xml: str) -> List[str]:
    """Non-barred public identities of a Cx-User-Data document, in order."""
    # Comments are dropped first: templates in the field carry comments with
    # "--", which XML does not allow there but the S-CSCF's parser accepts.
    text = re.sub(r"<!--.*?-->", "", user_data_xml, flags=re.S)
    root = ET.fromstring(text.encode("utf-8"))
    out: List[str] = []
    for pi in root.iter():
        if _local(pi.tag) != "PublicIdentity":
            continue
        identity, barred = "", False
        for child in pi:
            name = _local(child.tag)
            if name == "Identity":
                identity = (child.text or "").strip()
            elif name == "BarringIndication":
                barred = (child.text or "").strip().lower() in ("1", "true")
        if identity and not barred and identity not in out:
            out.append(identity)
    return out


def build_guss(uids: Iterable[str], uicc_type: str = "GBA") -> str:
    """GUSS XML with one Authentication Proxy USS listing `uids`."""
    uid_xml = "".join(f"<uid>{escape(u)}</uid>" for u in uids)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<guss xmlns="{GUSS_NAMESPACE}">'
        f"<bsfInfo><uiccType>{escape(uicc_type)}</uiccType></bsfInfo>"
        "<ussList>"
        f'<uss id="1" type="{USS_TYPE_AUTHENTICATION_PROXY}">'
        f"<uids>{uid_xml}</uids><flags/>"
        "</uss>"
        "</ussList>"
        "</guss>"
    )
