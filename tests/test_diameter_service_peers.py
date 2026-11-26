# Copyright 2026 phatlc <phatle.hsd@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for iterating DiameterService.activePeers while peers connect and disconnect.

Peers are added, updated and removed from the shared activePeers dict by handleConnection() and
handleActiveDiameterPeers() while other coroutines iterate over it with awaits inside the loop body
(https://github.com/nickvsnetworking/pyhss/issues/310).
"""

import asyncio
import contextlib
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from baseModels import Peer

top_dir = Path(Path(__file__) / "../..").resolve()
sys.path.append(str(top_dir / "services"))
diameterService = importlib.import_module("diameterService")


def _peer(ip, port, hostname, connected=True, connect_age=0, disconnect_age=0):
    now = datetime.now(diameterService.get_localzone())
    return Peer(
        IpAddress=ip,
        Port=port,
        Hostname=hostname,
        Connected=connected,
        TransportProtocol="",
        PeerType="",
        LastConnectTimestamp=(now - timedelta(seconds=connect_age)).isoformat("T"),
        LastDisconnectTimestamp="" if connected else (now - timedelta(seconds=disconnect_age)).isoformat("T"),
        ReconnectionCount=0,
        Metadata="",
    )


def _add_peer(peers):
    peers.setdefault("10.0.0.9-9999", _peer("10.0.0.9", "9999", "mme09"))


def _remove_peer(peers):
    peers.pop("10.0.0.2-2222", None)


def _logged_exceptions(service):
    """The service loops catch and log all exceptions instead of raising them."""
    calls = service.logTool.logAsync.await_args_list
    return [c.kwargs["message"] for c in calls if "Exception" in c.kwargs.get("message", "")]


async def _run_one_pass(coro):
    """Run a single pass of one of the service's endless loops.

    Every await inside the loop body is mocked, so the task runs until it reaches the asyncio.sleep()
    between two passes, where it gets cancelled.
    """
    task = asyncio.create_task(coro)
    for _ in range(3):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.fixture
def service():
    svc = diameterService.DiameterService()
    svc.logTool.logAsync = AsyncMock()
    svc.diameterLibrary.Request_280 = AsyncMock(return_value="0100")
    svc.redisDwrMessaging.sendMessage = AsyncMock()
    svc.redisPeerMessaging.deleteQueue = AsyncMock()
    svc.redisPeerMessaging.deleteHashKey = AsyncMock()
    svc.redisPeerMessaging.setHashValue = AsyncMock()
    svc.redisPeerLogMessaging.sendMetric = AsyncMock()
    svc.activePeers = {
        "10.0.0.1-1111": _peer("10.0.0.1", "1111", "mme01"),
        "10.0.0.2-2222": _peer("10.0.0.2", "2222", "mme02"),
    }
    return svc


@pytest.mark.parametrize("mutate", [_add_peer, _remove_peer])
def test_handleOutboundDwr_peer_churn_while_sending(service, mutate):
    service.redisDwrMessaging.sendMessage.side_effect = lambda **kwargs: mutate(service.activePeers)

    asyncio.run(_run_one_pass(service.handleOutboundDwr()))

    assert _logged_exceptions(service) == []
    queues = [c.kwargs["queue"] for c in service.redisDwrMessaging.sendMessage.await_args_list]
    assert queues == ["diameter-outbound-10.0.0.1-1111", "diameter-outbound-10.0.0.2-2222"]


@pytest.mark.parametrize("mutate", [_add_peer, _remove_peer])
def test_logActivePeers_peer_churn_while_sending_metrics(service, mutate):
    service.redisPeerLogMessaging.sendMetric.side_effect = lambda **kwargs: mutate(service.activePeers)

    asyncio.run(service.logActivePeers())

    assert _logged_exceptions(service) == []
    hosts = [c.kwargs["metricLabels"]["host"] for c in service.redisPeerLogMessaging.sendMetric.await_args_list]
    assert hosts == ["mme01", "mme02"]


def test_handleActiveDiameterPeers_prunes_while_peer_connects(service):
    service.activePeers.update(
        {
            # Older duplicate of mme01 ("10.0.0.1-1111" is the newest connection): pruned
            "10.0.0.3-3333": _peer("10.0.0.3", "3333", "mme01", connect_age=60),
            # Disconnected longer than active_diameter_peers_timeout (10s in tests/config.yaml): pruned
            "10.0.0.4-4444": _peer("10.0.0.4", "4444", "mme04", connected=False, disconnect_age=3600),
            # Disconnected recently: kept
            "10.0.0.5-5555": _peer("10.0.0.5", "5555", "mme05", connected=False, disconnect_age=1),
        }
    )
    service.redisPeerMessaging.setHashValue.side_effect = lambda **kwargs: _add_peer(service.activePeers)

    asyncio.run(_run_one_pass(service.handleActiveDiameterPeers()))

    assert _logged_exceptions(service) == []
    assert sorted(service.activePeers) == ["10.0.0.1-1111", "10.0.0.2-2222", "10.0.0.5-5555", "10.0.0.9-9999"]
    deleted = sorted(c.kwargs["key"] for c in service.redisPeerMessaging.deleteHashKey.await_args_list)
    assert deleted == ["10.0.0.3-3333", "10.0.0.4-4444"]
    # Pruned peers must not be written back to Redis
    stored = sorted(c.kwargs["key"] for c in service.redisPeerMessaging.setHashValue.await_args_list)
    assert stored == ["10.0.0.1-1111", "10.0.0.2-2222", "10.0.0.5-5555"]
