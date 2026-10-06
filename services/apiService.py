#!/usr/bin/env python3
# Copyright 2022-2025 Nick <nick@nickvsnetworking.com>
# Copyright 2023-2025 David Kneipp <david@davidkneipp.com>
# Copyright 2025 sysmocom - s.f.m.c. GmbH <info@sysmocom.de>
# Copyright 2025 Lennart Rosam <hello@takuto.de>
# SPDX-License-Identifier: AGPL-3.0-or-later
import sys
import json
from flask import Flask, request, jsonify, Response
from flask_restx import Api, Resource, fields, reqparse, abort
from database import geored_check_updated_endpoints
from werkzeug.middleware.proxy_fix import ProxyFix
from functools import wraps
import os

sys.path.append(os.path.realpath(os.path.dirname(__file__) + "/../lib"))

import time
import re
import requests
import traceback
import sqlalchemy
import socket
from urllib.parse import urlparse
from logtool import LogTool
from diameter import Diameter
from messaging import RedisMessaging
from baseModels import SubscriberInfo
import database
from network_control import (
    send_clr,
    push_clr_and_relay,
    teardown_subscriber,
    push_ims_profile,
    ims_profile_push_fields_changed,
    push_ppr_for_ifc_template_subscribers,
)
from pyhss_config import config
from enum_management import ENUMClient, ENUMManagementError


siteName = config.get("hss", {}).get("site_name", "")
originHostname = socket.gethostname()
lockProvisioning = config.get('hss', {}).get('lock_provisioning', False)
provisioningKey = config.get('hss', {}).get('provisioning_key', '')
mnc = config.get('hss', {}).get('MNC', '999')
mcc = config.get('hss', {}).get('MCC', '999')
originRealm = config.get('hss', {}).get('OriginRealm', f'mnc{mnc}.mcc{mcc}.3gppnetwork.org')
originHost = config.get('hss', {}).get('OriginHost', f'hss01')
productName = config.get('hss', {}).get('ProductName', f'PyHSS')

redisHost = config.get("redis", {}).get("host", "127.0.0.1")
redisPort = int(config.get("redis", {}).get("port", 6379))
redisUseUnixSocket = config.get('redis', {}).get('useUnixSocket', False)
redisUnixSocketPath = config.get('redis', {}).get('unixSocketPath', '/var/run/redis/redis-server.sock')

insecureAuc = config.get('api', {}).get('enable_insecure_auc', False)

redisMessaging = RedisMessaging(host=redisHost, port=redisPort, useUnixSocket=redisUseUnixSocket, unixSocketPath=redisUnixSocketPath)

logTool = LogTool(config)

diameterClient = Diameter(
                    redisMessaging=redisMessaging, 
                    logTool=logTool,
                    originHost=originHost, 
                    originRealm=originRealm, 
                    mnc=mnc,
                    mcc=mcc,
                    productName='PyHSS-client-API',
                    main_service=True
                )

databaseClient = database.Database(logTool=logTool, redisMessaging=redisMessaging, main_service=True)

enumClient = ENUMClient(config=config, log_tool=logTool, redis_messaging=redisMessaging)

# MSISDN inputs must be global E.164 numbers with a leading '+'
# (RFC 3966 / TS 23.003). They are stored canonically as digits-only;
# the '+' is re-added when rendering IMS identities (iFC/Sh/ENUM).
E164_MSISDN_PATTERN = re.compile(r'^\+[1-9][0-9]{6,14}$')


class InvalidMsisdnError(ValueError):
    """Raised when an MSISDN input is not a global E.164 number."""
    pass


def normalize_msisdn_input(json_data):
    """Validate '+E.164' msisdn / msisdn_list inputs and strip the '+' for storage."""
    if json_data.get('msisdn') is not None:
        msisdn = str(json_data['msisdn']).strip()
        if not E164_MSISDN_PATTERN.match(msisdn):
            raise InvalidMsisdnError(
                f"msisdn must be a global E.164 number with leading '+' (e.g. +4915888456043), got '{msisdn}'")
        json_data['msisdn'] = msisdn[1:]
    if json_data.get('msisdn_list') is not None:
        normalized = []
        for entry in str(json_data['msisdn_list']).split(','):
            entry = entry.strip()
            if not entry:
                continue
            if not E164_MSISDN_PATTERN.match(entry):
                raise InvalidMsisdnError(
                    f"msisdn_list entries must be global E.164 numbers with leading '+', got '{entry}'")
            normalized.append(entry[1:])
        json_data['msisdn_list'] = ','.join(normalized)


def send_pnr_for_local_subscriptions(ims_subscriber_data):
    """Send an Sh Push-Notification-Request (PNR, TS 29.328 section 6.1.4)
    with the updated repository data to every AS whose SNR subscription is
    stored in this node's Redis."""
    ims_subscriber_id = ims_subscriber_data.get('ims_subscriber_id')
    subscriptions = diameterClient.sh_get_subscriptions(ims_subscriber_id)
    if not subscriptions:
        return
    msisdn = ims_subscriber_data.get('msisdn')
    public_identity = None
    if msisdn:
        mnc = diameterClient.MNC.zfill(3)
        mcc = diameterClient.MCC.zfill(3)
        public_identity = f"sip:+{msisdn}@ims.mnc{mnc}.mcc{mcc}.3gppnetwork.org"
    for origin_host, subscription in subscriptions.items():
        if not isinstance(subscription, dict):
            subscription = {}
        service_indications = subscription.get('serviceIndications') or [None]
        user_data = diameterClient.sh_build_notification_user_data(ims_subscriber_data, service_indications[0])
        sent = diameterClient.sendDiameterRequest(
            requestType='PNR',
            hostname=origin_host,
            destinationHost=origin_host,
            destinationRealm=subscription.get('originRealm'),
            msisdn=msisdn,
            publicIdentity=public_identity,
            userData=user_data,
        )
        if sent:
            logTool.log(service='API', level='info', message=f"[API] Sent Sh PNR to {origin_host} for ims_subscriber {ims_subscriber_id}", redisClient=redisMessaging)
        else:
            logTool.log(service='API', level='warning', message=f"[API] Sh PNR to {origin_host} for ims_subscriber {ims_subscriber_id} was not sent: AS peer not connected and no DRA route available", redisClient=redisMessaging)


