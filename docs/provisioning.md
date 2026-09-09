# PyHSS API Service

Using the PyHSS API we can provision objects into the HSS.

**Note:** Complete documentation on using every API Ednpoint on the HSS is included in the generated Swagger documentation on the `/docs/` endpoint, this doc you're reading now is just an a quick-start guide.

General flow to setup network:
 * Define Charging Rules & TFTs (If using dedicated bearers)
 * Define APNs

General flow to setup subscriber for access to data services:
 * Define SIM Card data in AuC
 * Define Subscriber w allowed APNs & AMBRs

General flow to setup subscriber for access to voice services:
 * Define IMS Subscriber w allowed Sh Profile & iFC Template


### Define Subscriber AuC Object
The AuC Objects store information about the SIMs deployed in the network, it's up to you how much information you want to store for each SIM, PyHSS supports storing pretty much all of the key data for OTA, eSIM / SMDP, etc, however these fields are optional.

At a minimum you will need to specify the OPc/Ki & AMF of the card.

If your data output only include the OP you can use the `CryptoTool.py` from the `lib` folder to convert the OP values into OPc values.

```shell
curl -X 'PUT' \
  'http://10.97.0.36:8080/auc/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
    "ki": "11111111111111111111111111111111",
    "opc": "11111111111111111111111111111111",
    "amf": "8000"
  }'
```



### Define Subscriber for EPC Access
This defines IMSI 001010000000001 with access to APN with APN_ID 1 & APN_ID 2, where the APN with APN_ID 1 is the default APN for the subscriber. The AMBR values allow for 9999999 bytes per second (~8Mbps).

The subscriber's allowed APNs are configured per Diameter interface:

- `apn_list` -- comma-separated APN IDs returned in the S6a Update-Location-Answer (mobile / 3GPP access via MME). This field is required.
- `apn_list_swx` -- comma-separated APN IDs returned in the SWx Server-Assignment-Answer (untrusted non-3GPP access via ePDG). Optional. When NULL or empty the SWx SAA is rejected with `DIAMETER_ERROR_USER_NO_NON_3GPP_SUBSCRIPTION (5450)` per 3GPP TS 29.273 §5.2.2.4, so VoWiFi attach is denied. Operators that want VoWiFi must list at least the IMS APN here.

```shell
curl -X 'PUT' \
  'http://10.97.0.36:8080/subscriber/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "imsi": "362300000000301",
  "enabled": true,
  "auc_id": 1,
  "default_apn": 1,
  "apn_list": "1,2",
  "apn_list_swx": "1",
  "msisdn": "599416501",
  "ue_ambr_dl": 9999999,
  "ue_ambr_ul": 9999999,
  "nam": 0,
  "subscribed_rau_tau_timer": 0
}'
```

### Define an iFC Template
IMS subscribers reference an Initial Filter Criteria (iFC) template stored in the
database via `ifc_template_id`. Create the template first; `template_content` is a
Jinja2 XML document whose `iFC_vars` placeholders are resolved per-subscriber at
registration time.

```shell
curl -X 'PUT' \
  'http://10.97.0.36:8080/ifc_template/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "name": "default_ifc",
  "description": "Default VoLTE iFC",
  "template_content": "<?xml version=\"1.0\" encoding=\"UTF-8\"?><IMSSubscription>...</IMSSubscription>"
}'
```

The response contains the assigned `ifc_template_id`.

### Define Subscriber for IMS Access
This defines a subscriber for access to the IMS, referencing the
`ifc_template_id` created above.

Multiple MSISDNs can be defined as comma separated values in `msisdn_list` as required.

The XCAP/Sh profile will need to be updated with a valid profile for the sub.
```shell
curl -X 'PUT' \
  'http://10.97.0.36:8080/ims_subscriber/' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "msisdn": "12341235",
  "msisdn_list": "12341235",
  "imsi": "001010000000002",
  "ifc_template_id": 1,
  "xcap_profile": "<?xml version=\"1.0\" encoding=\"UTF-8\"?><simservs>Your XCAP Data...</simservs>"
}'
```

> The legacy `ifc_path` field (a path to an iFC file on disk) is **deprecated**;
> use `ifc_template_id` to reference a database-stored iFC template instead.