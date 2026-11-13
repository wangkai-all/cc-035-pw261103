import tempfile
from pathlib import Path

import pytest

from app.collector import Collector, GraphQLClient
from app.db import Database
from app.source import SourceState, create_source_app
from tests.transport import ASGITransport


@pytest.fixture
def source_state() -> SourceState:
    return SourceState()


@pytest.fixture
def source_client(source_state):
    from fastapi.testclient import TestClient
    return TestClient(create_source_app(source_state))


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


@pytest.fixture
def collector(db, source_state) -> Collector:
    transport = ASGITransport(create_source_app(source_state))
    client = GraphQLClient("http://source/graphql", transport=transport)
    return Collector(db, client)


@pytest.fixture
def snapshot(source_state):
    return source_state.create_snapshot(label="test")


@pytest.fixture
def batch(collector, snapshot) -> str:
    return collector.create_batch(snapshot.id, "test-source", page_size=2)
