# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ADR-0040 step 1: one propagation path for replicated state (KI-19).

Each Diameter node pushed every state change to every other node and to the
provisioning node, and replication from the master carried the same change to
every node a second time; about 80 % of the CPU of a state-changing request went
into spreading it. Replicated state now goes to the provisioning node only, and
to the other nodes only when the provisioning node does not take it."""
from geored_routing import push_targets

PROVISIONING = "http://cnaas-hss-api:8080"
PODS = ["http://hss-diameter-1.headless:8080", "http://hss-diameter-2.headless:8080"]
PEERS = PODS + [PROVISIONING]


def test_replicated_state_goes_to_the_provisioning_node_only():
    for body in ({"imsi": "1", "serving_mme": "mme", "serving_mme_realm": "r", "serving_mme_peer": "p"},
                 {"imsi": "1", "scscf": "sip:scscf", "scscf_state": 1},
                 {"imsi": "1", "pcscf": "pcscf", "pcscf_active_session": "s"},
                 {"auc_id": 1, "sqn": 42},
                 {"imsi": "1", "last_seen_mcc": "001", "last_seen_eci": "1"}):
        assert push_targets(body, PEERS, PROVISIONING) == ([PROVISIONING], PODS), body


def test_state_that_replication_does_not_carry_still_reaches_every_node():
    # serving_apn, emergency_subscriber and eir_history are not replicated; an
    # xcap_profile push makes each node notify its own Sh subscribers.
    for body in ({"imsi": "1", "serving_apn": "ims", "pcrf_session_id": "s"},
                 {"emergency_subscriber_ip": "10.0.0.1"},
                 {"imsi": "1", "imei": "35", "match_response_code": 0},
                 {"imsi": "1", "xcap_profile": "<x/>"}):
        assert push_targets(body, PEERS, PROVISIONING) == (PEERS, []), body


def test_unknown_push_is_not_narrowed():
    assert push_targets({"imsi": "1", "something_new": 1}, PEERS, PROVISIONING) == (PEERS, [])


def test_without_a_primary_every_node_gets_the_push_as_before():
    body = {"auc_id": 1, "sqn": 42}
    assert push_targets(body, PEERS, None) == (PEERS, [])
    assert push_targets(body, PEERS, "") == (PEERS, [])
    # A primary that is not among the endpoints would lose the update.
    assert push_targets(body, PODS, PROVISIONING) == (PODS, [])


def test_push_reports_whether_the_endpoint_took_it():
    # The fallback to the other nodes depends on this: sendGeored used to
    # return True whatever happened.
    import asyncio
    import os
    import sys
    import aiohttp
    from aiohttp import web
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "services"))
    from georedService import GeoredService

    class Quiet:
        async def logAsync(self, **kwargs):
            pass

        async def sendMetric(self, **kwargs):
            pass

    service = GeoredService.__new__(GeoredService)
    service.logTool, service.redisGeoredMessaging = Quiet(), Quiet()
    service.benchmarking, service.hostname = False, "hss01"

    async def accept(request):
        return web.json_response({})

    async def reject(request):
        return web.json_response({}, status=500)

    async def run():
        app = web.Application()
        app.router.add_patch("/ok/geored/", accept)
        app.router.add_patch("/fail/geored/", reject)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                body = {"auc_id": 1, "sqn": 42}
                ok = await service.sendGeored(session, f"http://127.0.0.1:{port}/ok/geored/", "PATCH", body)
                failed = await service.sendGeored(session, f"http://127.0.0.1:{port}/fail/geored/", "PATCH", body)
                refused = await service.sendGeored(session, "http://127.0.0.1:1/geored/", "PATCH", body)
        finally:
            await runner.cleanup()
        return ok, failed, refused

    assert asyncio.run(run()) == (True, False, False)
