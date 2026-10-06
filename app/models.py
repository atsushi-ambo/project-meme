"""主キー、外部キー、CHECK制約をPythonで定義する。"""
from datetime import datetime, timezone
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase

def now():
    return datetime.now(timezone.utc)

class Base(DeclarativeBase):
    pass

class Timestamps:
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now, onupdate=now)

class Cat(Timestamps, Base):
    __tablename__ = 'cats'
    id = Column(BigInteger, primary_key=True)
    name = Column(String(80), nullable=False)
    birthday = Column(Date)
    notes = Column(Text, nullable=False, default='')

class Observation(Timestamps, Base):
    __tablename__ = 'observations'
    id = Column(BigInteger, primary_key=True)
    cat_id = Column(ForeignKey('cats.id', ondelete='RESTRICT'), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    timezone = Column(String(64), nullable=False, default='Asia/Tokyo')
    location = Column(String(200), nullable=False, default='')
    description = Column(Text, nullable=False)
    revision = Column(BigInteger, nullable=False, default=1, server_default='1')
    is_sample = Column(Boolean, nullable=False, default=False, server_default='false')
    submission_key = Column(String(36), nullable=False, unique=True)
    submission_hash = Column(String(64), nullable=False)
    deletion_requested_at = Column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('revision >= 1', name='observation_revision'),
                      Index('ix_observation_date', 'cat_id', 'occurred_at'),)

class ObservationAppend(Base):
    """追記の再送を判別する管理行。本文は観察の一つのメモにだけ保存する。"""
    __tablename__ = 'observation_appends'
    operation_key = Column(String(36), primary_key=True)
    observation_id = Column(ForeignKey('observations.id', ondelete='CASCADE'), nullable=False, index=True)
    request_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)


class Tag(Base):
    __tablename__ = 'situation_tags'
    id = Column(BigInteger, primary_key=True)
    name = Column(String(40), nullable=False, unique=True)

class ObservationTag(Base):
    __tablename__ = 'observation_tags'
    observation_id = Column(ForeignKey('observations.id', ondelete='CASCADE'), primary_key=True)
    tag_id = Column(ForeignKey('situation_tags.id', ondelete='RESTRICT'), primary_key=True, index=True)

class Comment(Timestamps, Base):
    __tablename__ = 'observation_comments'
    id = Column(BigInteger, primary_key=True)
    observation_id = Column(ForeignKey('observations.id', ondelete='CASCADE'), nullable=False, index=True)
    body = Column(Text, nullable=False)

class DictionaryItem(Timestamps, Base):
    __tablename__ = 'dictionary_items'
    id = Column(BigInteger, primary_key=True)
    cat_id = Column(ForeignKey('cats.id', ondelete='RESTRICT'), nullable=False)
    kind = Column(String(20), nullable=False)
    title = Column(String(120), nullable=False)
    hypothesis = Column(Text)
    cue = Column(Text)
    notes = Column(Text, nullable=False, default='')
    __table_args__ = (CheckConstraint("(kind='hypothesis' AND hypothesis IS NOT NULL AND cue IS NULL) OR (kind='cue' AND cue IS NOT NULL AND hypothesis IS NULL)", name='dictionary_kind'),)

class Interaction(Timestamps, Base):
    __tablename__ = 'interactions'
    id = Column(BigInteger, primary_key=True)
    observation_id = Column(ForeignKey('observations.id', ondelete='CASCADE'), nullable=False, index=True)
    acted_at = Column(DateTime(timezone=True))
    owner_action = Column(Text, nullable=False)
    cue_item_id = Column(ForeignKey('dictionary_items.id', ondelete='SET NULL'))
    cat_response = Column(Text)
    responded_at = Column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('responded_at IS NULL OR acted_at IS NULL OR responded_at >= acted_at', name='response_order'),)

