# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest
import sqlalchemy

from database import AUC, Base, Database, IMS_SUBSCRIBER, SUBSCRIBER
from databaseSchema import DatabaseSchema
from logtool import LogTool
from pyhss_config import config

AUC_VALUES = {"ki": "3c6e0b8a9c15224a8228b9a98ca1531d", "opc": "762a2206fe0b4151ace403c86a11e479", "amf": "8000", "sqn": 0}
SUBSCRIBER_VALUES = {"default_apn": 1, "apn_list": "1"}
IMS_VALUES = {"pcscf_realm": "ims.example.org"}


def create(database, imsi_start, msisdn_start, count):
    return database.Bulk_Create_Subscribers(imsi_start, msisdn_start, count, AUC_VALUES, SUBSCRIBER_VALUES, IMS_VALUES)


def test_bulk_create_links_all_three_rows(create_test_db):
    """A bulk-created subscriber must be usable exactly like one created row by
    row: an AuC row, a subscriber pointing at it and an IMS subscriber, all
    under the same IMSI."""
    database = Database(LogTool(config))
    rows = create(database, "001019999000000", "494099999000000", 3)
    try:
        assert [row["imsi"] for row in rows] == ["001019999000000", "001019999000001", "001019999000002"]
        for row in rows:
            subscriber = database.Get_Subscriber(imsi=row["imsi"])
            assert subscriber["subscriber_id"] == row["subscriber_id"]
            assert subscriber["auc_id"] == row["auc_id"]
            assert database.Get_AuC(imsi=row["imsi"])["auc_id"] == row["auc_id"]
            ims_subscriber = database.Get_IMS_Subscriber(imsi=row["imsi"])
            assert ims_subscriber["ims_subscriber_id"] == row["ims_subscriber_id"]
            assert ims_subscriber["msisdn"] == row["msisdn"]
        assert database.Bulk_Count_Subscribers("001019999000000", 3) == {"auc": 3, "subscriber": 3, "ims_subscriber": 3}
    finally:
        database.Bulk_Delete_Subscribers("001019999000000", 3)


def test_bulk_create_is_all_or_nothing(create_test_db):
    """A request that collides with an existing identity must leave nothing
    behind, so the caller never has to find out which part of it exists."""
    database = Database(LogTool(config))
    create(database, "001019999000105", "494099999000105", 1)
    try:
        with pytest.raises(ValueError):
            create(database, "001019999000100", "494099999000100", 10)
        assert database.Bulk_Count_Subscribers("001019999000100", 10) == {"auc": 1, "subscriber": 1, "ims_subscriber": 1}
    finally:
        database.Bulk_Delete_Subscribers("001019999000100", 10)


def test_bulk_delete_stays_inside_the_range(create_test_db):
    """Deleting a range must not touch a neighbouring identity, nor a shorter
    one that merely sorts into the range as a string."""
    database = Database(LogTool(config))
    create(database, "001019999000200", "494099999000200", 4)
    create(database, "00101999900020", "49409999900020", 1)  # 14 digits
    try:
        deleted = database.Bulk_Delete_Subscribers("001019999000200", 3)
        assert deleted == {"auc": 3, "subscriber": 3, "ims_subscriber": 3}
        assert database.Get_Subscriber(imsi="001019999000203")["imsi"] == "001019999000203"
        assert database.Get_IMS_Subscriber(imsi="00101999900020")["imsi"] == "00101999900020"
    finally:
        database.Bulk_Delete_Subscribers("001019999000203", 1)
        database.Bulk_Delete_Subscribers("00101999900020", 1)


def test_identity_range_rejects_overflow_and_non_digits():
    """An identity range must keep its length: an overflow would silently
    create identities outside the caller's range."""
    assert Database.identity_range("0998", 2) == ("0998", "0999")
    with pytest.raises(ValueError):
        Database.identity_range("998", 3)
    with pytest.raises(ValueError):
        Database.identity_range("+4940", 1)


def test_count_served_ims_subscribers(create_test_db):
    """The count must equal the number of rows the full listing returns, since
    it replaces that listing for callers that only need the number."""
    database = Database(LogTool(config))
    create(database, "001019999000300", "494099999000300", 2)
    try:
        before = database.Count_Served_IMS_Subscribers()
        database.Update_Serving_CSCF(imsi="001019999000300", serving_cscf="scscf.ims.example.org", propagate=False)
        assert database.Count_Served_IMS_Subscribers() == before + 1
        assert database.Count_Served_IMS_Subscribers() == len(database.Get_Served_IMS_Subscribers())
    finally:
        database.Bulk_Delete_Subscribers("001019999000300", 2)


def test_upgrade_adds_lookup_indexes(tmp_path):
    """Existing databases are not recreated, so the schema upgrade is the only
    way they get the IMSI/MSISDN lookup indexes."""
    engine = sqlalchemy.create_engine(f"sqlite:///{tmp_path}/v4.db")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(sqlalchemy.text("DROP INDEX ix_ims_subscriber_imsi"))
        conn.execute(sqlalchemy.text("DROP INDEX ix_subscriber_msisdn"))
        conn.execute(sqlalchemy.text("INSERT INTO database_schema_version (upgrade_id, comment) VALUES (4, 'test')"))
        conn.commit()

    DatabaseSchema(LogTool(config), Base, engine, main_service=True)

    inspector = sqlalchemy.inspect(engine)
    assert "ix_ims_subscriber_imsi" in [index["name"] for index in inspector.get_indexes("ims_subscriber")]
    assert "ix_subscriber_msisdn" in [index["name"] for index in inspector.get_indexes("subscriber")]
    assert DatabaseSchema.latest == 5
