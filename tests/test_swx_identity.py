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


class EapAkaPrimeKeysTest(TestCase):
    """For EAP-AKA' the SWx answer carries CK' and IK', not CK and IK (TS 33.402
    section 6.2). The UE derives the same pair from its USIM; if the HSS hands
    out anything else, AT_MAC of the challenge fails on the UE and the attach
    is over. Vectors: RFC 5448 Appendix C."""

    CK = bytes.fromhex("5349fbe098649f948f5d2e973a81c00f")
    IK = bytes.fromhex("9744871ad32bf9bbd1dd5ce54e3e2e5a")
    AUTN = bytes.fromhex("bb52e91c747ac3ab2a5c23d15ee351d5")

    def test_rfc5448_case_1(self):
        ck_prime, ik_prime = Diameter.eap_aka_prime_ck_ik(self.CK, self.IK, b"WLAN", self.AUTN)
        self.assertEqual("0093962d0dd84aa5684b045c9edffa04", ck_prime.hex())
        self.assertEqual("ccfc230ca74fcc96c0a5d61164f5a76c", ik_prime.hex())

    def test_keys_depend_on_the_access_network(self):
        # Case 2: same vector, other network name. A vector issued for one
        # access network must be useless in another.
        ck_prime, ik_prime = Diameter.eap_aka_prime_ck_ik(self.CK, self.IK, b"HRPD", self.AUTN)
        self.assertEqual("3820f0277fa5f77732b1fb1d90c1a0da", ck_prime.hex())
        self.assertEqual("db94a0ab557ef6c9ab48619ca05b9a9f", ik_prime.hex())


class ForeignPlmnTest(TestCase):
    """roaming_enabled and the roaming rules only apply to a subscriber the
    HSS recognises as roaming. A PLMN is MCC plus MNC: a visited network that
    shares only one of them with home is still another operator's network, and
    treating it as home let barred subscribers attach there."""

    class _Home:
        MCC = "262"
        MNC = "24"
        is_foreign_plmn = Diameter.is_foreign_plmn

    def test_home_plmn_is_not_roaming(self):
        self.assertFalse(self._Home().is_foreign_plmn("262", "24"))

    def test_other_country_is_roaming(self):
        self.assertTrue(self._Home().is_foreign_plmn("001", "01"))

    def test_other_operator_in_the_home_country_is_roaming(self):
        # The case that was missed: same MCC, different MNC.
        self.assertTrue(self._Home().is_foreign_plmn("262", "22"))

    def test_same_mnc_in_another_country_is_roaming(self):
        self.assertTrue(self._Home().is_foreign_plmn("232", "24"))

    def test_mnc_padding_does_not_make_home_foreign(self):
        home = self._Home()
        home.MNC = "024"
        self.assertFalse(home.is_foreign_plmn("262", "24"))
