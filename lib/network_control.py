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


def peer_via(peer_field: Optional[str]) -> Optional[str]:
    """The DRA a serving-peer field '<host>;<hss>;<dra>' names (KI-74), if any."""
    parts = str(peer_field or "").split(";")
    return parts[2] if len(parts) > 2 and parts[2] and parts[2] != parts[0] else None


def scscf_diameter_destination(ims_subscriber: dict) -> Optional[str]:
    """Diameter identity of the serving S-CSCF, for Cx RTR/PPR Destination-Host.

    ``scscf`` holds the SIP Server-Name from the SAR (e.g. ``sip:scscf.<domain>``),
    which every S-CSCF pod shares and no Diameter peer carries as Origin-Host.
    ``scscf_peer`` holds the S-CSCF's own Diameter identity (from the SAR's
    Route-Record or Origin-Host) plus ``;<HSS Origin-Host>``. TS 29.228 §6.1.3
    and §6.1.4: RTR and PPR go to the S-CSCF assigned to the user, so they must
    name that identity; RFC 6733 §6.1.5 lets a relay forward on Destination-Host
    only when it names a known peer. With the Server-Name the DRA fell back to
    the Cx route, which points at the HSS, and the request was never answered.
    """
    return normalize_peer_hostname(ims_subscriber.get("scscf_peer"))


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
    immediate_reattach: bool = False,
) -> bool:
    """Send the teardown CLR from this node. Returns True when the CLR went out
    (or there was nobody to send it to), False when this node had no route.

    Cancellation-Type stays SUBSCRIPTION_WITHDRAWAL; the CLR-Flags bit
    Reattach-Required (TS 29.272 7.3.152) makes the MME send the Detach Request
    as "re-attach required" (TS 24.301 5.5.2.3.1), so the UE attaches again at
    once instead of staying detached until its radio is reset (KI-132).
    The broadcast to MME-type peers is only the fallback for a CLR that could
    not be addressed: sent as well, it reached the MME a second time (KI-154).
    """
    serving_mme = subscriber_info.get("serving_mme")
    serving_mme_realm = subscriber_info.get("serving_mme_realm")
    serving_mme_peer = normalize_peer_hostname(subscriber_info.get("serving_mme_peer"))

    if serving_mme_peer and serving_mme_realm and serving_mme:
        try:
            sent = diameter_client.sendDiameterRequest(
                requestType="CLR",
                hostname=serving_mme_peer,
                via=peer_via(subscriber_info.get("serving_mme_peer")),
                imsi=imsi,
                DestinationHost=serving_mme,
                DestinationRealm=serving_mme_realm,
                CancellationType=2,
                immediateReattach=immediate_reattach,
            )
            if sent:
                _log(log_tool, redis_messaging, "info", f"[network_control] Sent targeted CLR for IMSI {imsi} to {serving_mme_peer}")
                return True
        except Exception as exc:
            msg = f"CLR to serving MME failed for IMSI {imsi}: {exc}"
            warnings.append(msg)
            _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")

    if not serving_mme:
        # Nobody to address: nothing for any node to do.
        return True
    try:
        peers = diameter_client.broadcastDiameterRequest(
            requestType="CLR",
            peerType="MME",
            imsi=imsi,
            DestinationHost=serving_mme,
            DestinationRealm=serving_mme_realm,
            CancellationType=2,
            immediateReattach=immediate_reattach,
        )
        if isinstance(peers, list) and peers:
            return True
    except Exception as exc:
        msg = f"CLR broadcast to MME peers failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")
    warnings.append(f"CLR for IMSI {imsi} was not sent from this node: no route to {serving_mme_peer}")
    return False


def send_clr(diameter_client, imsi: str, clr: dict) -> bool:
    """Queue an operator-requested S6a CLR via this node's peer table.

    clr carries the PUT /push/clr body: diameterPeer, DestinationRealm,
    optional DestinationHost, cancellationType and immediateReattach.
    Returns True when the request was queued to a connected peer.
    """
    sent = diameter_client.sendDiameterRequest(
        requestType="CLR",
        hostname=clr["diameterPeer"],
        imsi=imsi,
        DestinationHost=clr.get("DestinationHost"),
        DestinationRealm=clr["DestinationRealm"],
        CancellationType=clr["cancellationType"],
        immediateReattach=clr["immediateReattach"],
    )
    return bool(sent)


