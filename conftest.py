"""Shared pytest fixtures/fakes.

FakeKV mimics the small slice of nats.js.kv.KeyValue's interface this
codebase actually uses (get/put/delete) so KV-backed logic can be unit
tested without a live NATS server, same as the mocked Anthropic/Gemini
clients used elsewhere.
"""
import pytest
from nats.js.errors import BucketNotFoundError, KeyNotFoundError


class _FakeEntry:
    def __init__(self, value: bytes):
        self.value = value


class FakeKV:
    def __init__(self):
        self._data: dict[str, bytes] = {}

    async def get(self, key: str):
        if key not in self._data:
            raise KeyNotFoundError()
        return _FakeEntry(self._data[key])

    async def put(self, key: str, value: bytes):
        self._data[key] = value

    async def delete(self, key: str):
        if key not in self._data:
            raise KeyNotFoundError()
        del self._data[key]


class FakeJS:
    """Fake JetStream context exposing just key_value()/create_key_value()
    (get-or-create pattern used throughout this codebase) plus publish(),
    recording published messages for assertions."""

    def __init__(self):
        self._buckets: dict[str, FakeKV] = {}
        self.published: list[tuple[str, bytes]] = []

    async def key_value(self, bucket: str):
        if bucket not in self._buckets:
            raise BucketNotFoundError()
        return self._buckets[bucket]

    async def create_key_value(self, bucket: str):
        self._buckets.setdefault(bucket, FakeKV())
        return self._buckets[bucket]

    async def publish(self, subject: str, payload: bytes):
        self.published.append((subject, payload))


@pytest.fixture
def fake_kv():
    return FakeKV()


@pytest.fixture
def fake_js():
    return FakeJS()
