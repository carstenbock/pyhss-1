# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Diameter node catches up on serving APN rows it missed (helm KI-310).

The serving APN table (the Gx session of each PDN connection) reaches a node
only by the push of the node that handled the CCR. A node that was restarting
when a PGW came back and its UEs attached again kept the Gx sessions of the
old PGW instance: its Re-Auth Requests were answered 5002, the AAR 4001, the
call had no voice bearer and the called phone did not ring, for about one call
in six (one node of six) until the subscriber's next attach."""
import datetime

import pytest

import serving_apn_sync
from database import APN, AUC, IMS_SUBSCRIBER, Database, SUBSCRIBER
from diameter import Diameter
from logtool import LogTool
from pyhss_config import config

IMSI = "001010000000001"
UE_IP = "10.46.0.4"
REALM = "ims.mnc001.mcc001.3gppnetwork.org"
PCSCF = "pcscf.ims.mnc001.mcc001.3gppnetwork.org"
OLD = "pgw.epc;1795689608;19;app_gx"      # Gx session of the PGW instance before its restart
NEW = "pgw.epc;1795691433;28;app_gx"
PEERS = ["http://hss-headless:8080"]


class NullMessaging:
    def __getattr__(self, name):
        return lambda *args, **kwargs: ""


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setitem(config["database"], "database", str(tmp_path / "sync.db"))
    database = Database(LogTool(config), main_service=True)
    database.CreateObj(APN, {"apn_id": 1, "apn": "internet", "apn_ambr_dl": 100000000, "apn_ambr_ul": 100000000})
    database.CreateObj(APN, {"apn_id": 2, "apn": "ims", "apn_ambr_dl": 100000000, "apn_ambr_ul": 100000000})
    database.CreateObj(AUC, {"auc_id": 1, "ki": "3c6e0b8a9c15224a8228b9a98ca1531d",
                             "opc": "762a2206fe0b4151ace403c86a11e479", "amf": "8000", "sqn": 0, "imsi": IMSI})
    database.CreateObj(SUBSCRIBER, {"auc_id": 1, "default_apn": 1, "apn_list": "1,2", "imsi": IMSI,
                                    "msisdn": "4915100000001"})
    database.CreateObj(IMS_SUBSCRIBER, {"imsi": IMSI, "msisdn": "4915100000001", "sh_profile": ""})
    yield database
    database.engine.dispose()


def attach(database, session, address, when):
    database.Update_Serving_APN(imsi=IMSI, apn="ims", pcrf_session_id=session, serving_pgw="pgw.epc",
                                subscriber_routing=address, serving_pgw_realm="epc", serving_pgw_peer="pgw.epc;hss01",
                                serving_pgw_timestamp=when, propagate=False)


def peer_row(session=NEW, address=UE_IP, when="2026-11-26T11:10:45Z"):
    return {"imsi": IMSI, "serving_apn": "ims", "pcrf_session_id": session, "serving_pgw": "pgw.epc",
            "serving_pgw_realm": "epc", "serving_pgw_peer": "pgw.epc;hss01", "serving_pgw_timestamp": when,
            "subscriber_routing": address}


class Peers:
    """Stands in for the other nodes' GET /geored/serving_apns."""
    def __init__(self, rows_by_address):
        self.rows_by_address, self.asked = rows_by_address, []

    def resolve(self, host, port):
        return sorted(self.rows_by_address)

    def get(self, url, params=None, timeout=None):
        address = url.split("//")[1].split(":")[0]
        self.asked.append((address, dict(params or {})))
        rows = self.rows_by_address[address]
        if rows is None:
            raise ConnectionError("node is down")
        if params and params.get("imsi"):
            rows = [row for row in rows if row["imsi"] == params["imsi"]]

        class Response:
            status_code = 200
            def json(self_inner):
                return {"serving_apns": rows if not params or params.get("page", 0) == 0 else []}
        return Response()


def local_session(database):
    rows = database.Get_Serving_APN_Rows()
    return rows[0]["pcrf_session_id"] if rows else None


def test_rows_carry_imsi_and_apn_name_like_a_push(db):
    attach(db, NEW, UE_IP, "2026-11-26T11:10:45Z")

    assert db.Get_Serving_APN_Rows() == [peer_row()]
    assert db.Get_Serving_APN_Rows(imsi="001019999999999") == []


