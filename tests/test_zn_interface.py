# Copyright 2025 sysmocom - s.f.m.c. GmbH <info@sysmocom.de>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Unit tests for Zn-Interface (GBA - Generic Bootstrapping Architecture)
Tests for lib/zn_interface.py according to 3GPP TS 29.109 and 3GPP TS 33.220
"""

import base64
import hashlib
import os
import unittest
from unittest.mock import MagicMock, patch


class TestZnInterface(unittest.TestCase):
    """Test cases for ZnInterface class"""

    def setUp(self):
        """Set up test fixtures with mocked dependencies"""
        # Mock diameter instance
        self.mock_diameter = MagicMock()
        self.mock_diameter.logTool = MagicMock()
        self.mock_diameter.redisMessaging = MagicMock()
        
        # Mock database instance
        self.mock_database = MagicMock()
        
        # Test configuration
        self.test_config = {
            'hss': {
                'Zn_enabled': True,
                'bsf': {
                    'bsf_hostname': 'bsf.epc.mnc001.mcc001.3gppnetwork.org',
                    'gaa_key_lifetime': 3600,
                    'naf_groups': [
                        {
                            'name': 'default_naf_group',
                            'naf_hostnames': [
                                'naf1.epc.mnc001.mcc001.3gppnetwork.org',
                                'naf2.epc.mnc001.mcc001.3gppnetwork.org'
                            ]
                        },
                        {
                            'name': 'secondary_naf_group',
                            'naf_hostnames': [
                                'naf3.example.com'
                            ]
                        }
                    ]
                }
            }
        }
        
        # Import and instantiate ZnInterface
        from zn_interface import ZnInterface
        self.zn_interface = ZnInterface(
            self.mock_diameter, 
            self.mock_database, 
            self.test_config
        )

    def test_init_zn_enabled(self):
        """Test ZnInterface initialization with Zn enabled"""
        self.assertTrue(self.zn_interface.zn_enabled)
        self.assertEqual(self.zn_interface.gaa_key_lifetime, 3600)
        self.assertEqual(
            self.zn_interface.bsf_config['bsf_hostname'],
            'bsf.epc.mnc001.mcc001.3gppnetwork.org'
        )

    def test_init_zn_disabled(self):
        """Test ZnInterface initialization with Zn disabled"""
        config_disabled = {'hss': {'Zn_enabled': False}}
        from zn_interface import ZnInterface
        zn = ZnInterface(self.mock_diameter, self.mock_database, config_disabled)
        self.assertFalse(zn.zn_enabled)

    # =========================================================================
    # B-TID Generation Tests
    # =========================================================================

    def test_generate_btid_format(self):
        """Test B-TID generation format: base64(RAND)@bsf_hostname"""
        # GIVEN
        rand = os.urandom(16)
        
        # WHEN
        btid = self.zn_interface.generate_btid(rand)
        
        # THEN
        self.assertIn('@', btid)
        parts = btid.split('@')
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1], 'bsf.epc.mnc001.mcc001.3gppnetwork.org')
        
        # Verify the first part is valid base64
        decoded = base64.b64decode(parts[0])
        self.assertEqual(decoded, rand)

    def test_generate_btid_with_custom_hostname(self):
        """Test B-TID generation with custom BSF hostname"""
        # GIVEN
        rand = os.urandom(16)
        custom_hostname = 'custom.bsf.example.org'
        
        # WHEN
        btid = self.zn_interface.generate_btid(rand, bsf_hostname=custom_hostname)
        
        # THEN
        self.assertTrue(btid.endswith(f'@{custom_hostname}'))

    def test_generate_btid_known_value(self):
        """Test B-TID generation with known input for reproducibility"""
        # GIVEN - known 16 byte value
        rand = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f'
        expected_rand_b64 = base64.b64encode(rand).decode('ascii')
        
        # WHEN
        btid = self.zn_interface.generate_btid(rand)
        
        # THEN
        expected_btid = f"{expected_rand_b64}@bsf.epc.mnc001.mcc001.3gppnetwork.org"
        self.assertEqual(btid, expected_btid)

    def test_generate_btid_logs_debug(self):
        """Test that B-TID generation logs at debug level"""
        # GIVEN
        rand = os.urandom(16)
        
        # WHEN
        self.zn_interface.generate_btid(rand)
        
        # THEN
        self.mock_diameter.logTool.log.assert_called()
        call_kwargs = self.mock_diameter.logTool.log.call_args[1]
        self.assertEqual(call_kwargs['level'], 'debug')
        self.assertIn('B-TID', call_kwargs['message'])

    # =========================================================================
    # Ks_NAF Derivation Tests
    # =========================================================================

    def test_derive_ks_naf_output_length(self):
        """Test Ks_NAF derivation produces 32 byte output (256 bits)"""
        # GIVEN
        ck = os.urandom(16)
        ik = os.urandom(16)
        naf_id = 'naf1.example.com'
        impi = '001010123456789@ims.example.com'
        
        # WHEN
        ks_naf = self.zn_interface.derive_ks_naf(ck, ik, naf_id, impi)
        
        # THEN
        self.assertEqual(len(ks_naf), 32)
        self.assertIsInstance(ks_naf, bytes)

    def test_derive_ks_naf_deterministic(self):
        """Test Ks_NAF derivation is deterministic for same inputs"""
        # GIVEN
        ck = b'\x00' * 16
        ik = b'\x01' * 16
        naf_id = 'naf1.example.com'
        impi = 'user@example.com'
        
        # WHEN
        ks_naf_1 = self.zn_interface.derive_ks_naf(ck, ik, naf_id, impi)
        ks_naf_2 = self.zn_interface.derive_ks_naf(ck, ik, naf_id, impi)
        
        # THEN
        self.assertEqual(ks_naf_1, ks_naf_2)

    def test_derive_ks_naf_different_for_different_naf(self):
        """Test Ks_NAF is different for different NAFs (key separation)"""
        # GIVEN
        ck = os.urandom(16)
        ik = os.urandom(16)
        impi = 'user@example.com'
        
        # WHEN
        ks_naf_1 = self.zn_interface.derive_ks_naf(ck, ik, 'naf1.example.com', impi)
        ks_naf_2 = self.zn_interface.derive_ks_naf(ck, ik, 'naf2.example.com', impi)
        
        # THEN
        self.assertNotEqual(ks_naf_1, ks_naf_2)

    def test_derive_ks_naf_different_for_different_user(self):
        """Test Ks_NAF is different for different users"""
        # GIVEN
        ck = os.urandom(16)
        ik = os.urandom(16)
        naf_id = 'naf1.example.com'
        
        # WHEN
        ks_naf_1 = self.zn_interface.derive_ks_naf(ck, ik, naf_id, 'user1@example.com')
        ks_naf_2 = self.zn_interface.derive_ks_naf(ck, ik, naf_id, 'user2@example.com')
        
        # THEN
        self.assertNotEqual(ks_naf_1, ks_naf_2)

    def test_derive_ks_naf_known_value(self):
        """Test Ks_NAF derivation with known values for verification"""
        # GIVEN - known inputs
        ck = b'\x00' * 16
        ik = b'\x01' * 16
        naf_id = 'naf.example.com'
        impi = 'user@example.com'
        
        # Calculate expected value manually
        ks = ck + ik
        kdf_input = ks + b'gba-me' + naf_id.encode('utf-8') + impi.encode('utf-8')
        expected_ks_naf = hashlib.sha256(kdf_input).digest()
        
        # WHEN
        ks_naf = self.zn_interface.derive_ks_naf(ck, ik, naf_id, impi)
        
        # THEN
        self.assertEqual(ks_naf, expected_ks_naf)

    # =========================================================================
    # Ks_ext_NAF Derivation Tests (2G/3G)
    # =========================================================================

    def test_derive_ks_ext_naf_output_length(self):
        """Test Ks_ext_NAF derivation produces 32 byte output"""
        # GIVEN
        kc = os.urandom(8)  # 2G/3G Kc is 8 bytes
        naf_id = 'naf1.example.com'
        impi = 'user@example.com'
        
        # WHEN
        ks_ext_naf = self.zn_interface.derive_ks_ext_naf(kc, naf_id, impi)
        
        # THEN
        self.assertEqual(len(ks_ext_naf), 32)
        self.assertIsInstance(ks_ext_naf, bytes)

    def test_derive_ks_ext_naf_deterministic(self):
        """Test Ks_ext_NAF derivation is deterministic"""
        # GIVEN
        kc = b'\x00' * 8
        naf_id = 'naf1.example.com'
        impi = 'user@example.com'
        
        # WHEN
        ks_ext_naf_1 = self.zn_interface.derive_ks_ext_naf(kc, naf_id, impi)
        ks_ext_naf_2 = self.zn_interface.derive_ks_ext_naf(kc, naf_id, impi)
        
        # THEN
        self.assertEqual(ks_ext_naf_1, ks_ext_naf_2)

    def test_derive_ks_ext_naf_different_for_different_naf(self):
        """Test Ks_ext_NAF is different for different NAFs"""
        # GIVEN
        kc = os.urandom(8)
        impi = 'user@example.com'
        
        # WHEN
        ks_ext_naf_1 = self.zn_interface.derive_ks_ext_naf(kc, 'naf1.example.com', impi)
        ks_ext_naf_2 = self.zn_interface.derive_ks_ext_naf(kc, 'naf2.example.com', impi)
        
        # THEN
        self.assertNotEqual(ks_ext_naf_1, ks_ext_naf_2)

    # =========================================================================
    # NAF Authorization Tests
    # =========================================================================

    def test_validate_naf_authorization_authorized(self):
        """Test NAF authorization returns True for authorized NAF"""
        # GIVEN - NAF in default_naf_group
        naf_hostname = 'naf1.epc.mnc001.mcc001.3gppnetwork.org'
        
        # WHEN
        result = self.zn_interface.validate_naf_authorization(naf_hostname)
        
        # THEN
        self.assertTrue(result)

    def test_validate_naf_authorization_second_group(self):
        """Test NAF authorization for NAF in secondary group"""
        # GIVEN - NAF in secondary_naf_group
        naf_hostname = 'naf3.example.com'
        
        # WHEN
        result = self.zn_interface.validate_naf_authorization(naf_hostname)
        
        # THEN
        self.assertTrue(result)

    def test_validate_naf_authorization_unauthorized(self):
        """Test NAF authorization returns False for unauthorized NAF"""
        # GIVEN - NAF not in any group
        naf_hostname = 'unauthorized.naf.example.com'
        
        # WHEN
        result = self.zn_interface.validate_naf_authorization(naf_hostname)
        
        # THEN
        self.assertFalse(result)

    def test_validate_naf_authorization_logs_authorized(self):
        """Test that authorized NAF is logged at debug level"""
        # GIVEN
        naf_hostname = 'naf1.epc.mnc001.mcc001.3gppnetwork.org'
        
        # WHEN
        self.zn_interface.validate_naf_authorization(naf_hostname)
        
        # THEN
        self.mock_diameter.logTool.log.assert_called()
        call_kwargs = self.mock_diameter.logTool.log.call_args[1]
        self.assertEqual(call_kwargs['level'], 'debug')
        self.assertIn('authorized', call_kwargs['message'])

    def test_validate_naf_authorization_logs_unauthorized(self):
        """Test that unauthorized NAF is logged at warning level"""
        # GIVEN
        naf_hostname = 'unauthorized.naf.example.com'
        
        # WHEN
        self.zn_interface.validate_naf_authorization(naf_hostname)
        
        # THEN
        self.mock_diameter.logTool.log.assert_called()
        call_kwargs = self.mock_diameter.logTool.log.call_args[1]
        self.assertEqual(call_kwargs['level'], 'warning')
        self.assertIn('NOT authorized', call_kwargs['message'])

    def test_validate_naf_authorization_empty_groups(self):
        """Test NAF authorization with empty naf_groups config"""
        # GIVEN - config without naf_groups
        config_empty = {
            'hss': {
                'Zn_enabled': True,
                'bsf': {
                    'bsf_hostname': 'bsf.example.org'
                    # No naf_groups defined
                }
            }
        }
        from zn_interface import ZnInterface
        zn = ZnInterface(self.mock_diameter, self.mock_database, config_empty)
        
        # WHEN
        result = zn.validate_naf_authorization('any.naf.example.com')
        
        # THEN
        self.assertFalse(result)


class TestZnInterfaceEdgeCases(unittest.TestCase):
    """Edge case and error handling tests for ZnInterface"""

    def setUp(self):
        """Set up test fixtures"""
        self.mock_diameter = MagicMock()
        self.mock_diameter.logTool = MagicMock()
        self.mock_diameter.redisMessaging = MagicMock()
        self.mock_database = MagicMock()
        
        self.test_config = {
            'hss': {
                'Zn_enabled': True,
                'bsf': {
                    'bsf_hostname': 'bsf.example.org',
                    'gaa_key_lifetime': 3600,
                    'naf_groups': []
                }
            }
        }
        
        from zn_interface import ZnInterface
        self.zn_interface = ZnInterface(
            self.mock_diameter, 
            self.mock_database, 
            self.test_config
        )

    def test_generate_btid_default_hostname(self):
        """Test B-TID uses default hostname when bsf_hostname not in config"""
        # GIVEN - config without bsf_hostname
        config_no_bsf = {
            'hss': {
                'Zn_enabled': True,
                'bsf': {}  # No bsf_hostname
            }
        }
        from zn_interface import ZnInterface
        zn = ZnInterface(self.mock_diameter, self.mock_database, config_no_bsf)
        
        # WHEN
        btid = zn.generate_btid(os.urandom(16))
        
        # THEN - should use default hostname
        self.assertIn('bsf.epc.mnc001.mcc001.3gppnetwork.org', btid)

    def test_derive_ks_naf_unicode_naf_id(self):
        """Test Ks_NAF derivation handles unicode in NAF ID"""
        # GIVEN
        ck = os.urandom(16)
        ik = os.urandom(16)
        naf_id = 'naf.例え.com'  # Japanese characters
        impi = 'user@example.com'
        
        # WHEN
        ks_naf = self.zn_interface.derive_ks_naf(ck, ik, naf_id, impi)
        
        # THEN
        self.assertEqual(len(ks_naf), 32)

    def test_derive_ks_naf_empty_strings(self):
        """Test Ks_NAF derivation with empty strings"""
        # GIVEN
        ck = os.urandom(16)
        ik = os.urandom(16)
        
        # WHEN
        ks_naf = self.zn_interface.derive_ks_naf(ck, ik, '', '')
        
        # THEN - should still produce valid output
        self.assertEqual(len(ks_naf), 32)

    def test_gaa_key_lifetime_default(self):
        """Test GAA key lifetime uses default when not specified"""
        # GIVEN
        config_no_lifetime = {
            'hss': {
                'Zn_enabled': True,
                'bsf': {
                    'bsf_hostname': 'bsf.example.org'
                    # No gaa_key_lifetime
                }
            }
        }
        from zn_interface import ZnInterface
        zn = ZnInterface(self.mock_diameter, self.mock_database, config_no_lifetime)
        
        # THEN
        self.assertEqual(zn.gaa_key_lifetime, 3600)  # Default value


if __name__ == '__main__':
    unittest.main()
