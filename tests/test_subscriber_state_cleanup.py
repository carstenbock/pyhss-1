# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-subscriber Redis state (Rx AF subscriptions, Sh subscriptions) must not
outlive its purpose: every Diameter node keeps it in its own Redis, and what is
never removed there adds up until the node runs out of memory."""
import pytest

import diameter
from database import Database
from diameter import Diameter
from logtool import LogTool
from messaging import RedisMessaging
from network_control import purge_subscriber_state_and_relay
from pyhss_config import config

AUC_VALUES = {"ki": "3c6e0b8a9c15224a8228b9a98ca1531d", "opc": "762a2206fe0b4151ace403c86a11e479", "amf": "8000", "sqn": 0}
SUBSCRIBER_VALUES = {"default_apn": 1, "apn_list": "1"}
IMS_VALUES = {"pcscf_realm": "ims.example.org"}

IMS_APN = 3
PCSCF = "pcscf.ims.example.org"
AS = "mmtel.ims.example.org"
REALM = "ims.example.org"


class Clock:
    def __init__(self):
        self.now = 1_700_000_000

    def time(self):
        return self.now


class FakeRedis:
    """The Redis commands the subscription store uses, with key expiry driven
    by the test clock."""

    def __init__(self, clock):
        self.clock = clock
        self.hashes = {}
        self.deadlines = {}

    def _expire_due(self):
        for name in [name for name, deadline in self.deadlines.items() if deadline <= self.clock.now]:
            self.hashes.pop(name, None)
            del self.deadlines[name]

    def keys(self):
        self._expire_due()
        return sorted(self.hashes)

    def hlen(self, name):
        self._expire_due()
        return len(self.hashes.get(name, {}))

    def ttl(self, name):
        self._expire_due()
        if name not in self.hashes:
            return -2
        return self.deadlines[name] - self.clock.now if name in self.deadlines else -1

    def hset(self, name, key, value):
        self._expire_due()
        self.hashes.setdefault(name, {})[key.encode()] = value.encode()

    def hgetall(self, name):
        self._expire_due()
        return dict(self.hashes.get(name, {}))

    def hdel(self, name, key):
        self._expire_due()
        fields = self.hashes.get(name, {})
        fields.pop(key.encode(), None)
        if not fields:
            self.hashes.pop(name, None)
            self.deadlines.pop(name, None)

    def expire(self, name, seconds):
        self._expire_due()
        if name in self.hashes:
            self.deadlines[name] = self.clock.now + seconds

    def unlink(self, *names):
        self._expire_due()
        existing = [name for name in names if name in self.hashes]
        for name in existing:
            del self.hashes[name]
            self.deadlines.pop(name, None)
        return len(existing)


class ApnTable:
    """Stands in for the node's database: the APNs that exist."""

    def GetAll(self, obj_type):
        return [{"apn_id": 1}, {"apn_id": IMS_APN}]


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(diameter.time, "time", clock.time)
    return clock


def make_node(clock, database=None):
    """A PyHSS node as far as the subscription store goes: the Diameter library
    on top of the node's own Redis."""
    messaging = RedisMessaging()
    messaging.redisClient = FakeRedis(clock)
    node = Diameter.__new__(Diameter)
    node.redisMessaging = messaging
    node.database = database or ApnTable()
    return node


def keys(node):
    return node.redisMessaging.redisClient.keys()


def relay_to(*nodes):
    """What relay_to_diameter_nodes and POST /geored/purge_subscriber_state do
    together: hand the ids to every Diameter node."""
    def relay(path, payload):
        assert path == "/geored/purge_subscriber_state"
        return [{"deleted": node.purge_subscriber_state(**payload)} for node in nodes]
    return relay


def register(node, subscriber_id, session_id, expires=600):
    node.rx_store_af_subscription(subscriber_id=subscriber_id, apn_id=IMS_APN, af_session_id=session_id,
                                  af_peer=PCSCF, af_realm=REALM, af_session_expires=expires)


def test_re_registrations_do_not_pile_up_rx_sessions(clock):
    """Every re-registration opens a new Rx session and the old one is never
    closed with an STR. A subscriber that stays registered for weeks must not
    cost more memory than one that registered once: this is what made the
    node's memory grow during the steady state of a load test."""
    node = make_node(clock)
    for registration in range(50):
        register(node, 7, f"pcscf;{registration}", expires=600)
        clock.now += 700
    register(node, 7, "pcscf;last", expires=600)

    assert node.redisMessaging.redisClient.hlen(f"af_subscriptions:7:{IMS_APN}") == 1
    assert list(node.rx_get_af_subscriptions(7, IMS_APN)) == ["pcscf;last"]


def test_a_new_rx_session_keeps_the_sessions_that_are_still_valid(clock):
    """Bounding the hash must not cost a live session: on Gx CCR-T every AF
    that still holds the signalling bearer gets its ASR (TS 29.214 section
    4.4.5), also the one whose session outlasts a shorter, newer one."""
    node = make_node(clock)
    register(node, 7, "pcscf;long", expires=3600)
    clock.now += 10
    register(node, 7, "pcscf;short", expires=60)
    clock.now += 600

    assert list(node.rx_get_af_subscriptions(7, IMS_APN)) == ["pcscf;long"]


