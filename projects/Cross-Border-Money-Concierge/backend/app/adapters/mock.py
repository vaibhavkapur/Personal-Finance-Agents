"""Credential-free provider simulator with a separate persisted provider ledger.
Writes are idempotent by request reference. Coordinator calls are outside its transaction.
"""
from typing import Protocol
from datetime import datetime, timedelta
from uuid import uuid4
from ..persistence import db as d
from ..domain.lifecycle import require

class RemittanceProviderAdapter(Protocol):
    def initiate(self, transfer: dict, request_ref: str) -> dict: ...
    def find_transfer(self, request_ref: str) -> dict: ...
    def provide_documents(self, transfer_ref: str, packet: dict, request_ref: str) -> dict: ...
    def request_cancellation(self, transfer_ref: str, request_ref: str) -> dict: ...

class MockProvider:
    capabilities = {'environment': 'mock', 'lookup_by_request_ref': True, 'documents': True, 'cancellation': True}
    def __init__(self, database, clock):
        self.db, self.clock = database, clock

    def find_transfer(self, request_ref):
        with self.db.tx() as conn:
            found = d.rows(conn, d.provider_transfers, d.provider_transfers.c.request_ref == request_ref)
            return found[0] if found else None

    def initiate(self, transfer, request_ref):
        with self.db.tx() as conn:
            found = d.rows(conn, d.provider_transfers, d.provider_transfers.c.request_ref == request_ref)
            if found:
                return found[0]
            now = self.clock(conn)
            result = {'id': 'mock_' + uuid4().hex[:16], 'request_ref': request_ref,
                      'provider_id': transfer['provider_id'], 'accepted_at': now.isoformat(),
                      'status': 'rejected' if transfer['scenario'] == 'rejection' else 'funding_pending',
                      'scenario': transfer['scenario'], 'documents_received': False,
                      'source_total_minor': transfer['quote']['source_total_minor'],
                      'fee_minor': transfer['quote']['fee_minor'], 'recipient_minor': transfer['quote']['recipient_minor'],
                      'delivery_hours': transfer['quote']['delivery_window']['hours'][1], 'environment': 'mock'}
            d.put(conn, d.provider_transfers, result)
        if transfer['scenario'] in {'timeout', 'malformed'}:
            raise TimeoutError('Provider accepted the request but the response was unavailable.')
        return result

    def provide_documents(self, transfer_ref, packet, request_ref):
        with self.db.tx() as conn:
            previous = d.rows(conn, d.provider_operations, d.provider_operations.c.request_ref == request_ref)
            if previous:
                return previous[0]
            value = d.get(conn, d.provider_transfers, transfer_ref)
            require(value is not None, 'Provider transfer not found', 404)
            value['documents_received'] = True
            value['documents_received_at'] = self.clock(conn).isoformat()
            d.put(conn, d.provider_transfers, value)
            return d.put(conn, d.provider_operations, {'id': request_ref, 'request_ref': request_ref, 'status': 'accepted', 'manifest_hash': packet['manifest_hash'], 'provider_reference': transfer_ref, 'environment': 'mock'})

    def request_cancellation(self, transfer_ref, request_ref):
        with self.db.tx() as conn:
            previous = d.rows(conn, d.provider_operations, d.provider_operations.c.request_ref == request_ref)
            if previous:
                return previous[0]
            value = d.get(conn, d.provider_transfers, transfer_ref)
            require(value is not None, 'Provider transfer not found', 404)
            now = self.clock(conn)
            too_late = now >= datetime.fromisoformat(value['accepted_at']) + timedelta(hours=value['delivery_hours'])
            denied = value['scenario'] == 'cancel_denied' or too_late
            if not denied:
                value['status'] = 'cancelled'
                d.put(conn, d.provider_transfers, value)
            return d.put(conn, d.provider_operations, {'id': request_ref, 'request_ref': request_ref, 'status': 'denied' if denied else 'cancelled', 'provider_reference': transfer_ref, 'refund_status': 'not_started' if denied else 'pending', 'environment': 'mock'})

    def lookup_operation(self, request_ref):
        with self.db.tx() as conn:
            found = d.rows(conn, d.provider_operations, d.provider_operations.c.request_ref == request_ref)
            return found[0] if found else None
