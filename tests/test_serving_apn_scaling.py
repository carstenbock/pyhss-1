# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Setting up a PDN session must cost the same whether the HSS holds ten
sessions or fifty thousand. It once read every stored session to find the ones
holding the new session's address, on each node that received the update, and
the HSS ran out of CPU at about 10,000 attached subscribers."""
from database import Database, SERVING_APN
from logtool import LogTool
from pyhss_config import config

AUC_VALUES = {"ki": "3c6e0b8a9c15224a8228b9a98ca1531d", "opc": "762a2206fe0b4151ace403c86a11e479", "amf": "8000", "sqn": 0}
SUBSCRIBER_VALUES = {"default_apn": 1, "apn_list": "1"}
IMS_VALUES = {"pcscf_realm": "ims.example.org"}
FIRST_IMSI = "001019997000000"
PGW = "pgw.epc.example.org"


def attach(database, imsi, address, session_id):
    database.Update_Serving_APN(imsi=imsi, apn="internet", pcrf_session_id=session_id, serving_pgw=PGW,
                                subscriber_routing=address, propagate=False)


def sessions(database, rows):
    found = {}
    for row in rows:
        try:
            found[row["imsi"]] = database.Get_Serving_APN(subscriber_id=row["subscriber_id"], apn_id=1)["subscriber_routing"]
        except Exception:
            pass
    return found


def test_new_session_does_not_read_the_other_sessions(create_test_db, monkeypatch):
    database = Database(LogTool(config))
    rows = database.Bulk_Create_Subscribers(FIRST_IMSI, "494099997000000", 3, AUC_VALUES, SUBSCRIBER_VALUES, IMS_VALUES)
    try:
        attach(database, rows[0]["imsi"], "10.46.0.1", "pgw;1")
        attach(database, rows[1]["imsi"], "10.46.0.2", "pgw;2")

        read_in_full = []
        get_all = database.GetAll
        monkeypatch.setattr(database, "GetAll", lambda obj_type: read_in_full.append(obj_type) or get_all(obj_type))
        attach(database, rows[2]["imsi"], "10.46.0.3", "pgw;3")

        assert SERVING_APN not in read_in_full
        assert sessions(database, rows) == {rows[0]["imsi"]: "10.46.0.1", rows[1]["imsi"]: "10.46.0.2",
                                            rows[2]["imsi"]: "10.46.0.3"}
    finally:
        database.Bulk_Delete_Subscribers(FIRST_IMSI, 3)


def test_address_handed_out_again_replaces_the_session_that_held_it(create_test_db):
    """One address belongs to one PDN session: when the PGW gives an address to
    a second subscriber, the first one's session ended without the HSS being
    told, and its row must go. Sessions on other addresses stay."""
    database = Database(LogTool(config))
    rows = database.Bulk_Create_Subscribers(FIRST_IMSI, "494099997000000", 3, AUC_VALUES, SUBSCRIBER_VALUES, IMS_VALUES)
    try:
        attach(database, rows[0]["imsi"], "10.46.0.1", "pgw;1")
        attach(database, rows[1]["imsi"], "10.46.0.2", "pgw;2")
        attach(database, rows[2]["imsi"], "10.46.0.1", "pgw;3")

        assert sessions(database, rows) == {rows[1]["imsi"]: "10.46.0.2", rows[2]["imsi"]: "10.46.0.1"}
    finally:
        database.Bulk_Delete_Subscribers(FIRST_IMSI, 3)
