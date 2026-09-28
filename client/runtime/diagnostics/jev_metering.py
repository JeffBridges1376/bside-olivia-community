"""Durable provider-attempt receipts. Unknown usage is NULL, never zero.

This meter does not debit wallets or record prompts/answers/API credentials.
Each actual network attempt gets its own receipt, including retries. The caller
must create it before sending and complete it before returning the response.
"""
from datetime import datetime, timezone
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import uuid


class JevMeter:
    def __init__(self, path=None):
        self.path = path or os.environ.get('OLIVIA_JEV_USAGE_DB')

    @contextmanager
    def _connect(self):
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
        connection.execute('''CREATE TABLE IF NOT EXISTS provider_attempts (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL, purpose TEXT NOT NULL,
            model TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
            outcome TEXT NOT NULL, input_tokens INTEGER, provider_request_id TEXT,
            CHECK(input_tokens IS NULL OR input_tokens >= 0))''')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def begin(self, request_id, purpose, model):
        if not self.path:
            return None
        attempt = uuid.uuid4().hex
        with self._connect() as db:
            db.execute('INSERT INTO provider_attempts VALUES (?,?,?,?,?,NULL,?,NULL,NULL)',
                       (attempt, request_id, purpose, model, datetime.now(timezone.utc).isoformat(), 'pending'))
        return attempt

    def finish(self, attempt, outcome, payload=None, provider_request_id=None):
        if attempt is None:
            return
        usage = payload.get('usage') if isinstance(payload, dict) else None
        tokens = usage.get('input_tokens') if isinstance(usage, dict) else None
        if type(tokens) is not int or tokens < 0:
            tokens = None
        with self._connect() as db:
            # Finishing/replaying a receipt cannot produce another chargeable row.
            db.execute('''UPDATE provider_attempts SET completed_at=?, outcome=?,
                input_tokens=?, provider_request_id=? WHERE id=? AND completed_at IS NULL''',
                (datetime.now(timezone.utc).isoformat(), outcome, tokens,
                 provider_request_id[:256] if isinstance(provider_request_id, str) else None, attempt))
