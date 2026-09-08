# Copyright 2025 volte.io UG (haftungsbeschränkt)
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
ENUM Management Module for PyHSS

This module provides functionality to manage ENUM (E.164 Number Mapping) entries
in PowerDNS servers. It creates, updates, and deletes NAPTR records according to
RFC 6116 when IMS subscribers are provisioned.

Example ENUM mapping:
  MSISDN: +491721234567
  DNS Name: 7.6.5.4.3.2.1.7.2.9.4.e164.arpa
  NAPTR Record: 10 10 "u" "E2U+sip" "!^.*$!sip:+491721234567@ims.mnc001.mcc001.3gppnetwork.org!" .
"""

import time

import requests
from typing import List, Dict, Optional, Tuple, Any


class ENUMManagementError(Exception):
    """Exception raised when ENUM management operations fail."""
    pass


class ENUMClient:
    """
    Client for managing ENUM entries across multiple PowerDNS servers.
    
    Supports multiple PowerDNS API endpoints, each with multiple domains.
    Creates NAPTR records for MSISDNs according to RFC 6116.
    """

    def __init__(self, config: dict, log_tool=None, redis_messaging=None):
        """
        Initialize the ENUM client.
        
        Args:
            config: The PyHSS configuration dictionary containing 'enum' section
            log_tool: Optional LogTool instance for logging
            redis_messaging: Optional RedisMessaging instance for logging
        """
        self.config = config
        self.log_tool = log_tool
        self.redis_messaging = redis_messaging
        
        # ENUM configuration
        self.enum_config = config.get('enum', {})
        self.enabled = self.enum_config.get('enabled', False)
        self.strict_mode = self.enum_config.get('strict_mode', False)
        self.naptr_order = self.enum_config.get('naptr_order', 10)
        self.naptr_preference = self.enum_config.get('naptr_preference', 10)
        self.naptr_ttl = self.enum_config.get('naptr_ttl', 3600)
        self.endpoints = self.enum_config.get('endpoints', [])
        self.sms_enabled = self.enum_config.get('sms_enabled', False)

    def _log(self, level: str, message: str):
        """Log a message if log_tool is available."""
        if self.log_tool:
            self.log_tool.log(
                service='ENUM',
                level=level,
                message=message,
                redisClient=self.redis_messaging
            )

    def _metric(self, name: str, value: float, help_text: str, metric_type: str = 'gauge'):
        """Publish a metric if redis_messaging is available.

        Gauges, not counters: what an operator needs from a reconciliation is its
        current state -- when it last ran and what it found -- and a counter that
        only ever grows cannot answer "is the zone right now consistent with the
        database". A monotonic total would also make a restart look like the
        problem went away.
        """
        if not self.redis_messaging:
            return

        try:
            self.redis_messaging.sendMetric(
                serviceName='enum',
                metricName=name,
                metricType=metric_type,
                metricAction='set' if metric_type == 'gauge' else 'inc',
                metricValue=float(value),
                metricHelp=help_text,
                metricExpiry=None
            )
        except Exception as e:
            # A reconciliation that worked must not be reported as failed because
            # its metric could not be published.
            self._log('warning', f"could not publish ENUM metric {name}: {str(e)}")

    @staticmethod
    def msisdn_to_enum_name(msisdn: str, domain: str) -> str:
        """
        Convert an MSISDN to an ENUM DNS name per RFC 6116.
        
        Args:
            msisdn: The MSISDN (e.g., "491721234567" or "+491721234567")
            domain: The ENUM domain (e.g., "e164.arpa")
            
        Returns:
            The ENUM DNS name (e.g., "7.6.5.4.3.2.1.7.2.9.4.e164.arpa")
        """
        # Remove any leading '+' and non-digit characters
        clean_msisdn = ''.join(filter(str.isdigit, msisdn))
        
        # Reverse the digits and join with dots
        reversed_digits = '.'.join(reversed(clean_msisdn))
        
        # Append the domain
        return f"{reversed_digits}.{domain}"

    def generate_naptr_content(self, msisdn: str, sip_domain: str) -> str:
        """
        Generate NAPTR record content for an MSISDN per RFC 6116.
        
        Args:
            msisdn: The MSISDN (digits only, no '+')
            sip_domain: The SIP domain for the URI (e.g., "ims.mnc001.mcc001.3gppnetwork.org")
            
        Returns:
            The NAPTR record content string
        """
        # Clean MSISDN (digits only)
        clean_msisdn = ''.join(filter(str.isdigit, msisdn))
        
        # Format: order preference "flags" "service" "regexp" replacement
        # Example: 10 10 "u" "E2U+sip" "!^.*$!sip:+491721234567@ims.example.com!" .
        # The replacement URI carries the global E.164 number with leading '+'
        # so it matches the +E.164 IMPUs registered at the S-CSCF.
        return (
            f'{self.naptr_order} {self.naptr_preference} "u" "E2U+sip" '
            f'"!^.*$!sip:+{clean_msisdn}@{sip_domain}!" .'
        )

    def generate_sms_naptr_content(self, msisdn: str) -> str:
        """
        Generate the SMS reachability NAPTR content for an MSISDN (RFC 4355).

        This record says the number can receive SMS, and nothing about where to
        send it: IANA registers the "sms" Enumservice with the subtypes "tel" and
        "mailto" only, so its URI is a tel: URI. Where an SMS is delivered comes
        from the E2U+sip record, as it does for a call.

        Writing it separately is what makes SMS steerable on its own -- an
        operator can withdraw SMS for a subscriber without touching voice, and a
        message centre can tell "not our subscriber" from "ours, but no SMS".

        Args:
            msisdn: The MSISDN (digits only, no '+')

        Returns:
            The NAPTR record content string
        """
        clean_msisdn = ''.join(filter(str.isdigit, msisdn))

        return (
            f'{self.naptr_order} {self.naptr_preference} "u" "E2U+sms:tel" '
            f'"!^.*$!tel:+{clean_msisdn}!" .'
        )

    def naptr_records_for(self, msisdn: str, sip_domain: str) -> List[dict]:
        """
        The NAPTR records a single MSISDN should have.

        One rrset per name carries every record for that name, because PowerDNS
        REPLACE is per rrset: sending only the SIP record would delete the SMS one
        alongside it.

        Args:
            msisdn: The MSISDN (digits only, no '+')
            sip_domain: The SIP domain for the URI

        Returns:
            List of PowerDNS record dictionaries
        """
        records = [
            {'content': self.generate_naptr_content(msisdn, sip_domain), 'disabled': False}
        ]

        if self.sms_enabled:
            records.append(
                {'content': self.generate_sms_naptr_content(msisdn), 'disabled': False}
            )

        return records

    def _parse_msisdn_list(self, msisdn: Optional[str], msisdn_list: Optional[str]) -> List[str]:
        """
        Parse primary MSISDN and msisdn_list into a list of all MSISDNs.
        
        Args:
            msisdn: Primary MSISDN
            msisdn_list: Comma-separated list of additional MSISDNs
            
        Returns:
            List of all MSISDNs (cleaned, digits only)
        """
        all_msisdns = []
        
        if msisdn:
            clean = ''.join(filter(str.isdigit, msisdn))
            if clean:
                all_msisdns.append(clean)
        
        if msisdn_list:
            for m in msisdn_list.split(','):
                clean = ''.join(filter(str.isdigit, m.strip()))
                if clean and clean not in all_msisdns:
                    all_msisdns.append(clean)
        
        return all_msisdns

    def _make_pdns_request(
        self,
        endpoint: dict,
        zone: str,
        rrsets: List[dict]
    ) -> Tuple[bool, Optional[str]]:
        """
        Make a request to PowerDNS API to update records.
        
        Args:
            endpoint: PowerDNS endpoint configuration
            zone: The DNS zone to update
            rrsets: List of rrset changes
            
        Returns:
            Tuple of (success, error_message)
        """
        url = f"{endpoint['url']}/api/v1/servers/localhost/zones/{zone}"
        headers = {
            'X-API-Key': endpoint['api_key'],
            'Content-Type': 'application/json'
        }
        payload = {'rrsets': rrsets}
        
        try:
            response = requests.patch(url, json=payload, headers=headers, timeout=10)
            if response.status_code in (200, 204):
                return True, None
            else:
                error_msg = f"PowerDNS API error: {response.status_code} - {response.text}"
                return False, error_msg
        except requests.exceptions.RequestException as e:
            return False, f"PowerDNS request failed: {str(e)}"

    def _zone_enum_msisdns(
        self,
        endpoint: dict,
        zone: str
    ) -> Tuple[Optional[set], Optional[str]]:
        """
        The MSISDNs that currently have NAPTR records in a zone.

        Reads the zone back rather than trusting that what was written is what is
        there, which is the only way to find entries the database no longer
        accounts for. An ENUM entry for a deleted subscriber is worse than a
        missing one: a message centre reads presence as "ours" and keeps trying to
        deliver to a subscriber who is gone, instead of routing the message out to
        the network that now owns the number.

        Args:
            endpoint: PowerDNS endpoint configuration
            zone: The DNS zone to read

        Returns:
            Tuple of (set of MSISDNs without '+', error_message). The set is None
            when the zone could not be read, which is not the same as an empty one.
        """
        url = f"{endpoint['url']}/api/v1/servers/localhost/zones/{zone}"
        headers = {'X-API-Key': endpoint['api_key']}

        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code != 200:
                return None, f"PowerDNS API error: {response.status_code} - {response.text}"
            zone_data = response.json()
        except requests.exceptions.RequestException as e:
            return None, f"PowerDNS request failed: {str(e)}"
        except ValueError as e:
            return None, f"PowerDNS returned unreadable JSON: {str(e)}"

        suffix = '.' + zone.rstrip('.') + '.'
        msisdns = set()

        for rrset in zone_data.get('rrsets', []):
            if rrset.get('type') != 'NAPTR':
                continue

            name = rrset.get('name', '')
            if not name.endswith(suffix):
                continue

            # "1.0.0.0.9.9.9.0.7.1.9.4." -> "491709990001"
            labels = name[:-len(suffix)].split('.')
            if not all(len(label) == 1 and label.isdigit() for label in labels):
                continue

            msisdns.add(''.join(reversed(labels)))

        return msisdns, None

    def create_enum_entries(
        self,
        msisdn: Optional[str],
        msisdn_list: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create ENUM entries for an IMS subscriber's MSISDNs.
        
        Args:
            msisdn: Primary MSISDN
            msisdn_list: Comma-separated list of additional MSISDNs
            
        Returns:
            Dictionary with results per endpoint
            
        Raises:
            ENUMManagementError: If strict_mode is True and any endpoint fails
        """
        if not self.enabled:
            self._log('debug', "ENUM management is disabled, skipping create")
            return {'status': 'disabled'}
        
        all_msisdns = self._parse_msisdn_list(msisdn, msisdn_list)
        if not all_msisdns:
            self._log('debug', "No MSISDNs provided for ENUM creation")
            return {'status': 'no_msisdns'}
        
        self._log('info', f"Creating ENUM entries for MSISDNs: {all_msisdns}")
        
        results = {'status': 'ok', 'endpoints': {}, 'errors': []}
        
        for endpoint in self.endpoints:
            endpoint_name = endpoint.get('name', endpoint.get('url', 'unknown'))
            sip_domain = endpoint.get('sip_domain', '')
            results['endpoints'][endpoint_name] = {'domains': {}}
            
            for domain in endpoint.get('domains', []):
                rrsets = []
                
                for m in all_msisdns:
                    enum_name = self.msisdn_to_enum_name(m, domain)
                    
                    rrsets.append({
                        'name': enum_name + '.',  # PowerDNS requires trailing dot
                        'type': 'NAPTR',
                        'ttl': self.naptr_ttl,
                        'changetype': 'REPLACE',
                        'records': self.naptr_records_for(m, sip_domain)
                    })
                
                success, error = self._make_pdns_request(endpoint, domain, rrsets)
                results['endpoints'][endpoint_name]['domains'][domain] = {
                    'success': success,
                    'msisdns': all_msisdns
                }
                
                if not success:
                    error_detail = f"{endpoint_name}/{domain}: {error}"
                    results['errors'].append(error_detail)
                    self._log('error', f"ENUM create failed - {error_detail}")
                    
                    if self.strict_mode:
                        results['status'] = 'error'
                        raise ENUMManagementError(f"ENUM creation failed: {error_detail}")
                else:
                    self._log('info', f"ENUM entries created on {endpoint_name}/{domain}")
        
        if results['errors']:
            results['status'] = 'partial'
        
        return results

    def delete_enum_entries(
        self,
        msisdn: Optional[str],
        msisdn_list: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Delete ENUM entries for an IMS subscriber's MSISDNs.
        
        Args:
            msisdn: Primary MSISDN
            msisdn_list: Comma-separated list of additional MSISDNs
            
        Returns:
            Dictionary with results per endpoint
            
        Raises:
            ENUMManagementError: If strict_mode is True and any endpoint fails
        """
        if not self.enabled:
            self._log('debug', "ENUM management is disabled, skipping delete")
            return {'status': 'disabled'}
        
        all_msisdns = self._parse_msisdn_list(msisdn, msisdn_list)
        if not all_msisdns:
            self._log('debug', "No MSISDNs provided for ENUM deletion")
            return {'status': 'no_msisdns'}
        
        self._log('info', f"Deleting ENUM entries for MSISDNs: {all_msisdns}")
        
        results = {'status': 'ok', 'endpoints': {}, 'errors': []}
        
        for endpoint in self.endpoints:
            endpoint_name = endpoint.get('name', endpoint.get('url', 'unknown'))
            results['endpoints'][endpoint_name] = {'domains': {}}
            
            for domain in endpoint.get('domains', []):
                rrsets = []
                
                for m in all_msisdns:
                    enum_name = self.msisdn_to_enum_name(m, domain)
                    
                    rrsets.append({
                        'name': enum_name + '.',  # PowerDNS requires trailing dot
                        'type': 'NAPTR',
                        'changetype': 'DELETE',
                        'records': []
                    })
                
                success, error = self._make_pdns_request(endpoint, domain, rrsets)
                results['endpoints'][endpoint_name]['domains'][domain] = {
                    'success': success,
                    'msisdns': all_msisdns
                }
                
                if not success:
                    error_detail = f"{endpoint_name}/{domain}: {error}"
                    results['errors'].append(error_detail)
                    self._log('error', f"ENUM delete failed - {error_detail}")
                    
                    if self.strict_mode:
                        results['status'] = 'error'
                        raise ENUMManagementError(f"ENUM deletion failed: {error_detail}")
                else:
                    self._log('info', f"ENUM entries deleted on {endpoint_name}/{domain}")
        
        if results['errors']:
            results['status'] = 'partial'
        
        return results

    def update_enum_entries(
        self,
        old_msisdn: Optional[str],
        old_msisdn_list: Optional[str],
        new_msisdn: Optional[str],
        new_msisdn_list: Optional[str]
    ) -> Dict[str, Any]:
        """
        Update ENUM entries when MSISDNs change.
        
        Computes the difference between old and new MSISDNs, deletes removed ones,
        and creates new ones.
        
        Args:
            old_msisdn: Previous primary MSISDN
            old_msisdn_list: Previous comma-separated list of additional MSISDNs
            new_msisdn: New primary MSISDN
            new_msisdn_list: New comma-separated list of additional MSISDNs
            
        Returns:
            Dictionary with results
            
        Raises:
            ENUMManagementError: If strict_mode is True and any operation fails
        """
        if not self.enabled:
            self._log('debug', "ENUM management is disabled, skipping update")
            return {'status': 'disabled'}
        
        old_set = set(self._parse_msisdn_list(old_msisdn, old_msisdn_list))
        new_set = set(self._parse_msisdn_list(new_msisdn, new_msisdn_list))
        
        to_delete = old_set - new_set
        to_create = new_set - old_set
        
        self._log('info', f"ENUM update: delete {to_delete}, create {to_create}")
        
        results = {
            'status': 'ok',
            'deleted': [],
            'created': [],
            'errors': []
        }
        
        # Delete removed MSISDNs
        if to_delete:
            delete_list = ','.join(to_delete)
            try:
                delete_result = self.delete_enum_entries(None, delete_list)
                results['deleted'] = list(to_delete)
                if delete_result.get('errors'):
                    results['errors'].extend(delete_result['errors'])
            except ENUMManagementError as e:
                results['errors'].append(str(e))
                if self.strict_mode:
                    results['status'] = 'error'
                    raise
        
        # Create new MSISDNs
        if to_create:
            create_list = ','.join(to_create)
            try:
                create_result = self.create_enum_entries(None, create_list)
                results['created'] = list(to_create)
                if create_result.get('errors'):
                    results['errors'].extend(create_result['errors'])
            except ENUMManagementError as e:
                results['errors'].append(str(e))
                if self.strict_mode:
                    results['status'] = 'error'
                    raise
        
        if results['errors']:
            results['status'] = 'partial'
        
        return results

    def reconcile_all(self, database_client) -> Dict[str, Any]:
        """
        Reconcile all ENUM entries from the database.
        
        Iterates through all IMS subscribers in the database and ensures
        their ENUM entries exist in all configured PowerDNS servers, then reads
        each zone back to find entries the database no longer accounts for.
        
        Results are published as metrics as well as returned, because the thing
        worth alerting on -- the zone and the database disagreeing -- is only
        visible from here.
        
        Args:
            database_client: Database client instance to query IMS subscribers
            
        Returns:
            Dictionary with reconciliation results
        """
        if not self.enabled:
            self._log('info', "ENUM management is disabled, skipping reconciliation")
            return {'status': 'disabled'}
        
        self._log('info', "Starting ENUM reconciliation")
        
        results = {
            'status': 'ok',
            'processed': 0,
            'succeeded': 0,
            'failed': 0,
            'errors': [],
            'subscribers': [],
            'orphaned': []
        }
        
        provisioned_msisdns = set()
        
        try:
            # Import IMS_SUBSCRIBER model from database module
            from database import IMS_SUBSCRIBER
            
            # Get all IMS subscribers with pagination (0-based page index)
            page = 0
            page_size = 100
            
            while True:
                subscribers = database_client.getAllPaginated(
                    IMS_SUBSCRIBER,
                    page,
                    page_size
                )
                
                if not subscribers or len(subscribers) == 0:
                    break
                
                for sub in subscribers:
                    results['processed'] += 1
                    msisdn = sub.get('msisdn')
                    msisdn_list = sub.get('msisdn_list')
                    sub_id = sub.get('ims_subscriber_id')
                    provisioned_msisdns.update(
                        self._parse_msisdn_list(msisdn, msisdn_list)
                    )
                    
                    try:
                        # Create/update ENUM entries for this subscriber
                        create_result = self.create_enum_entries(msisdn, msisdn_list)
                        
                        if create_result.get('status') in ('ok', 'disabled', 'no_msisdns'):
                            results['succeeded'] += 1
                            results['subscribers'].append({
                                'ims_subscriber_id': sub_id,
                                'msisdn': msisdn,
                                'status': 'ok'
                            })
                        else:
                            results['failed'] += 1
                            results['subscribers'].append({
                                'ims_subscriber_id': sub_id,
                                'msisdn': msisdn,
                                'status': 'partial',
                                'errors': create_result.get('errors', [])
                            })
                    except ENUMManagementError as e:
                        results['failed'] += 1
                        results['errors'].append(f"Subscriber {sub_id}: {str(e)}")
                        results['subscribers'].append({
                            'ims_subscriber_id': sub_id,
                            'msisdn': msisdn,
                            'status': 'error',
                            'error': str(e)
                        })
                
                page += 1
                
                # Safety check to prevent infinite loops
                if page > 10000:
                    self._log('warning', "Reconciliation stopped at page 10000")
                    break
        
        except Exception as e:
            results['status'] = 'error'
            results['errors'].append(f"Reconciliation failed: {str(e)}")
            self._log('error', f"ENUM reconciliation failed: {str(e)}")
        
        # Only look for orphans once the database side is known to be complete.
        # A half-read subscriber list would make every unread subscriber's entry
        # look orphaned, and reporting a healthy zone as inconsistent is worse
        # than reporting nothing.
        if results['status'] != 'error':
            results['orphaned'] = self._find_orphans(provisioned_msisdns, results)
        
        if results['failed'] > 0:
            results['status'] = 'partial'
        
        self._log('info', f"ENUM reconciliation complete: {results['processed']} processed, "
                         f"{results['succeeded']} succeeded, {results['failed']} failed, "
                         f"{len(results['orphaned'])} orphaned")
        
        self._metric('prom_enum_reconcile_timestamp_seconds', time.time(),
                     'Unix time of the last completed ENUM reconciliation')
        self._metric('prom_enum_reconcile_corrected', results['succeeded'],
                     'IMS subscribers whose ENUM entries were written on the last reconciliation')
        self._metric('prom_enum_reconcile_failed', results['failed'],
                     'IMS subscribers whose ENUM entries could not be written')
        self._metric('prom_enum_reconcile_orphaned', len(results['orphaned']),
                     'ENUM entries present in DNS with no IMS subscriber behind them')
        
        return results

    def _find_orphans(
        self,
        provisioned_msisdns: set,
        results: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """
        ENUM entries in DNS that no IMS subscriber accounts for.

        Reported rather than deleted. An orphan is as likely to mean the database
        lost a subscriber as that DNS kept a stale one, and deleting on that guess
        would make a database problem permanent -- the number would stop being
        reachable rather than merely be reachable when it should not be. An
        operator can act on the number once they know it.

        Args:
            provisioned_msisdns: MSISDNs found in the database, digits only
            results: Reconciliation results, appended to on read failure

        Returns:
            List of orphan descriptions
        """
        orphans = []

        for endpoint in self.endpoints:
            endpoint_name = endpoint.get('name', endpoint.get('url', 'unknown'))

            for domain in endpoint.get('domains', []):
                in_dns, error = self._zone_enum_msisdns(endpoint, domain)

                if in_dns is None:
                    # Unread, not empty. Saying "no orphans" here would be a claim
                    # we have not checked.
                    detail = f"{endpoint_name}/{domain}: {error}"
                    results['errors'].append(f"orphan check skipped - {detail}")
                    self._log('warning', f"ENUM orphan check skipped - {detail}")
                    continue

                for msisdn in sorted(in_dns - provisioned_msisdns):
                    orphans.append({
                        'msisdn': msisdn,
                        'endpoint': endpoint_name,
                        'domain': domain
                    })
                    self._log('warning',
                              f"ENUM entry for +{msisdn} on {endpoint_name}/{domain} "
                              f"has no IMS subscriber behind it")

        return orphans

