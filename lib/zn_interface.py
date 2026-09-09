#!/usr/bin/env python3
# Copyright 2019-2025 Nick <nick@nickvsnetworking.com>
# Copyright 2023 David Kneipp <david@davidkneipp.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zn-Interface Extension for PyHSS
Implements 3GPP TS 29.109 for GBA (Generic Bootstrapping Architecture)

This module provides helper functions for:
- B-TID (Bootstrapping Transaction Identifier) generation
- Ks_NAF key derivation
- NAF authorization validation

The Diameter MAR/MAA handler is implemented in lib/diameter.py (Answer_16777221_303,
3GPP TS 29.109 §6 Zh, App-Id 16777221). Note: Zn (16777220) is the NAF<->BSF
interface; the BSF<->HSS GBA interface is Zh (16777221).
"""

import hashlib
import base64


class ZnInterface:
    """
    Zn-Interface Implementation for BSF-HSS Communication
    
    Provides GBA (Generic Bootstrapping Architecture) helper functions
    according to 3GPP TS 29.109 and 3GPP TS 33.220.
    """
    
    def __init__(self, diameter_instance, database_instance, config):
        """
        Initialize the ZnInterface.
        
        Args:
            diameter_instance: Instance of the Diameter class
            database_instance: Instance of the Database class
            config: Configuration dictionary (from config.yaml)
        """
        self.diameter = diameter_instance
        self.database = database_instance
        self.config = config
        self.logTool = diameter_instance.logTool
        self.redisMessaging = diameter_instance.redisMessaging
        
        # GBA/Zn specific configuration
        self.zn_enabled = config.get('hss', {}).get('Zn_enabled', False)
        self.bsf_config = config.get('hss', {}).get('bsf', {})
        self.gaa_key_lifetime = self.bsf_config.get('gaa_key_lifetime', 3600)
        
    def generate_btid(self, rand, bsf_hostname=None):
        """
        Generate B-TID (Bootstrapping Transaction Identifier)
        Format: base64(RAND)@bsf_hostname
        
        Args:
            rand: 16 byte RAND value
            bsf_hostname: BSF Hostname (optional, uses config if not provided)
            
        Returns:
            B-TID as string
        """
        if bsf_hostname is None:
            bsf_hostname = self.bsf_config.get('bsf_hostname', 'bsf.epc.mnc001.mcc001.3gppnetwork.org')
        
        # Encode RAND in Base64
        rand_b64 = base64.b64encode(rand).decode('ascii')
        btid = f"{rand_b64}@{bsf_hostname}"
        
        self.logTool.log(service='HSS', level='debug', 
                        message=f"Generated B-TID: {btid}", 
                        redisClient=self.redisMessaging)
        
        return btid
    
    def derive_ks_naf(self, ck, ik, naf_id, impi):
        """
        Derive Ks_NAF according to 3GPP TS 33.220
        Ks_NAF = KDF(CK || IK, "gba-me", NAF_Id, IMPI)
        
        Args:
            ck: Cipher Key (16 bytes)
            ik: Integrity Key (16 bytes)
            naf_id: NAF Identifier (FQDN of the NAF)
            impi: IMS Private Identity
            
        Returns:
            Ks_NAF (32 bytes)
        """
        # Ks = CK || IK
        ks = ck + ik
        
        # Encode NAF_Id and IMPI
        naf_id_bytes = naf_id.encode('utf-8')
        impi_bytes = impi.encode('utf-8')
        
        # Key Derivation Function (simplified - use HMAC-SHA256 in production)
        kdf_input = ks + b'gba-me' + naf_id_bytes + impi_bytes
        ks_naf = hashlib.sha256(kdf_input).digest()
        
        self.logTool.log(service='HSS', level='debug', 
                        message=f"Derived Ks_NAF for NAF: {naf_id}", 
                        redisClient=self.redisMessaging)
        
        return ks_naf
    
    def derive_ks_ext_naf(self, kc, naf_id, impi):
        """
        Derive Ks_ext_NAF for 2G/3G networks
        Ks_ext_NAF = KDF(Kc, "gba-me", NAF_Id, IMPI)
        
        Args:
            kc: Cipher Key from 2G/3G (8 bytes)
            naf_id: NAF Identifier
            impi: IMS Private Identity
            
        Returns:
            Ks_ext_NAF (32 bytes)
        """
        naf_id_bytes = naf_id.encode('utf-8')
        impi_bytes = impi.encode('utf-8')
        
        # Key Derivation for 2G/3G
        kdf_input = kc + b'gba-me' + naf_id_bytes + impi_bytes
        ks_ext_naf = hashlib.sha256(kdf_input).digest()
        
        self.logTool.log(service='HSS', level='debug', 
                        message=f"Derived Ks_ext_NAF for 2G/3G NAF: {naf_id}", 
                        redisClient=self.redisMessaging)
        
        return ks_ext_naf
    
    def validate_naf_authorization(self, naf_hostname):
        """
        Check if a NAF is authorized to use GBA
        
        Args:
            naf_hostname: Hostname of the NAF
            
        Returns:
            Boolean - True if authorized
        """
        naf_groups = self.bsf_config.get('naf_groups', [])
        
        for group in naf_groups:
            if naf_hostname in group.get('naf_hostnames', []):
                self.logTool.log(service='HSS', level='debug', 
                                message=f"NAF {naf_hostname} is authorized", 
                                redisClient=self.redisMessaging)
                return True
        
        self.logTool.log(service='HSS', level='warning', 
                        message=f"NAF {naf_hostname} is NOT authorized", 
                        redisClient=self.redisMessaging)
        return False
