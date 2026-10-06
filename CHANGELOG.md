# Changelog

All notable changes to PyHSS are documented in this file, beginning from [Service Overhaul #168](https://github.com/nickvsnetworking/pyhss/pull/168).

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `PUT /oam/bulk/subscribers` creates up to 50,000 consecutive subscribers (AuC, subscriber and IMS subscriber) in one transaction, `DELETE /oam/bulk/subscribers` deletes an IMSI range and `GET /oam/bulk/subscribers` counts it. They bypass the operation log, webhooks, geored and ENUM, and deleting does not deregister; they are meant for load-test ranges, where row-by-row requests take about a day per million subscribers.
- `GET /oam/serving_subs_ims/count` returns the number of IMS-registered subscribers without loading every row.

### Changed

- Database schema version 5 adds the indexes `ix_ims_subscriber_imsi` and `ix_subscriber_msisdn`. Cx and Sh look IMS subscribers up by IMSI on every transaction, which was a full table scan. On a replicated setup the master must be upgraded before the replicas start the new version.
- The database methods no longer run `Base.metadata.create_all()` on every read; the schema is prepared once at start-up.

### Fixed

- Diameter over SCTP: the listening socket now sets `SCTP_NODELAY` and a default payload protocol identifier of 46, and accepted associations inherit both. Without the first, an answer sent while the peer still delayed its SACK for the previous one waited up to 200 ms for it (RFC 3539 section 3.2: Nagle is not used with SCTP); without the second, every message left with PPID 0 instead of the 46 that RFC 6733 section 2.1.1 assigns to clear-text Diameter. Each Diameter message is also sent in its own `send()` on both transports: messages written while the socket was not writable were joined in asyncio's buffer and would have left as one SCTP message.
- Diameter: the inbound worker now forwards a request to the HSS service as soon as it arrives and batches only what is already queued behind it. It used to collect requests for a fixed 100 ms window (the docstring said 10 ms), which added a uniform 0–100 ms wait to every answer, about 50 ms on average and about 200 ms per IMS registration with its four Cx requests.

## [1.7.13] - 2026-09-08

### Added

- ENUM: an SMS reachability NAPTR (`E2U+sms:tel`, RFC 4355) is written next to the `E2U+sip` record for every provisioned MSISDN when the new `enum.sms_enabled` is set. It asserts that the number can receive SMS and is not a delivery target -- IANA registers the `sms` Enumservice only with the `tel` and `mailto` subtypes, so it resolves to a `tel:` URI, and delivery still follows `E2U+sip`. Writing it separately lets SMS be withdrawn for a subscriber without touching voice, and lets a message centre tell "not our subscriber" from "ours, but no SMS". Off by default, so existing zones are unchanged. (image `hss:1.7.14`)
- ENUM: `GET /oam/reconcile/enum` now reads each zone back and reports the MSISDNs that have NAPTR records with no IMS subscriber behind them, under a new `orphaned` key. Orphans are reported and never deleted: an orphan is as likely to mean the database lost a subscriber as that DNS kept a stale one, and deleting on that guess would take a reachable number off the air. A zone that cannot be read is recorded as a skipped check rather than as "no orphans". (image `hss:1.7.14`)
- ENUM: reconciliation publishes `enum_reconcile_timestamp_seconds`, `enum_reconcile_corrected`, `enum_reconcile_failed` and `enum_reconcile_orphaned` as gauges, so an operator can alert on the zone and the database having drifted apart rather than reading a JSON response. Gauges rather than counters because the question is whether the zone is consistent *now*, which a monotonic total cannot answer. (image `hss:1.7.14`)

## [1.7.12] - 2026-09-04

### Added

- New `pcrf` teardown domain (default on subscriber delete, `PATCH /subscriber` with `enabled: false`, `GET /oam/deregister/<imsi>` and `POST /geored/network_teardown`): sends a Gx RAR carrying Session-Release-Cause `UE_SUBSCRIPTION_REASON` per serving APN (TS 29.212 §4.5.9.6), so the PCEF tears the IP-CAN session down with a CCR-T and the existing handler aborts the P-CSCF's Rx AF session. When the Gx peer is not connected the Rx ASR is sent directly instead of leaking the AF session (image `hss:1.7.12`).

### Changed

- Cx MAR, UAR and SAR, and the Zh/GBA MAR now reject an administratively disabled subscriber with Result-Code 5003 (`DIAMETER_AUTHORIZATION_REJECTED`), matching the SWx handlers. De-registration requests are exempt (Cx UAR `User-Authorization-Type` `DE_REGISTRATION`, Cx SAR assignment types other than `REGISTRATION`/`RE_REGISTRATION`) so stored S-CSCF state can still be cleared (image `hss:1.7.12`).
- SWx RTR is now sent only to the 3GPP AAA Server bound by the SAR (TS 29.273 §8.1.2.3) instead of additionally fanning out to every host in `SWX_AAA_DESTINATION_HOSTS`; the list is now a fallback used only when no binding exists (image `hss:1.7.12`).
- The `swx_aaa_server:<imsi>` binding is written only for registration/update Server-Assignment-Types, its TTL raised from 24 h to 7 days to match the AAA's established-session lifetime, and it is deleted on de-registration types (image `hss:1.7.12`).

### Fixed

- SWx SAR no longer rejects a disabled subscriber's de-registration with 5003: the `enabled` check ran before `Server-Assignment-Type` was parsed, so `USER_DEREGISTRATION` was refused and the AAA binding was never released (image `hss:1.7.12`).

### Removed

- Dead `deregisterApn()` helper. It had no callers and could never send a message (`serving_pgw` was assigned to the realm variable, so its guard was always false), and it used a PCRF-originated CCR, which TS 29.212 does not define; the `pcrf` teardown domain supersedes it (image `hss:1.7.12`).

## [1.7.11] - 2026-09-04

### Fixed

- SWx MAR/SAR now reject a disabled subscriber with Result-Code 5003 (`DIAMETER_AUTHORIZATION_REJECTED`) so VoWiFi cannot re-attach after `enabled: false` (image `hss:1.7.11`).

## [1.7.10] - 2026-09-03

### Fixed

- SWx RTR now sets Destination-Host to the bound 3GPP-AAA-Server-Name (from SAR) or `SWX_AAA_DESTINATION_HOSTS`, so the DRA does not loop Application-Id 16777265 back to the HSS (image `hss:1.7.10`).

## [1.7.9] - 2026-09-03

### Fixed

- Provisioning API now logs Diameter-relay HTTP status and treats non-2xx as failure; SWx RTR is no longer logged as sent when the Diameter node has no AAA/DRA peers (image `hss:1.7.9`).

## [1.7.8] - 2026-09-03

### Added

- HSS-initiated network teardown on subscriber delete, IMS-subscriber delete, `PATCH /subscriber` with `enabled: false`, and `GET /oam/deregister/<imsi>`: sends S6a CLR (Cancellation-Type `SUBSCRIPTION_WITHDRAWAL`), Cx RTR with stored IMPUs, and SWx RTR to AAA peers; clears serving MME/S-CSCF state (image `hss:1.7.8`).
- Cx Push-Profile (PPR/PPA, command 305): sent to the serving S-CSCF when an IMS subscriber's `ifc_template_id`, `ifc_path`, `msisdn`, or `msisdn_list` changes, and fan-out to registered subscribers when an iFC template's `template_content` is updated (image `hss:1.7.8`).
- Shared `network_control` helper for CLR/Cx-RTR/SWx-RTR teardown and Cx PPR profile push; API responses include a `warnings` list for best-effort Diameter failures. In split provisioning/Diameter deployments the API relays teardown and PPR to Diameter pods via `/geored/network_teardown` and `/geored/push_ims_profile` (image `hss:1.7.8`).

### Fixed

- Cx RTR now includes all stored IMPUs (`sip:+<msisdn>@...`, `tel:+...`, extras from `msisdn_list`) instead of a hardcoded `sip:<imsi>@` identity (image `hss:1.7.8`).
- `PATCH /subscriber` `enabled: false` now reads serving nodes from the database and sends CLR type 2 (was type 1 and required `serving_mme` in the PATCH body) (image `hss:1.7.8`).
- Fixed `deregisterIms()` and targeted Cx RTR call sites that passed `peerType=` instead of `hostname=` to `sendDiameterRequest` (image `hss:1.7.8`).

### Added

- Sh PUR (command 307) now writes `xcap_profile` (and clears the deprecated `sh_profile` column) and georeds `{imsi, xcap_profile}` to peer APIs, including the provisioning master, so Ut/XCAP edits and a master backup see the same repository data. `PATCH /geored/` accepts `xcap_profile` and PNRs local SNR subscriptions without looping through `sh_notify_endpoints`. Helm env vars `ENABLE_GEO_REDUNDANCY`, `SYNC_ENDPOINTS`, `SH_NOTIFY_ENDPOINTS`, and `RX_NOTIFY_ENDPOINTS` overlay `config.yaml` after load (image `hss:1.7.6`).

## [1.7.7] - 2026-03-19

### Added

- Sh Subscribe-Notifications (SNR/SNA, command 308, TS 29.328 §6.1.3): Application Servers can subscribe to (and unsubscribe from) repository-data changes per subscriber and Service-Indication; subscriptions are stored in Redis (shared, unprefixed `sh_subscriptions:<id>` keys) and the SNA attaches the current User-Data when Send-Data-Indication is present (image `hss:1.7.0`).
- Sh Push-Notification (PNR/PNA, command 309, TS 29.328 §6.1.4): on REST PATCH of an `ims_subscriber` that changes `xcap_profile` / `sh_profile`, the API sends a PNR with the updated `<Sh-Data>` (User-Data AVP 702) to every subscribed AS (image `hss:1.7.0`).
- New `POST /geored/sh_profile_updated` endpoint and `hss.sh_notify_endpoints` config key (env `SH_NOTIFY_ENDPOINTS`): in split provisioning/Diameter deployments the provisioning API relays profile changes to the Diameter nodes, which send PNRs for the subscriptions they hold; endpoint hostnames are resolved to all A/AAAA records so one headless-service URL fans out to every node (image `hss:1.7.0`).
- New `POST /geored/rx_terminate_af_subscriptions` endpoint and `hss.rx_notify_endpoints` config key (env `RX_NOTIFY_ENDPOINTS`): in active/active deployments the Rx AF (signalling-bearer) subscription and the Gx session can be handled by different Diameter nodes (each has node-local Redis), so on a Gx CCR-T the handling node now relays the termination to all Diameter nodes and the node holding the subscription sends the Rx ASR (TS 29.214) over its own Diameter link to the AF; endpoint hostnames are resolved to all A/AAAA records so one headless-service URL fans out to every node. The CCR-T handler (`GxCCR3_to_RxSTR`) terminates locally then relays; `rx_terminate_local_af_subscriptions` drops each subscription after aborting it, so the relayed self-call is a no-op (image `hss:1.7.1`).

- ENUM NAPTR replacement URIs are now generated as `sip:+<E.164>@<domain>` so ENUM results match the registered `+E.164` IMPUs (image `hss:1.6.7`).
- GBA Zh interface end-to-end (3GPP TS 29.109 §6): the GBA Multimedia-Authentication (MAR/MAA) handler now registers on the **Zh** Application-Id `16777221` (was the Zn id `16777220`), advertises Zh in the CER/CEA capabilities (gated by `hss.Zn_enabled`), and echoes `16777221` in the MAA Application-Id and `Vendor-Specific-Application-Id`. `Public-Identity` (AVP 601) is now optional on the Zh MAR (GBA is IMPI-based; the BSF does not send an IMPU), so the BSF's MAR is answered with a real Milenage vector instead of being rejected with `DIAMETER_MISSING_AVP`. New `config.yaml` keys `hss.Zn_enabled` and `hss.bsf.{bsf_hostname,gaa_key_lifetime}` (env `ZN_ENABLED`, `BSF_HOSTNAME`, `GAA_KEY_LIFETIME`) wire the handler on (image `hss:1.6.5`).

### Fixed

- Rx AAR/RAA (`Answer_16777236_265`, `Answer_16777236_258`, TS 29.214) now strip a leading `+` (and a `tel:` scheme) from a sip/tel-URI `Subscription-Id` before resolving the subscriber. Since IMPUs are rendered as global E.164 (`sip:+<msisdn>@<domain>`, image `hss:1.6.7`), the P-CSCF (AF) sent `Subscription-Id-Data: sip:+<msisdn>@...`; the handlers looked up the MSISDN with the `+` still attached, which never matched the MSISDN stored without `+`, so `validateImsSubscriber` saw neither IMSI nor MSISDN and the AAR was rejected with `DIAMETER_AUTHENTICATION_REJECTED (4001)`. This aligns the Rx handlers with `Get_IMS_Subscriber_Details_from_AVP` and the Sh resolver, which already strip the `+` (image `hss:1.7.2`).
- Rx AF (signalling-bearer) subscriptions are now stored in Redis instead of the `serving_apn.af_subscriptions` `VARCHAR(1024)` column. The P-CSCF (AF) subscribes to the IMS signalling bearer via an Rx AAR (Media-Type Control, TS 29.214); the old code appended every AF session to a `repr()`-serialised list in a 1024-char column with broken expiry pruning, so after a few calls every AAR failed with `MySQLdb.DataError (1406) "Data too long for column 'af_subscriptions'"` and no dedicated-bearer Rx ASR was ever sent on Gx CCR-T. Subscriptions now live in a per-subscriber/APN Redis hash (`af_subscriptions:<subscriber_id>:<apn_id>`, field = Rx Session-Id, value = `{af_peer, af_realm, af_session_expires}`), mirroring the Sh `sh_subscriptions:<id>` store, with expired entries pruned on read. The legacy column and its broken PCRF geored sync of `af_subscriptions` are removed from the write path (the column is retained but unused) (image `hss:1.7.1`).
- The Diameter library now prefixes the shared Redis keys (`diameterPeers`, `diameter-inbound`, `diameter-outbound-*`) with the configured Diameter Origin-Host instead of the OS hostname, matching diameterService/hssService. This fixes API-initiated Diameter requests (PNR, CLR, RTR, ...) silently failing to find peers or queue messages whenever the container hostname differs from the Origin-Host (image `hss:1.7.0`).
- Fixed swapped `MCC` / `MNC` placeholders in the Docker image `config.yaml`, which produced reversed IMS home network domains (e.g. `ims.mnc262.mcc024...` instead of `ims.mnc024.mcc262...`) in iFC/Sh rendering (image `hss:1.6.7`).
- The GBA MAA `Vendor-Specific-Application-Id` AVP previously encoded a malformed Auth-Application-Id (`0x010055d4`); it is now built from the correct Zh id (`16777221`).
- The Zh/GBA interface failed to initialize whenever `hss.Zn_enabled` was set: `Diameter._initialize_zn_interface()` constructed `ZnInterface(self, self.database, self.config)`, but the `Diameter` class has no `config` attribute (it uses the module-level `config` from `pyhss_config`), so every startup logged `Failed to initialize Zn-Interface: 'Diameter' object has no attribute 'config'` and left GBA disabled. It now passes the module-level `config` dict. The success path also sets `self.zn_enabled = True` (previously the flag was only ever set to `False` on error), so the Zh MAA handler's B-TID logging no longer trips an `AttributeError` and the Zh MAR/MAA handler (3GPP TS 29.109 §6) comes up as intended (image `hss:1.7.4`).

- Sh UDR/UDA per-Data-Reference dispatch (TS 29.328/29.329): the HSS now parses Data-Reference, Service-Indication, Requested-Domain, and Current-Location AVPs from inbound UDR and returns only the requested data. Supported Data-Reference values: RepositoryData (0), IMSPublicIdentity (10), IMSUserState (11), S-CSCFName (12), LocationInformation (14), MSISDN (17), TADSinformation (26), STN-SR (27), UE-SRVCC-Capability (28).
- Sh UDR subscriber lookup now supports `tel:` Public-Identity URIs alongside `sip:` URIs.
- New `ims_subscriber` columns: `stn_sr` (Session Transfer Number for SRVCC) and `ue_srvcc_capability` (UE SRVCC Capability), with `databaseSchema` v4 upgrade.
- T-ADS Information assembly from live subscriber state (VoiceOverPS session support, CS/PS domain registration, SRVCC capability, last UE activity time) for SCC-AS queries.
- Legacy Jinja2 template fallback: UDR without Data-Reference AVPs still renders the full `default_sh_user_data.xml` template for backward compatibility.
- Fixed legacy template bug where `repository_data` was set to empty string instead of `None`, preventing the default MMTEL-Services block from rendering.
- Per-interface APN allowlist on `subscriber`: new `apn_list_swx` column (comma-separated APN IDs) controls which APNs are returned over SWx, separate from the existing `apn_list` used on S6a. Exposed through the REST API (auto-generated `SUBSCRIBER` schema), a new "Allowed APNs (SWx / untrusted ePDG access)" multi-select on the HSS GUI subscriber form, and a `databaseSchema` v3 upgrade that ALTERs existing databases (PyHSS `1.0.3`).
- Documentation: mid-session Gx RAR / `PUT /pcrf/` PCC rule install in `docs/PCRF_Notes.md`

### Changed

- `default_ifc.xml` and `default_sh_user_data.xml` now render MSISDN public identities as global E.164 with leading `+` (`sip:+<msisdn>@<domain>`, `tel:+<msisdn>`). This fixes terminating VoWiFi calls failing with 404 at the IPSec gateway because the dialed `tel:+E164` To URI did not match the registration's P-Associated-URI aliases stored without `+` (image `hss:1.6.7`).
- `Get_IMS_Subscriber_Details_from_AVP` now classifies identities with a leading `+` as MSISDN before applying the 15/16-digit IMSI length heuristic.
- SWx Server-Assignment-Answer (`Answer_16777265_301`) now expands the subscriber's `apn_list_swx` into one `APN-Configuration` AVP per allowed APN inside `Non-3GPP-User-Data`, instead of always returning a single hardcoded `ims` APN. The top-level Non-3GPP-User-Data AMBR now reflects the subscriber's UE-AMBR (was hardcoded 50/100 Mbit/s).
- Sh PUR now stores repository data in `xcap_profile` (clearing `sh_profile`) instead of the deprecated `sh_profile` column, matching the GUI services editor (image `hss:1.7.6`).

### Breaking

- The REST API now rejects subscriber / ims_subscriber MSISDNs that are not global E.164 numbers with a leading `+` (e.g. `+4915888456043`) with HTTP 400. Storage remains digits-only; `GET .../msisdn/<msisdn>` lookups accept both forms (image `hss:1.6.7`).
- SWx Server-Assignment-Request is now rejected with Experimental-Result `DIAMETER_ERROR_USER_NO_NON_3GPP_SUBSCRIPTION (5450)` (3GPP TS 29.273 §5.2.2.4) for any subscriber whose `apn_list_swx` is NULL or empty. After upgrading, operators must populate `apn_list_swx` (typically with the IMS APN's id) on every subscriber that should be allowed VoWiFi/ePDG attach -- running the schema migration alone is not enough.
- 2G / 3G support via Osmocom GSUP.
- Support for running PyHSS services in Docker containers and provide official Docker images.
- Database types postgresql and sqlite.
- Config loading from `/etc/pyhss/config.yaml`, `/usr/share/pyhss/config.yaml`,
  the `PYHSS_CONFIG` env var, or (old behavior) `config.yaml` at the top of the
  source tree, depending on which is available.
- Running services outside of the source tree.
- Building PyHSS with `python3 -m build` and as debian package.
- RAT restriction checking for subscribers.
- Automatic database upgrades (from 1.0.1 or higher).

### Changed

- Set the default database backend to SQLite.

### Removed

- Unused options from config.yaml.
- Debug prints in API service.

### Fixed

- Fix unit tests and run them with pytest in CI.
- Let services/apiService return HTTP status code 500 on errors instead of 200.

## [1.0.2] - 2024-07-03

### Added

- Configurable DWRs sendable to connected peers.
- Configurable outbound roaming rules on a per-network and per-subscriber basis.
- /pcrf/clr_subscriber for ease of use.
- Support for OCS webhook notifications on CCR-I and CCR-T.

### Fixed

- Removed '+' from MSISDNs when storing in the database.
- CCR-based logical bug when emergency attach procedure is performed.
- Repeated ECRs leaking open SQL sessions.
- Forced string evaluation for tacDatabasePath.

## [1.0.1] - 2024-01-23

### Removed

 - Assert on missing "IMS Services" for AAA/Audio Request

### Changed

- Reduced verbosity of failing subscriber lookups to debug
- Added CORS headers: [Zarya/171](https://github.com/nickvsnetworking/pyhss/pull/171)
- Gx RAR now dynamically creates TFT up to 512k based on UE request.
- SQN Resync now propogates via Geored when enabled 
- Renamed sh_profile to xcap_profile in ims_subscriber
- Rebuilt keys using unique namespace for redis-sentinel / stateless compatibility.
- The database schema was changed as follows. If you have a PyHSS database
  created with version 1.0.0 that you would like to use with 1.0.1 or newer,
  apply these changes manually. Newer versions of PyHSS have automatic database
  migrations.
<details>

```diff
--- a/release_1.0.0.sql
+++ b/release_1.0.1.sql
@@ -13,6 +13,12 @@ CREATE TABLE apn (
 	arp_preemption_capability BOOLEAN,
 	arp_preemption_vulnerability BOOLEAN,
 	charging_rule_list VARCHAR(18),
+	nbiot BOOLEAN,
+	nidd_scef_id VARCHAR(512),
+	nidd_scef_realm VARCHAR(512),
+	nidd_mechanism INTEGER,
+	nidd_rds INTEGER,
+	nidd_preferred_data_mode INTEGER,
 	last_modified VARCHAR(100),
 	PRIMARY KEY (apn_id)
 );
@@ -80,22 +86,40 @@ CREATE TABLE eir_history (
 	PRIMARY KEY (imsi_imei_history_id),
 	UNIQUE (imsi_imei)
 );
 CREATE TABLE ims_subscriber (
 	ims_subscriber_id INTEGER NOT NULL,
 	msisdn VARCHAR(18),
 	msisdn_list VARCHAR(1200),
 	imsi VARCHAR(18),
-	ifc_path VARCHAR(18),
+	ifc_path VARCHAR(512),
 	pcscf VARCHAR(512),
 	pcscf_realm VARCHAR(512),
 	pcscf_active_session VARCHAR(512),
 	pcscf_timestamp DATETIME,
 	pcscf_peer VARCHAR(512),
+	xcap_profile TEXT(12000),
 	sh_profile TEXT(12000),
 	scscf VARCHAR(512),
 	scscf_timestamp DATETIME,
 	scscf_realm VARCHAR(512),
 	scscf_peer VARCHAR(512),
+	sh_template_path VARCHAR(512),
 	last_modified VARCHAR(100),
 	PRIMARY KEY (ims_subscriber_id),
 	UNIQUE (msisdn)
@@ -115,6 +139,9 @@ CREATE TABLE operation_log (
 	auc_id INTEGER,
 	subscriber_id INTEGER,
 	ims_subscriber_id INTEGER,
+	roaming_rule_id INTEGER,
+	roaming_network_id INTEGER,
+	emergency_subscriber_id INTEGER,
 	charging_rule_id INTEGER,
 	tft_id INTEGER,
 	eir_id INTEGER,
@@ -127,12 +154,33 @@ CREATE TABLE operation_log (
 	FOREIGN KEY(auc_id) REFERENCES auc (auc_id),
 	FOREIGN KEY(subscriber_id) REFERENCES subscriber (subscriber_id),
 	FOREIGN KEY(ims_subscriber_id) REFERENCES ims_subscriber (ims_subscriber_id),
+	FOREIGN KEY(roaming_rule_id) REFERENCES roaming_rule (roaming_rule_id),
+	FOREIGN KEY(roaming_network_id) REFERENCES roaming_network (roaming_network_id),
+	FOREIGN KEY(emergency_subscriber_id) REFERENCES emergency_subscriber (emergency_subscriber_id),
 	FOREIGN KEY(charging_rule_id) REFERENCES charging_rule (charging_rule_id),
 	FOREIGN KEY(tft_id) REFERENCES tft (tft_id),
 	FOREIGN KEY(eir_id) REFERENCES eir (eir_id),
 	FOREIGN KEY(imsi_imei_history_id) REFERENCES eir_history (imsi_imei_history_id),
 	FOREIGN KEY(subscriber_attributes_id) REFERENCES subscriber_attributes (subscriber_attributes_id)
 );
 CREATE TABLE serving_apn (
 	serving_apn_id INTEGER NOT NULL,
 	subscriber_id INTEGER,
@@ -160,6 +208,8 @@ CREATE TABLE subscriber (
 	ue_ambr_dl INTEGER,
 	ue_ambr_ul INTEGER,
 	nam INTEGER,
+	roaming_enabled BOOLEAN,
+	roaming_rule_list VARCHAR(512),
 	subscribed_rau_tau_timer INTEGER,
 	serving_mme VARCHAR(512),
 	serving_mme_timestamp DATETIME,
```
</details>

### Fixed

- Geored failing when multiple peers defined and socket closes.
- Error in Update_Serving_MME when supplied with a NoneType timestamp.

### Added

- Support for CLR-based PCSCF restoration via `/pcrf/pcscf_restoration` and `/pcrf/pcscf_restoration_subscriber` in API.
- Optional immediateReattach parameter in Request_16777251_317, via CLR-Flags
- Sh-IMS-Data and IMSPrivateUserIdentity to default_sh_user_data.xml
- Optional config parameter `api.enable_insecure_auc` to allow retrieval of AuC keys through API
- sh_template_path in ims_subscriber
- generateUpgade.sh for generating alembic upgrade scripts
- Control of outbound roaming S6a AIR and ULA responses through roaming_rule and roaming_network objects.
- Roaming management on a per-subscriber basis, through subscriber.roaming_enabled and subscriber.roaming_rule_list.
- Support for Gx and Rx auth of unknown subscribers attaching via SOS.
- Preliminary support for SCTP.
- Additional prometheus metrics.

## [1.0.0] - 2023-09-27

### Added

 - Systemd service files for PyHSS services
 - /oam/diameter_peers endpoint
 - /oam/deregister/{imsi} endpoint
 - /geored/peers endpoint
 - /geored/webhooks endpoint
 - Dependency on Redis 7 for inter-service messaging
 - Significant performance improvements under load
 - Basic Rx support for RAA, AAA, ASA and STA
 - Rx MO call flow support (AAR -> RAR -> RAA -> AAA)
 - Dedicated bearer setup and teardown on Rx call
 - Asymmetric geored support
 - Configurable redis connection (Unix socket or TCP)
 - Basic database upgrade support in tools/databaseUpgrade
 - PCSCF state storage in ims_subscriber
 - (Experimental) Working horizontal scalability

### Changed

- Split logical functions of PyHSS into 6 service processes
- Logtool no longer handles metric processing
- Updated config.yaml
- Gx CCR-T now flushes PGW / IMS data, depending on Called-Station-Id
- Benchmarked capability of at least ~500 diameter requests per second with a response time of under 2 seconds on a local network.

### Fixed

 - Memory leaking in diameter.py
 - Gx CCA now supports apn inside a plmn based uri
 - AVP_Preemption_Capability and AVP_Preemption_Vulnerability now presents correctly in all diameter messages
 - Crash when webhook or geored endpoints enabled and no peers defined
 - CPU overutilization on all services

### Removed

- Multithreading in all services, except for metricService

[1.0.0]: https://github.com/nickvsnetworking/pyhss/releases/tag/1.0.0
[1.0.1]: https://github.com/nickvsnetworking/pyhss/releases/tag/1.0.1
[1.0.2]: https://github.com/nickvsnetworking/pyhss/releases/tag/1.0.2