def test_a_pushed_row_keeps_its_attach_time(db):
    # The time of a pushed row was replaced by the time of its arrival
    # (datetime.strptime on the module), so no node could tell an old row
    # from a new one.
    attach(db, NEW, UE_IP, "2026-11-26T11:10:45Z")

    assert db.Get_Serving_APN_Rows()[0]["serving_pgw_timestamp"] == "2026-11-26T11:10:45Z"


def test_a_node_that_missed_a_push_takes_the_row_over_at_start(db):
    # KI-310: this node was restarting at 11:10 and still holds the session of
    # the PGW instance before; the other nodes hold the one of 11:10:45.
    attach(db, OLD, "10.46.0.3", "2026-11-26T10:40:58Z")
    peers = Peers({"10.0.0.1": [peer_row()], "10.0.0.2": [peer_row()], "10.0.0.9": [peer_row(OLD, "10.46.0.3", "2026-11-26T10:40:58Z")]})

    result = serving_apn_sync.sync(db, PEERS, get=peers.get, resolve=peers.resolve, own=["10.0.0.9"])

    assert local_session(db) == NEW
    assert db.Get_Serving_APN_Rows()[0]["subscriber_routing"] == UE_IP
    assert result == {"peers": 2, "rows": 1, "updated": 1, "removed": 0}
    assert "10.0.0.9" not in [address for address, _ in peers.asked]        # never asks itself


def test_a_newer_local_row_is_kept(db):
    # The UE attached through this node a moment ago; a peer still shows the
    # session before (this node's push is on its way).
    attach(db, NEW, UE_IP, "2026-11-26T11:10:45Z")
    peers = Peers({"10.0.0.1": [peer_row(OLD, "10.46.0.3", "2026-11-26T10:40:58Z")]})

    serving_apn_sync.sync(db, PEERS, get=peers.get, resolve=peers.resolve)

    assert local_session(db) == NEW


def test_a_row_no_peer_holds_any_more_is_dropped(db):
    # The PDN connection ended while this node was down.
    attach(db, OLD, "10.46.0.3", "2026-11-26T10:40:58Z")
    peers = Peers({"10.0.0.1": [], "10.0.0.2": []})

    result = serving_apn_sync.sync(db, PEERS, get=peers.get, resolve=peers.resolve,
                                   now=lambda: datetime.datetime(2026, 11, 26, 12, 22, 30))

    assert db.Get_Serving_APN_Rows() == []
    assert result["removed"] == 1


def test_a_row_of_the_last_minute_is_not_dropped(db):
    attach(db, NEW, UE_IP, "2026-11-26T12:22:00Z")
    peers = Peers({"10.0.0.1": []})

    serving_apn_sync.sync(db, PEERS, get=peers.get, resolve=peers.resolve,
                          now=lambda: datetime.datetime(2026, 11, 26, 12, 22, 30))

    assert local_session(db) == NEW


def test_nothing_changes_when_no_peer_answers(db):
    # Every node starts at once: an empty answer from nobody must not empty the table.
    attach(db, OLD, "10.46.0.3", "2026-11-26T10:40:58Z")
    peers = Peers({"10.0.0.1": None, "10.0.0.2": None})

    assert serving_apn_sync.sync(db, PEERS, get=peers.get, resolve=peers.resolve) is None
    assert local_session(db) == OLD


def test_unknown_session_gets_the_current_row_from_a_peer(db):
    attach(db, OLD, "10.46.0.3", "2026-11-26T10:40:58Z")
    peers = Peers({"10.0.0.1": [peer_row(OLD, "10.46.0.3", "2026-11-26T10:40:58Z")], "10.0.0.2": [peer_row()]})

    row = serving_apn_sync.fresh_row(db, PEERS, IMSI, "ims", OLD, get=peers.get, resolve=peers.resolve)

    assert row["pcrf_session_id"] == NEW
    assert local_session(db) == NEW
    assert all(params.get("imsi") == IMSI for _, params in peers.asked)     # one subscriber, not the table


def test_unknown_session_without_a_better_row_stays_as_it_is(db):
    attach(db, OLD, "10.46.0.3", "2026-11-26T10:40:58Z")
    peers = Peers({"10.0.0.1": [peer_row(OLD, "10.46.0.3", "2026-11-26T10:40:58Z")], "10.0.0.2": None})

    assert serving_apn_sync.fresh_row(db, PEERS, IMSI, "ims", OLD, get=peers.get, resolve=peers.resolve) is None
    assert local_session(db) == OLD