def relay_sh_profile_update(ims_subscriber_data):
    """Relay an Sh profile change to remote PyHSS nodes over their REST API.

    In split deployments the provisioning API and the Diameter front ends run
    in separate pods with separate Redis instances, so SNR subscriptions are
    only visible on the Diameter nodes. Each endpoint hostname is resolved to
    all of its A/AAAA records so a single Kubernetes headless-service URL fans
    out to every node; nodes without a subscription treat the call as a no-op.
    """
    endpoints = config.get('hss', {}).get('sh_notify_endpoints', []) or []
    if not endpoints:
        return
    ims_subscriber_id = ims_subscriber_data.get('ims_subscriber_id')
    for endpoint in endpoints:
        parsed = urlparse(endpoint)
        host = parsed.hostname
        port = parsed.port or 8080
        try:
            addresses = sorted({addrinfo[4][0] for addrinfo in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
        except Exception as e:
            logTool.log(service='API', level='warning', message=f"[API] Sh notify relay: failed to resolve {host}: {e}", redisClient=redisMessaging)
            continue
        for address in addresses:
            urlHost = f"[{address}]" if ':' in address else address
            url = f"{parsed.scheme}://{urlHost}:{port}/geored/sh_profile_updated"
            try:
                requests.post(url, json={'ims_subscriber_id': ims_subscriber_id}, timeout=2)
                logTool.log(service='API', level='debug', message=f"[API] Sh notify relay sent to {url} for ims_subscriber {ims_subscriber_id}", redisClient=redisMessaging)
            except Exception as e:
                logTool.log(service='API', level='warning', message=f"[API] Sh notify relay to {url} failed: {e}", redisClient=redisMessaging)


def relay_to_diameter_nodes(path, payload):
    """Fan out a Diameter-side action to HSS Diameter API pods.

    Provisioning and Diameter run in separate pods with separate Redis, so
    outbound CLR/RTR/PPR must be executed on the Diameter nodes that hold the
    peer table. Uses the same SH_NOTIFY_ENDPOINTS headless-service expansion
    as Sh PNR relay. Diameter nodes must not call this helper (no loop).
    Returns the JSON bodies of the nodes that answered with HTTP < 400.
    """
    results = []
    endpoints = config.get('hss', {}).get('sh_notify_endpoints', []) or []
    if not endpoints:
        return results
    for endpoint in endpoints:
        parsed = urlparse(endpoint)
        host = parsed.hostname
        port = parsed.port or 8080
        try:
            addresses = sorted({addrinfo[4][0] for addrinfo in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})
        except Exception as e:
            logTool.log(service='API', level='warning', message=f"[API] Diameter relay: failed to resolve {host}: {e}", redisClient=redisMessaging)
            continue
        for address in addresses:
            urlHost = f"[{address}]" if ':' in address else address
            url = f"{parsed.scheme}://{urlHost}:{port}{path}"
            try:
                resp = requests.post(url, json=payload, timeout=5)
                if resp.status_code >= 400:
                    logTool.log(
                        service='API',
                        level='warning',
                        message=f"[API] Diameter relay to {url} returned HTTP {resp.status_code}: {resp.text[:200]}",
                        redisClient=redisMessaging,
                    )
                else:
                    try:
                        results.append(resp.json())
                    except ValueError:
                        results.append({})
            except Exception as e:
                logTool.log(service='API', level='warning', message=f"[API] Diameter relay to {url} failed: {e}", redisClient=redisMessaging)
    return results


def teardown_subscriber_and_relay(imsi, domains):
    # Keep serving-node fields until Diameter pods have sent CLR/RTR.
    warnings = teardown_subscriber(
        diameterClient,
        databaseClient,
        imsi,
        domains=domains,
        diameter_realm=originRealm,
        mcc=mcc,
        mnc=mnc,
        clear_serving_state=False,
        log_tool=logTool,
        redis_messaging=redisMessaging,
    )
    relay_to_diameter_nodes('/geored/network_teardown', {'imsi': imsi, 'domains': list(domains)})
    if 'epc' in domains:
        try:
            databaseClient.Update_Serving_MME(imsi=imsi, serving_mme=None)
        except Exception as exc:
            warnings.append(f"Failed to clear serving MME for IMSI {imsi}: {exc}")
    if 'ims' in domains:
        try:
            databaseClient.Update_Serving_CSCF(imsi=imsi, serving_cscf=None)
        except Exception as exc:
            warnings.append(f"Failed to clear serving S-CSCF for IMSI {imsi}: {exc}")
    return warnings


def push_ims_profile_and_relay(ims_subscriber):
    warnings = push_ims_profile(
        diameterClient,
        ims_subscriber,
        mcc=mcc,
        mnc=mnc,
        log_tool=logTool,
        redis_messaging=redisMessaging,
    )
    imsi = ims_subscriber.get('imsi')
    if imsi:
        relay_to_diameter_nodes('/geored/push_ims_profile', {'imsi': imsi})
    return warnings
    """Send PNRs for subscriptions held locally and relay the change to any
    configured remote PyHSS nodes that hold their own subscriptions."""
    send_pnr_for_local_subscriptions(ims_subscriber_data)
    relay_sh_profile_update(ims_subscriber_data)


apiService = Flask(__name__)

APN = database.APN
Serving_APN = database.SERVING_APN
AUC = database.AUC
SUBSCRIBER = database.SUBSCRIBER
IMS_SUBSCRIBER = database.IMS_SUBSCRIBER
TFT = database.TFT
CHARGING_RULE = database.CHARGING_RULE
EIR = database.EIR
IMSI_IMEI_HISTORY = database.IMSI_IMEI_HISTORY
SUBSCRIBER_ATTRIBUTES = database.SUBSCRIBER_ATTRIBUTES
OPERATION_LOG = database.OPERATION_LOG_BASE
SUBSCRIBER_ROUTING = database.SUBSCRIBER_ROUTING
ROAMING_NETWORK = database.ROAMING_NETWORK
ROAMING_RULE = database.ROAMING_RULE
EMERGENCY_SUBSCRIBER = database.EMERGENCY_SUBSCRIBER
IFC_TEMPLATE = database.IFC_TEMPLATE


apiService.wsgi_app = ProxyFix(apiService.wsgi_app)
api = Api(apiService, version='1.0', title=f'{siteName + " - " if siteName else ""}{originHostname} - PyHSS OAM API',
    description='Restful API for working with PyHSS',
    doc='/docs/'
)

ns_apn = api.namespace('apn', description='PyHSS APN Functions')
ns_auc = api.namespace('auc', description='PyHSS AUC Functions')
ns_subscriber = api.namespace('subscriber', description='PyHSS SUBSCRIBER Functions')
ns_ims_subscriber = api.namespace('ims_subscriber', description='PyHSS IMS SUBSCRIBER Functions')
ns_tft = api.namespace('tft', description='PyHSS TFT Functions')
ns_charging_rule = api.namespace('charging_rule', description='PyHSS Charging Rule Functions')
ns_eir = api.namespace('eir', description='PyHSS PyHSS Equipment Identity Register')
ns_imsi_imei = api.namespace('imsi_imei', description='PyHSS IMSI / IMEI Mapping')
ns_subscriber_attributes = api.namespace('subscriber_attributes', description='PyHSS Subscriber Attributes')
ns_operation_log = api.namespace('operation_logs', description='PyHSS Operation Logs')
ns_oam = api.namespace('oam', description='PyHSS OAM Functions')
ns_pcrf = api.namespace('pcrf', description='PyHSS PCRF Dynamic Functions')
ns_geored = api.namespace('geored', description='PyHSS GeoRedundancy Functions')
ns_push = api.namespace('push', description='PyHSS Push Async Diameter Command')
ns_roaming = api.namespace('roaming', description='PyHSS Roaming Functions')
ns_ifc_template = api.namespace('ifc_template', description='PyHSS IFC Template Functions')

parser = reqparse.RequestParser()
parser.add_argument('APN JSON', type=str, help='APN Body')
parser.add_argument('operation_id', type=str, help='Operation ID', location='args')

paginatorParser = reqparse.RequestParser()
paginatorParser.add_argument('page', type=int, required=False, default=0, help='Page number for pagination')
paginatorParser.add_argument('page_size', type=int, required=False, default=config['api'].get('page_size', 100), help='Number of items per page for pagination')

# Rows per bulk request: one request is one database transaction.
BULK_SUBSCRIBERS_MAX = 50000
bulkRangeParser = reqparse.RequestParser()
bulkRangeParser.add_argument('imsi_start', type=str, required=True, location='args', help='First IMSI of the range')
bulkRangeParser.add_argument('count', type=int, required=True, location='args', help='Number of consecutive IMSIs')

APN_model = api.schema_model('APN JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(APN)
)

Serving_APN_model = api.schema_model('Serving APN JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(Serving_APN)
)

AUC_model = api.schema_model('AUC JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(AUC)
)

SUBSCRIBER_model = api.schema_model('SUBSCRIBER JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(SUBSCRIBER)
)

SUBSCRIBER_ROUTING_model = api.schema_model('SUBSCRIBER_ROUTING JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(SUBSCRIBER_ROUTING)
)

ROAMING_NETWORK_model = api.schema_model('ROAMING_NETWORK JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(ROAMING_NETWORK)
)

ROAMING_RULE_model = api.schema_model('ROAMING_RULE JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(ROAMING_RULE)
)

EMERGENCY_SUBSCRIBER_model = api.schema_model('EMERGENCY_SUBSCRIBER JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(EMERGENCY_SUBSCRIBER)
)

#Legacy support for sh_profile. sh_profile is deprecated as of v1.0.1.
imsSubscriberModel = databaseClient.Generate_JSON_Model_for_Flask(IMS_SUBSCRIBER)
imsSubscriberModel['sh_profile'] = fields.String(required=False, description=IMS_SUBSCRIBER.sh_profile.doc),

IMS_SUBSCRIBER_model = api.schema_model('IMS_SUBSCRIBER JSON', databaseClient.Generate_JSON_Model_for_Flask(IMS_SUBSCRIBER))

TFT_model = api.schema_model('TFT JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(TFT)
)

CHARGING_RULE_model = api.schema_model('CHARGING_RULE JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(CHARGING_RULE)
)

EIR_model = api.schema_model('EIR JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(EIR)
)

IMSI_IMEI_HISTORY_model = api.schema_model('IMSI_IMEI_HISTORY JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(IMSI_IMEI_HISTORY)
)

SUBSCRIBER_ATTRIBUTES_model = api.schema_model('SUBSCRIBER_ATTRIBUTES JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(SUBSCRIBER_ATTRIBUTES)
)

IFC_TEMPLATE_model = api.schema_model('IFC_TEMPLATE JSON', 
    databaseClient.Generate_JSON_Model_for_Flask(IFC_TEMPLATE)
)

PCRF_Push_model = api.model('PCRF_Rule', {
    'imsi': fields.String(required=True, description='IMSI of Subscriber to push rule to'),
    'apn_id': fields.Integer(required=True, description='APN_ID of APN to push rule on'),
    'charging_rule_id' : fields.Integer(required=True, description='charging_rule_id to push'),
})

PCRF_PCSCF_Restoration_Subscriber_model = api.model('PCRF_PCSCF_Restoration_Subscriber', {
    'imsi': fields.String(required=True, description='IMSI of IMS Subscriber'),
    'msisdn': fields.String(required=True, description='MSISDN of IMS Subscriber'),
})

PCRF_PCSCF_Restoration_model = api.model('PCRF_PCSCF_Restoration', {
    'pcscf': fields.String(required=True, description='Serving PCSCF to send restoration for'),
})

Push_CLR_Model = api.model('CLR', {
    'DestinationRealm': fields.String(required=True, description='Destination Realm to set'),
    'DestinationHost': fields.String(required=False, description='Destination Host (Optional)'),
    'cancellationType' : fields.Integer(required=True, default=2, description='Cancellation Type as per 3GPP TS 29.272 / 7.3.24'),
    'diameterPeer': fields.String(required=True, description='Diameter peer to send to'),
    'immediateReattach': fields.Boolean(required=True, default=True, description='Whether or not the UE should reattach immediately')
})

GeoRed_model = api.model('GeoRed', {
    'imsi': fields.String(required=True, description='IMSI of Subscriber to Update'),
    'serving_mme': fields.String(description=SUBSCRIBER.serving_mme.doc),
    'serving_mme_realm': fields.String(description=SUBSCRIBER.serving_mme_realm.doc),
    'serving_mme_peer': fields.String(description=SUBSCRIBER.serving_mme_peer.doc),
    'serving_mme_timestamp' : fields.String(description=SUBSCRIBER.serving_mme_timestamp.doc),
    'serving_apn' : fields.String(description='Access Point Name of APN'),
    'pcrf_session_id' : fields.String(description=Serving_APN.pcrf_session_id.doc),
    'pcscf' : fields.String(description=IMS_SUBSCRIBER.pcscf.doc),
    'pcscf_realm' : fields.String(description=IMS_SUBSCRIBER.pcscf_realm.doc),
    'pcscf_peer' : fields.String(description=IMS_SUBSCRIBER.pcscf_peer.doc),
    'pcscf_timestamp' : fields.String(description=IMS_SUBSCRIBER.pcscf_timestamp.doc),
    'pcscf_active_session' : fields.String(description=IMS_SUBSCRIBER.pcscf_active_session.doc),
    'subscriber_routing' : fields.String(description=Serving_APN.subscriber_routing.doc),
    'serving_pgw' : fields.String(description=Serving_APN.serving_pgw.doc),
    'serving_pgw_realm' : fields.String(description=Serving_APN.serving_pgw_realm.doc),
    'serving_pgw_peer' : fields.String(description=Serving_APN.serving_pgw_peer.doc),
    'serving_pgw_timestamp' : fields.String(description=Serving_APN.serving_pgw_timestamp.doc),
    'af_subscriptions' : fields.String(description=Serving_APN.af_subscriptions.doc),
    'scscf' : fields.String(description=IMS_SUBSCRIBER.scscf.doc),
    'scscf_realm' : fields.String(description=IMS_SUBSCRIBER.scscf_realm.doc),
    'scscf_peer' : fields.String(description=IMS_SUBSCRIBER.scscf_peer.doc),
    'scscf_timestamp' : fields.String(description=IMS_SUBSCRIBER.scscf_timestamp.doc),
    'xcap_profile' : fields.String(description=IMS_SUBSCRIBER.xcap_profile.doc),
    'imei' : fields.String(description=EIR.imei.doc),
    'match_response_code' : fields.String(description=EIR.match_response_code.doc),
    'emergency_subscriber_id': fields.String(description=EMERGENCY_SUBSCRIBER.emergency_subscriber_id.doc),
    'emergency_subscriber_imsi': fields.String(description=EMERGENCY_SUBSCRIBER.imsi.doc),
    'emergency_subscriber_serving_pgw': fields.String(description=EMERGENCY_SUBSCRIBER.serving_pgw.doc),
    'emergency_subscriber_serving_pgw_timestamp': fields.String(description=EMERGENCY_SUBSCRIBER.serving_pgw_timestamp.doc),
    'emergency_subscriber_serving_pcscf': fields.String(description=EMERGENCY_SUBSCRIBER.serving_pcscf.doc),
    'emergency_subscriber_serving_pcscf_timestamp': fields.String(description=EMERGENCY_SUBSCRIBER.serving_pcscf_timestamp.doc),
    'emergency_subscriber_gx_origin_realm': fields.String(description=EMERGENCY_SUBSCRIBER.gx_origin_realm.doc),
    'emergency_subscriber_gx_origin_host': fields.String(description=EMERGENCY_SUBSCRIBER.gx_origin_host.doc),
    'emergency_subscriber_rat_type': fields.String(description=EMERGENCY_SUBSCRIBER.rat_type.doc),
    'emergency_subscriber_ip': fields.String(description=EMERGENCY_SUBSCRIBER.ip.doc),
    'emergency_subscriber_access_network_gateway_address': fields.String(description=EMERGENCY_SUBSCRIBER.access_network_gateway_address.doc),
    'emergency_subscriber_access_network_charging_address': fields.String(description=EMERGENCY_SUBSCRIBER.access_network_charging_address.doc),
    'emergency_subscriber_delete': fields.Boolean(description="Whether to delete the emergency subscriber on receipt"),
    'serving_msc': fields.String(description=SUBSCRIBER.serving_msc.doc),
    'serving_msc_timestamp': fields.String(description=SUBSCRIBER.serving_msc_timestamp.doc),
    'serving_vlr': fields.String(description=SUBSCRIBER.serving_vlr.doc),
    'serving_vlr_timestamp': fields.String(description=SUBSCRIBER.serving_vlr_timestamp.doc),
    'serving_sgsn': fields.String(description=SUBSCRIBER.serving_sgsn.doc),
    'serving_sgsn_timestamp': fields.String(description=SUBSCRIBER.serving_sgsn_timestamp.doc),
    'last_seen_eci': fields.String(description=SUBSCRIBER.last_seen_eci.doc),
    'last_seen_enodeb_id': fields.String(description=SUBSCRIBER.last_seen_enodeb_id.doc),
    'last_seen_cell_id': fields.String(description=SUBSCRIBER.last_seen_cell_id.doc),
    'last_seen_tac': fields.String(description=SUBSCRIBER.last_seen_tac.doc),
    'last_seen_mcc': fields.String(description=SUBSCRIBER.last_seen_mcc.doc),
    'last_seen_mnc': fields.String(description=SUBSCRIBER.last_seen_mnc.doc),
    'last_location_update_timestamp': fields.String(description=SUBSCRIBER.last_location_update_timestamp.doc)
})

def no_auth_required(f):
    f.no_auth_required = True
    return f

def auth_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if getattr(f, 'no_auth_required', False) or (lockProvisioning == False):
            return f(*args, **kwargs)
        if 'Provisioning-Key' not in request.headers or request.headers['Provisioning-Key'] != config['hss']['provisioning_key']:
            return {'Result': 'Unauthorized - Provisioning-Key Invalid'}, 401
        return f(*args, **kwargs)
    return decorated_function

def auth_before_request():
    if request.path.startswith('/docs') or request.path.startswith('/swagger') or request.path.startswith('/metrics'):
        return None
    if request.method == "OPTIONS":
        res = Response()
        res.headers['X-Content-Type-Options'] = '*'
        return res
    if request.endpoint and 'static' not in request.endpoint:
        view_function = apiService.view_functions[request.endpoint]
        if hasattr(view_function, 'view_class'):
            view_class = view_function.view_class
            view_method = getattr(view_class, request.method.lower(), None)
            if view_method:
                if(lockProvisioning == False):
                    return None
                if request.method == 'GET' and not getattr(view_method, 'auth_required', False):
                    return None
                elif request.method in ['POST', 'PUT', 'PATCH', 'DELETE'] and not getattr(view_method, 'no_auth_required', False):
                    pass
                else:
                    return None

        if 'Provisioning-Key' not in request.headers or request.headers['Provisioning-Key'] != config['hss']['provisioning_key']:
            return {'Result': 'Unauthorized - Provisioning-Key Invalid'}, 401
    return None

def handle_exception(e):

    logTool.log(service='API', level='error', message=f"[API] An error occurred: {e}", redisClient=redisMessaging)
    response_json = {'result': 'Failed'}

    if isinstance(e, sqlalchemy.exc.SQLAlchemyError):
        response_json['reason'] = f'A database integrity error occurred: {e}'
        return response_json, 400
    elif isinstance(e, ValueError):
        error_message = str(e)
        if "OperationalError" in error_message:
            response_json['reason'] = f'A database operational error occurred: {e}'
            return response_json, 400
        if "IntegrityError" in error_message:
            response_json['reason'] = f'A database integrity error occurred: {e}'
            return response_json, 400
        if "CSV file does not exist" in error_message:
            response_json['reason'] = f'EIR CSV file is not defined / does not exist'
            return response_json, 410
    else:
        response_json['reason'] = f'An internal server error occurred: {e}'
        logTool.log(service='API', level='error', message=f"[API] Additional Error Information: {traceback.format_exc()}\n{sys.exc_info()[2]}", redisClient=redisMessaging)
        return response_json, 500

apiService.before_request(auth_before_request)

@apiService.errorhandler(404)
def page_not_found(e):
    return  {"Result": "Not Found"}, 404

@apiService.after_request
def apply_caching(response):
    response.headers["HSS"] = str(config['hss']['OriginHost'])
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,DELETE,PATCH,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, Content-Length, X-Requested-With, Provisioning-Key"
    return response

@ns_apn.route('/<string:apn_id>')
class PyHSS_APN_Get(Resource):
    def get(self, apn_id):
        '''Get all APN data for specified APN ID'''
        try:
            apn_data = databaseClient.GetObj(APN, apn_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, apn_id):
        '''Delete all APN data for specified APN ID'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(APN, apn_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_apn.doc('Update APN Object')
    @ns_apn.expect(APN_model)
    def patch(self, apn_id):
        '''Update APN data for specified APN ID'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            apn_data = databaseClient.UpdateObj(APN, json_data, apn_id, False, operation_id)

            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)   

@ns_apn.route('/')
class PyHSS_APN(Resource):
    @ns_apn.doc('Create APN Object')
    @ns_apn.expect(APN_model)
    def put(self):
        '''Create new APN'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            apn_id = databaseClient.CreateObj(APN, json_data, False, operation_id)

            return apn_id, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_apn.route('/list')
class PyHSS_OAM_All_APNs(Resource):
    @ns_apn.expect(paginatorParser)
    def get(self):
        '''Get all APNs'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(APN, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/<string:auc_id>')
