# HSS-initiated network teardown and profile push helpers.
# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Shared helpers for HSS-initiated Diameter procedures (CLR, Cx RTR, SWx RTR, Cx PPR).

Used by the REST API and OAM endpoints to tear down live EPC/IMS/VoWiFi sessions
when a subscriber is deleted, blocked, or administratively deregistered.
"""

from __future__ import annotations

import os
import traceback
from typing import List, Optional, Sequence

from database import IMS_SUBSCRIBER


def get_ims_domain(mcc: str, mnc: str) -> str:
    """Return the IMS home domain (Private User Identity realm)."""
    return f"ims.mnc{str(mnc).zfill(3)}.mcc{str(mcc).zfill(3)}.3gppnetwork.org"


def normalize_peer_hostname(peer_field: Optional[str]) -> Optional[str]:
    """Strip Diameter peer metadata suffix (peer;originHost) and return hostname."""
    if not peer_field:
        return None
    return str(peer_field).split(";")[0]


def normalize_scscf_destination(scscf: Optional[str]) -> Optional[str]:
    """Normalize stored S-CSCF name to a Diameter Destination-Host."""
    if not scscf:
        return None
    host = str(scscf).replace("sip:", "")
    if ";" in host:
        host = host.split(";")[0]
    return host


def build_public_identities(ims_subscriber: dict, domain: str) -> List[str]:
    """Build the set of IMPUs for Cx RTR from stored IMS subscriber data."""
    identities: List[str] = []
    msisdn = ims_subscriber.get("msisdn")
    if msisdn:
        identities.append(f"sip:+{msisdn}@{domain}")
        identities.append(f"tel:+{msisdn}")
    msisdn_list = ims_subscriber.get("msisdn_list") or ""
    for entry in str(msisdn_list).split(","):
        entry = entry.strip()
        if not entry:
            continue
        identities.append(f"sip:+{entry}@{domain}")
        identities.append(f"tel:+{entry}")
    imsi = ims_subscriber.get("imsi")
    if imsi:
        identities.append(f"sip:{imsi}@{domain}")
    # Preserve order, drop duplicates.
    return list(dict.fromkeys(identities))


def _log(log_tool, redis_messaging, level: str, message: str) -> None:
    if log_tool is not None:
        log_tool.log(service="API", level=level, message=message, redisClient=redis_messaging)


def _send_clr(
    diameter_client,
    subscriber_info: dict,
    imsi: str,
    warnings: List[str],
    log_tool=None,
    redis_messaging=None,
) -> None:
    serving_mme = subscriber_info.get("serving_mme")
    serving_mme_realm = subscriber_info.get("serving_mme_realm")
    serving_mme_peer = normalize_peer_hostname(subscriber_info.get("serving_mme_peer"))

    if serving_mme_peer and serving_mme_realm and serving_mme:
        try:
            diameter_client.sendDiameterRequest(
                requestType="CLR",
                hostname=serving_mme_peer,
                imsi=imsi,
                DestinationHost=serving_mme,
                DestinationRealm=serving_mme_realm,
                CancellationType=2,
                immediateReattach=False,
            )
            _log(log_tool, redis_messaging, "info", f"[network_control] Sent targeted CLR for IMSI {imsi} to {serving_mme_peer}")
        except Exception as exc:
            msg = f"CLR to serving MME failed for IMSI {imsi}: {exc}"
            warnings.append(msg)
            _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")

    try:
        diameter_client.broadcastDiameterRequest(
            requestType="CLR",
            peerType="MME",
            imsi=imsi,
            DestinationHost=serving_mme,
            DestinationRealm=serving_mme_realm,
            CancellationType=2,
            immediateReattach=False,
        )
    except Exception as exc:
        msg = f"CLR broadcast to MME peers failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")


def _send_cx_rtr(
    diameter_client,
    ims_subscriber_info: dict,
    imsi: str,
    ims_domain: str,
    warnings: List[str],
    log_tool=None,
    redis_messaging=None,
) -> None:
    serving_scscf = ims_subscriber_info.get("scscf")
    serving_scscf_realm = ims_subscriber_info.get("scscf_realm")
    serving_scscf_peer = normalize_peer_hostname(ims_subscriber_info.get("scscf_peer"))
    destination_host = normalize_scscf_destination(serving_scscf)
    public_identities = build_public_identities(ims_subscriber_info, ims_domain)

    if serving_scscf_peer and serving_scscf_realm and destination_host:
        try:
            diameter_client.sendDiameterRequest(
                requestType="RTR",
                hostname=serving_scscf_peer,
                imsi=imsi,
                domain=ims_domain,
                destinationHost=destination_host,
                destinationRealm=serving_scscf_realm,
                publicIdentities=public_identities,
            )
            _log(log_tool, redis_messaging, "info", f"[network_control] Sent targeted Cx RTR for IMSI {imsi} to {serving_scscf_peer}")
        except Exception as exc:
            msg = f"Cx RTR to serving S-CSCF failed for IMSI {imsi}: {exc}"
            warnings.append(msg)
            _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")

    try:
        diameter_client.broadcastDiameterRequest(
            requestType="RTR",
            peerType="SCSCF",
            imsi=imsi,
            domain=ims_domain,
            destinationHost=destination_host,
            destinationRealm=serving_scscf_realm,
            publicIdentities=public_identities,
        )
    except Exception as exc:
        msg = f"Cx RTR broadcast to S-CSCF peers failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")


def _aaa_rtr_destination_hosts(diameter_client, imsi: str) -> List[str]:
    """AAA Origin-Hosts that must appear as SWx RTR Destination-Host.

    TS 29.273 §8.1.2.3: the RTR goes to the 3GPP AAA Server registered for the
    subscriber, which the HSS learned from the SWx SAR. When that binding is
    known it is the only destination; ``SWX_AAA_DESTINATION_HOSTS`` is a
    fan-out fallback for when no binding exists (e.g. after a Redis flush).

    A Destination-Host is mandatory either way: DRA route ``hss-s6a`` matches
    Application-Id 16777265 and next-hops the HSS, so an RTR without it is
    looped straight back. RFC 6733 §6.1.5: a present Destination-Host selects
    the AAA peer first.
    """
    redis = getattr(diameter_client, "redisMessaging", None)
    if redis is not None:
        try:
            bound = redis.getValue(key=f"swx_aaa_server:{imsi}", usePrefix=False)
            if bound:
                return [bound.decode("utf-8") if isinstance(bound, bytes) else str(bound)]
        except Exception:
            pass
    hosts: List[str] = []
    extra = os.environ.get("SWX_AAA_DESTINATION_HOSTS", "")
    try:
        from pyhss_config import config as _cfg
        cfg_hosts = (_cfg.get("hss") or {}).get("swx_aaa_destination_hosts") or extra
    except Exception:
        cfg_hosts = extra
    if isinstance(cfg_hosts, (list, tuple)):
        hosts.extend(str(h).strip() for h in cfg_hosts if str(h).strip())
    else:
        hosts.extend(h.strip() for h in str(cfg_hosts).replace(" ", ",").split(",") if h.strip())
    return list(dict.fromkeys(hosts))


def _send_swx_rtr(
    diameter_client,
    imsi: str,
    diameter_realm: str,
    warnings: List[str],
    log_tool=None,
    redis_messaging=None,
) -> None:
    dest_hosts = _aaa_rtr_destination_hosts(diameter_client, imsi)
    if not dest_hosts:
        msg = (
            f"SWx RTR for IMSI {imsi} skipped: no AAA Destination-Host "
            f"(set SWX_AAA_DESTINATION_HOSTS or wait for an SWx SAR binding)"
        )
        warnings.append(msg)
        _log(log_tool, redis_messaging, "warning", f"[network_control] {msg}")
        return
    try:
        sent = 0
        for dest_host in dest_hosts:
            peers = diameter_client.broadcastDiameterRequest(
                requestType="SWX_RTR",
                peerType="aaa",
                imsi=imsi,
                destinationRealm=diameter_realm,
                destinationHost=dest_host,
            )
            peer_count = len(peers) if isinstance(peers, list) else 0
            if peer_count:
                sent += 1
                _log(
                    log_tool,
                    redis_messaging,
                    "info",
                    f"[network_control] Broadcast SWx RTR for IMSI {imsi} to {dest_host} via {peer_count} peer(s)",
                )
            else:
                warnings.append(f"SWx RTR to {dest_host} found no AAA/DRA peers for IMSI {imsi}")
        if not sent:
            warnings.append(f"SWx RTR broadcast found no AAA/DRA peers for IMSI {imsi}")
    except Exception as exc:
        msg = f"SWx RTR broadcast failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")


def _send_gx_session_release(
    diameter_client,
    database_client,
    subscriber_info: dict,
    imsi: str,
    warnings: List[str],
    log_tool=None,
    redis_messaging=None,
) -> None:
    """Release every active Gx IP-CAN session for the subscriber.

    TS 29.212 §4.5.9.6 (PCRF-initiated IP-CAN Session Termination): the PCRF
    sends RAR with Session-Release-Cause; the PCEF answers RAA and follows with
    a CCR-T. The existing CCR-T handler then emits the Rx ASR toward the P-CSCF
    and clears the stored S-CSCF / P-CSCF / serving-APN state.

    Session-Release-Cause UE_SUBSCRIPTION_REASON (1) per TS 29.212 §5.3.44 --
    the subscription was disabled or removed.
    """
    subscriber_id = subscriber_info.get("subscriber_id")
    if subscriber_id is None:
        return

    try:
        serving_apns = database_client.Get_Serving_APNs(subscriber_id=subscriber_id)
    except Exception as exc:
        warnings.append(f"Failed to list serving APNs for IMSI {imsi}: {exc}")
        return

    for apn_name, apn_data in (serving_apns.get("apns") or {}).items():
        if not apn_data:
            continue
        pcrf_session_id = apn_data.get("pcrf_session_id")
        serving_pgw = apn_data.get("serving_pgw")
        serving_pgw_realm = apn_data.get("serving_pgw_realm")
        serving_pgw_peer = normalize_peer_hostname(apn_data.get("serving_pgw_peer"))
        if not (pcrf_session_id and serving_pgw and serving_pgw_realm and serving_pgw_peer):
            continue

        sent = ""
        try:
            sent = diameter_client.sendDiameterRequest(
                requestType="RAR",
                hostname=serving_pgw_peer,
                sessionId=pcrf_session_id,
                servingPgw=serving_pgw,
                servingRealm=serving_pgw_realm,
                sessionReleaseCause=1,
            )
        except Exception as exc:
            warnings.append(f"Gx session release for IMSI {imsi} APN {apn_name} failed: {exc}")
            _log(log_tool, redis_messaging, "error", f"[network_control] {traceback.format_exc()}")

        if sent:
            _log(
                log_tool,
                redis_messaging,
                "info",
                f"[network_control] Sent Gx RAR (Session-Release-Cause) for IMSI {imsi} APN {apn_name} to {serving_pgw_peer}",
            )
            continue

        # The PGW peer is not connected, so no CCR-T will arrive to trigger the
        # Rx ASR. Abort the AF sessions directly instead of leaking them.
        warnings.append(f"Gx peer {serving_pgw_peer} not connected for IMSI {imsi} APN {apn_name}; aborting Rx sessions directly")
        try:
            diameter_client.GxCCR3_to_RxSTR(imsi, apn_name)
        except Exception as exc:
            warnings.append(f"Rx ASR fallback for IMSI {imsi} APN {apn_name} failed: {exc}")
            _log(log_tool, redis_messaging, "error", f"[network_control] {traceback.format_exc()}")


def teardown_subscriber(
    diameter_client,
    database_client,
    imsi: str,
    domains: Optional[Sequence[str]] = None,
    diameter_realm: Optional[str] = None,
    mcc: Optional[str] = None,
    mnc: Optional[str] = None,
    clear_serving_state: bool = True,
    log_tool=None,
    redis_messaging=None,
) -> List[str]:
    """
    Send HSS-initiated teardown messages for the given IMSI.

    domains: subset of 'pcrf', 'epc', 'ims', 'swx' (default: all four).
    Returns a list of non-fatal warning strings.
    """
    warnings: List[str] = []
    active_domains = set(domains or ("pcrf", "epc", "ims", "swx"))

    subscriber_info: dict = {}
    ims_subscriber_info: dict = {}

    try:
        subscriber_info = database_client.Get_Subscriber(imsi=str(imsi))
    except Exception:
        subscriber_info = {}

    try:
        ims_subscriber_info = database_client.Get_IMS_Subscriber(imsi=str(imsi))
    except Exception:
        ims_subscriber_info = {}

    resolved_mcc = mcc or getattr(diameter_client, "MCC", "999")
    resolved_mnc = mnc or getattr(diameter_client, "MNC", "999")
    ims_domain = get_ims_domain(resolved_mcc, resolved_mnc)
    resolved_realm = diameter_realm or subscriber_info.get("serving_mme_realm") or ims_subscriber_info.get("scscf_realm")

    # Runs first: releasing the IP-CAN session lets the P-CSCF tear an in-progress
    # call down cleanly (via the Rx ASR the CCR-T triggers) before the S6a CLR
    # detaches the UE and the Cx RTR force-deregisters the S-CSCF.
    if "pcrf" in active_domains:
        _send_gx_session_release(
            diameter_client,
            database_client,
            subscriber_info,
            imsi,
            warnings,
            log_tool,
            redis_messaging,
        )

    if "epc" in active_domains:
        _send_clr(diameter_client, subscriber_info, imsi, warnings, log_tool, redis_messaging)
        if clear_serving_state:
            try:
                database_client.Update_Serving_MME(imsi=imsi, serving_mme=None)
            except Exception as exc:
                warnings.append(f"Failed to clear serving MME for IMSI {imsi}: {exc}")

    if "ims" in active_domains:
        _send_cx_rtr(
            diameter_client,
            ims_subscriber_info,
            imsi,
            ims_domain,
            warnings,
            log_tool,
            redis_messaging,
        )
        if clear_serving_state:
            try:
                database_client.Update_Serving_CSCF(imsi=imsi, serving_cscf=None)
            except Exception as exc:
                warnings.append(f"Failed to clear serving S-CSCF for IMSI {imsi}: {exc}")

    if "swx" in active_domains and resolved_realm:
        _send_swx_rtr(diameter_client, imsi, resolved_realm, warnings, log_tool, redis_messaging)

    return warnings


def push_ims_profile(
    diameter_client,
    ims_subscriber: dict,
    mcc: Optional[str] = None,
    mnc: Optional[str] = None,
    log_tool=None,
    redis_messaging=None,
) -> List[str]:
    """
    Send Cx PPR to the serving S-CSCF when the subscriber is IMS-registered.

    Returns a list of non-fatal warning strings.
    """
    warnings: List[str] = []
    imsi = ims_subscriber.get("imsi")
    serving_scscf = ims_subscriber.get("scscf")
    serving_scscf_realm = ims_subscriber.get("scscf_realm")
    serving_scscf_peer = normalize_peer_hostname(ims_subscriber.get("scscf_peer"))
    destination_host = normalize_scscf_destination(serving_scscf)

    if not imsi or not serving_scscf_peer or not serving_scscf_realm or not destination_host:
        _log(
            log_tool,
            redis_messaging,
            "debug",
            f"[network_control] Skipping Cx PPR for IMSI {imsi}: no serving S-CSCF on record",
        )
        return warnings

    resolved_mcc = mcc or getattr(diameter_client, "MCC", "999")
    resolved_mnc = mnc or getattr(diameter_client, "MNC", "999")
    ims_domain = get_ims_domain(resolved_mcc, resolved_mnc)

    try:
        cx_user_data = diameter_client.build_cx_user_data(ims_subscriber)
    except Exception as exc:
        msg = f"Failed to build Cx-User-Data for PPR (IMSI {imsi}): {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")
        return warnings

    try:
        diameter_client.sendDiameterRequest(
            requestType="PPR",
            hostname=serving_scscf_peer,
            imsi=imsi,
            domain=ims_domain,
            destinationHost=destination_host,
            destinationRealm=serving_scscf_realm,
            cxUserData=cx_user_data,
        )
        _log(log_tool, redis_messaging, "info", f"[network_control] Sent Cx PPR for IMSI {imsi} to {serving_scscf_peer}")
    except Exception as exc:
        msg = f"Cx PPR to serving S-CSCF failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")

    return warnings


def ims_profile_push_fields_changed(old_subscriber: dict, patch_body: dict) -> bool:
    """Return True when a PATCH touches fields that require a Cx PPR."""
    watched = ("ifc_template_id", "ifc_path", "msisdn", "msisdn_list")
    return any(field in patch_body for field in watched)


def push_ppr_for_ifc_template_subscribers(
    diameter_client,
    database_client,
    ifc_template_id: int,
    mcc: Optional[str] = None,
    mnc: Optional[str] = None,
    log_tool=None,
    redis_messaging=None,
) -> List[str]:
    """Fan out Cx PPR to registered IMS subscribers using the given iFC template."""
    warnings: List[str] = []
    try:
        for entry in database_client.GetAll(IMS_SUBSCRIBER):
            if str(entry.get("ifc_template_id")) != str(ifc_template_id):
                continue
            if not entry.get("scscf") or not entry.get("scscf_peer"):
                continue
            warnings.extend(
                push_ims_profile(
                    diameter_client,
                    entry,
                    mcc=mcc,
                    mnc=mnc,
                    log_tool=log_tool,
                    redis_messaging=redis_messaging,
                )
            )
    except Exception as exc:
        warnings.append(f"PPR fan-out for iFC template {ifc_template_id} failed: {exc}")
        _log(log_tool, redis_messaging, "error", f"[network_control] {traceback.format_exc()}")
    return warnings
