# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An AuC entry provisioned without an SQN must still authenticate.

The API accepted an AuC without "sqn" and stored NULL; every vector request
for it (MAR for SWx/Cx, AIR for S6a) then failed with int(None), the HSS
answered DIAMETER_UNABLE_TO_COMPLY and the UE never attached or registered
(volte.io KI-251)."""
from database import Database, AUC
from logtool import LogTool
from pyhss_config import config

KI = "465B5CE8B199B49FAA5F0A2EE238A6BC"
OPC = "CD63CB71954A9F4E48A5994E37A02BAF"


def _auc_without_sqn(database, auc_id):
    # As an API client sends it, following the old provisioning guide
    database.CreateObj(AUC, {"auc_id": auc_id, "ki": KI, "opc": OPC, "amf": "8000",
                             "imsi": "26242340300%04d" % auc_id})


def test_auc_created_without_sqn_starts_at_one(create_test_db):
    database = Database(LogTool(config))
    _auc_without_sqn(database, 51)
    assert database.GetObj(AUC, 51)["sqn"] == 1


def test_auc_created_without_sqn_authenticates(create_test_db):
    database = Database(LogTool(config))
    _auc_without_sqn(database, 52)
    vector = database.Get_Vectors_AuC(52, "sip_auth", plmn="62f242")
    assert len(vector["SIP_Authenticate"]) == 32
    # The next request uses a fresh SQN, as for any other entry
    assert database.GetObj(AUC, 52)["sqn"] == 101


def test_stored_null_sqn_authenticates(create_test_db):
    # An entry created before the default holds NULL in the database
    database = Database(LogTool(config))
    _auc_without_sqn(database, 53)
    database.UpdateObj(AUC, {"sqn": None}, 53, True)
    assert database.GetObj(AUC, 53)["sqn"] is None
    for action in ("air", "sip_auth"):
        database.Get_Vectors_AuC(53, action, plmn="62f242")
    assert database.GetObj(AUC, 53)["sqn"] == 201