class PyHSS_AUC_Get(Resource):
    def get(self, auc_id):
        '''Get all AuC data for specified AuC ID'''
        try:
            auc_data = databaseClient.GetObj(AUC, auc_id)

            if not insecureAuc:
                auc_data = databaseClient.Sanitize_Keys(auc_data)
            return auc_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, auc_id):
        '''Delete all AUC data for specified AUC ID'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(AUC, auc_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_auc.doc('Update AUC Object')
    @ns_auc.expect(AUC_model)
    def patch(self, auc_id):
        '''Update AuC data for specified AuC ID'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            auc_data = databaseClient.UpdateObj(AUC, json_data, auc_id, False, operation_id)
            if not insecureAuc:
                auc_data = databaseClient.Sanitize_Keys(auc_data)
            
            return auc_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/iccid/<string:iccid>')
class PyHSS_AUC_Get_ICCID(Resource):
    def get(self, iccid):
        '''Get all AuC data for specified ICCID'''
        try:
            auc_data = databaseClient.Get_AuC(iccid=iccid)
            if not insecureAuc:
                auc_data = databaseClient.Sanitize_Keys(auc_data)
            return auc_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/imsi/<string:imsi>')
class PyHSS_AUC_Get_IMSI(Resource):
    def get(self, imsi):
        '''Get all AuC data for specified IMSI'''
        try:
            auc_data = databaseClient.Get_AuC(imsi=imsi)
            if not insecureAuc:
                auc_data = databaseClient.Sanitize_Keys(auc_data)
            return auc_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/')
class PyHSS_AUC(Resource):
    @ns_auc.doc('Create AUC Object')
    @ns_auc.expect(AUC_model)
    def put(self):
        '''Create new AUC'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(AUC, json_data, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/list')
class PyHSS_AUC_All(Resource):
    @ns_auc.expect(paginatorParser)
    def get(self):
        '''Get all AuC Data (except keys)'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(AUC, args['page'], args['page_size'])
            if not insecureAuc:
                sanitizedData = []
                for aucRecord in data:
                    databaseClient.Sanitize_Keys(aucRecord)
                    sanitizedData.append(aucRecord)
                return sanitizedData
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/eap_aka/plmn/<string:plmn>/imsi/<string:imsi>')
class PyHSS_AUC_Get_EAP_AKA_Vectors(Resource):
    def get(self, imsi, plmn):
        '''Get EAP-AKA vectors for specified IMSI and PLMN'''
        try:
            #Get data from AuC
            auc_data = databaseClient.Get_AuC(imsi=imsi)
            plmn = diameterClient.EncodePLMN(mcc=plmn[0:3], mnc=plmn[3:])
            vector_dict = databaseClient.Get_Vectors_AuC(auc_data['auc_id'], action='eap_aka', plmn=plmn)
            return vector_dict, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/aka/vector_count/<string:vector_count>/imsi/<string:imsi>')
class PyHSS_AUC_Get_AKA_Vectors(Resource):
    def get(self, imsi, vector_count):
        '''Get AKA vectors for specified IMSI and PLMN'''
        try:
            #Get data from AuC
            auc_data = databaseClient.Get_AuC(imsi=imsi)
            plmn = diameterClient.EncodePLMN(mcc=config['hss']['MCC'], mnc=config['hss']['MNC'])
            vector_dict = databaseClient.Get_Vectors_AuC(auc_data['auc_id'], action='aka', plmn=plmn, requested_vectors=int(vector_count))
            return vector_dict, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_auc.route('/aka/resync/imsi/<string:imsi>/auts/<string:auts>/rand/<string:rand>')
class PyHSS_AUC_Get_AKA_Vectors_Resync(Resource):
    def get(self, imsi, auts, rand):
        '''do SQN resync'''
        try:
            #Get data from AuC
            auc_data = databaseClient.Get_AuC(imsi=imsi)
            rand = binascii.unhexlify(rand)
            vector_dict = databaseClient.Get_Vectors_AuC(auc_data['auc_id'], action='sqn_resync', auts=auts, rand=rand)
            return vector_dict, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/<string:subscriber_id>')
class PyHSS_SUBSCRIBER_Get(Resource):
    def get(self, subscriber_id):
        '''Get all SUBSCRIBER data for specified subscriber_id'''
        try:
            apn_data = databaseClient.GetObj(SUBSCRIBER, subscriber_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, subscriber_id):
        '''Delete all data for specified subscriber_id'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            subscriber_data = databaseClient.GetObj(SUBSCRIBER, subscriber_id)
            imsi = subscriber_data.get('imsi')
            warnings = []
            if imsi:
                warnings = teardown_subscriber_and_relay(imsi, ['pcrf', 'epc', 'ims', 'swx'])
            data = databaseClient.DeleteObj(SUBSCRIBER, subscriber_id, False, operation_id)
            if warnings:
                data = dict(data) if isinstance(data, dict) else {'result': data}
                data['warnings'] = warnings
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_subscriber.doc('Update SUBSCRIBER Object')
    @ns_subscriber.expect(SUBSCRIBER_model)
    def patch(self, subscriber_id):
        '''Update SUBSCRIBER data for specified subscriber_id'''
        try:
            json_data = request.get_json(force=True)
            try:
                normalize_msisdn_input(json_data)
            except InvalidMsisdnError as validation_error:
                return {"error": str(validation_error)}, 400
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)

            existing_subscriber = databaseClient.GetObj(SUBSCRIBER, subscriber_id)
            data = databaseClient.UpdateObj(SUBSCRIBER, json_data, subscriber_id, False, operation_id)
            warnings = []

            #If the subscriber is enabled, trigger an ISD in 2G
            if 'enabled' in json_data and json_data['enabled'] == True:
                update_event = databaseClient.Get_Gsup_SubscriberInfo(json_data.get('imsi', existing_subscriber.get('imsi')))
                redisMessaging.sendMessage('subscriber_update', update_event.model_dump_json())

            #If the "enabled" flag on the subscriber is now disabled, tear down live sessions
            if 'enabled' in json_data and json_data['enabled'] == False:
                imsi = data.get('imsi') or existing_subscriber.get('imsi')
                if imsi:
                    warnings = teardown_subscriber_and_relay(imsi, ['pcrf', 'epc', 'ims', 'swx'])

            if warnings:
                data = dict(data) if isinstance(data, dict) else {'result': data}
                data['warnings'] = warnings
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/')
class PyHSS_SUBSCRIBER(Resource):
    @ns_subscriber.doc('Create SUBSCRIBER Object')
    @ns_subscriber.expect(SUBSCRIBER_model)
    def put(self):
        '''Create new SUBSCRIBER'''
        try:
            json_data = request.get_json(force=True)
            try:
                normalize_msisdn_input(json_data)
            except InvalidMsisdnError as validation_error:
                return {"error": str(validation_error)}, 400
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(SUBSCRIBER, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/imsi/<string:imsi>')
class PyHSS_SUBSCRIBER_IMSI(Resource):
    def get(self, imsi):
        '''Get data for IMSI'''
        try:
            data = databaseClient.Get_Subscriber(imsi=imsi, get_attributes=True)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/msisdn/<string:msisdn>')
class PyHSS_SUBSCRIBER_MSISDN(Resource):
    def get(self, msisdn):
        '''Get data for MSISDN'''
        try:
            # Accept both '+E.164' and digits-only lookups (storage is digits-only)
            data = databaseClient.Get_Subscriber(msisdn=msisdn.lstrip('+'), get_attributes=True)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/list')
class PyHSS_SUBSCRIBER_All(Resource):
    @ns_subscriber.expect(paginatorParser)
    def get(self):
        '''Get all Subscribers'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(SUBSCRIBER, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/routing/')
