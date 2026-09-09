# Copyright 2025 sysmocom - s.f.m.c. GmbH <info@sysmocom.de>
# Copyright 2026 Lennart Rosam <hello@takuto.de>
# Copyright 2026 eta <eta@eta.st>
# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import sys
from pathlib import Path

import yaml
from gsup.protocol.gsup_msg import GMMCause

config = None


def _as_bool(val):
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def _split_urls(val):
    if not val:
        return []
    return [u.strip() for u in str(val).replace(",", " ").split() if u.strip()]


def _apply_env_overrides(cfg):
    """Overlay Helm / Docker env vars that are not always in config.yaml.template.

    Helm injects ENABLE_GEO_REDUNDANCY, SYNC_ENDPOINTS, SH_NOTIFY_ENDPOINTS,
    RX_NOTIFY_ENDPOINTS. Docker compose uses GEORED_ENABLED / GEORED_ENDPOINT_N.
    """
    if not isinstance(cfg, dict):
        cfg = {}

    geored = cfg.setdefault("geored", {})
    enabled = os.environ.get("ENABLE_GEO_REDUNDANCY", os.environ.get("GEORED_ENABLED"))
    if enabled is not None:
        geored["enabled"] = _as_bool(enabled)

    endpoints = os.environ.get("SYNC_ENDPOINTS")
    if endpoints is None:
        collected = []
        i = 1
        while True:
            ep = os.environ.get(f"GEORED_ENDPOINT_{i}")
            if not ep:
                break
            collected.append(ep.strip())
            i += 1
        if collected:
            endpoints = " ".join(collected)
    if endpoints is not None:
        geored["endpoints"] = _split_urls(endpoints)

    actions = os.environ.get("GEORED_SYNC_ACTIONS")
    if actions:
        geored["sync_actions"] = [a.strip() for a in actions.replace(",", " ").split() if a.strip()]
    elif isinstance(geored.get("sync_actions"), str):
        geored["sync_actions"] = [a.strip() for a in geored["sync_actions"].replace(",", " ").split() if a.strip()]
    if not geored.get("sync_actions"):
        geored["sync_actions"] = ["HSS", "IMS", "PCRF", "EIR"]

    hss = cfg.setdefault("hss", {})
    sh_notify = os.environ.get("SH_NOTIFY_ENDPOINTS")
    if sh_notify is not None:
        hss["sh_notify_endpoints"] = _split_urls(sh_notify)
    rx_notify = os.environ.get("RX_NOTIFY_ENDPOINTS")
    if rx_notify is not None:
        hss["rx_notify_endpoints"] = _split_urls(rx_notify)
    swx_aaa = os.environ.get("SWX_AAA_DESTINATION_HOSTS")
    if swx_aaa is not None:
        hss["swx_aaa_destination_hosts"] = [h.strip() for h in swx_aaa.replace(" ", ",").split(",") if h.strip()]

    return cfg


def load_config():
    global config

    if "PYHSS_CONFIG" in os.environ:
        paths = [os.environ["PYHSS_CONFIG"]]
        if not os.path.exists(paths[0]):
            print(f"ERROR: PYHSS_CONFIG is set, but file does not exist: {paths[0]}")
            sys.exit(1)
    else:
        paths = [
            "/etc/pyhss/config.yaml",
            "/usr/share/pyhss/config.yaml",
            Path(__file__).resolve().parent.parent / "config.yaml",
        ]

    for path in paths:
        if os.path.exists(path):
            with open(path, "r") as stream:
                config = _apply_env_overrides(yaml.safe_load(stream))
            return

    print("ERROR: failed to find PyHSS config, tried these paths:")
    for path in paths:
        print(f" * {path}")
    sys.exit(1)


def validate_config():
    """Validate configuration values at startup.

    Refuses to start if config options have unexpected values,
    preventing silent misconfiguration. Add future validations here."""

    valid_reject_causes = {"IMSI_UNKNOWN", "ROAMING_NOT_ALLOWED"}
    reject_cause = (
        config.get("hss", {}).get("roaming", {}).get("inbound", {}).get("reject_unknown_imsis_with", "IMSI_UNKNOWN")
    )
    if reject_cause not in valid_reject_causes:
        print(
            f"ERROR: invalid value for hss.roaming.inbound.reject_unknown_imsis_with: '{reject_cause}'. "
            f"Valid options are: {', '.join(sorted(valid_reject_causes))}"
        )
        sys.exit(1)


load_config()
validate_config()


def get_unknown_subscriber_2g_reject_cause() -> GMMCause:
    if (
        config.get("hss", {}).get("roaming", {}).get("inbound", {}).get("reject_unknown_imsis_with", "IMSI_UNKNOWN")
        == "ROAMING_NOT_ALLOWED"
    ):
        return GMMCause.ROAMING_NOTALLOWED
    else:
        return GMMCause.IMSI_UNKNOWN
