"""分類の版を戻すとき、入力済みの記録を暗黙に捨てない。"""
import os
import subprocess
import uuid

import pytest
from sqlalchemy import select, text

from app.db import transaction
from app.models import CallType, Media, SoundAnalysis, SoundBehavior, now
from test_app import client, observation


def migrate(*args, check=True):
    # clientの安全確認を通った隔離DBだけ。既存のmigrationテストと同じ所有者で実行する。
    env = {**os.environ, 'DATABASE_URL': 'postgresql+psycopg://meme_owner:test-owner-only@db:5432/meme'}
    return subprocess.run(['alembic', *args], env=env, check=check, capture_output=True, text=True)


def legacy_sound(client):
    oid = observation(client, '移行検証用の架空の記録。')
    with transaction() as db:
        media = Media(observation_id=oid, storage_key=uuid.uuid4().hex, upload_key=str(uuid.uuid4()),
                      original_name='fixture.wav', reserved_bytes=1)
        db.add(media)
        db.flush()
        sound = SoundAnalysis(media_id=media.id, start_ms=0, end_ms=1000, rms=0, peak=0,
                              observation='旧版にも残る観察')
        db.add(sound)
        db.flush()
        return sound.id


@pytest.mark.parametrize('data', ['custom_type', 'renamed_type', 'description', 'features',
                                  'supports', 'contradicts', 'updated_at', 'behavior', 'call_count'])
def test_voice_downgrade_refuses_populated_fields(client, data):
    sid = legacy_sound(client)
    with transaction() as db:
        sound = db.get(SoundAnalysis, sid)
        if data == 'custom_type':
            db.add(CallType(name='架空の新しい声', description='私用の説明'))
        elif data == 'renamed_type':
            db.scalar(select(CallType).order_by(CallType.id)).name = '架空の変更名'
        elif data == 'description':
            db.scalar(select(CallType).order_by(CallType.id)).description = '架空の追記説明'
        elif data == 'features':
            sound.features = {'pitch_hz': {'median': 600}}
        elif data in {'supports', 'contradicts'}:
            sound.assessment = data
        elif data == 'updated_at':
            sound.updated_at = now()
        elif data == 'behavior':
            db.add(SoundBehavior(sound_analysis_id=sid, behavior='look'))
        else:
            sound.call_count = 2
    with transaction() as db:
        before = dict(db.execute(select(SoundAnalysis.__table__).where(SoundAnalysis.id == sid)).mappings().one())
        types_before = list(db.execute(select(CallType.__table__).order_by(CallType.id)))
        behaviors_before = list(db.execute(select(SoundBehavior.__table__)))
    try:
        result = migrate('downgrade', 'da307c85e591', check=False)
        assert result.returncode != 0
        assert 'RuntimeError: 声の分類・行動・その後の記録を自動削除できません。' in result.stderr
        with transaction() as db:
            assert dict(db.execute(select(SoundAnalysis.__table__).where(SoundAnalysis.id == sid)).mappings().one()) == before
            assert list(db.execute(select(CallType.__table__).order_by(CallType.id))) == types_before
            assert list(db.execute(select(SoundBehavior.__table__))) == behaviors_before
    finally:
        migrate('upgrade', 'head')


@pytest.mark.parametrize('features', [None, {}])
def test_voice_downgrade_allows_untouched_defaults_and_empty_features(client, features):
    sid = legacy_sound(client)
    with transaction() as db:
        db.get(SoundAnalysis, sid).features = features
    try:
        migrate('downgrade', 'da307c85e591')
        with transaction() as db:
            assert db.scalar(text('SELECT observation FROM sound_analyses WHERE id = :id'), {'id': sid}) == '旧版にも残る観察'
    finally:
        migrate('upgrade', 'head')
    with transaction() as db:
        assert db.get(SoundAnalysis, sid).observation == '旧版にも残る観察'


def test_sound_revision_upgrade_and_downgrade_preserve_labels(client):
    sid = legacy_sound(client)
    with transaction() as db:
        sound = db.get(SoundAnalysis, sid)
        sound.revision = 7
        sound.owner_response = '移行しても残す架空の対応'
        sound.assessment = 'supports'
    try:
        migrate('downgrade', 'f4c7a9102b6d')
        with transaction() as db:
            assert not db.scalar(text("SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                                      "WHERE table_name = 'sound_analyses' AND column_name = 'revision')"))
            assert db.execute(text('SELECT observation, owner_response, assessment FROM sound_analyses WHERE id = :id'),
                              {'id': sid}).one() == ('旧版にも残る観察', '移行しても残す架空の対応', 'supports')
        migrate('upgrade', 'head')
        with transaction() as db:
            sound = db.get(SoundAnalysis, sid)
            assert sound.revision == 1
            assert (sound.observation, sound.owner_response, sound.assessment) == (
                '旧版にも残る観察', '移行しても残す架空の対応', 'supports')
            assert db.scalar(text("SELECT is_nullable FROM information_schema.columns "
                                  "WHERE table_name = 'sound_analyses' AND column_name = 'revision'")) == 'NO'
            # ORMの既定値ではなく、DBの既定値で新規行も1になる。
            revision = db.scalar(text("INSERT INTO sound_analyses "
                "(media_id, start_ms, end_ms, rms, peak, observation, hypothesis, unknown, created_at) "
                "SELECT media_id, 1000, 2000, 0, 0, '', '', '', created_at FROM sound_analyses WHERE id = :id "
                'RETURNING revision'), {'id': sid})
            assert revision == 1
    finally:
        migrate('upgrade', 'head')