class PyHSS_SUBSCRIBER_ROUTING_Create(Resource):
    @ns_ims_subscriber.doc('Create Subscriber Routing Object')
    @ns_ims_subscriber.expect(SUBSCRIBER_ROUTING_model)
    def put(self):
        '''Create new Subscriber Routing Binding'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(SUBSCRIBER_ROUTING, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/routing/<string:subscriber_id>/<string:apn_id>')
class PyHSS_SUBSCRIBER_SUBSCRIBER_ROUTING(Resource):
    def get(self, subscriber_id, apn_id):
        '''Get Subscriber Routing for specified subscriber_id & apn_id'''
        try:
            apn_data = databaseClient.Get_SUBSCRIBER_ROUTING(subscriber_id, apn_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, subscriber_id, apn_id):
        '''Delete Subscriber Routing binding for specified subscriber_id & apn_id'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            apn_data = databaseClient.Get_SUBSCRIBER_ROUTING(subscriber_id, apn_id)
            data = databaseClient.DeleteObj(SUBSCRIBER_ROUTING, apn_data['subscriber_routing_id'], False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber.route('/routing/<string:subscriber_routing_id>')
class PyHSS_SUBSCRIBER_SUBSCRIBER_ROUTING(Resource):
    @ns_subscriber.doc('Update SUBSCRIBER_ROUTING Object')
    @ns_subscriber.expect(SUBSCRIBER_ROUTING_model)
    def patch(self, subscriber_routing_id):
        '''Update SUBSCRIBER data for specified subscriber_routing_id'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(SUBSCRIBER_ROUTING, json_data, subscriber_routing_id, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ims_subscriber.route('/<string:ims_subscriber_id>')
class PyHSS_IMS_SUBSCRIBER_Get(Resource):
    def get(self, ims_subscriber_id):
        '''Get all SUBSCRIBER data for specified ims_subscriber_id'''
        try:
            apn_data = databaseClient.GetObj(IMS_SUBSCRIBER, ims_subscriber_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, ims_subscriber_id):
        '''Delete all data for specified ims_subscriber_id'''
        try:
            # Get subscriber data before deletion to know which ENUM entries to remove
            subscriber_data = databaseClient.GetObj(IMS_SUBSCRIBER, ims_subscriber_id)
            msisdn = subscriber_data.get('msisdn')
            msisdn_list = subscriber_data.get('msisdn_list')
            imsi = subscriber_data.get('imsi')
            warnings = []
            if imsi:
                warnings = teardown_subscriber_and_relay(imsi, ['ims'])

            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(IMS_SUBSCRIBER, ims_subscriber_id, False, operation_id)

            # Delete ENUM entries after subscriber deletion
            try:
                enumClient.delete_enum_entries(msisdn=msisdn, msisdn_list=msisdn_list)
            except ENUMManagementError as enum_error:
                # In strict mode, log the error but don't fail - subscriber is already deleted
                logTool.log(service='API', level='error', message=f"[API] ENUM deletion failed after subscriber deletion: {enum_error}", redisClient=redisMessaging)

            if warnings:
                data = dict(data) if isinstance(data, dict) else {'result': data}
                data['warnings'] = warnings
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_ims_subscriber.doc('Update IMS SUBSCRIBER Object')
    @ns_ims_subscriber.expect(IMS_SUBSCRIBER_model)
    def patch(self, ims_subscriber_id):
        '''Update IMS SUBSCRIBER data for specified ims_subscriber'''
        try:
            json_data = request.get_json(force=True)
            try:
                normalize_msisdn_input(json_data)
            except InvalidMsisdnError as validation_error:
                return {"error": str(validation_error)}, 400

            # Get current subscriber data before update to compare MSISDNs
            old_subscriber = databaseClient.GetObj(IMS_SUBSCRIBER, ims_subscriber_id)
            old_msisdn = old_subscriber.get('msisdn')
            old_msisdn_list = old_subscriber.get('msisdn_list')

            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(IMS_SUBSCRIBER, json_data, ims_subscriber_id, False, operation_id)

            # Update ENUM entries if MSISDNs changed
            new_msisdn = data.get('msisdn')
            new_msisdn_list = data.get('msisdn_list')
            try:
                enumClient.update_enum_entries(
                    old_msisdn=old_msisdn,
                    old_msisdn_list=old_msisdn_list,
                    new_msisdn=new_msisdn,
                    new_msisdn_list=new_msisdn_list
                )
            except ENUMManagementError as enum_error:
                # In strict mode, log but don't fail - subscriber is already updated
                logTool.log(service='API', level='error', message=f"[API] ENUM update failed: {enum_error}", redisClient=redisMessaging)

            # Push the changed Sh repository data to subscribed ASs (TS 29.328 6.1.4)
            if 'xcap_profile' in json_data or 'sh_profile' in json_data:
                try:
                    notify_subscribed_application_servers(data)
                except Exception as pnr_error:
                    logTool.log(service='API', level='error', message=f"[API] Sh PNR notification failed: {pnr_error}", redisClient=redisMessaging)

            warnings = []
            if ims_profile_push_fields_changed(old_subscriber, json_data):
                warnings = push_ims_profile_and_relay(data)

            if warnings:
                data = dict(data) if isinstance(data, dict) else {'result': data}
                data['warnings'] = warnings
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ims_subscriber.route('/')
class PyHSS_IMS_SUBSCRIBER(Resource):
    @ns_ims_subscriber.doc('Create IMS SUBSCRIBER Object')
    @ns_ims_subscriber.expect(IMS_SUBSCRIBER_model)
    def put(self):
        '''Create new IMS SUBSCRIBER'''
        try:
            json_data = request.get_json(force=True)
            try:
                normalize_msisdn_input(json_data)
            except InvalidMsisdnError as validation_error:
                return {"error": str(validation_error)}, 400
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(IMS_SUBSCRIBER, json_data, False, operation_id)

            # Create ENUM entries for the new subscriber
            try:
                enumClient.create_enum_entries(
                    msisdn=json_data.get('msisdn'),
                    msisdn_list=json_data.get('msisdn_list')
                )
            except ENUMManagementError as enum_error:
                # In strict mode, ENUM errors are raised - rollback subscriber creation
                logTool.log(service='API', level='error', message=f"[API] ENUM creation failed, rolling back subscriber: {enum_error}", redisClient=redisMessaging)
                databaseClient.DeleteObj(IMS_SUBSCRIBER, data.get('ims_subscriber_id'), False, operation_id)
                return {"error": f"ENUM creation failed: {str(enum_error)}"}, 500

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ims_subscriber.route('/ims_subscriber_msisdn/<string:msisdn>')
class PyHSS_IMS_SUBSCRIBER_MSISDN(Resource):
    def get(self, msisdn):
        '''Get IMS data for MSISDN'''
        try:
            # Accept both '+E.164' and digits-only lookups (storage is digits-only)
            data = databaseClient.Get_IMS_Subscriber(msisdn=msisdn.lstrip('+'))
            return data, 200
        except Exception as E:
            print("Flask Exception: " + str(E))
            return handle_exception(E), 400

@ns_ims_subscriber.route('/ims_subscriber_imsi/<string:imsi>')
class PyHSS_IMS_SUBSCRIBER_IMSI(Resource):
    def get(self, imsi):
        '''Get IMS data for imsi'''
        try:
            data = databaseClient.Get_IMS_Subscriber(imsi=imsi)
            return data, 200
        except Exception as E:
            print("Flask Exception: " + str(E))
            return handle_exception(E), 400

@ns_ims_subscriber.route('/list')
class PyHSS_IMS_Subscriber_All(Resource):
    @ns_ims_subscriber.expect(paginatorParser)
    def get(self):
        '''Get all IMS Subscribers'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(IMS_SUBSCRIBER, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E), 400

@ns_roaming.route('/rule/')
class PyHSS_ROAMING_RULE(Resource):
    @ns_roaming.doc('Create Roaming Rule')
    @ns_roaming.expect(ROAMING_RULE_model)
    def put(self):
        '''Create new Roaming Rule'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(ROAMING_RULE, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_roaming.route('/rule/list')
class PyHSS_ROAMING_RULE_All(Resource):
    @ns_tft.expect(paginatorParser)
    def get(self):
        '''Get all roaming rules'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(ROAMING_RULE, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_roaming.route('/rule/<string:roaming_rule_id>')
class PyHSS_ROAMING_RULE_Get(Resource):
    def get(self, roaming_rule_id):
        '''Get all data for specified roaming_rule_id'''
        try:
            apn_data = databaseClient.GetObj(ROAMING_RULE, roaming_rule_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, roaming_rule_id):
        '''Delete all data for specified roaming_rule_id'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(ROAMING_RULE, roaming_rule_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_roaming.doc('Update ROAMING_RULE Object')
    @ns_roaming.expect(ROAMING_RULE_model)
    def patch(self, roaming_rule_id):
        '''Update data for specified roaming_rule_id'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(ROAMING_RULE, json_data, roaming_rule_id, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)
        
@ns_roaming.route('/network/')
class PyHSS_ROAMING_NETWORK(Resource):
    @ns_roaming.doc('Create Roaming Network')
    @ns_roaming.expect(ROAMING_NETWORK_model)
    def put(self):
        '''Create new Roaming Network'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(ROAMING_NETWORK, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_roaming.route('/network/list')
class PyHSS_ROAMING_NETWORK_All(Resource):
    @ns_tft.expect(paginatorParser)
    def get(self):
        '''Get all roaming networks'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(ROAMING_NETWORK, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_roaming.route('/network/<string:roaming_network_id>')
class PyHSS_ROAMING_NETWORK_Get(Resource):
    def get(self, roaming_network_id):
        '''Get all data for specified roaming_network_id'''
        try:
            apn_data = databaseClient.GetObj(ROAMING_NETWORK, roaming_network_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, roaming_network_id):
        '''Delete all data for specified roaming_network_id'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(ROAMING_NETWORK, roaming_network_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_roaming.doc('Update ROAMING_NETWORK Object')
    @ns_roaming.expect(ROAMING_NETWORK_model)
    def patch(self, roaming_network_id):
        '''Update data for specified roaming_network_id'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(ROAMING_NETWORK, json_data, roaming_network_id, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_tft.route('/<string:tft_id>')
class PyHSS_TFT_Get(Resource):
    def get(self, tft_id):
        '''Get all TFT data for specified tft_id'''
        try:
            apn_data = databaseClient.GetObj(TFT, tft_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, tft_id):
        '''Delete all data for specified tft_id'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(TFT, tft_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_ims_subscriber.doc('Update IMS tft_id Object')
    @ns_ims_subscriber.expect(TFT_model)
    def patch(self, tft_id):
        '''Update tft_id data for specified tft_id'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(TFT, json_data, tft_id, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_tft.route('/')
class PyHSS_TFT(Resource):
    @ns_tft.doc('Create TFT Object')
    @ns_tft.expect(TFT_model)
    def put(self):
        '''Create new TFT'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(TFT, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_tft.route('/list')
class PyHSS_TFT_All(Resource):
    @ns_tft.expect(paginatorParser)
    def get(self):
        '''Get all TFTs'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(TFT, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)
        
@ns_charging_rule.route('/<string:charging_rule_id>')
class PyHSS_Charging_Rule_Get(Resource):
    def get(self, charging_rule_id):
        '''Get all Charging Rule data for specified charging_rule_id'''
        try:
            apn_data = databaseClient.GetObj(CHARGING_RULE, charging_rule_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, charging_rule_id):
        '''Delete all data for specified charging_rule_id'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(CHARGING_RULE, charging_rule_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_charging_rule.doc('Update charging_rule_id Object')
    @ns_charging_rule.expect(CHARGING_RULE_model)
    def patch(self, charging_rule_id):
        '''Update charging_rule_id data for specified charging_rule_id'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(CHARGING_RULE, json_data, charging_rule_id, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_charging_rule.route('/')
class PyHSS_Charging_Rule(Resource):
    @ns_charging_rule.doc('Create ChargingRule Object')
    @ns_charging_rule.expect(CHARGING_RULE_model)
    def put(self):
        '''Create new ChargingRule'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(CHARGING_RULE, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_charging_rule.route('/list')
class PyHSS_Charging_Rule_All(Resource):
    @ns_charging_rule.expect(paginatorParser)
    def get(self):
        '''Get all Charging Rules'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(CHARGING_RULE, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_eir.route('/<string:eir_id>')
class PyHSS_EIR_Get(Resource):
    def get(self, eir_id):
        '''Get all EIR data for specified eir_id'''
        try:
            eir_data = databaseClient.GetObj(EIR, eir_id)
            return eir_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, eir_id):
        '''Delete all data for specified eir_data'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(EIR, eir_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_eir.doc('Update eir Object')
    @ns_eir.expect(EIR_model)
    def patch(self, eir_id):
        '''Update eir_id data for specified eir_id'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(EIR, json_data, eir_id, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_eir.route('/')
class PyHSS_EIR(Resource):
    @ns_eir.doc('Create EIR Object')
    @ns_eir.expect(EIR_model)
    def put(self):
        '''Create new EIR Rule'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(EIR, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_eir.route('/eir_history/<string:attribute>')
class PyHSS_EIR_HISTORY(Resource):
    def get(self, attribute):
        '''Get history for IMSI or IMEI'''
        try:
            data = databaseClient.Get_IMEI_IMSI_History(attribute=attribute)
            #Add device info for each entry
            data_w_device_info = []
            for record in data:
                record['imei_result'] = databaseClient.getTacDataFromImei(imei=str(record['imei']))
                data_w_device_info.append(record)
            return data_w_device_info, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, attribute):
        '''Get Delete for IMSI or IMEI'''
        try:
            data = databaseClient.Get_IMEI_IMSI_History(attribute=attribute)
            for record in data:
                databaseClient.DeleteObj(IMSI_IMEI_HISTORY, record['imsi_imei_history_id'])
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_eir.route('/eir_history/list')
class PyHSS_EIR_All_History(Resource):
    @ns_eir.expect(paginatorParser)
    def get(self):
        '''Get EIR history for all subscribers'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(IMSI_IMEI_HISTORY, args['page'], args['page_size'])
            for record in data:
                record['imsi'] = record['imsi_imei'].split(',')[0]
                record['imei'] = record['imsi_imei'].split(',')[1]
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_eir.route('/list')
class PyHSS_EIR_All(Resource):
    @ns_eir.expect(paginatorParser)
    def get(self):
        '''Get all EIR Rules'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(EIR, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_eir.route('/lookup_imei/<string:imei>')
class PyHSS_EIR_TAC(Resource):
    def get(self, imei):
        '''Get Device Info from IMEI'''
        try:
            data = databaseClient.getTacDataFromImei(imei=imei)
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber_attributes.route('/list')
class PyHSS_Subscriber_Attributes_All(Resource):
    @ns_subscriber_attributes.expect(paginatorParser)
    def get(self):
        '''Get all Subscriber Attributes'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(SUBSCRIBER_ATTRIBUTES, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber_attributes.route('/<string:subscriber_id>')