def push_clr_and_relay(diameter_client, imsi: str, clr: dict, relay) -> dict:
    """Send an operator-requested CLR from this node and from every Diameter node.

    The MME may be connected to any Diameter node, and each node only knows its
    own peers, so the CLR is also relayed. relay(path, payload) returns the JSON
    bodies of the nodes that answered. The CLR counts as sent when this node or
    at least one relayed node queued it.
    """
    sent_locally = send_clr(diameter_client, imsi, clr)
    node_results = relay("/geored/push_clr", dict(clr, imsi=imsi)) or []
    nodes_sent = sum(1 for result in node_results if result.get("result") == "OK")
    return {
        "sent": sent_locally or nodes_sent > 0,
        "sent_locally": sent_locally,
        "relayed": len(node_results) > 0,
        "nodes_sent": nodes_sent,
    }


def relay_teardown_domains(post, node_urls, payload: dict, domains) -> List[str]:
    """Hand a teardown to the Diameter nodes one at a time (KI-154).

    Every node sent every message of the teardown, so one /oam/deregister gave
    the MME ten CLRs. Now a node gets only the domains no earlier node could
    send, and the relay stops when none is left. post(url, payload) returns the
    node's JSON body or None; a node older than HSS 1.7.35 has no 'handled'
    and is taken to have sent everything. Returns the domains nobody sent.
    """
    remaining = list(domains)
    for url in node_urls:
        if not remaining:
            break
        body = post(url, dict(payload, domains=list(remaining)))
        if body is None:
            continue
        handled = body.get("handled")
        if handled is None:
            return []
        remaining = [domain for domain in remaining if not handled.get(domain)]
    return remaining


def push_charging_rule(diameter_client, database_client, imsi: str, apn_id, charging_rule_id) -> dict:
    """Send a Gx RAR installing a predefined charging rule from this node.

    Returns {'result': 'OK' | 'NotSent' | 'NoSession', 'pgw': <serving PGW>}.
    'NotSent' means this node has no route to the PGW (no PGW or DRA peer),
    which is always the case on the provisioning node (KI-149).
    """
    subscriber = database_client.Get_Subscriber(imsi=str(imsi))
    serving_apn = database_client.Get_Serving_APN(subscriber_id=subscriber["subscriber_id"], apn_id=apn_id)
    if not serving_apn:
        return {"result": "NoSession", "pgw": None}
    charging_rule = database_client.Get_Charging_Rule(charging_rule_id)
    charging_rule["apn_data"] = database_client.Get_APN(apn_id)
    serving_pgw = serving_apn.get("serving_pgw")
    sent = diameter_client.sendDiameterRequest(
        requestType="RAR",
        hostname=normalize_peer_hostname(serving_apn.get("serving_pgw_peer")),
        sessionId=serving_apn.get("pcrf_session_id"),
        chargingRules=charging_rule,
        ueIp=serving_apn.get("subscriber_routing"),
        servingPgw=serving_pgw,
        servingRealm=serving_apn.get("serving_pgw_realm"),
    )
    return {"result": "OK" if sent else "NotSent", "pgw": serving_pgw}


def push_charging_rule_and_relay(diameter_client, database_client, imsi: str, apn_id, charging_rule_id, relay) -> dict:
    """Send the Gx RAR of PUT /pcrf/ from this node, else from one Diameter node.

    The provisioning node holds no Diameter peers, so the push is relayed;
    relay(path, payload, until) stops at the first node that sent it, so the
    PGW gets the rule once. Returns this node's or the sending node's result.
    """
    local = push_charging_rule(diameter_client, database_client, imsi, apn_id, charging_rule_id)
    if local["result"] != "NotSent":
        return local
    payload = {"imsi": str(imsi), "apn_id": apn_id, "charging_rule_id": charging_rule_id}
    results = relay("/geored/push_charging_rule", payload, lambda body: body.get("result") == "OK") or []
    for body in results:
        if body.get("result") == "OK":
            return body
    return local


def purge_subscriber_state_and_relay(diameter_client, relay, subscriber_ids=(), ims_subscriber_ids=(),
                                     log_tool=None, redis_messaging=None) -> None:
    """Remove the Sh and Rx subscription state of deleted subscribers from Redis.

    The state lives in the Redis of whichever node handled the SNR or AAR. With
    provisioning and Diameter in separate pods that is a Redis this process
    cannot reach, so the ids are relayed to every Diameter node as well. The
    database rows are already gone: a failure is logged, not raised.
    """
    payload = {"subscriber_ids": list(subscriber_ids), "ims_subscriber_ids": list(ims_subscriber_ids)}
    if not payload["subscriber_ids"] and not payload["ims_subscriber_ids"]:
        return
    try:
        diameter_client.purge_subscriber_state(**payload)
    except Exception as error:
        _log(log_tool, redis_messaging, "warning",
             f"[API] Purging subscriber state from the local Redis failed: {error}")
    relay("/geored/purge_subscriber_state", payload)


