# Copyright 2026 volte.io
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Catch-up of the serving APN table between HSS Diameter nodes.

A node learns the Gx session of a subscriber's PDN connection either from the
CCR it handles itself or from the push of the node that handled it. The table
is not replicated by the database. A node that is down when a push is sent
keeps the row it had: after a PGW restart it then addresses its Re-Auth
Requests to a Gx session the PGW no longer knows (5002), and a call gets no
voice bearer (helm KI-310).

Two repairs, both by asking the other nodes over their API
(GET /geored/serving_apns):

- sync(): at start, take over every row a peer holds that is newer than the
  local one, and drop local rows no answering peer holds any more.
- fresh_row(): when the PGW answers "unknown session", get the subscriber's
  current row for a retry.
"""
import datetime
import socket
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import requests

CONNECT_TIMEOUT = 0.5
READ_TIMEOUT = 2.0
PAGE_SIZE = 2000
# A local row younger than this is not dropped: its push may still be on its
# way to the peers.
KEEP_SECONDS = 60


def _addresses(host: str, port: int) -> List[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)})


def own_addresses() -> List[str]:
    try:
        return _addresses(socket.gethostname(), 0)
    except OSError:
        return []


def peer_urls(endpoints: Iterable[str], resolve: Callable = _addresses, own: Iterable[str] = ()) -> List[str]:
    """One base URL per address behind the endpoints (a headless service name
    resolves to every node), without this node's own addresses."""
    own, urls = set(own), []
    for endpoint in endpoints or []:
        parsed = urlparse(endpoint)
        host, port = parsed.hostname, parsed.port or 8080
        if not host:
            continue
        try:
            addresses = resolve(host, port)
        except OSError:
            continue
        for address in addresses:
            if address in own:
                continue
            literal = f"[{address}]" if ":" in address else address
            url = f"{parsed.scheme or 'http'}://{literal}:{port}"
            if url not in urls:
                urls.append(url)
    return urls


def parse_time(value) -> Optional[datetime.datetime]:
    if isinstance(value, datetime.datetime):
        return value.replace(tzinfo=None)
    for pattern in ('%Y-%m-%dT%H:%M:%SZ', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f'):
        try:
            return datetime.datetime.strptime(str(value), pattern)
        except (TypeError, ValueError):
            continue
    return None


def fetch_rows(url: str, imsi: Optional[str] = None, get: Callable = requests.get) -> Optional[List[dict]]:
    """The serving APN rows of one peer; None when it does not answer."""
    rows, page = [], 0
    while True:
        params = {'page': page, 'page_size': PAGE_SIZE}
        if imsi:
            params['imsi'] = imsi
        try:
            response = get(f"{url}/geored/serving_apns", params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            if response.status_code != 200:
                return None
            chunk = response.json().get('serving_apns', [])
        except Exception:
            return None
        rows.extend(chunk)
        if imsi or len(chunk) < PAGE_SIZE:
            return rows
        page += 1


def _key(row: dict) -> Tuple[str, str]:
    return str(row.get('imsi')), str(row.get('serving_apn')).lower()


def newest(rows: Iterable[dict]) -> Dict[Tuple[str, str], dict]:
    """Per subscriber and APN the row with the latest attach time."""
    best: Dict[Tuple[str, str], dict] = {}
    for row in rows:
        if not row.get('pcrf_session_id') or not row.get('serving_pgw'):
            continue
        current = best.get(_key(row))
        when = parse_time(row.get('serving_pgw_timestamp')) or datetime.datetime.min
        if current is None or when > (parse_time(current.get('serving_pgw_timestamp')) or datetime.datetime.min):
            best[_key(row)] = row
    return best


def _store(database, row: dict):
    database.Update_Serving_APN(
        imsi=str(row['imsi']), apn=row['serving_apn'], pcrf_session_id=row['pcrf_session_id'],
        serving_pgw=row['serving_pgw'], subscriber_routing=row.get('subscriber_routing'),
        serving_pgw_realm=row.get('serving_pgw_realm'), serving_pgw_peer=row.get('serving_pgw_peer'),
        serving_pgw_timestamp=row.get('serving_pgw_timestamp'), propagate=False)


def fresh_row(database, endpoints: Iterable[str], imsi: str, apn_name: str, stale_session_id: str,
              get: Callable = requests.get, resolve: Callable = _addresses, own: Iterable[str] = (),
              log: Callable[[str, str], None] = lambda level, message: None) -> Optional[dict]:
    """The subscriber's serving APN as the peers know it, if one of them holds
    another Gx session than the one the PGW has just refused. It is stored
    locally and returned; None when no peer knows better."""
    rows = []
    for url in peer_urls(endpoints, resolve, own):
        answer = fetch_rows(url, imsi=imsi, get=get)
        if answer:
            rows.extend(answer)
    row = newest(rows).get((str(imsi), str(apn_name).lower()))
    if not row or row['pcrf_session_id'] == stale_session_id:
        return None
    _store(database, row)
    log('warning', f"[serving_apn_sync] IMSI {imsi} APN {apn_name}: Gx session {stale_session_id} is unknown at the PGW, "
                   f"took {row['pcrf_session_id']} from a peer")
    return row


def sync(database, endpoints: Iterable[str], get: Callable = requests.get, resolve: Callable = _addresses,
         own: Iterable[str] = (), now: Callable[[], datetime.datetime] = datetime.datetime.utcnow,
         log: Callable[[str, str], None] = lambda level, message: None) -> Optional[dict]:
    """Bring the local table up to the peers' state. Returns the counts, or
    None when no peer answered (nothing is changed then)."""
    answered, rows = 0, []
    for url in peer_urls(endpoints, resolve, own):
        answer = fetch_rows(url, get=get)
        if answer is None:
            continue
        answered += 1
        rows.extend(answer)
    if not answered:
        return None
    remote = newest(rows)
    local, page = {}, 0
    while True:
        chunk = database.Get_Serving_APN_Rows(page=page, page_size=PAGE_SIZE)
        local.update({_key(row): row for row in chunk})
        if len(chunk) < PAGE_SIZE:
            break
        page += 1
    updated = removed = 0
    for key, row in remote.items():
        mine = local.get(key)
        if mine and mine.get('pcrf_session_id') == row['pcrf_session_id']:
            continue
        theirs = parse_time(row.get('serving_pgw_timestamp')) or datetime.datetime.min
        if mine and (parse_time(mine.get('serving_pgw_timestamp')) or datetime.datetime.min) > theirs:
            continue  # the local row is the newer one; its push reaches the peers
        _store(database, row)
        updated += 1
    cutoff = now() - datetime.timedelta(seconds=KEEP_SECONDS)
    for key, mine in local.items():
        if key in remote:
            continue
        when = parse_time(mine.get('serving_pgw_timestamp'))
        if when is not None and when > cutoff:
            continue
        database.Update_Serving_APN(imsi=str(mine['imsi']), apn=mine['serving_apn'], pcrf_session_id=mine.get('pcrf_session_id'),
                                    serving_pgw=None, subscriber_routing=mine.get('subscriber_routing'), propagate=False)
        removed += 1
    result = {'peers': answered, 'rows': len(remote), 'updated': updated, 'removed': removed}
    log('info' if not (updated or removed) else 'warning', f"[serving_apn_sync] serving APN catch-up from {answered} peers: "
        f"{len(remote)} rows there, {updated} taken over, {removed} dropped here")
    return result