class PyHSS_Attributes_Get(Resource):
    def get(self, subscriber_id):
        '''Get all attributes / values for specified Subscriber ID'''
        try:
            apn_data = databaseClient.Get_Subscriber_Attributes(subscriber_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber_attributes.route('/<string:subscriber_attributes_id>')
class PyHSS_Attributes_Get(Resource):
    def delete(self, subscriber_attributes_id):
        '''Delete specified attribute ID'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(SUBSCRIBER_ATTRIBUTES, subscriber_attributes_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_subscriber_attributes.doc('Update Attribute Object')
    @ns_subscriber_attributes.expect(SUBSCRIBER_ATTRIBUTES_model)
    def patch(self, subscriber_attributes_id):
        '''Update data for specified attribute ID'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(SUBSCRIBER_ATTRIBUTES, json_data, subscriber_attributes_id, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_subscriber_attributes.route('/')
class PyHSS_Attributes(Resource):
    @ns_subscriber_attributes.doc('Create Attribute Object')
    @ns_subscriber_attributes.expect(SUBSCRIBER_ATTRIBUTES_model)
    def put(self):
        '''Create new Attribute for Subscriber'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.CreateObj(SUBSCRIBER_ATTRIBUTES, json_data, False, operation_id)

            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_operation_log.route('/list')
class PyHSS_Operation_Log_List(Resource):
    @ns_operation_log.expect(paginatorParser)
    def get(self):
        '''Get all Operation Logs'''
        try:
            args = paginatorParser.parse_args()
            OperationLogs = databaseClient.get_all_operation_logs(args['page'], args['page_size'])
            return OperationLogs, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_operation_log.route('/last')
class PyHSS_Operation_Log_Last(Resource):
    def get(self):
        '''Get the most recent Operation Log'''
        try:
            OperationLogs = databaseClient.get_last_operation_log()
            return OperationLogs, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_operation_log.route('/list/table/<string:table_name>')
class PyHSS_Operation_Log_List_Table(Resource):
    @ns_operation_log.expect(paginatorParser)
    def get(self, table_name):
        '''Get all Operation Logs for a given table'''
        try:
            args = paginatorParser.parse_args()
            OperationLogs = databaseClient.get_all_operation_logs_by_table(table_name, args['page'], args['page_size'])
            return OperationLogs, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/diameter_peers')
class PyHSS_OAM_Peers(Resource):
    def get(self):
        '''Get active Diameter Peers'''
        try:
            diameterPeers = redisMessaging.getAllHashData("diameterPeers", usePrefix=True, prefixHostname=originHostname, prefixServiceName='diameter')
            return diameterPeers, 200
        except Exception as E:
            logTool.log(service='API', level='error', message=f"[API] An error occurred: {traceback.format_exc()}", redisClient=redisMessaging)
            print(E)
            return handle_exception(E)

@ns_oam.route('/deregister/<string:imsi>')
class PyHSS_OAM_Deregister(Resource):
    def get(self, imsi):
        '''Deregisters a given IMSI from the entire network.'''
        try:
            subscriberInfo = databaseClient.Get_Subscriber(imsi=str(imsi))
            imsSubscriberInfo = databaseClient.Get_IMS_Subscriber(imsi=str(imsi))
            subscriberId = subscriberInfo.get('subscriber_id', None)

            warnings = teardown_subscriber_and_relay(imsi, ['pcrf', 'epc', 'ims', 'swx'])

            # If a subscriber has an active serving apn, grab the pcrf session id for that apn and send a CCR-T, then a Registration Termination Request to the serving pgw peer.
            if subscriberId is not None:
                servingApns = databaseClient.Get_Serving_APNs(subscriber_id=subscriberId)
                if len(servingApns.get('apns', {})) > 0:
                    for apnKey, apnDict in servingApns['apns'].items():
                        pcrfSessionId = None
                        servingPgwPeer = None
                        servingPgwRealm = None
                        servingPgw = None
                        for apnDataKey, apnDataValue in servingApns['apns'][apnKey].items():
                            if apnDataKey == 'pcrf_session_id':
                                pcrfSessionId = apnDataValue
                            if apnDataKey == 'serving_pgw_peer':
                                servingPgwPeer = apnDataValue
                            if apnDataKey == 'serving_pgw_realm':
                                servingPgwRealm = apnDataValue
                            if apnDataKey == 'serving_pgw':
                                servingPgwRealm = apnDataValue
                            
                        if pcrfSessionId is not None and servingPgwPeer is not None and servingPgwRealm is not None and servingPgw is not None:
                            if ';' in servingPgwPeer:
                                servingPgwPeer = servingPgwPeer.split(';')[0]

                            diameterClient.sendDiameterRequest(
                            requestType='CCR',
                            hostname=servingPgwPeer,
                            imsi=imsi,
                            destinationHost=servingPgw, 
                            destinationRealm=servingPgwRealm,
                            ccr_type=3,
                            sessionId=pcrfSessionId,
                            domain=servingPgwRealm
                            )

                            diameterClient.sendDiameterRequest(
                            requestType='RTR',
                            hostname=servingPgwPeer,
                            imsi=imsi,
                            destinationHost=servingPgw, 
                            destinationRealm=servingPgwRealm, 
                            domain=servingPgwRealm
                            )
                        
                        diameterClient.broadcastDiameterRequest(
                            requestType='CCR',
                            peerType='PGW',
                            imsi=imsi,
                            destinationHost=servingPgw, 
                            destinationRealm=servingPgwRealm,
                            ccr_type=3,
                            sessionId = pcrfSessionId,
                            domain=servingPgwRealm
                            )
                        
                        diameterClient.broadcastDiameterRequest(
                        requestType='RTR',
                        peerType='PGW',
                        imsi=imsi,
                        destinationHost=servingPgw, 
                        destinationRealm=servingPgwRealm, 
                        domain=servingPgwRealm
                        )

                        databaseClient.Update_Serving_APN(imsi=imsi, apn=apnKey, pcrf_session_id=None, serving_pgw=None, subscriber_routing='')

            subscriberInfo = databaseClient.Get_Subscriber(imsi=str(imsi))
            imsSubscriberInfo = databaseClient.Get_IMS_Subscriber(imsi=str(imsi))
            servingApns = databaseClient.Get_Serving_APNs(subscriber_id=subscriberId)

            response = {'subscriber': subscriberInfo, 'ims_subscriber': imsSubscriberInfo, 'pcrf': servingApns}
            if warnings:
                response['warnings'] = warnings
            return response, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route("/ping")
class PyHSS_OAM_Ping(Resource):
    def get(self):
        """Ping the API to check if it's alive"""
        try:
            apiPingResponse = {"result": "OK"}
            return apiPingResponse, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/rollback_operation/last')
class PyHSS_OAM_Rollback_Last(Resource):
    @auth_required
    def get(self):
        '''Undo the last Insert/Update/Delete operation'''
        try:
            RollbackResponse = databaseClient.rollback_last_change()
            return RollbackResponse, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/rollback_operation/<string:operation_id>')
class PyHSS_OAM_Rollback_Last_Table(Resource):
    @auth_required
    def get(self, operation_id):
        '''Undo the last Insert/Update/Delete operation for a given operation id'''
        try:
            RollbackResponse = databaseClient.rollback_change_by_operation_id(operation_id)
            return RollbackResponse, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/serving_subs')
class PyHSS_OAM_Serving_Subs(Resource):
    def get(self):
        '''Get all Subscribers served by HSS'''
        try:
            data = databaseClient.Get_Served_Subscribers()
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/serving_subs_pcrf')
class PyHSS_OAM_Serving_Subs_PCRF(Resource):
    def get(self):
        '''Get all Subscribers served by PCRF'''
        try:
            data = databaseClient.Get_Served_PCRF_Subscribers()
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/serving_subs_ims')
class PyHSS_OAM_Serving_Subs_IMS(Resource):
    def get(self):
        '''Get all Subscribers served by IMS'''
        try:
            data = databaseClient.Get_Served_IMS_Subscribers()
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/serving_subs_ims/count')
class PyHSS_OAM_Serving_Subs_IMS_Count(Resource):
    def get(self):
        '''Get the number of Subscribers served by IMS'''
        try:
            return {'count': databaseClient.Count_Served_IMS_Subscribers()}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/bulk/subscribers')
class PyHSS_OAM_Bulk_Subscribers(Resource):
    @ns_oam.expect(bulkRangeParser)
    def get(self):
        '''Count the AuC, Subscriber and IMS Subscriber rows inside an IMSI range'''
        try:
            args = bulkRangeParser.parse_args()
            return databaseClient.Bulk_Count_Subscribers(args['imsi_start'], args['count']), 200
        except ValueError as E:
            return {'result': 'Failed', 'reason': str(E)}, 400
        except Exception as E:
            print(E)
            return handle_exception(E)

    def put(self):
        '''Create consecutive subscribers (AuC + Subscriber + IMS Subscriber) in one transaction'''
        try:
            json_data = request.get_json(force=True)
            count = int(json_data.get('count', 0))
            if count > BULK_SUBSCRIBERS_MAX:
                return {'result': 'Failed', 'reason': f'count exceeds {BULK_SUBSCRIBERS_MAX} per request'}, 400
            msisdn_start = str(json_data.get('msisdn_start', '')).strip()
            if not E164_MSISDN_PATTERN.match(msisdn_start):
                return {'result': 'Failed', 'reason': "msisdn_start must be a global E.164 number with leading '+'"}, 400
            data = databaseClient.Bulk_Create_Subscribers(
                imsi_start=json_data.get('imsi_start', ''),
                msisdn_start=msisdn_start[1:],
                count=count,
                auc=json_data.get('auc') or {},
                subscriber=json_data.get('subscriber') or {},
                ims_subscriber=json_data.get('ims_subscriber') or {},
            )
            return {'created': len(data), 'subscribers': data}, 200
        except ValueError as E:
            return {'result': 'Failed', 'reason': str(E)}, 400
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_oam.expect(bulkRangeParser)
    def delete(self):
        '''Delete every AuC, Subscriber and IMS Subscriber row inside an IMSI range (no network deregistration)'''
        try:
            args = bulkRangeParser.parse_args()
            if args['count'] > BULK_SUBSCRIBERS_MAX:
                return {'result': 'Failed', 'reason': f'count exceeds {BULK_SUBSCRIBERS_MAX} per request'}, 400
            return {'deleted': databaseClient.Bulk_Delete_Subscribers(args['imsi_start'], args['count'])}, 200
        except ValueError as E:
            return {'result': 'Failed', 'reason': str(E)}, 400
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/reconcile/ims/<string:imsi>')
class PyHSS_OAM_Reconcile_IMS(Resource):
    def get(self, imsi):
        '''Get current location of IMS Subscriber from all linked HSS nodes'''
        response_dict = {}
        try:
            #Get local database result
            local_result = databaseClient.Get_IMS_Subscriber(imsi=imsi)
            response_dict['localhost'] = {}
            for keys in local_result:
                if 'cscf' in keys:
                    response_dict['localhost'][keys] = local_result[keys]

            #Get remote HSS results
            remote_peers = config.get('geored', {}).get('sync_endpoints', geored_check_updated_endpoints(config))
            for remote_HSS in remote_peers:
                print("Pulling data from remote HSS: " + str(remote_HSS))
                try:
                    response = requests.get(remote_HSS + '/ims_subscriber/ims_subscriber_imsi/' + str(imsi))
                    response_dict[remote_HSS] = {}
                    for keys in response.json():
                        if 'cscf' in keys:
                            response_dict[remote_HSS][keys] = response.json()[keys]
                except Exception as E:
                    print("Exception pulling from " + str(remote_HSS) + " " + str(E))
                    response_dict[remote_HSS] = str(E)
            mismatch_list = []
            #Compare to check they all agree
            for remote_HSS in response_dict:
                for comparitor_hss in response_dict:
                    try:
                        if (response_dict[remote_HSS]['scscf'] != response_dict[comparitor_hss]['scscf']):
                            print("\t Mismatch between " + str(remote_HSS) + " and " + str(comparitor_hss))
                            mismatch_record = {
                                str(remote_HSS) : response_dict[remote_HSS]['scscf'],
                                str(comparitor_hss) : response_dict[comparitor_hss]['scscf'],
                                }
                            mismatch_list.append(mismatch_record)
                    except:
                        continue
            print("mismatch_list: " + str(mismatch_list))
            response_dict['mismatches'] = mismatch_list
            return response_dict, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_oam.route('/reconcile/enum')
class PyHSS_OAM_Reconcile_ENUM(Resource):
    def get(self):
        '''Reconcile ENUM entries - recreate all NAPTR records from IMS subscriber database'''
        try:
            result = enumClient.reconcile_all(databaseClient)
            if result.get('status') == 'error':
                return result, 500
            return result, 200
        except Exception as E:
            print(E)
            logTool.log(service='API', level='error', message=f"[API] ENUM reconciliation failed: {traceback.format_exc()}", redisClient=redisMessaging)
            return handle_exception(E)

@ns_pcrf.route('/pcrf_subscriber/list')
class PyHSS_PCRF_Get_All_Served_Subscribers(Resource):
    def get(self):
        '''Get PCRF Data for all Subscribers'''
        try:
            """
            - Get all Serving APNs.

            - For each Serving APN:
              - The corresponding Subscriber is retrieved.
              - The Subscriber is added to the servedSubscribers dictionary, if it doesn't exist.
              - The Subscribers 'apn' key is initialized with blank dictionaries for each apn name, if the apn name doesn't exist already.
              - The Serving APN is added to the respective Subscriber 'apn' key.

            - The servedSubscribers dictionary is returned.
            """

            servedSubscribers = {}

            """
            Get all Serving APNs.
            """
            servingApns = databaseClient.GetAll(Serving_APN)

            """
            For each Serving APN:
            """
            for servingApn in servingApns:
                """
                The corresponding Subscriber is retrieved.
                """
                subscriberId = servingApn.get('subscriber_id', None)
                subscriber = databaseClient.GetObj(SUBSCRIBER, subscriberId)
                subscriberImsi = subscriber.get('imsi', None)
                servingApnId = servingApn.get('apn', None)
                apnObject = databaseClient.Get_APN(servingApnId)
                servingApnName = apnObject.get('apn')

                """
                The Subscriber is added to the servedSubscribers dictionary, if it doesn't exist.
                """
                if subscriberImsi not in servedSubscribers:
                    servedSubscribers[subscriberImsi] = databaseClient.Sanitize_Datetime(subscriber)

                """
                The Subscribers 'apn' key is initialized with blank dictionaries for each apn name, if the apn name doesn't exist already.
                """

                subscriberApnIds = subscriber['apn_list'].split(',')

                if 'apns' not in servedSubscribers[subscriberImsi]:
                    servedSubscribers[subscriberImsi]['apns'] = {}

                for subscriberApnId in subscriberApnIds:

                    apnData = databaseClient.Get_APN(subscriberApnId)
                    subscriberApnName = str(apnData['apn'])

                    if subscriberApnName in servedSubscribers[subscriberImsi]['apns']:
                        continue
                    else:
                        servedSubscribers[subscriberImsi]['apns'][subscriberApnName] = {}

                """
                The Serving APN is added to the respective Subscriber 'apn' key.
                """

                servedSubscribers[subscriberImsi]['apns'][servingApnName] = servingApn

            return servedSubscribers
        
        except Exception as E:
            print("Flask Exception: " + str(E))
            return handle_exception(traceback.format_exc())

@ns_pcrf.route('/pcrf_subscriber_imsi/<string:imsi>')
class PyHSS_OAM_Get_PCRF_Subscriber_all_APN(Resource):
    def get(self, imsi):
        '''Get PCRF Data for a Subscriber'''
        try:
            #ToDo - Move the mapping an APN name to an APN ID for a sub into the Database functions
            serving_sub_final = {}
            serving_sub_final['subscriber_data'] = {}
            serving_sub_final['apns'] = {}

            #Resolve Subscriber ID
            subscriber_data = databaseClient.Get_Subscriber(imsi=str(imsi))
            serving_sub_final['subscriber_data'] = databaseClient.Sanitize_Datetime(subscriber_data)

            #Split the APN list into a list
            apn_list = subscriber_data['apn_list'].split(',')
            #Remove the default APN from the list
            try:
                apn_list.remove(str(subscriber_data['default_apn']))
            except:
                print("Failed to remove default APN (" + str(subscriber_data['default_apn']) + " from APN List")
                pass
            
            #Add default APN in first position
            apn_list.insert(0, str(subscriber_data['default_apn']))

            #Get APN ID from APN
            for list_apn_id in apn_list:
                apn_data = databaseClient.Get_APN(list_apn_id)
                try:
                    serving_sub_final['apns'][str(apn_data['apn'])] = {}
                    serving_sub_final['apns'][str(apn_data['apn'])] = databaseClient.Sanitize_Datetime(databaseClient.Get_Serving_APN(subscriber_id=subscriber_data['subscriber_id'], apn_id=list_apn_id))
                except:
                    serving_sub_final['apns'][str(apn_data['apn'])] = {}
                    print("Failed to get Serving APN for APN ID " + str(list_apn_id))

            return serving_sub_final, 200
        except Exception as E:
            print("Flask Exception: " + str(E))
            
            return handle_exception(E)


@ns_pcrf.route('/pcrf_subscriber_imsi/<string:imsi>/<string:apn_id>')
class PyHSS_OAM_Get_PCRF_Subscriber(Resource):
    def get(self, imsi, apn_id):
        '''Get PCRF data'''
        try:
            #ToDo - Move the mapping an APN name to an APN ID for a sub into the Database functions
            apn_id_final = None

            #Resolve Subscriber ID
            subscriber_data = databaseClient.Get_Subscriber(imsi=str(imsi))

            #Split the APN list into a list
            apn_list = subscriber_data['apn_list'].split(',')
            #Remove the default APN from the list
            try:
                apn_list.remove(str(subscriber_data['default_apn']))
            except:
                print("Failed to remove default APN (" + str(subscriber_data['default_apn']) + " from APN List")
                pass
            #Add default APN in first position
            apn_list.insert(0, str(subscriber_data['default_apn']))

            #Get APN ID from APN
            for list_apn_id in apn_list:
                #Get each APN in List
                apn_data = databaseClient.Get_APN(list_apn_id)
                if str(apn_data['apn_id']).lower() == str(apn_id).lower():
                    apn_id_final = apn_data['apn_id']

            data = databaseClient.Get_Serving_APN(subscriber_id=subscriber_data['subscriber_id'], apn_id=apn_id_final)
            data = databaseClient.Sanitize_Datetime(data)
            return data, 200
        except Exception as E:
            print("Flask Exception: " + str(E))
            
            return handle_exception(E)

@ns_pcrf.route('/pcrf_serving_apn_ip/<string:ip_address>')
class PyHSS_PCRF_Get_Serving_APN_IP(Resource):
    def get(self, ip_address):
        '''Get Serving APN Data for an IP Address'''
        try:
            serving_apn_final = {}
            try:
                serving_apn = databaseClient.Get_Serving_APN_By_IP(str(ip_address))
            except:
                serving_apn = None
            if serving_apn:
                serving_apn_final = databaseClient.Sanitize_Datetime(serving_apn)
            return serving_apn_final, 200
        except Exception as E:
            print("Flask Exception: " + str(E))
            return handle_exception(E)

@ns_pcrf.route('/')
class PyHSS_PCRF(Resource):
    @ns_pcrf.doc('Push Charging Rule to a Subscriber')
    @ns_pcrf.expect(PCRF_Push_model)
    def put(self):
        '''Push predefined Charging Rule to Subscriber'''
    
        json_data = request.get_json(force=True)
        #Get IMSI
        subscriber_data = databaseClient.Get_Subscriber(imsi=str(json_data['imsi']))
        print("subscriber_data: " + str(subscriber_data))

        #Get PCRF Session
        servingApn = databaseClient.Get_Serving_APN(subscriber_id=subscriber_data['subscriber_id'], apn_id=json_data['apn_id'])          
        print("pcrf_session_data: " + str(servingApn))

        #Get Charging Rules
        ChargingRule = databaseClient.Get_Charging_Rule(json_data['charging_rule_id'])
        ChargingRule['apn_data'] = databaseClient.Get_APN(json_data['apn_id'])
        print("Got ChargingRule: " + str(ChargingRule))

        subscriberId = subscriber_data.get('subscriber_id', None)
        servingPgwPeer = servingApn.get('serving_pgw_peer', None).split(';')[0]
        servingPgw = servingApn.get('serving_pgw', None)
        servingPgwRealm = servingApn.get('serving_pgw_realm', None)
        pcrfSessionId = servingApn.get('pcrf_session_id', None)
        ueIp = servingApn.get('subscriber_routing', None)

        diameterResponse = diameterClient.sendDiameterRequest(
                requestType='RAR',
                hostname=servingPgwPeer,
                sessionId=pcrfSessionId,
                chargingRules=ChargingRule,
                ueIp=ueIp,
                servingPgw=servingPgw,
                servingRealm=servingPgwRealm
            )
        
        result = {"Result": "Successfully sent Gx RAR", "destinationClients": str(servingPgw)}
        return result, 200

@ns_pcrf.route('/clr_subscriber')
class PyHSS_PCRF_CLR_Subscriber(Resource):
    @ns_pcrf.doc('Trigger Cancel Location Request for a Subscriber')
    @ns_pcrf.expect(PCRF_PCSCF_Restoration_Subscriber_model)
    def put(self):
        '''Trigger CLR for a Subscriber'''

        try:        
            jsonData = request.get_json(force=True)
            #Get IMSI

            imsi = jsonData.get('imsi', None)
            msisdn = jsonData.get('msisdn', None)
            subscriberData = None
            imsSubscriberData = None

            if not imsi and not msisdn:
                result = {"Result": "Error: IMSI or MSISDN Required"}
                return result, 400
            
            if imsi:
                subscriberData = databaseClient.Get_Subscriber(imsi=imsi)
            else:
                imsSubscriberData = databaseClient.Get_IMS_Subscriber(msisdn=msisdn)
                imsi = imsSubscriberData.get('imsi', None)
                subscriberData = databaseClient.Get_Subscriber(imsi=imsi)
            
            try:
                servingMmePeer = subscriberData.get('serving_mme_peer').split(';')[0]
            except Exception as e:
                result = {"Result": "Error: Subscriber is not currently served by an MME"}
                return result, 400
            
            servingMmeRealm = subscriberData.get('serving_mme_realm', None)
            servingMme = subscriberData.get('serving_mme', None)

            diameterRequest = diameterClient.sendDiameterRequest(
                requestType='CLR',
                hostname=servingMmePeer,
                imsi=imsi, 
                DestinationHost=servingMme, 
                DestinationRealm=servingMmeRealm, 
                CancellationType=2,
                immediateReattach=True
            )

            if diameterRequest == '':
                result = {"Result": f"Unable to send Cancel Location Request via {servingMmePeer} for IMSI {imsi} - is the diameter peer connected?"}
                return result, 400
            
            result = {"Result": f"Successfully sent Cancel Location Request via {servingMmePeer} for IMSI {imsi}"}
            return result, 200

        except Exception as E:
            print("Flask Exception: " + str(E))
            result = {"Result": f"Unahndled error: {E}"}
            return result, 500

@ns_pcrf.route('/pcscf_restoration_subscriber')
class PyHSS_PCRF_PSCSF_Restoration_Subscriber(Resource):
    @ns_pcrf.doc('Trigger PCSCF Restoration for an IMS Subscriber')
    @ns_pcrf.expect(PCRF_PCSCF_Restoration_Subscriber_model)
    def put(self):
        '''Trigger PCSCF Restoration for an IMS Subscriber'''

        try:        
            jsonData = request.get_json(force=True)
            #Get IMSI

            imsi = jsonData.get('imsi', None)
            msisdn = jsonData.get('msisdn', None)

            if not imsi and not msisdn:
                result = {"Result": "Error: IMSI or MSISDN Required"}
                return result, 400
            
            if imsi:
                subscriberData = databaseClient.Get_Subscriber(imsi=imsi)
                imsSubscriberData = databaseClient.Get_IMS_Subscriber(imsi=imsi)
            else:
                imsSubscriberData = databaseClient.Get_IMS_Subscriber(msisdn=msisdn)
                subscriberData = databaseClient.Get_Subscriber(imsi=imsSubscriberData.get('imsi', None))
            
            try:
                servingMmePeer = subscriberData.get('serving_mme_peer').split(';')[0]
            except Exception as e:
                result = {"Result": "Error: Subscriber is not currently served by an MME"}
                return result, 400
            
            imsi = imsSubscriberData.get('imsi', None)
            servingMmeRealm = subscriberData.get('serving_mme_realm', None)
            servingMme = subscriberData.get('serving_mme', None)

            diameterRequest = diameterClient.sendDiameterRequest(
                requestType='CLR',
                hostname=servingMmePeer,
                imsi=imsi, 
                DestinationHost=servingMme, 
                DestinationRealm=servingMmeRealm, 
                CancellationType=2,
                immediateReattach=True
            )
            
            result = {"Result": f"Successfully sent PCSCF Restoration request via {servingMmePeer} for IMSI {imsi}"}
            return result, 200

        except Exception as E:
            print("Flask Exception: " + str(E))
            return handle_exception(E)

@ns_pcrf.route('/pcscf_restoration')
class PyHSS_PCRF_PSCSF_Restoration_Subscriber(Resource):
    @ns_pcrf.doc('Trigger PCSCF Restoration for all IMS Subscribers attached to PCSCF')
    @ns_pcrf.expect(PCRF_PCSCF_Restoration_model)
    def put(self):
        '''Trigger PCSCF Restoration for all IMS Subscribers attached to PCSCF'''

        try:        
            jsonData = request.get_json(force=True)

            pcscf = jsonData.get('pcscf', None)

            if not pcscf:
                result = {"Result": "Error: PCSCF Required"}
                return result, 400

            activeSubscribers = databaseClient.Get_Subscribers_By_Pcscf(pcscf=pcscf)
            logTool.log(service='API', level='debug', message=f"[API] [pcscf_restoration] Active Subscribers for {pcscf}: {activeSubscribers}", redisClient=redisMessaging)

            if len(activeSubscribers) > 0:
                for imsSubscriber in activeSubscribers:
                    try:
                        imsi = imsSubscriber.get('imsi', None)
                        if not imsi:
                            continue
                        subscriberData = databaseClient.Get_Subscriber(imsi=imsi)
                        servingMmePeer = subscriberData.get('serving_mme_peer').split(';')[0]

                        imsi = subscriberData.get('imsi', None)
                        servingMmeRealm = subscriberData.get('serving_mme_realm', None)
                        servingMme = subscriberData.get('serving_mme', None)

                        diameterRequest = diameterClient.sendDiameterRequest(
                            requestType='CLR',
                            hostname=servingMmePeer,
                            imsi=imsi, 
                            DestinationHost=servingMme, 
                            DestinationRealm=servingMmeRealm, 
                            CancellationType=2,
                            immediateReattach=True
                        )

                    except Exception as e:
                        logTool.log(service='API', level='error', message=f"[API] [pcscf_restoration] Error sending CLR for subscriber: {traceback.format_exc()}", redisClient=redisMessaging)
                        continue
            
            result = {"Result": f"Successfully sent PCSCF Restoration request for PCSCF: {pcscf}"}
            return result, 200

        except Exception as E:
            print("Flask Exception: " + str(E))
            return handle_exception(E)

@ns_pcrf.route('/<string:charging_rule_id>')
class PyHSS_PCRF_Complete(Resource):
    def get(self, charging_rule_id):
        '''Get full Charging Rule + TFTs'''
        try:
            data = databaseClient.Get_Charging_Rule(charging_rule_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)


@ns_pcrf.route('/subscriber_routing/<string:subscriber_routing>')
class PyHSS_PCRF_SUBSCRIBER_ROUTING(Resource):
    def get(self, subscriber_routing):
        '''Get Subscriber info from Subscriber Routing'''
        try:
            data = databaseClient.Get_UE_by_IP(subscriber_routing)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_pcrf.route('/emergency_subscriber/<string:emergency_subscriber_id>')
class PyHSS_EMERGENCY_SUBSCRIBER_Get(Resource):
    def get(self, emergency_subscriber_id):
        '''Get all emergency_subscriber data for specified emergency_subscriber ID'''
        try:
            apn_data = databaseClient.GetObj(EMERGENCY_SUBSCRIBER, emergency_subscriber_id)
            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, emergency_subscriber_id):
        '''Delete all emergency_subscriber data for specified emergency_subscriber ID'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(EMERGENCY_SUBSCRIBER, emergency_subscriber_id, False, operation_id)
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_pcrf.doc('Update EMERGENCY_SUBSCRIBER Object')
    @ns_pcrf.expect(EMERGENCY_SUBSCRIBER_model)
    def patch(self, emergency_subscriber_id):
        '''Update emergency_subscriber data for specified emergency_subscriber ID'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            apn_data = databaseClient.UpdateObj(EMERGENCY_SUBSCRIBER, json_data, emergency_subscriber_id, False, operation_id)

            return apn_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)   

@ns_pcrf.route('/emergency_subscriber/')
class PyHSS_EMERGENCY_SUBSCRIBER(Resource):
    @ns_pcrf.doc('Create EMERGENCY_SUBSCRIBER Object')
    @ns_pcrf.expect(EMERGENCY_SUBSCRIBER_model)
    def put(self):
        '''Create new EMERGENCY_SUBSCRIBER'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            emergency_subscriber_id = databaseClient.CreateObj(EMERGENCY_SUBSCRIBER, json_data, False, operation_id)

            return emergency_subscriber_id, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_pcrf.route('/emergency_subscriber/list')
class PyHSS_ALL_EMERGENCY_SUBSCRIBER(Resource):
    @ns_apn.expect(paginatorParser)
    def get(self):
        '''Get all Emergency Subscribers'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(EMERGENCY_SUBSCRIBER, args['page'], args['page_size'])
            return (data), 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_geored.route('/sh_profile_updated')
class PyHSS_Geored_Sh_Profile_Updated(Resource):
    @ns_geored.doc('Receive notification that an IMS subscriber Sh profile changed')
    @no_auth_required
    def post(self):
        '''Send Sh PNR to ASs with subscriptions held on this node for the given ims_subscriber_id'''
        try:
            json_data = request.get_json(force=True)
            ims_subscriber_id = json_data.get('ims_subscriber_id')
            if ims_subscriber_id is None:
                return {'error': 'ims_subscriber_id is required'}, 400
            data = databaseClient.GetObj(IMS_SUBSCRIBER, ims_subscriber_id)
            send_pnr_for_local_subscriptions(data)
            return {'result': 'OK'}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_geored.route('/network_teardown')
class PyHSS_Geored_Network_Teardown(Resource):
    @ns_geored.doc('Execute HSS-initiated Gx RAR/CLR/RTR/SWx-RTR using this node Diameter peer table')
    @no_auth_required
    def post(self):
        '''Send Gx session release, S6a CLR, Cx RTR and/or SWx RTR for the given IMSI from this Diameter node'''
        try:
            json_data = request.get_json(force=True)
            imsi = json_data.get('imsi')
            if not imsi:
                return {'error': 'imsi is required'}, 400
            domains = json_data.get('domains') or ['pcrf', 'epc', 'ims', 'swx']
            warnings = teardown_subscriber(
                diameterClient,
                databaseClient,
                imsi,
                domains=domains,
                diameter_realm=originRealm,
                mcc=mcc,
                mnc=mnc,
                clear_serving_state=False,
                log_tool=logTool,
                redis_messaging=redisMessaging,
            )
            return {'result': 'OK', 'warnings': warnings}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_geored.route('/push_clr')
class PyHSS_Geored_Push_Clr(Resource):
    @ns_geored.doc('Send an operator-requested S6a CLR from this Diameter node')
    @no_auth_required
    def post(self):
        '''Send S6a CLR for the given IMSI via this node's Diameter peer table (no further relay)'''
        try:
            json_data = request.get_json(force=True)
            imsi = json_data.get('imsi')
            if not imsi:
                return {'error': 'imsi is required'}, 400
            for key in ('diameterPeer', 'DestinationRealm', 'cancellationType', 'immediateReattach'):
                if key not in json_data:
                    return {'error': f'{key} is required'}, 400
            sent = send_clr(diameterClient, imsi, json_data)
            return {'result': 'OK' if sent else 'NotSent', 'peer': json_data['diameterPeer'], 'node': originHostname}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_geored.route('/push_ims_profile')
class PyHSS_Geored_Push_Ims_Profile(Resource):
    @ns_geored.doc('Send Cx PPR from this Diameter node')
    @no_auth_required
    def post(self):
        '''Send Cx PPR for a registered IMS subscriber from this Diameter node'''
        try:
            json_data = request.get_json(force=True)
            imsi = json_data.get('imsi')
            if not imsi:
                return {'error': 'imsi is required'}, 400
            ims_data = databaseClient.Get_IMS_Subscriber(imsi=str(imsi))
            warnings = push_ims_profile(
                diameterClient,
                ims_data,
                mcc=mcc,
                mnc=mnc,
                log_tool=logTool,
                redis_messaging=redisMessaging,
            )
            return {'result': 'OK', 'warnings': warnings}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_geored.route('/push_ifc_template')
class PyHSS_Geored_Push_Ifc_Template(Resource):
    @ns_geored.doc('Fan out Cx PPR for an iFC template from this Diameter node')
    @no_auth_required
    def post(self):
        '''Send Cx PPR to S-CSCFs of registered subscribers using the given iFC template'''
        try:
            json_data = request.get_json(force=True)
            ifc_template_id = json_data.get('ifc_template_id')
            if ifc_template_id is None:
                return {'error': 'ifc_template_id is required'}, 400
            warnings = push_ppr_for_ifc_template_subscribers(
                diameterClient,
                databaseClient,
                int(ifc_template_id),
                mcc=mcc,
                mnc=mnc,
                log_tool=logTool,
                redis_messaging=redisMessaging,
            )
            return {'result': 'OK', 'warnings': warnings}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_geored.route('/rx_terminate_af_subscriptions')
class PyHSS_Geored_Rx_Terminate_Af_Subscriptions(Resource):
    @ns_geored.doc('Receive a Gx CCR-T termination relay and abort matching Rx AF subscriptions')
    @no_auth_required
    def post(self):
        '''Send Rx ASR to AFs whose signalling-bearer subscription for the given IMSI/APN is held on this node'''
        try:
            json_data = request.get_json(force=True)
            imsi = json_data.get('imsi')
            apn = json_data.get('apn')
            if imsi is None or apn is None:
                return {'error': 'imsi and apn are required'}, 400
            diameterClient.rx_terminate_local_af_subscriptions(imsi, apn)
            return {'result': 'OK'}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_geored.route('/')
class PyHSS_Geored(Resource):
    @ns_geored.doc('Receive GeoRed data')
    @ns_geored.expect(GeoRed_model)
    @no_auth_required
    def patch(self):
        '''Get Geored data Pushed'''
        try:
            json_data = request.get_json(force=True)
            print("JSON Data sent in Geored request: " + str(json_data))
            response_data = []
            if 'serving_mme' in json_data:
                print("Updating serving MME")
                response_data.append(databaseClient.Update_Serving_MME(imsi=str(json_data['imsi']), serving_mme=json_data['serving_mme'], serving_mme_realm=json_data['serving_mme_realm'], serving_mme_peer=json_data['serving_mme_peer'], propagate=False))
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "HSS",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')

            if 'last_seen_mcc' in json_data:
                print("Updating Subscriber Location")
                response_data.append(databaseClient.update_subscriber_location(imsi=str(json_data['imsi']),
                                                                                last_seen_eci=json_data['last_seen_eci'],
                                                                                last_seen_enodeb_id=json_data['last_seen_enodeb_id'],
                                                                                last_seen_cell_id=json_data['last_seen_cell_id'],
                                                                                last_seen_tac=json_data['last_seen_tac'],
                                                                                last_seen_mcc=json_data['last_seen_mcc'],
                                                                                last_seen_mnc=json_data['last_seen_mnc'],
                                                                                last_location_update_timestamp=json_data['last_location_update_timestamp'],
                                                                                propagate=False))
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "HSS",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')
            if 'serving_apn' in json_data:
                print("Updating serving APN")
                if 'serving_pgw_realm' not in json_data:
                    json_data['serving_pgw_realm'] = None
                if 'serving_pgw_peer' not in json_data:
                    json_data['serving_pgw_peer'] = None
                if 'serving_pgw_timestamp' not in json_data:
                    json_data['serving_pgw_timestamp'] = None
                if json_data['serving_pgw'] == None:
                    subscriber_details = databaseClient.Get_Subscriber(imsi=str(json_data['imsi']))
                    stored_apn = databaseClient.Get_APN_by_Name(apn=json_data['serving_apn'])
                    matching_apn_id = stored_apn.get('apn_id', None)
                    matching_subscriber_id = subscriber_details.get('subscriber_id', None)
                    serving_apn = databaseClient.Get_Serving_APN(subscriber_id=matching_subscriber_id, apn_id=matching_apn_id)
                    if serving_apn:
                        serving_apn_session_id = serving_apn.get('pcrf_session_id', "")
                        print(f"Stored Session ID for {json_data['imsi']} is {serving_apn_session_id}, Session ID recieved in Geored update is: {json_data['pcrf_session_id']}")
                        if serving_apn_session_id == json_data['pcrf_session_id']:
                            response_data.append(databaseClient.Update_Serving_APN(
                                imsi=str(json_data['imsi']), 
                                apn=json_data['serving_apn'],
                                pcrf_session_id=json_data['pcrf_session_id'],
                                serving_pgw=json_data['serving_pgw'],
                                subscriber_routing=json_data['subscriber_routing'],
                                serving_pgw_realm=json_data['serving_pgw_realm'],
                                serving_pgw_peer=json_data['serving_pgw_peer'],
                                serving_pgw_timestamp=json_data['serving_pgw_timestamp'],
                                propagate=False))
                            print(f"Removed Serving APN {json_data['serving_apn']} for: {json_data['imsi']}")
                        else:
                            print("Incoming Session ID does not match stored session ID - refusing to remove Serving APN.")
                else:
                    response_data.append(databaseClient.Update_Serving_APN(
                        imsi=str(json_data['imsi']), 
                        apn=json_data['serving_apn'],
                        pcrf_session_id=json_data['pcrf_session_id'],
                        serving_pgw=json_data['serving_pgw'],
                        subscriber_routing=json_data['subscriber_routing'],
                        serving_pgw_realm=json_data['serving_pgw_realm'],
                        serving_pgw_peer=json_data['serving_pgw_peer'],
                        serving_pgw_timestamp=json_data['serving_pgw_timestamp'],
                        propagate=False))
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "PCRF",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')
            if 'scscf' in json_data:
                print("Updating Serving SCSCF")
                if 'scscf_realm' not in json_data:
                    json_data['scscf_realm'] = None
                if 'scscf_peer' not in json_data:
                    json_data['scscf_peer'] = None
                if 'scscf_timestamp' not in json_data:
                    json_data['scscf_timestamp'] = None
                response_data.append(databaseClient.Update_Serving_CSCF(imsi=str(json_data['imsi']), serving_cscf=json_data['scscf'], scscf_realm=json_data['scscf_realm'], scscf_peer=json_data['scscf_peer'], scscf_timestamp=json_data['scscf_timestamp'], propagate=False))
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "IMS_SCSCF",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')
            if 'pcscf' in json_data:
                print("Updating Proxy SCSCF")
                if 'pcscf_realm' not in json_data:
                    json_data['pcscf_realm'] = None
                if 'pcscf_peer' not in json_data:
                    json_data['pcscf_peer'] = None
                if 'pcscf_timestamp' not in json_data:
                    json_data['pcscf_timestamp'] = None
                if 'pcscf_active_session' not in json_data:
                    json_data['pcscf_active_session'] = None
                response_data.append(databaseClient.Update_Proxy_CSCF(imsi=str(json_data['imsi']), proxy_cscf=json_data['pcscf'], pcscf_realm=json_data['pcscf_realm'], pcscf_peer=json_data['pcscf_peer'], pcscf_timestamp=json_data['pcscf_timestamp'], pcscf_active_session=json_data['pcscf_active_session'], propagate=False))
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "IMS_PCSCF",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')
            if 'imei' in json_data:
                print("Updating EIR")
                response_data.append(databaseClient.Store_IMSI_IMEI_Binding(str(json_data['imsi']), str(json_data['imei']), str(json_data['match_response_code']), propagate=False))
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "IMEI",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')
            if 'xcap_profile' in json_data:
                print("Updating Sh xcap_profile")
                ims_data = databaseClient.Get_IMS_Subscriber(imsi=str(json_data['imsi']))
                response_data.append(databaseClient.UpdateObj(
                    IMS_SUBSCRIBER,
                    {'xcap_profile': json_data['xcap_profile'], 'sh_profile': None},
                    ims_data['ims_subscriber_id'],
                ))
                ims_data['xcap_profile'] = json_data['xcap_profile']
                ims_data['sh_profile'] = None
                # Local PNR only. The originating Diameter node already notified
                # its own SNR subscribers; this receiver notifies any SNR stored
                # in this node's Redis. Do not call relay_sh_profile_update
                # (that would loop through SH_NOTIFY_ENDPOINTS).
                send_pnr_for_local_subscriptions(ims_data)
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc',
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "SH",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True,
                                    prefixHostname=originHostname,
                                    prefixServiceName='metric')
            if 'auc_id' in json_data:
                print("Updating AuC")
                response_data.append(databaseClient.Update_AuC(json_data['auc_id'], json_data['sqn'], propagate=False))
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "SQN",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')
            if 'emergency_subscriber_ip' in json_data:
                """
                If we receive a geored payload containing emergency_subscriber_id, create or update the matching emergency_subscriber_id.
                If emergency_subscriber_id exists as None, then remove the emergency subscriber.
                """
                print("Updating Emergency Subscriber")
                subscriberData = {
                    "imsi": json_data.get('emergency_subscriber_imsi'),
                    "servingPgw": json_data.get('emergency_subscriber_serving_pgw'),
                    "requestTime": json_data.get('emergency_subscriber_serving_pgw_timestamp'),
                    "servingPcscf": json_data.get('emergency_subscriber_serving_pcscf'),
                    "aarRequestTime": json_data.get('emergency_subscriber_serving_pcscf_timestamp'),
                    "gxOriginRealm": json_data.get('emergency_subscriber_gx_origin_realm'),
                    "gxOriginHost": json_data.get('emergency_subscriber_gx_origin_host'),
                    "ratType": json_data.get('emergency_subscriber_rat_type'),
                    "ip": json_data.get('emergency_subscriber_ip'),
                    "accessNetworkGatewayAddress": json_data.get('emergency_subscriber_access_network_gateway_address'),
                    "accessNetworkChargingAddress": json_data.get('emergency_subscriber_access_network_charging_address'),
                }

                if not json_data.get('emergency_subscriber_ip', None):
                    logTool.log(service='API', level='error', message=f"[API] emergency_subscriber_ip missing from geored request. No changes to emergency_subscriber made.", redisClient=redisMessaging)
                    return {'result': 'Failed', 'Reason' : "emergency_subscriber_ip missing from geored request"}

                if 'emergency_subscriber_delete' in json_data:
                    if json_data.get('emergency_subscriber_delete', False):
                        databaseClient.Delete_Emergency_Subscriber(subscriberIp=subscriberData.get('ip'), imsi=subscriberData.get('imsi'), propagate=False)
                        return {}, 200

                response_data.append(databaseClient.Update_Emergency_Subscriber(emergencySubscriberId=json_data['emergency_subscriber_id'],
                                                                                subscriberData=subscriberData,
                                                                                imsi=subscriberData.get('imsi'),
                                                                                subscriberIp=subscriberData.get('ip'),
                                                                                gxSessionId=subscriberData.get('servingPgw'),
                                                                                propagate=False))
                
                redisMessaging.sendMetric(serviceName='api', metricName='prom_flask_http_geored_endpoints',
                                    metricType='counter', metricAction='inc', 
                                    metricValue=1.0, metricHelp='Number of Geored Pushes Received',
                                    metricLabels={
                                        "endpoint": "EMERGENCY_SUBSCRIBER",
                                        "geored_host": request.remote_addr,
                                    },
                                    metricExpiry=60,
                                    usePrefix=True, 
                                    prefixHostname=originHostname, 
                                    prefixServiceName='metric')
            return response_data, 200
        except Exception as E:
            print(f"Exception when updating: {traceback.format_exc()}")
            response_json = {'result': 'Failed', 'Reason' : str(E), 'Partial Response Data' : str(response_data)}
            return response_json, 500

    def get(self):
        '''Return the active geored schema'''
        try:
            geored_model_json = {}
            for key in GeoRed_model:
                geored_model_json[key] = 'string'
            return geored_model_json, 200
        except Exception as E:
            print(f"Exception when returning geored schema: {traceback.format_exc()}")
            response_json = {'result': 'Failed', 'Reason' : "Unable to return Geored Schema: " + str(E)}
            return response_json, 500

