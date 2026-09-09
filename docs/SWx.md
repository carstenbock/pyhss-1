# SWx-Interface Implementation for PyHSS

## Overview

This implementation extends PyHSS with the **SWx interface** between the
**3GPP AAA Server** and the **HSS** according to **3GPP TS 29.273** clauses
8.1 and 8.2. SWx is the Diameter interface used to authorise, register and
update subscribers that attach to EPC via **untrusted non-3GPP access**
(primarily VoWiFi via ePDG) — the AAA Server pulls authentication vectors
and the non-3GPP subscription profile from the HSS, and the HSS can push
profile updates or terminate the registration.

Reference specifications:

- **3GPP TS 29.273** §8.1/§8.2 — SWx Diameter application.
- **3GPP TS 33.402** §6.2 — EAP-AKA' key derivation (CK'/IK').
- **3GPP TS 23.003** §19.3.2 — NAI format for non-3GPP access.
- **IETF RFC 4187** — EAP-AKA.
- **IETF RFC 5448** — EAP-AKA'.
- **IETF RFC 6733** §7 — Result-Code vs Experimental-Result.

## Architecture

```
UE ───(IKEv2/EAP-AKA')───► ePDG ──SWm──► 3GPP AAA Server ──SWx──► PyHSS
                                                                    │
                           SWx MAR/MAA  (AVP) ◄──── vectors ────────┤
                           SWx SAR/SAA  (AVP) ◄──── profile ────────┤
                           SWx PPR/PPA  (AVP) ◄──── HSS push ───────┤
                           SWx RTR/RTA  (AVP) ◄──── HSS teardown ───┘
```

- **Application-Id:** `16777265`
- **Vendor-Id:** `10415`
- **Auth-Session-State:** `NO_STATE_MAINTAINED (1)` (per spec).

## Supported Commands

| Code | Command                          | Direction  | Status |
|------|----------------------------------|------------|--------|
| 303  | Multimedia-Auth Request / Answer | AAA ↔ HSS  | ✓      |
| 301  | Server-Assignment Request / Answer | AAA ↔ HSS | ✓      |
| 305  | Push-Profile Request / Answer    | HSS → AAA  | ✓      |
| 304  | Registration-Termination Request / Answer | HSS → AAA | ✓ |

## Configuration

### `config.yaml`

```yaml
hss:
  # Enable SWx Interface (3GPP AAA Server <-> HSS, TS 29.273) for VoWiFi /
  # ePDG. Supports MAR/MAA (EAP-AKA / EAP-AKA'), SAR/SAA and HSS-initiated
  # PPR/RTR.
  SWx_enabled: True
```

### Helm (`hss-chart/values.yaml`)

```yaml
hss:
  # Default "True" — required for VoWiFi deployments.
  swxEnabled: "True"
```

The chart's ConfigMap exports `SWX_ENABLED` and the container's
`docker-entrypoint.d/10-configure-interfaces.sh` substitutes it into
`config.yaml`.

When `SWx_enabled` is `False`, PyHSS does **not** advertise Application-Id
`16777265` in its CER (see `peerconnect()` in `lib/diameter.py`).

## MAR / MAA (303) — EAP-AKA / EAP-AKA'

### Request (AAA → HSS)

