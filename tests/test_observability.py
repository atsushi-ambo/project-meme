import json
import logging
import os
import re
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text
from app.main import app
from app.config import settings
from app.db import transaction
from app.models import Base


@pytest.fixture
def client():
    assert os.environ.get('MEME_TEST_MODE') == '1' and settings.media_root in {Path('/media'), Path('/tmp/meme-test-media')}, 'Use python3 scripts/test.py; refusing to clear non-test data'
    settings.media_root.mkdir(parents=True, exist_ok=True)
    (settings.media_root / 'staging').mkdir(exist_ok=True)
    (settings.media_root / 'deleting-all').unlink(missing_ok=True)
    with transaction(exclusive=True) as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(delete(table))
    with TestClient(app, base_url='http://localhost:8000') as c:
        yield c


def test_request_id_header_is_present_and_unique(client):
    first = client.get('/healthz')
    second = client.get('/healthz')
    assert first.status_code == 200
    assert re.fullmatch(r'[0-9a-f]{32}', first.headers['x-request-id'])
    assert first.headers['x-request-id'] != second.headers['x-request-id']


def test_access_log_is_structured_json(client, caplog):
    with caplog.at_level(logging.INFO, logger='meme.http'):
        response = client.get('/healthz')
    lines = [record.getMessage() for record in caplog.records if record.name == 'meme.http']
    assert lines, 'アクセスログが出ていません'
    entry = json.loads(lines[-1])
    assert entry['event'] == 'http_request'
    assert entry['method'] == 'GET'
    assert entry['path'] == '/healthz'
    assert entry['status'] == 200
    assert entry['request_id'] == response.headers['x-request-id']
    assert entry['duration_ms'] >= 0


def test_trigram_search_indexes_exist(client):
    with transaction() as db:
        extension = db.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"))
        indexes = set(db.scalars(text(
            "SELECT indexname FROM pg_indexes WHERE indexname IN "
            "('ix_observations_description_trgm', 'ix_comments_body_trgm', 'ix_health_details_trgm')")))
    assert extension == 1
    assert indexes == {'ix_observations_description_trgm', 'ix_comments_body_trgm',
                       'ix_health_details_trgm'}