@ns_geored.route('/peers')
class PyHSS_Geored_Peers(Resource):
    def patch(self):
        '''Update the configured geored peers'''
        try:
            json_data = request.get_json(force=True)
            print("JSON Data sent: " + str(json_data))
            georedEnabled = config.get('geored', {}).get('enabled', False)
            if not georedEnabled:
                return {'result': 'Failed', 'Reason' : "Geored not enabled"}
            if 'endpoints' not in json_data:
                return {'result': 'Failed', 'Reason' : "No endpoints in request"}
            if not isinstance(json_data['endpoints'], list):
                return {'result': 'Failed', 'Reason' : "Endpoints must be a list"}
            config['geored']['endpoints'] = json_data['endpoints']
            update_file = config.get('geored', {}).get('update_file', '/tmp/pyhss_geored_endpoints.txt')
            if update_file and update_file != '':
                # Writing the data to a YAML file
                with open(update_file, 'w') as file:
                    yaml.dump(config['geored']['endpoints'], file)

            return {'result': 'Success'}, 200
        except Exception as E:
            print("Exception when updating geored peers: " + str(E))
            response_json = {'result': 'Failed', 'Reason' : "Unable to update Geored peers: " + str(E)}
            return response_json
    def get(self):
        '''Return the configured geored peers'''
        try:
            georedEnabled = config.get('geored', {}).get('enabled', False)
            if not georedEnabled:
                return {'result': 'Failed', 'Reason' : "Geored not enabled"}
            georedPeers = config.get('geored', {}).get('endpoints', [])
            return {'peers': georedPeers}, 200
        except Exception as E:
            print("Exception when returning geored peers: " + str(E))
            response_json = {'result': 'Failed', 'Reason' : "Unable to return Geored peers: " + str(E)}
            return response_json