| AVP                               | Code (Vendor) | Required | Notes |
|-----------------------------------|---------------|----------|-------|
| User-Name                         | 1             | M        | NAI per TS 23.003 §19.3.2 (prefix `0` = EAP-AKA, `6` = EAP-AKA') |
| SIP-Auth-Data-Item                | 612 (10415)   | M        | grouped |
| ├─ SIP-Authentication-Scheme      | 608 (10415)   | M        | `"EAP-AKA"` or `"EAP-AKA'"` |
| ├─ SIP-Authorization              | 610 (10415)   | O        | AUTS for re-sync (not yet implemented) |
| SIP-Number-Auth-Items             | 607 (10415)   | M        | requested number of vectors |
| Access-Network-Identifier (ANID)  | 1263 (10415)  | C        | required for EAP-AKA' |
| Visited-Network-Identifier        | 600 (10415)   | O        | |

### NAI parsing

The `_swx_parse_nai()` helper in `lib/diameter.py` recognises:

- `0<IMSI>@nai.epc.mnc<MNC>.mcc<MCC>.3gppnetwork.org` — EAP-AKA permanent.
- `6<IMSI>@nai.epc.mnc<MNC>.mcc<MCC>.3gppnetwork.org` — EAP-AKA' permanent.
- `2/3/4/5/7…` — pseudonym / fast-reauth (currently rejected, reserved for
  future AuC pseudonym store).
- Realm parsing: MNC / MCC are extracted from `mnc<3>.mcc<3>` when present
  but the IMSI lookup is driven off the username portion only.

### Authentication vector generation

For each requested item (`min(SIP-Number-Auth-Items, max_items)`):

1. `database.Get_Vectors_AuC(auc_id, action="sip_auth")` produces
   `(RAND, AUTN, XRES, CK, IK)` via Milenage.
2. For `EAP-AKA'`: `derive_eap_aka_prime_keys()` in `lib/S6a_crypt.py`
   computes `(CK', IK') = HMAC-SHA-256(CK‖IK, "EAP-AKA'"_prime_KDF_input)`
   per TS 33.402 Annex A.2 / RFC 5448 §3.3, using:
   - `FC = 0x20`
   - `P0 = Access-Network-Identifier` (e.g. `"WLAN"`)
   - `P1 = SQN ⊕ AK` (lifted from `AUTN[0:6]`)

### Answer (HSS → AAA)

```
SIP-Auth-Data-Item (612) [repeated `num_items` times]
  ├─ SIP-Item-Number (613)
  ├─ SIP-Authentication-Scheme (608)      # echoed from request
  ├─ SIP-Authenticate (609)                # RAND || AUTN (32 octets)
  ├─ SIP-Authorization (610)               # XRES
  ├─ Confidentiality-Key (625)             # CK or CK'
  └─ Integrity-Key (626)                   # IK or IK'
SIP-Number-Auth-Items (607)                # count actually returned
Result-Code (268) = DIAMETER_SUCCESS (2001)
```

## SAR / SAA (301) — Registration & Profile Download

### Server-Assignment-Type (AVP 614) handling

| Value | Name                               | HSS action |
|-------|------------------------------------|------------|
| 1     | `REGISTRATION`                     | Bind `3GPP-AAA-Server-Name` (AVP 318) for this IMSI in Redis, return profile |
| 2     | `RE_REGISTRATION`                  | Refresh binding, return profile |
| 3     | `UNREGISTERED_USER`                | Return profile without binding (AAA inspection) |
| 5     | `USER_DEREGISTRATION`              | Clear binding |
| 6     | `ADMINISTRATIVE_DEREGISTRATION`    | Clear binding |
| 12    | `AAA_USER_DATA_REQUEST`            | Return profile without re-binding |

If a registration attempt arrives while the subscriber is already bound to a
**different** AAA Server, the HSS rejects with
`DIAMETER_ERROR_IDENTITY_ALREADY_REGISTERED (5005)` in Experimental-Result
and includes the stored `3GPP-AAA-Server-Name` so the new AAA can proxy /
defer.

### Non-3GPP-User-Data (AVP 1500)

Built per TS 29.273 §8.2.3.1 — directly carries **repeated**
`APN-Configuration (1430)` entries (not wrapped in `APN-Configuration-Profile
(1429)`, which is S6a-specific) plus `AMBR (1435)`:

```
Non-3GPP-User-Data (1500)
  ├─ Subscription-ID-Type + Subscription-ID-Data
  ├─ Non-3GPP-IP-Access (1501)
  ├─ Non-3GPP-IP-Access-APN (1502)
  ├─ AMBR (1435)
  │    ├─ Max-Requested-Bandwidth-UL (516)
  │    └─ Max-Requested-Bandwidth-DL (515)
  └─ APN-Configuration (1430) [repeated]
       ├─ Context-Identifier (1423)
       ├─ PDN-Type (1456)
       ├─ Service-Selection (493)
       ├─ EPS-Subscribed-QoS-Profile (1431)
       └─ AMBR (1435)
```

APN list and UE-AMBR are read per-subscriber from the HSS DB via
`database.Get_APN()` / `database.Get_SUBSCRIBER_ROUTING()`. If the
subscriber has no APNs configured for non-3GPP access, the HSS rejects
with `DIAMETER_ERROR_USER_NO_NON_3GPP_SUBSCRIPTION (5401)`.

### AAA-Server binding storage

The `3GPP-AAA-Server-Name (318)` is persisted in Redis keyed by IMSI (TTL
aligned with the non-3GPP session) using the existing `redisMessaging`
helpers. This avoids a DB schema change and lets the HSS later address the
correct AAA Server when issuing PPR / RTR.

## PPR (305) — HSS-initiated Push Profile

Triggered when the subscriber's non-3GPP subscription data changes (APN
list, AMBR, barring). The HSS looks up the bound AAA Server in Redis, then
sends:

```
Vendor-Specific-Application-Id (260)      # 16777265 / 10415
Auth-Session-State (277) = 1 (NO_STATE_MAINTAINED)
Origin-Host / Origin-Realm
Destination-Host / Destination-Realm       # from stored AAA-Server-Name
User-Name (1)                              # IMPI / NAI
Non-3GPP-User-Data (1500)                  # updated profile
```

Can also be triggered manually via the API:

```
POST /push/swx/ppr/<imsi>
```

## RTR (304) — HSS-initiated Registration Termination

Used to cancel an ongoing non-3GPP registration (equivalent of S6a CLR for
VoWiFi). The HSS locates the bound AAA Server, sends RTR with
`Deregistration-Reason (615)`, and clears the local binding on send.

API triggers:

```
POST /deregister/<imsi>                     # best-effort fan-out to AAA peers
POST /push/swx/rtr/<imsi>                   # explicit SWx RTR with optional reasonCode / reasonInfo
```

Default `Deregistration-Reason-Code (616)` is `0` (PERMANENT_TERMINATION); use
`1` (NEW_SERVER_ASSIGNED) or `2` (SERVER_CHANGE) per TS 29.229 / TS 29.273.

## Error Reporting

| Code | Vehicle | Meaning |
|------|---------|---------|
| 2001 | Result-Code (268) | DIAMETER_SUCCESS |
| 5001 | Result-Code (268) | DIAMETER_ERROR_USER_UNKNOWN (base code, not Experimental-Result) |
| 5004 | Result-Code (268) | DIAMETER_INVALID_AVP_VALUE |
| 5005 | Result-Code (268) + Failed-AVP (279) | DIAMETER_MISSING_AVP (e.g. missing SIP-Authentication-Scheme) |
| 5005 | Experimental-Result (297) / 10415 | DIAMETER_ERROR_IDENTITY_ALREADY_REGISTERED (SWx-specific re-use of 5005) |
| 5401 | Experimental-Result (297) / 10415 | DIAMETER_ERROR_USER_NO_NON_3GPP_SUBSCRIPTION |
| 4001 | Result-Code (268) | DIAMETER_AUTHENTICATION_REJECTED |

Per RFC 6733 §7, base-protocol codes go in `Result-Code`, vendor-specific
codes in `Experimental-Result`. The SWx-specific 5xxx codes defined in TS
29.273 clause 8.1.4 always use Experimental-Result + Vendor-Id `10415`.

## Current Limitations

- **AUTS re-sync:** MAR containing `SIP-Authorization (AUTS)` is not yet
  used to resynchronise SQN; a fresh vector is returned instead.
- **Pseudonym / fast-reauth NAIs** (`2/3/4/5/7` leading digit) are
  rejected — there is no AuC pseudonym store yet.
- **AAA-Server binding** lives in Redis, not the RDBMS; it will therefore
  not survive a full Redis flush. Migration to a DB-backed table is planned.
- **PPR / RTR are emitted on-demand** via dedicated API endpoints (mirroring
  the S6a CLR pattern) rather than fully integrated into every provisioning
  code path. Operators can still trigger them programmatically from their
  provisioning automation.
- **OC-Supported-Features / Supported-Features** negotiation is not
  implemented (optional per TS 29.273).

## Related Files

| File | Purpose |
|------|---------|
| `lib/diameter.py` | `Answer_16777265_303` (MAA), `Answer_16777265_301` (SAA), `Request_16777265_305` (PPR), `Request_16777265_304` (RTR), SWx helper methods, dispatch table |
| `lib/S6a_crypt.py` | `derive_eap_aka_prime_keys()` for CK'/IK' |
| `lib/database.py` | `Get_Vectors_AuC`, `Get_APN`, `Get_SUBSCRIBER_ROUTING` — reused from S6a |
| `services/apiService.py` | `/push/swx/ppr/<imsi>`, `/push/swx/rtr/<imsi>`, `/deregister/<imsi>` hooks |
| `config.yaml` | `hss.SWx_enabled` toggle |
| `docker-entrypoint.d/10-configure-interfaces.sh` | `SWX_ENABLED` env var substitution |
| `hss-chart/values.yaml` | `hss.swxEnabled` Helm value |
