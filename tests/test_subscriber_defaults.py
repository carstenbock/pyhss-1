# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A subscriber provisioned without a Network Access Mode must be packet-only.

The platform has no CS domain. With PACKET_AND_CIRCUIT the MME grants a
combined EPS/IMSI attach, the phone tries CS fallback for its calls, and
the call fails while the phone detaches (KI-121)."""
from database import Database
from logtool import LogTool
from pyhss_config import config

ONLY_PACKET = 2


def test_subscriber_without_nam_is_packet_only(create_test_db):
    # The fixture creates its subscriber without "nam", as an API client may.
    database = Database(LogTool(config))
    subscriber = database.Get_Subscriber(imsi="262423403000001")
    assert subscriber["nam"] == ONLY_PACKET