@ns_geored.route('/webhooks')
class PyHSS_Geored_Webhooks(Resource):
    def get(self):
        '''Return the configured geored webhooks'''
        try:
            georedEnabled = config.get('webhooks', {}).get('enabled', False)
            if not georedEnabled:
                return {'result': 'Failed', 'Reason' : "Webhooks not enabled"}
            georedWebhooks = config.get('webhooks', {}).get('endpoints', [])
            return {'endpoints': georedWebhooks}, 200
        except Exception as E:
            print("Exception when returning geored webhooks: " + str(E))
            response_json = {'result': 'Failed', 'Reason' : "Unable to return Geored webhooks: " + str(E)}
            return response_json

@ns_push.route('/clr/<string:imsi>')
class PyHSS_Push_CLR(Resource):
    @ns_push.expect(Push_CLR_Model)
    @ns_push.doc('Push CLR (Cancel Location Request) to MME')
    def put(self, imsi):
        try:
            '''Push CLR (Cancel Location Request) to MME'''
            json_data = request.get_json(force=True)
            if 'DestinationHost' not in json_data:
                json_data['DestinationHost'] = None
            outcome = push_clr_and_relay(diameterClient, imsi, json_data, relay_to_diameter_nodes)
            if not outcome['sent']:
                return dict(outcome, result=f'Failed queueing CLR to {json_data["diameterPeer"]}'), 400

            try:
                subscriber_details = databaseClient.Get_Subscriber(imsi=str(imsi))
                if subscriber_details['serving_mme'] == json_data['DestinationHost']:
                    databaseClient.Update_Serving_MME(imsi=imsi, serving_mme=None)
            except Exception as exc:
                logTool.log(service='API', level='warning', message=f"[API] CLR sent but serving MME for IMSI {imsi} not cleared: {exc}", redisClient=redisMessaging)

            return dict(outcome, result=f'Successfully queued CLR to {json_data["diameterPeer"]}'), 200
        except Exception as E:
            print("Exception when sending CLR: " + str(E))
            response_json = {'result': 'Failed', 'Reason' : "Unable to send CLR: " + str(E)}
            return response_json


