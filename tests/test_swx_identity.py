# Copyright 2026 Carsten Bock <carsten@bock.info>
# SPDX-License-Identifier: AGPL-3.0-or-later
from unittest import TestCase

from diameter import Diameter

REALM = "@nai.epc.mnc024.mcc262.3gppnetwork.org"
IMSI = "262240000099001"


class SwxIdentityTest(TestCase):
    """SWx carries the UE's EAP identity, not a bare IMSI (TS 23.003 section
    19.3.2). The subscriber lookup needs the IMSI: a leftover method digit
    turns every attach of that EAP method into "user unknown"."""

    def test_eap_aka_identity(self):
        self.assertEqual(IMSI, Diameter.imsi_from_eap_nai("0" + IMSI + REALM))

    def test_eap_aka_prime_identity(self):
        # The case that was rejected with 5001: TS 33.402 makes EAP-AKA' the
        # method for non-3GPP access, and its identities start with "6".
        self.assertEqual(IMSI, Diameter.imsi_from_eap_nai("6" + IMSI + REALM))

    def test_bare_imsi_of_mcc_6xx_is_not_shortened(self):
        # 15 digits starting with 6 are a complete IMSI (e.g. MCC 602), not a
        # prefixed one: removing the 6 would look up somebody else's number.
        self.assertEqual("602030000000001",
                         Diameter.imsi_from_eap_nai("602030000000001" + REALM))

    def test_eap_aka_identity_of_test_plmn(self):
        # IMSIs of MCC 001 start with 0 themselves; only the first digit goes.
        self.assertEqual("001010000000001",
                         Diameter.imsi_from_eap_nai("0001010000000001" + REALM))

    def test_username_without_realm_is_the_imsi(self):
        self.assertEqual(IMSI, Diameter.imsi_from_eap_nai(IMSI))