def _send_cx_rtr(
    diameter_client,
    ims_subscriber_info: dict,
    imsi: str,
    ims_domain: str,
    warnings: List[str],
    log_tool=None,
    redis_messaging=None,
) -> bool:
    serving_scscf_realm = ims_subscriber_info.get("scscf_realm")
    serving_scscf_peer = scscf_diameter_destination(ims_subscriber_info)
    public_identities = build_public_identities(ims_subscriber_info, ims_domain)

    if not (serving_scscf_peer and serving_scscf_realm):
        # Without the S-CSCF's Diameter identity the RTR cannot be addressed: a
        # request without a usable Destination-Host follows the DRA's Cx route
        # back to the HSS.
        if ims_subscriber_info.get("scscf"):
            msg = f"Cx RTR for IMSI {imsi} not sent: no S-CSCF Diameter identity on record"
            warnings.append(msg)
            _log(log_tool, redis_messaging, "warning", f"[network_control] {msg}")
        else:
            _log(log_tool, redis_messaging, "info", f"[network_control] No Cx RTR for IMSI {imsi}: no serving S-CSCF on record")
        return True

    sent = ""
    try:
        sent = diameter_client.sendDiameterRequest(
            requestType="RTR",
            hostname=serving_scscf_peer,
            imsi=imsi,
            domain=ims_domain,
            destinationHost=serving_scscf_peer,
            destinationRealm=serving_scscf_realm,
            publicIdentities=public_identities,
        )
    except Exception as exc:
        msg = f"Cx RTR to serving S-CSCF failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")
        return False

    if sent:
        _log(log_tool, redis_messaging, "info", f"[network_control] Sent targeted Cx RTR for IMSI {imsi} to {serving_scscf_peer}")
        return True

    # sendDiameterRequest returns empty without raising when neither the S-CSCF
    # nor a DRA is in this process's peer table (always so in the API service;
    # the Diameter nodes send it from /geored/network_teardown).
    msg = f"Cx RTR for IMSI {imsi} was not sent from this node: peer {serving_scscf_peer} is not connected"
    warnings.append(msg)
    _log(log_tool, redis_messaging, "warning", f"[network_control] {msg}")
    return False


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
) -> bool:
    dest_hosts = _aaa_rtr_destination_hosts(diameter_client, imsi)
    if not dest_hosts:
        msg = (
            f"SWx RTR for IMSI {imsi} skipped: no AAA Destination-Host "
            f"(set SWX_AAA_DESTINATION_HOSTS or wait for an SWx SAR binding)"
        )
        warnings.append(msg)
        _log(log_tool, redis_messaging, "warning", f"[network_control] {msg}")
        return True
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
        return bool(sent)
    except Exception as exc:
        msg = f"SWx RTR broadcast failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")
    return False


def _send_gx_session_release(
    diameter_client,
    database_client,
    subscriber_info: dict,
    imsi: str,
    warnings: List[str],
    log_tool=None,
    redis_messaging=None,
    defer_unsent: bool = False,
) -> bool:
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
        return True

    try:
        serving_apns = database_client.Get_Serving_APNs(subscriber_id=subscriber_id)
    except Exception as exc:
        warnings.append(f"Failed to list serving APNs for IMSI {imsi}: {exc}")
        return True

    handled = True
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

        if defer_unsent:
            # Another node (the relay that follows) may reach the PGW.
            handled = False
            continue
        # The PGW peer is not connected, so no CCR-T will arrive to trigger the
        # Rx ASR. Abort the AF sessions directly instead of leaking them.
        warnings.append(f"Gx peer {serving_pgw_peer} not connected for IMSI {imsi} APN {apn_name}; aborting Rx sessions directly")
        try:
            diameter_client.GxCCR3_to_RxSTR(imsi, apn_name)
        except Exception as exc:
            warnings.append(f"Rx ASR fallback for IMSI {imsi} APN {apn_name} failed: {exc}")
            _log(log_tool, redis_messaging, "error", f"[network_control] {traceback.format_exc()}")
    return handled


# Serving-node fields the teardown needs to address CLR and RTR.
SERVING_FIELDS = {
    "subscriber": ("serving_mme", "serving_mme_realm", "serving_mme_peer"),
    "ims_subscriber": ("scscf", "scscf_realm", "scscf_peer"),
}


def serving_state_snapshot(database_client, imsi: str) -> dict:
    """Read the serving-node fields of a subscriber, for the teardown relay.

    The API reads the primary database. A Diameter node reads its own replica,
    which can lack a registration the primary already has (KI-38), so the relay
    carries what the API read.
    """
    readers = {"subscriber": database_client.Get_Subscriber, "ims_subscriber": database_client.Get_IMS_Subscriber}
    snapshot = {}
    for table, fields in SERVING_FIELDS.items():
        try:
            row = readers[table](imsi=str(imsi)) or {}
        except Exception:
            row = {}
        snapshot[table] = {field: row.get(field) for field in fields if row.get(field)}
    return snapshot