### IFC Template Endpoints ###

@ns_ifc_template.route('/<string:ifc_template_id>')
class PyHSS_IFC_Template_Get(Resource):
    def get(self, ifc_template_id):
        '''Get IFC template data for specified template ID'''
        try:
            template_data = databaseClient.GetObj(IFC_TEMPLATE, ifc_template_id)
            return template_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    def delete(self, ifc_template_id):
        '''Delete IFC template for specified template ID'''
        try:
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.DeleteObj(IFC_TEMPLATE, ifc_template_id, False, operation_id)
            # Publish cache invalidation message
            try:
                invalidation_msg = json.dumps({'action': 'invalidate', 'template_id': int(ifc_template_id)})
                redisMessaging.publish('ifc_template_invalidation', invalidation_msg)
            except Exception as pub_error:
                print(f"Warning: Failed to publish cache invalidation: {pub_error}")
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

    @ns_ifc_template.doc('Update IFC Template')
    @ns_ifc_template.expect(IFC_TEMPLATE_model)
    def patch(self, ifc_template_id):
        '''Update IFC template for specified template ID'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            data = databaseClient.UpdateObj(IFC_TEMPLATE, json_data, ifc_template_id, False, operation_id)
            # Publish cache invalidation message
            try:
                invalidation_msg = json.dumps({'action': 'invalidate', 'template_id': int(ifc_template_id)})
                redisMessaging.publish('ifc_template_invalidation', invalidation_msg)
            except Exception as pub_error:
                print(f"Warning: Failed to publish cache invalidation: {pub_error}")

            warnings = []
            if 'template_content' in json_data:
                warnings = push_ppr_for_ifc_template_subscribers(
                    diameterClient,
                    databaseClient,
                    int(ifc_template_id),
                    mcc=mcc,
                    mnc=mnc,
                    log_tool=logTool,
                    redis_messaging=redisMessaging,
                )
                relay_to_diameter_nodes('/geored/push_ifc_template', {'ifc_template_id': int(ifc_template_id)})

            if warnings:
                data = dict(data) if isinstance(data, dict) else {'result': data}
                data['warnings'] = warnings
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ifc_template.route('/')
class PyHSS_IFC_Template(Resource):
    @ns_ifc_template.doc('Create IFC Template Object')
    @ns_ifc_template.expect(IFC_TEMPLATE_model)
    def put(self):
        '''Create new IFC template'''
        try:
            json_data = request.get_json(force=True)
            args = parser.parse_args()
            operation_id = args.get('operation_id', None)
            template_id = databaseClient.CreateObj(IFC_TEMPLATE, json_data, False, operation_id)
            return template_id, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ifc_template.route('/list')
class PyHSS_IFC_Template_List(Resource):
    @ns_ifc_template.expect(paginatorParser)
    def get(self):
        '''Get all IFC templates'''
        try:
            args = paginatorParser.parse_args()
            data = databaseClient.getAllPaginated(IFC_TEMPLATE, args['page'], args['page_size'])
            return data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ifc_template.route('/name/<string:template_name>')
class PyHSS_IFC_Template_By_Name(Resource):
    def get(self, template_name):
        '''Get IFC template by name'''
        try:
            template_data = databaseClient.Get_IFC_Template_by_Name(template_name)
            return template_data, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ifc_template.route('/cache/invalidate')
class PyHSS_IFC_Template_Cache_Invalidate(Resource):
    def post(self):
        '''Invalidate all IFC template caches'''
        try:
            invalidation_msg = json.dumps({'action': 'invalidate_all'})
            redisMessaging.publish('ifc_template_invalidation', invalidation_msg)
            return {'result': 'Cache invalidation message published'}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)

@ns_ifc_template.route('/cache/invalidate/<string:ifc_template_id>')
class PyHSS_IFC_Template_Cache_Invalidate_Single(Resource):
    def post(self, ifc_template_id):
        '''Invalidate specific IFC template cache'''
        try:
            invalidation_msg = json.dumps({'action': 'invalidate', 'template_id': int(ifc_template_id)})
            redisMessaging.publish('ifc_template_invalidation', invalidation_msg)
            return {'result': f'Cache invalidation message published for template {ifc_template_id}'}, 200
        except Exception as E:
            print(E)
            return handle_exception(E)


def main():
    apiService.run(debug=False, host='0.0.0.0', port=8080)


if __name__ == '__main__':
    main()