def test_rx_state_of_a_silent_subscriber_expires(clock):
    """A subscriber that never comes back (deleted, or simply gone) sends no
    request that could clean up after it, so its key has to go by itself once
    its last session ran out, and not before."""
    node = make_node(clock)
    register(node, 7, "pcscf;1", expires=600)

    clock.now += 599
    assert keys(node) == [f"af_subscriptions:7:{IMS_APN}"]
    clock.now += 600
    assert keys(node) == []


def test_sh_subscription_of_an_existing_subscriber_never_expires(clock):
    """An AS subscribes once and relies on being notified for as long as the
    subscriber exists (TS 29.328 section 6.1.3: until it unsubscribes). The
    clean-up of Rx state must not touch it, however long ago the SNR was."""
    node = make_node(clock)
    node.sh_store_subscription(9, AS, REALM, ["MMTEL-Services"])
    clock.now += 10 * 365 * 86400
    register(node, 9, "pcscf;1")
    clock.now += 10 * 365 * 86400

    assert list(node.sh_get_subscriptions(9)) == [AS]
    assert node.redisMessaging.redisClient.ttl("sh_subscriptions:9") == -1


def test_deleting_a_subscriber_removes_its_state_from_the_diameter_node(clock):
    """The provisioning API deletes the subscriber, but the state is in the
    Redis of the Diameter node that handled the AAR and the SNR. Deleting from
    the API's own Redis finds nothing there; the delete has to reach the node.
    Subscribers that still exist keep everything, also those whose id merely
    starts with the same digits."""
    api = make_node(clock)
    diameter_node = make_node(clock)
    other_diameter_node = make_node(clock)
    for subscriber_id in (7, 70):
        register(diameter_node, subscriber_id, "pcscf;1")
        register(other_diameter_node, subscriber_id, "pcscf;2")
    for ims_subscriber_id in (9, 90):
        diameter_node.sh_store_subscription(ims_subscriber_id, AS, REALM, ["MMTEL-Services"])

    purge_subscriber_state_and_relay(api, relay_to(diameter_node, other_diameter_node),
                                     subscriber_ids=[7], ims_subscriber_ids=[9])

    assert keys(diameter_node) == [f"af_subscriptions:70:{IMS_APN}", "sh_subscriptions:90"]
    assert keys(other_diameter_node) == [f"af_subscriptions:70:{IMS_APN}"]
    assert list(diameter_node.sh_get_subscriptions(90)) == [AS]


def test_diameter_nodes_are_purged_even_if_the_local_redis_fails(clock):
    """The API's own Redis holds none of the state in a split deployment, so a
    problem with it is no reason to leave the Diameter nodes uncleaned."""
    class BrokenNode:
        def purge_subscriber_state(self, **ids):
            raise ConnectionError("redis down")

    diameter_node = make_node(clock)
    diameter_node.sh_store_subscription(9, AS, REALM, [])

    purge_subscriber_state_and_relay(BrokenNode(), relay_to(diameter_node), ims_subscriber_ids=[9])

    assert keys(diameter_node) == []


def test_bulk_delete_leaves_no_state_for_the_deleted_range(clock, create_test_db):
    """A range delete removes the rows in one statement and never looks at a
    single subscriber, so it has to name the ids whose state goes with them.
    100,000 subscribers deleted this way left their keys behind on every node.
    The neighbour outside the range stays subscribed."""
    database = Database(LogTool(config))
    rows = database.Bulk_Create_Subscribers("001019998000000", "494099998000000", 4,
                                            AUC_VALUES, SUBSCRIBER_VALUES, IMS_VALUES)
    try:
        node = make_node(clock, database)
        for row in rows:
            node.rx_store_af_subscription(subscriber_id=row["subscriber_id"], apn_id=1, af_session_id="pcscf;1",
                                          af_peer=PCSCF, af_realm=REALM, af_session_expires=600)
            node.sh_store_subscription(row["ims_subscriber_id"], AS, REALM, ["MMTEL-Services"])

        ids = database.Bulk_Subscriber_Ids("001019998000000", 3)
        database.Bulk_Delete_Subscribers("001019998000000", 3)
        purge_subscriber_state_and_relay(make_node(clock, database), relay_to(node), **ids)

        neighbour = rows[3]
        assert sorted(ids["subscriber_ids"]) == sorted(row["subscriber_id"] for row in rows[:3])
        assert sorted(ids["ims_subscriber_ids"]) == sorted(row["ims_subscriber_id"] for row in rows[:3])
        assert keys(node) == [f"af_subscriptions:{neighbour['subscriber_id']}:1",
                              f"sh_subscriptions:{neighbour['ims_subscriber_id']}"]
    finally:
        database.Bulk_Delete_Subscribers("001019998000000", 4)
