# API Service

PyHSS ships with a [Flask-RESTX](https://flask-restx.readthedocs.io/) based HTTP API
that exposes CRUD operations on every object in the subscriber database, plus a
range of OAM (Operations, Administration & Maintenance) and dynamic PCRF functions.

It is the recommended way to provision and operate PyHSS. If you would rather work
directly in Python, `lib/database.py` exposes the same hooks the API calls
internally.

- [Quick reference](#quick-reference)
- [Running the API service](#running-the-api-service)
- [Interactive documentation (Swagger)](#interactive-documentation-swagger)
- [Request basics](#request-basics)
- [Authentication](#authentication)
- [Conventions](#conventions)
- [Error handling](#error-handling)
- [End-to-end provisioning walkthrough](#end-to-end-provisioning-walkthrough)
- [Endpoint reference](#endpoint-reference)
- [Integrating from Python](#integrating-from-python)

---

## Quick reference

| Namespace | Base path | Purpose |
|-----------|-----------|---------|
| APN | `/apn` | Access Point Name profiles |
| AUC | `/auc` | SIM / authentication data (Ki, OPc, AMF, SQN) and vector generation |
| Subscriber | `/subscriber` | EPC subscriber profiles & APN routing |
| IMS Subscriber | `/ims_subscriber` | IMS / VoLTE subscriber profiles |
| TFT | `/tft` | Traffic Flow Templates |
| Charging Rule | `/charging_rule` | Predefined PCC / charging rules |
| EIR | `/eir` | Equipment Identity Register rules & history |
| Subscriber Attributes | `/subscriber_attributes` | Arbitrary key/value metadata per subscriber |
| Roaming | `/roaming` | Roaming networks & rules |
| IFC Template | `/ifc_template` | Initial Filter Criteria (iFC) templates |
| Operation Logs | `/operation_logs` | Audit trail of write operations |
| OAM | `/oam` | Health, peers, de-registration, reconciliation, rollback |
| PCRF | `/pcrf` | Dynamic Gx actions (rule push, CLR, PCSCF restoration, emergency subs) |
| GeoRed | `/geored` | Geographic redundancy state push & peer management |
| Push | `/push` | Asynchronous Diameter commands (e.g. CLR) |

> The base path of every endpoint is the namespace itself (for example
> `http://hssip:8080/subscriber/`). There is **no** `/api/v1` prefix.

---

## Running the API service

The API service listens on TCP port **8080** and binds to `0.0.0.0`.

Run it directly:

```shell
python3 services/apiService.py
```

Run it as a container by setting `CONTAINER_ROLE=api` (see `docker/docker-compose.yaml`):

```shell
cd docker && docker compose up --build -d
```

Run it as a service with the bundled unit file `systemd/pyhss_api.service`.

For production deployments, PyHSS API uses Flask and can be served behind your
preferred WSGI server.

---

## Interactive documentation (Swagger)

Once the service is running, browse to:

```
http://hssip:8080/docs/
```

This is the Swagger UI, generated from the live models. It documents every field
of every object, and includes a **"Try it out"** feature so you can issue requests
directly from the browser.

![Adding data to PyHSS via the Swagger UI](https://github.com/nickvsnetworking/pyhss/raw/master/docs/images/PyHSS_API_Swagger.gif)

The raw OpenAPI definition is available at:

```
http://hssip:8080/swagger.json
```

This can be fed into client generators (`openapi-generator`, `swagger-codegen`, …)
to produce typed SDKs in your language of choice.

---

## Request basics

- All request and response bodies are JSON.
- Send `Content-Type: application/json` on every request that carries a body
  (`PUT`/`PATCH`).
- The HTTP verbs follow REST conventions:

| Verb | Meaning |
|------|---------|
| `GET` | Read a single object or a list |
| `PUT` | **Create** a new object |
| `PATCH` | Update an existing object |
| `DELETE` | Delete an object |

> Note that creation uses `PUT`, not `POST`.

CORS is enabled for all origins, and the service echoes the configured
`OriginHost` back in an `HSS` response header.

---

## Authentication

Authentication is controlled by two `hss` configuration keys:

```yaml
hss:
  lock_provisioning: False               # default
  provisioning_key: "changeThisKeyInProduction"
```

(or the equivalent environment variables `HSS_LOCK_PROVISIONING` and
`HSS_PROVISIONING_KEY` when running in Docker.)

- When `lock_provisioning` is **`False`** (the default), the API is open and no
  credentials are required.
- When `lock_provisioning` is **`True`**, every **write** request
  (`PUT`/`PATCH`/`DELETE`) must include the provisioning key in a
  `Provisioning-Key` header. Read-only `GET` requests remain open, except for a
  small number of state-changing OAM `GET` endpoints (such as the rollback
  endpoints) that are also protected.

```shell
curl -X PUT 'http://hssip:8080/apn/' \
  -H 'Content-Type: application/json' \
  -H 'Provisioning-Key: changeThisKeyInProduction' \
  -d '{ "apn": "internet", ... }'
```

If the key is missing or wrong, the API responds with `401` and:

```json
{ "Result": "Unauthorized - Provisioning-Key Invalid" }
```

> The Swagger UI (`/docs`), `/swagger.json` and `/metrics` are always reachable
> without a key.

By default the AuC `GET` endpoints redact the secret key material (`ki`, `opc`).
To allow the API to return these values, set `api.enable_insecure_auc: True`.
Leave this disabled in production.

---

## Conventions

**Auto-generated IDs.** When creating an object you do **not** supply its primary
key (`apn_id`, `auc_id`, `subscriber_id`, …). PyHSS assigns it and returns the full
object, including the new ID, in the create response.

**`last_modified`.** Every object carries a `last_modified` timestamp that PyHSS
maintains automatically. It is returned on reads but you never need to set it.

**Pagination.** All `…/list` endpoints accept two optional query parameters:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `page` | `0` | Zero-based page index |
| `page_size` | `api.page_size` config value (100) | Items per page |

```shell
curl 'http://hssip:8080/subscriber/list?page=0&page_size=50'
```

**Operation IDs & rollback.** Write endpoints accept an optional `operation_id`
query parameter. Operations sharing the same `operation_id` are grouped together in
the audit log, so they can be rolled back as a unit via the
[`/oam/rollback_operation/...`](#oam) endpoints.

```shell
curl -X PUT 'http://hssip:8080/apn/?operation_id=batch-2024-06-01' \
  -H 'Content-Type: application/json' -d '{ ... }'
```

---

## Error handling

Responses use standard HTTP status codes:

| Status | Meaning |
|--------|---------|
| `200` | Success |
| `400` | Bad request / database integrity or operational error |
| `401` | Missing or invalid `Provisioning-Key` |
| `404` | Object or route not found |
| `410` | Required external resource missing (e.g. EIR CSV file not defined) |
| `500` | Unhandled internal error |

Error bodies follow this shape:

```json
{
  "result": "Failed",
  "reason": "A database integrity error occurred: ..."
}
```

---

## End-to-end provisioning walkthrough

A minimal flow to bring a subscriber onto data and voice services. Each step builds
on the IDs returned by the previous step. (Add `-H 'Provisioning-Key: ...'` to the
write requests if `lock_provisioning` is enabled.)

### 1. Create the SIM authentication data (AuC)

At a minimum, supply the Ki, OPc and AMF. (If you only have the OP, convert it to
OPc with `lib/CryptoTool.py`.)

```shell
curl -X PUT 'http://hssip:8080/auc/' \
  -H 'Content-Type: application/json' \
  -d '{
    "ki": "465B5CE8B199B49FAA5F0A2EE238A6BC",
    "opc": "E8ED289DEBA952E4283B54E88E6183CA",
    "amf": "8000",
    "sqn": 0
  }'
```

Response (truncated) — note the assigned `auc_id`:

```json
{ "auc_id": 1, "amf": "8000", "sqn": 0, ... }
```

### 2. Create one or more APNs

```shell
curl -X PUT 'http://hssip:8080/apn/' \
  -H 'Content-Type: application/json' \
  -d '{
    "apn": "internet",
    "apn_ambr_dl": 9999999,
    "apn_ambr_ul": 9999999,
    "qci": 9,
    "arp_priority": 1,
    "arp_preemption_capability": true,
    "arp_preemption_vulnerability": false
  }'
```

Response: `{ "apn_id": 1, ... }`.

### 3. Create the EPC subscriber

Reference the `auc_id` from step 1 and the `apn_id`(s) from step 2.

The allowed APNs are tracked per Diameter interface:

- `apn_list` — comma-separated APN IDs returned in the S6a Update-Location-Answer
  (mobile / 3GPP access via the MME). **Required.**
- `apn_list_swx` — comma-separated APN IDs returned in the SWx
  Server-Assignment-Answer (untrusted non-3GPP access via the ePDG). Optional. When
  NULL or empty the SWx SAA is rejected with
  `DIAMETER_ERROR_USER_NO_NON_3GPP_SUBSCRIPTION (5450)` per 3GPP TS 29.273
  §5.2.2.4, so VoWiFi attach is denied. Operators that want VoWiFi must list at
  least the IMS APN here.

```shell
curl -X PUT 'http://hssip:8080/subscriber/' \
  -H 'Content-Type: application/json' \
  -d '{
    "imsi": "001010000000001",
    "enabled": true,
    "auc_id": 1,
    "default_apn": 1,
    "apn_list": "1",
    "apn_list_swx": "1",
    "msisdn": "491792021244",
    "ue_ambr_dl": 9999999,
    "ue_ambr_ul": 9999999,
    "nam": 0,
    "subscribed_rau_tau_timer": 600
  }'
```

### 4. (VoLTE) Create an iFC template

The Initial Filter Criteria (iFC) returned to the S-CSCF are sourced from a
database-stored template, referenced by IMS subscribers via `ifc_template_id`.
Create the template first — `template_content` is a Jinja2 XML document (the
`iFC_vars` placeholders are filled in per-subscriber at registration time):

```shell
curl -X PUT 'http://hssip:8080/ifc_template/' \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "default_ifc",
    "description": "Default VoLTE iFC",
    "template_content": "<?xml version=\"1.0\" encoding=\"UTF-8\"?><IMSSubscription>...</IMSSubscription>"
  }'
```

Response: `{ "ifc_template_id": 1, ... }`. You can fetch a template by name with
`GET /ifc_template/name/default_ifc`.

### 5. (VoLTE) Create the IMS subscriber

Reference the `ifc_template_id` from step 4. Multiple MSISDNs can be supplied as
comma-separated values in `msisdn_list`.

```shell
curl -X PUT 'http://hssip:8080/ims_subscriber/' \
  -H 'Content-Type: application/json' \
  -d '{
    "imsi": "001010000000001",
    "msisdn": "491792021244",
    "msisdn_list": "491792021244",
    "ifc_template_id": 1,
    "xcap_profile": "<?xml version=\"1.0\" encoding=\"UTF-8\"?><simservs>...</simservs>"
  }'
```

The subscriber is now able to attach for data and register to the IMS.

> The older `ifc_path` field (a path to an iFC template file on disk) is
> **deprecated** — use `ifc_template_id` to reference a database-stored
> [iFC template](#ifc-template--ifc_template) instead.

---

## Endpoint reference

Below is a per-namespace reference. The exact set of fields for each object is
generated live from the database schema, so always consult the
[Swagger UI](#interactive-documentation-swagger) for the authoritative,
version-specific field list. The examples here use representative fields.

### APN — `/apn`

| Method & path | Description |
|---------------|-------------|
| `PUT /apn/` | Create an APN |
| `GET /apn/<apn_id>` | Get an APN |
| `PATCH /apn/<apn_id>` | Update an APN |
| `DELETE /apn/<apn_id>` | Delete an APN |
| `GET /apn/list` | List APNs (paginated) |

```shell
# Create
curl -X PUT 'http://hssip:8080/apn/' -H 'Content-Type: application/json' \
  -d '{"apn":"ims","apn_ambr_dl":256000,"apn_ambr_ul":256000,"qci":5,
       "arp_priority":1,"arp_preemption_capability":true,"arp_preemption_vulnerability":false}'

# Read
curl 'http://hssip:8080/apn/1'
```

### AUC — `/auc`

| Method & path | Description |
|---------------|-------------|
| `PUT /auc/` | Create AuC (SIM) data |
| `GET /auc/<auc_id>` | Get AuC entry by ID |
| `PATCH /auc/<auc_id>` | Update AuC entry |
| `DELETE /auc/<auc_id>` | Delete AuC entry |
| `GET /auc/list` | List AuC entries (paginated) |
| `GET /auc/iccid/<iccid>` | Find AuC entry by ICCID |
| `GET /auc/imsi/<imsi>` | Find AuC entry by IMSI |
| `GET /auc/eap_aka/plmn/<plmn>/imsi/<imsi>` | Generate EAP-AKA vectors (VoWiFi) |
| `GET /auc/aka/vector_count/<count>/imsi/<imsi>` | Generate `<count>` EPS-AKA vectors |
| `GET /auc/aka/resync/imsi/<imsi>/auts/<auts>/rand/<rand>` | Re-synchronise SQN using AUTS |

```shell
# Generate 3 EPS-AKA authentication vectors for an IMSI
curl 'http://hssip:8080/auc/aka/vector_count/3/imsi/001010000000001'
```

> By default `ki`/`opc` are redacted from AuC reads. Enable
> `api.enable_insecure_auc` only if you must retrieve them via the API.

### Subscriber — `/subscriber`

| Method & path | Description |
|---------------|-------------|
| `PUT /subscriber/` | Create a subscriber |
| `GET /subscriber/<subscriber_id>` | Get subscriber by ID |
| `PATCH /subscriber/<subscriber_id>` | Update subscriber |
| `DELETE /subscriber/<subscriber_id>` | Delete subscriber |
| `GET /subscriber/list` | List subscribers (paginated) |
| `GET /subscriber/imsi/<imsi>` | Get subscriber by IMSI |
| `GET /subscriber/msisdn/<msisdn>` | Get subscriber by MSISDN |
| `PUT /subscriber/routing/` | Create a subscriber→APN routing entry |
| `GET /subscriber/routing/<subscriber_id>/<apn_id>` | Get a routing entry |
| `DELETE /subscriber/routing/<subscriber_id>/<apn_id>` | Delete a routing entry |
| `PATCH /subscriber/routing/<subscriber_routing_id>` | Update a routing entry |

```shell
curl 'http://hssip:8080/subscriber/imsi/001010000000001'
```

Reads by IMSI/MSISDN also include the subscriber's `attributes` (see
[Subscriber Attributes](#subscriber-attributes--subscriber_attributes)).

### IMS Subscriber — `/ims_subscriber`

| Method & path | Description |
|---------------|-------------|
| `PUT /ims_subscriber/` | Create an IMS subscriber |
| `GET /ims_subscriber/<ims_subscriber_id>` | Get by ID |
| `PATCH /ims_subscriber/<ims_subscriber_id>` | Update |
| `DELETE /ims_subscriber/<ims_subscriber_id>` | Delete |
| `GET /ims_subscriber/list` | List (paginated) |
| `GET /ims_subscriber/ims_subscriber_msisdn/<msisdn>` | Get by MSISDN |
| `GET /ims_subscriber/ims_subscriber_imsi/<imsi>` | Get by IMSI |

### TFT — `/tft`

Traffic Flow Templates referenced by charging rules via `tft_group_id`.

| Method & path | Description |
|---------------|-------------|
| `PUT /tft/` | Create a TFT |
| `GET /tft/<tft_id>` | Get a TFT |
| `PATCH /tft/<tft_id>` | Update a TFT |
| `DELETE /tft/<tft_id>` | Delete a TFT |
| `GET /tft/list` | List TFTs (paginated) |

```shell
curl -X PUT 'http://hssip:8080/tft/' -H 'Content-Type: application/json' \
  -d '{"tft_group_id":1,"tft_string":"permit out ip from any to any","direction":1}'
```

`direction`: `0` unspecified, `1` downlink, `2` uplink, `3` bidirectional.

### Charging Rule — `/charging_rule`

| Method & path | Description |
|---------------|-------------|
| `PUT /charging_rule/` | Create a charging rule |
| `GET /charging_rule/<charging_rule_id>` | Get a charging rule |
| `PATCH /charging_rule/<charging_rule_id>` | Update a charging rule |
| `DELETE /charging_rule/<charging_rule_id>` | Delete a charging rule |
| `GET /charging_rule/list` | List charging rules (paginated) |

```shell
curl -X PUT 'http://hssip:8080/charging_rule/' -H 'Content-Type: application/json' \
  -d '{"rule_name":"voice","qci":1,"arp_priority":5,
       "arp_preemption_capability":true,"arp_preemption_vulnerability":false,
       "mbr_dl":128000,"mbr_ul":128000,"gbr_dl":128000,"gbr_ul":128000,
       "tft_group_id":1,"precedence":100,"rating_group":4000}'
```

To retrieve a rule together with its resolved TFT list, use `GET /pcrf/<charging_rule_id>`.

### EIR — `/eir`

Equipment Identity Register match rules and IMSI/IMEI sighting history.

| Method & path | Description |
|---------------|-------------|
| `PUT /eir/` | Create an EIR rule |
| `GET /eir/<eir_id>` | Get an EIR rule |
| `PATCH /eir/<eir_id>` | Update an EIR rule |
| `DELETE /eir/<eir_id>` | Delete an EIR rule |
| `GET /eir/list` | List EIR rules (paginated) |
| `GET /eir/lookup_imei/<imei>` | Look up TAC/IMEI metadata |
| `GET /eir/eir_history/<attribute>` | Get IMSI/IMEI history for an attribute |
| `DELETE /eir/eir_history/<attribute>` | Delete history for an attribute |
| `GET /eir/eir_history/list` | List IMSI/IMEI history (paginated) |

```shell
# Blacklist any IMEI starting with 777 for a specific IMSI (regex mode)
curl -X PUT 'http://hssip:8080/eir/' -H 'Content-Type: application/json' \
  -d '{"imei":"^777.*","imsi":"^001010000000001$","regex_mode":1,"match_response_code":1}'
```

`regex_mode`: `0` exact match, `1` regex. `match_response_code`: `0` whitelist,
`1` blacklist, `2` greylist.

### Subscriber Attributes — `/subscriber_attributes`

A key/value store for attaching arbitrary metadata (external IDs, customer names,
integration data) to a subscriber.

| Method & path | Description |
|---------------|-------------|
| `PUT /subscriber_attributes/` | Create an attribute |
| `GET /subscriber_attributes/<subscriber_id>` | List attributes for a subscriber |
| `PATCH /subscriber_attributes/<subscriber_attributes_id>` | Update an attribute |
| `DELETE /subscriber_attributes/<subscriber_attributes_id>` | Delete an attribute |
| `GET /subscriber_attributes/list` | List all attributes (paginated) |

```shell
curl -X PUT 'http://hssip:8080/subscriber_attributes/' -H 'Content-Type: application/json' \
  -d '{"subscriber_id":1,"key":"CustomerName","value":"Example Corp"}'
```

### Roaming — `/roaming`

| Method & path | Description |
|---------------|-------------|
| `PUT /roaming/network/` | Create a roaming network (MCC/MNC) |
| `GET /roaming/network/<roaming_network_id>` | Get a roaming network |
| `PATCH /roaming/network/<roaming_network_id>` | Update a roaming network |
| `DELETE /roaming/network/<roaming_network_id>` | Delete a roaming network |
| `GET /roaming/network/list` | List roaming networks (paginated) |
| `PUT /roaming/rule/` | Create a roaming rule (allow/deny per network) |
| `GET /roaming/rule/<roaming_rule_id>` | Get a roaming rule |
| `PATCH /roaming/rule/<roaming_rule_id>` | Update a roaming rule |
| `DELETE /roaming/rule/<roaming_rule_id>` | Delete a roaming rule |
| `GET /roaming/rule/list` | List roaming rules (paginated) |

```shell
curl -X PUT 'http://hssip:8080/roaming/network/' -H 'Content-Type: application/json' \
  -d '{"name":"Partner DE","preference":1,"mcc":"262","mnc":"02"}'

curl -X PUT 'http://hssip:8080/roaming/rule/' -H 'Content-Type: application/json' \
  -d '{"roaming_network_id":1,"allow":true,"enabled":true}'
```

### IFC Template — `/ifc_template`

Reusable Jinja2 XML templates for Initial Filter Criteria.

| Method & path | Description |
|---------------|-------------|
| `PUT /ifc_template/` | Create a template |
| `GET /ifc_template/<ifc_template_id>` | Get a template |
| `PATCH /ifc_template/<ifc_template_id>` | Update a template (invalidates cache) |
| `DELETE /ifc_template/<ifc_template_id>` | Delete a template (invalidates cache) |
| `GET /ifc_template/list` | List templates (paginated) |
| `GET /ifc_template/name/<template_name>` | Get a template by name |
| `POST /ifc_template/cache/invalidate` | Invalidate all cached templates |
| `POST /ifc_template/cache/invalidate/<ifc_template_id>` | Invalidate one cached template |

```shell
curl -X PUT 'http://hssip:8080/ifc_template/' -H 'Content-Type: application/json' \
  -d '{"name":"default_ifc","description":"Default VoLTE iFC",
       "template_content":"<?xml version=\"1.0\" encoding=\"UTF-8\"?><IMSSubscription>...</IMSSubscription>"}'
```

IMS subscribers reference a template by its `ifc_template_id`. Editing or deleting
a template publishes a cache-invalidation message so the new content is picked up
without a restart.

### Operation Logs — `/operation_logs`

Read-only audit trail of all write operations.

| Method & path | Description |
|---------------|-------------|
| `GET /operation_logs/list` | List operation logs (paginated) |
| `GET /operation_logs/last` | Get the most recent operation |
| `GET /operation_logs/list/table/<table_name>` | List operations for a table (paginated) |

### OAM — `/oam`

Operations and maintenance functions.

| Method & path | Description |
|---------------|-------------|
| `GET /oam/ping` | Liveness check — returns `{"result": "OK"}` |
| `GET /oam/diameter_peers` | List currently connected Diameter peers |
| `GET /oam/serving_subs` | Subscribers currently served by the HSS |
| `GET /oam/serving_subs_pcrf` | Subscribers currently served by the PCRF |
| `GET /oam/serving_subs_ims` | Subscribers currently served by the IMS |
| `GET /oam/deregister/<imsi>` | De-register an IMSI from the whole network (CLR/RTR/CCR-T) |
| `GET /oam/reconcile/ims/<imsi>` | Compare IMS location across all GeoRed HSS nodes |
| `GET /oam/reconcile/enum` | Rebuild all ENUM/NAPTR records from the IMS database |
| `GET /oam/rollback_operation/last` | Roll back the last write operation |
| `GET /oam/rollback_operation/<operation_id>` | Roll back all writes for an `operation_id` |

```shell
# Health probe
curl 'http://hssip:8080/oam/ping'

# See which Diameter peers are connected
curl 'http://hssip:8080/oam/diameter_peers'
```

### PCRF — `/pcrf`

Dynamic Gx / Rx actions against currently attached subscribers, plus emergency
subscriber management.

| Method & path | Description |
|---------------|-------------|
| `PUT /pcrf/` | Push a predefined charging rule to a subscriber (Gx RAR) |
| `GET /pcrf/<charging_rule_id>` | Get a charging rule with its resolved TFT list |
| `PUT /pcrf/clr_subscriber` | Trigger a Cancel Location Request for a subscriber |
| `PUT /pcrf/pcscf_restoration_subscriber` | Trigger P-CSCF restoration for one IMS subscriber |
| `PUT /pcrf/pcscf_restoration` | Trigger P-CSCF restoration for all subscribers on a P-CSCF |
| `GET /pcrf/pcrf_subscriber/list` | List all PCRF-served subscribers |
| `GET /pcrf/pcrf_subscriber_imsi/<imsi>` | PCRF session info for an IMSI (all APNs) |
| `GET /pcrf/pcrf_subscriber_imsi/<imsi>/<apn_id>` | PCRF session info for an IMSI on one APN |
| `GET /pcrf/pcrf_serving_apn_ip/<ip_address>` | Find the serving APN by UE IP |
| `GET /pcrf/subscriber_routing/<subscriber_routing>` | Look up a subscriber routing entry |
| `PUT /pcrf/emergency_subscriber/` | Create an emergency subscriber |
| `GET /pcrf/emergency_subscriber/<emergency_subscriber_id>` | Get an emergency subscriber |
| `PATCH /pcrf/emergency_subscriber/<emergency_subscriber_id>` | Update an emergency subscriber |
| `DELETE /pcrf/emergency_subscriber/<emergency_subscriber_id>` | Delete an emergency subscriber |
| `GET /pcrf/emergency_subscriber/list` | List emergency subscribers (paginated) |

```shell
# Install charging rule 1 on APN 1 for an attached subscriber
curl -X PUT 'http://hssip:8080/pcrf/' -H 'Content-Type: application/json' \
  -d '{"imsi":"001010000000001","apn_id":1,"charging_rule_id":1}'
```

### GeoRed — `/geored`

Geographic redundancy state replication. The `PATCH /geored/` endpoint is normally
called by peer HSS nodes, not by integrators.

| Method & path | Description |
|---------------|-------------|
| `PATCH /geored/` | Apply pushed GeoRed state (MME/CSCF/APN/EIR/SQN/emergency, …) |
| `GET /geored/` | Return the active GeoRed schema |
| `GET /geored/peers` | List configured GeoRed peers |
| `PATCH /geored/peers` | Replace the list of GeoRed peer endpoints |
| `GET /geored/webhooks` | List configured webhook endpoints |

### Push — `/push`

| Method & path | Description |
|---------------|-------------|
| `PUT /push/clr/<imsi>` | Push a Cancel Location Request to a specific MME |

```shell
curl -X PUT 'http://hssip:8080/push/clr/001010000000001' \
  -H 'Content-Type: application/json' \
  -d '{
    "DestinationRealm": "epc.mnc001.mcc001.3gppnetwork.org",
    "DestinationHost": "mme01",
    "cancellationType": 2,
    "diameterPeer": "mme01.epc...",
    "immediateReattach": true
  }'
```

`cancellationType` follows 3GPP TS 29.272 §7.3.24.

---

## Integrating from Python

The API is plain HTTP + JSON, so the standard `requests` library is all you need.
A complete, runnable set of examples lives in
[`tests/test_API.py`](../tests/test_API.py).

```python
import requests

BASE_URL = "http://hssip:8080"
# Only needed when hss.lock_provisioning is True:
HEADERS = {
    "Content-Type": "application/json",
    # "Provisioning-Key": "changeThisKeyInProduction",
}


def create(path, body):
    r = requests.put(f"{BASE_URL}{path}", json=body, headers=HEADERS)
    r.raise_for_status()
    return r.json()


# 1. SIM auth data
auc = create("/auc/", {
    "ki": "465B5CE8B199B49FAA5F0A2EE238A6BC",
    "opc": "E8ED289DEBA952E4283B54E88E6183CA",
    "amf": "8000",
    "sqn": 0,
})

# 2. APN
apn = create("/apn/", {
    "apn": "internet",
    "apn_ambr_dl": 9999999,
    "apn_ambr_ul": 9999999,
    "qci": 9,
    "arp_priority": 1,
    "arp_preemption_capability": True,
    "arp_preemption_vulnerability": False,
})

# 3. Subscriber, referencing the IDs created above
subscriber = create("/subscriber/", {
    "imsi": "001010000000001",
    "enabled": True,
    "auc_id": auc["auc_id"],
    "default_apn": apn["apn_id"],
    "apn_list": str(apn["apn_id"]),
    "apn_list_swx": str(apn["apn_id"]),
    "msisdn": "491792021244",
    "ue_ambr_dl": 9999999,
    "ue_ambr_ul": 9999999,
    "nam": 0,
    "subscribed_rau_tau_timer": 600,
})

# Read it back by IMSI
print(requests.get(f"{BASE_URL}/subscriber/imsi/001010000000001").json())
```

### Tips

- Generate a typed client from `/swagger.json` for larger integrations.
- Capture the IDs returned by create (`PUT`) calls — downstream objects reference
  them (e.g. a subscriber references `auc_id` and APN IDs).
- Group related writes with the same `?operation_id=...` so they can be rolled back
  together if a batch fails.
- Use `GET /oam/ping` as a readiness/liveness probe.

## See also

- [Provisioning quick-start](provisioning.md)
- [Databases](databases.md)
- [GeoRedundancy](geored.md)
- [EIR](EIR.md)
- [Zn / GBA](Zn.md)