class Evidence(Timestamps, Base):
    __tablename__ = 'dictionary_evidence'
    dictionary_item_id = Column(ForeignKey('dictionary_items.id', ondelete='CASCADE'), primary_key=True)
    observation_id = Column(ForeignKey('observations.id', ondelete='CASCADE'), primary_key=True, index=True)
    assessment = Column(String(20), nullable=False)
    rationale = Column(Text, nullable=False, default='')
    __table_args__ = (CheckConstraint("assessment IN ('supports','contradicts','unknown')", name='assessment_values'),)

class Health(Timestamps, Base):
    __tablename__ = 'health_entries'
    id = Column(BigInteger, primary_key=True)
    cat_id = Column(ForeignKey('cats.id', ondelete='RESTRICT'), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    timezone = Column(String(64), nullable=False, default='Asia/Tokyo')
    event_type = Column(String(20), nullable=False)
    count = Column(Integer)
    weight_value = Column(Numeric(8, 3))
    weight_unit = Column(String(2))
    meal_interval_minutes = Column(Integer)
    appetite = Column(String(20))
    energy = Column(String(20))
    details = Column(Text, nullable=False, default='')
    is_sample = Column(Boolean, nullable=False, default=False, server_default='false')
    submission_key = Column(String(36), nullable=False, unique=True)
    submission_hash = Column(String(64), nullable=False)
    deletion_requested_at = Column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("event_type IN ('vomit','food','water','toilet','weight','energy','other')", name='health_kind'),
        CheckConstraint("(event_type='weight' AND weight_value > 0 AND weight_value < 100000 AND weight_unit IN ('kg','g') AND weight_value IS NOT NULL AND weight_unit IS NOT NULL) OR (event_type <> 'weight' AND weight_value IS NULL AND weight_unit IS NULL)", name='weight_pair'),
        CheckConstraint("count IS NULL OR (event_type='vomit' AND count BETWEEN 1 AND 1000)", name='vomit_count'),
        CheckConstraint("meal_interval_minutes IS NULL OR (event_type='vomit' AND meal_interval_minutes BETWEEN 0 AND 10080)", name='meal_interval'),
        Index('ix_health_date', 'cat_id', 'occurred_at'),
    )

class Media(Timestamps, Base):
    __tablename__ = 'media'
    id = Column(BigInteger, primary_key=True)
    observation_id = Column(ForeignKey('observations.id', ondelete='RESTRICT'), index=True)
    health_entry_id = Column(ForeignKey('health_entries.id', ondelete='RESTRICT'), index=True)
    storage_key = Column(String(32), nullable=False, unique=True)
    original_name = Column(String(200), nullable=False)
    mime_type = Column(String(80))
    bytes = Column(BigInteger)
    reserved_bytes = Column(BigInteger, nullable=False)
    upload_key = Column(String(36), nullable=False, unique=True)
    captured_at = Column(DateTime(timezone=True))
    captured_at_source = Column(String(20), nullable=False, default='unknown')
    status = Column(String(20), nullable=False, default='pending')
    retention_started_at = Column(DateTime(timezone=True))
    expires_at = Column(DateTime(timezone=True))
    keep = Column(Boolean, nullable=False, default=False)
    deleted_at = Column(DateTime(timezone=True))
    deletion_reason = Column(String(40))
    retry_count = Column(Integer, nullable=False, default=0)
    next_retry_at = Column(DateTime(timezone=True))
    last_error_code = Column(String(40))
    __table_args__ = (
        CheckConstraint('(observation_id IS NULL) <> (health_entry_id IS NULL)', name='media_parent'),
        CheckConstraint("status IN ('pending','ready','failed','delete_pending','deleted')", name='media_status'),
        CheckConstraint('reserved_bytes >= 0 AND (bytes IS NULL OR bytes > 0)', name='media_size'),
        CheckConstraint("status <> 'ready' OR (bytes IS NOT NULL AND mime_type IS NOT NULL AND retention_started_at IS NOT NULL AND expires_at IS NOT NULL)", name='media_ready'),
        Index('ix_media_expiration', 'status', 'expires_at'),
    )