def _fill_missing(local: dict, snapshot: Optional[dict]) -> dict:
    """Return local with the snapshot's values where local has none."""
    if not snapshot:
        return local
    merged = dict(local or {})
    for field, value in snapshot.items():
        if value and not merged.get(field):
            merged[field] = value
    return merged


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
    serving_state: Optional[dict] = None,
    immediate_reattach: bool = False,
    report: Optional[dict] = None,
    defer_unsent: bool = False,
) -> List[str]:
    """
    Send HSS-initiated teardown messages for the given IMSI.

    domains: subset of 'pcrf', 'epc', 'ims', 'swx' (default: all four).
    serving_state: serving-node fields read by the caller (see
    serving_state_snapshot); they fill fields this node's database lacks.
    report: filled with {domain: True when this node sent its messages (or
    there was nothing to send), False when it had no route}, so the caller
    can hand only the rest to another node (KI-154).
    defer_unsent: leave what this node cannot send to the next node instead
    of using the Rx ASR fallback for an unreachable PGW.
    immediate_reattach: set Reattach-Required in the S6a CLR. True for an
    operator deregistration of a subscriber who keeps the subscription; False
    for a subscriber delete or barring, where the UE must not come back.
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

    serving_state = serving_state or {}
    subscriber_info = _fill_missing(subscriber_info, serving_state.get("subscriber"))
    ims_subscriber_info = _fill_missing(ims_subscriber_info, serving_state.get("ims_subscriber"))

    resolved_mcc = mcc or getattr(diameter_client, "MCC", "999")
    resolved_mnc = mnc or getattr(diameter_client, "MNC", "999")
    ims_domain = get_ims_domain(resolved_mcc, resolved_mnc)
    resolved_realm = diameter_realm or subscriber_info.get("serving_mme_realm") or ims_subscriber_info.get("scscf_realm")

    # Runs first: releasing the IP-CAN session lets the P-CSCF tear an in-progress
    # call down cleanly (via the Rx ASR the CCR-T triggers) before the S6a CLR
    # detaches the UE and the Cx RTR force-deregisters the S-CSCF.
    report = report if report is not None else {}
    if "pcrf" in active_domains:
        report["pcrf"] = _send_gx_session_release(
            diameter_client,
            database_client,
            subscriber_info,
            imsi,
            warnings,
            log_tool,
            redis_messaging,
            defer_unsent=defer_unsent,
        )

    if "epc" in active_domains:
        report["epc"] = _send_clr(diameter_client, subscriber_info, imsi, warnings, log_tool, redis_messaging,
                                  immediate_reattach=immediate_reattach)
        if clear_serving_state:
            try:
                database_client.Update_Serving_MME(imsi=imsi, serving_mme=None)
            except Exception as exc:
                warnings.append(f"Failed to clear serving MME for IMSI {imsi}: {exc}")

    if "ims" in active_domains:
        report["ims"] = _send_cx_rtr(
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

    if "swx" in active_domains:
        report["swx"] = (_send_swx_rtr(diameter_client, imsi, resolved_realm, warnings, log_tool, redis_messaging)
                         if resolved_realm else True)

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
    serving_scscf_realm = ims_subscriber.get("scscf_realm")
    serving_scscf_peer = scscf_diameter_destination(ims_subscriber)

    if not imsi or not serving_scscf_peer or not serving_scscf_realm:
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

    sent = ""
    try:
        sent = diameter_client.sendDiameterRequest(
            requestType="PPR",
            hostname=serving_scscf_peer,
            imsi=imsi,
            domain=ims_domain,
            destinationHost=serving_scscf_peer,
            destinationRealm=serving_scscf_realm,
            cxUserData=cx_user_data,
        )
    except Exception as exc:
        msg = f"Cx PPR to serving S-CSCF failed for IMSI {imsi}: {exc}"
        warnings.append(msg)
        _log(log_tool, redis_messaging, "error", f"[network_control] {msg}\n{traceback.format_exc()}")
        return warnings

    if sent:
        _log(log_tool, redis_messaging, "info", f"[network_control] Sent Cx PPR for IMSI {imsi} to {serving_scscf_peer}")
        return warnings

    # sendDiameterRequest returns empty without raising when the peer is absent from
    # this process's peer table, which is always the case in the API service: the Cx
    # connections belong to the diameter nodes. Logging success regardless left a
    # caller believing the S-CSCF had been refreshed while it kept serving the iFC it
    # cached at registration.
    msg = (
        f"Cx PPR for IMSI {imsi} was not sent: peer {serving_scscf_peer} is not connected to "
        f"this node, so the S-CSCF keeps the iFC it cached until the subscriber re-registers"
    )
    warnings.append(msg)
    _log(log_tool, redis_messaging, "warning", f"[network_control] {msg}")

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
