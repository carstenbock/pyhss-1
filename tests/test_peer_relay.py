# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A dead peer pod must not stall the Diameter worker (KI-171).

r16 (2026-11-12 18:35 UTC): the HPA removed one HSS pod at peak load. The
serial Diameter workers posted their Rx relays (CCR-T, STR) to its address
inside the request path; Cx, S6a and Rx answers stalled on every node, the DRA
counted 3,806 relay timeouts in one minute and the run was aborted."""
import threading
import time

import requests

from peer_relay import PeerRelay


def test_submitting_a_relay_does_not_wait_for_the_peer():
    release = threading.Event()

    def black_hole(url, json, timeout):
        release.wait(5)                        # a peer that accepts nothing
        raise requests.ConnectTimeout("connect timed out")

    relay = PeerRelay(post=black_hole, resolve=lambda host, port: ["10.0.0.9"])
    started = time.monotonic()
    for _ in range(100):
        assert relay.submit(["http://hss-headless:8080"], "/geored/rx_terminate_af_subscriptions", {"imsi": "1"}, "t")
    assert time.monotonic() - started < 0.1    # the worker is free at once
    release.set()


def test_failed_peer_is_skipped_until_its_dead_time_is_over():
    calls = []
    clock = [0.0]

    def post(url, json, timeout):
        calls.append(url)
        if "10.0.0.9" in url:
            raise requests.ConnectionError("refused")

    relay = PeerRelay(post=post, resolve=lambda host, port: ["10.0.0.8", "10.0.0.9"], now=lambda: clock[0],
                      dead_seconds=30, start=False)
    relay.deliver(["http://hss-headless:8080"], "/x", {}, "t")
    relay.deliver(["http://hss-headless:8080"], "/x", {}, "t")
    assert calls == ["http://10.0.0.8:8080/x", "http://10.0.0.9:8080/x", "http://10.0.0.8:8080/x"]

    clock[0] = 31                               # dead time over: tried again
    relay.deliver(["http://hss-headless:8080"], "/x", {}, "t")
    assert calls[-1] == "http://10.0.0.9:8080/x"


def test_relays_keep_their_order():
    # An STR's forget must reach a node before a later CCR-T's terminate (KI-84).
    seen = []
    done = threading.Event()

    def post(url, json, timeout):
        seen.append(json["n"])
        if json["n"] == 9:
            done.set()

    relay = PeerRelay(post=post, resolve=lambda host, port: ["10.0.0.8"])
    for n in range(10):
        relay.submit(["http://hss-headless:8080"], "/x", {"n": n}, "t")
    assert done.wait(2)
    assert seen == list(range(10))


def test_rx_relay_returns_at_once_with_an_unreachable_peer(monkeypatch):
    # The worker's own call: before, requests.post with a 2 s timeout per node.
    from diameter import Diameter
    from logtool import LogTool
    from pyhss_config import config
    monkeypatch.setitem(config["hss"], "rx_notify_endpoints", ["http://10.255.255.1:8080"])
    node = Diameter.__new__(Diameter)
    node.logTool, node.redisMessaging = LogTool(config), None

    started = time.monotonic()
    node._rx_relay("/geored/rx_terminate_af_subscriptions", {"imsi": "1", "apn": "ims"}, "rx_relay_terminate")

    assert time.monotonic() - started < 0.2


def test_sigterm_closes_the_listener_and_sends_a_dpr_to_every_peer(monkeypatch):
    # KI-171: a pod the HPA removed was simply killed; the DRAs kept sending it
    # requests until their watchdog noticed. Now the Diameter service stops
    # accepting connections and sends a DPR, so the DRAs route to the others.
    import asyncio
    import json
    import os
    import sys
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services"))
    import diameterService
    from diameterAsync import DiameterAsync
    from baseModels import Peer

    class Quiet:
        async def logAsync(self, **kwargs):
            pass

    class Outbound:
        def __init__(self):
            self.sent = []

        async def sendMessage(self, queue, message, **kwargs):
            self.sent.append((queue, json.loads(message)["OutboundHex"]))

    class Listener:
        closed = False

        def close(self):
            self.closed = True

    exits = []
    monkeypatch.setattr(diameterService.os, "_exit", exits.append)
    service = diameterService.DiameterService.__new__(diameterService.DiameterService)
    service.logTool, service.diameterLibrary = Quiet(), DiameterAsync(logTool=Quiet())
    service.redisDwrMessaging, service.server = Outbound(), Listener()
    service.originHost, service.originRealm, service.hostname = "hss-3", "realm", "hss-3"
    service.shutdownDrainSeconds, service.shuttingDown = 0, False
    peer = dict(Hostname="dra-0", TransportProtocol="", PeerType="", LastConnectTimestamp="", LastDisconnectTimestamp="",
                ReconnectionCount=0, Metadata="")
    service.activePeers = {"10.0.0.1-3868": Peer(IpAddress="10.0.0.1", Port="3868", Connected=True, **peer),
                           "10.0.0.2-3868": Peer(IpAddress="10.0.0.2", Port="3868", Connected=False, **peer)}

    asyncio.run(service.shutdown())

    assert service.server.closed and exits == [0]
    [(queue, dpr)] = service.redisDwrMessaging.sent           # only the connected peer
    assert queue == "diameter-outbound-10.0.0.1-3868"
    assert int(dpr[10:16], 16) == 282 and dpr.endswith("00000000")   # DPR, Disconnect-Cause REBOOTING