# --- the AAR: one retry with the session a peer holds ---

def media_component(node):
    rx = lambda code, data: node.generate_vendor_avp(code, "c0", 10415, data)
    text = lambda s: s.encode().hex()
    rtp = rx(519, rx(509, node.int_to_hex(1, 4))
             + rx(507, text(f"permit in 17 from {UE_IP} to 10.0.0.3 60000"))
             + rx(507, text(f"permit out 17 from 10.0.0.3 to {UE_IP} 50000")))
    return rx(517, rx(518, node.int_to_hex(1, 4)) + rtp + rx(520, node.int_to_hex(0, 4))
              + rx(515, node.int_to_hex(41000, 4)) + rx(516, node.int_to_hex(41000, 4))
              + rx(511, node.int_to_hex(2, 4)))


def make_node(database, results):
    node = Diameter.__new__(Diameter)
    node.logTool = LogTool(config)
    node.redisMessaging = NullMessaging()
    node.database = database
    node.OriginHost = node.string_to_hex("hss01")
    node.OriginRealm = node.string_to_hex(REALM)
    node.MNC, node.MCC, node.hostname = "001", "001", "hss01"
    node.rars = []

    def await_rar(requestType, hostname, **kwargs):
        node.rars.append(kwargs)
        raa = node.generate_avp(268, 40, node.int_to_hex(results[min(len(node.rars), len(results)) - 1], 4))
        return node.generate_diameter_packet("01", "00", 258, 16777238, "00000001", "00000001", raa)
    node.awaitDiameterRequestAndResponse = await_rar
    return node


def aar_result(node):
    avp = node.generate_avp(263, 40, node.string_to_hex(f"{PCSCF};1;call"))
    avp += node.generate_avp(264, 40, node.string_to_hex(PCSCF))
    avp += node.generate_avp(296, 40, node.string_to_hex(REALM))
    avp += node.generate_avp(443, 40, node.generate_avp(450, 40, node.int_to_hex(2, 4))
                             + node.generate_avp(444, 40, node.string_to_hex(f"sip:{IMSI}@{REALM}")))
    avp += node.generate_avp(8, 40, "0a2e0004")                                        # Framed-IP-Address 10.46.0.4
    avp += media_component(node)
    packet = node.generate_diameter_packet("01", "c0", 265, 16777236, "00000001", "00000002", avp)
    packet_vars, avps = node.decode_diameter_packet(bytes.fromhex(packet))
    _, answer = node.decode_diameter_packet(bytes.fromhex(node.Answer_16777236_265(packet_vars, avps)))
    return int(node.get_avp_data(answer, 268)[0], 16)


def test_aar_retries_with_the_session_a_peer_holds(db):
    # The call of 13:09 UTC: the node's row names the old Gx session, the PGW
    # answers 5002. With the peers' row the second RAR succeeds and the call
    # gets its bearer instead of AAA 4001.
    attach(db, OLD, UE_IP, "2026-11-26T10:40:58Z")
    node = make_node(db, [5002, 2001])
    asked = []

    def from_peers(imsi, apn_name, stale_session_id):
        asked.append((imsi, apn_name, stale_session_id))
        attach(db, NEW, UE_IP, "2026-11-26T11:10:45Z")
        return peer_row()
    node.serving_apn_from_peers = from_peers

    assert aar_result(node) == 2001
    assert asked == [(IMSI, "ims", OLD)]
    assert [rar["sessionId"] for rar in node.rars] == [OLD, NEW]
    assert node.rars[1]["chargingRules"][0]["rule_name"] == node.rars[0]["chargingRules"][0]["rule_name"]


def test_aar_is_refused_when_no_peer_knows_better(db):
    attach(db, OLD, UE_IP, "2026-11-26T10:40:58Z")
    node = make_node(db, [5002])
    node.serving_apn_from_peers = lambda imsi, apn_name, stale_session_id: None

    assert aar_result(node) == 4001
    assert len(node.rars) == 1


def test_aar_with_a_known_session_asks_no_peer(db):
    attach(db, NEW, UE_IP, "2026-11-26T11:10:45Z")
    node = make_node(db, [2001])
    node.serving_apn_from_peers = lambda *args: pytest.fail("no peer lookup for a session the PGW knows")

    assert aar_result(node) == 2001
    assert len(node.rars) == 1
