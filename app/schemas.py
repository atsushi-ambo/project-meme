"""ブラウザの入力はここで型・長さ・組み合わせを確認する。"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

JST = ZoneInfo('Asia/Tokyo')
RecordId = Annotated[int, Field(ge=1, le=9223372036854775807)]

def parse_time(value):
    if value in ('', None):
        return None
    if not isinstance(value, (str, datetime)):
        raise ValueError('日時はISO形式の文字列で指定してください。')
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else value
        utc = (dt.replace(tzinfo=JST) if dt.tzinfo is None else dt).astimezone(timezone.utc)
        utc.astimezone(JST)  # 保存できても画面の日本時間で表示できない値は拒否する。
        return utc
    except (ValueError, OverflowError):
        raise ValueError('日時をUTCへ変換できません。対応範囲内の日時を指定してください。') from None

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class ObservationInput(Input):
    occurred_at: datetime
    location: str = Field(default='', max_length=200)
    description: str = Field(min_length=1, max_length=10000)
    tags: list[RecordId] = Field(default_factory=list, max_length=6)
    submission_key: UUID
    _time = field_validator('occurred_at', mode='before')(parse_time)

class HealthInput(Input):
    occurred_at: datetime
    event_type: Literal['vomit','food','water','toilet','weight','energy','other']
    count: int | None = Field(default=None, ge=1, le=1000)
    weight_value: Decimal | None = Field(default=None, gt=0, lt=100000, max_digits=8, decimal_places=3)
    weight_unit: Literal['kg','g'] | None = None
    meal_interval_minutes: int | None = Field(default=None, ge=0, le=10080)
    appetite: Literal['usual','less','none','more'] | None = None
    energy: Literal['usual','less','very_low','more'] | None = None
    details: str = Field(default='', max_length=10000)
    submission_key: UUID
    _time = field_validator('occurred_at', mode='before')(parse_time)

    @field_validator('count','weight_value','weight_unit','meal_interval_minutes','appetite','energy', mode='before')
    @classmethod
    def empty(cls, value):
        return None if value == '' else value

    @model_validator(mode='after')
    def combinations(self):
        if self.event_type == 'weight':
            if self.weight_value is None or self.weight_unit is None:
                raise ValueError('体重には数値と単位を入力してください。')
        elif self.weight_value is not None or self.weight_unit is not None:
            raise ValueError('体重以外の記録では体重と単位を空欄にしてください。')
        if self.event_type != 'vomit' and (self.count is not None or self.meal_interval_minutes is not None):
            raise ValueError('回数と食事との間隔は嘔吐の記録で入力してください。')
        return self

class DictionaryInput(Input):
    kind: Literal['hypothesis','cue']
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=3000)
    notes: str = Field(default='', max_length=3000)

class InteractionInput(Input):
    acted_at: datetime | None = None
    owner_action: str = Field(default='', max_length=3000)
    cue_item_id: RecordId | None = None
    cat_response: str | None = Field(default=None, max_length=3000)
    responded_at: datetime | None = None
    _times = field_validator('acted_at','responded_at', mode='before')(parse_time)

    @field_validator('cue_item_id','cat_response', mode='before')
    @classmethod
    def empty(cls, value):
        return None if value == '' else value

    @model_validator(mode='after')
    def order(self):
        if not self.owner_action and not self.cat_response:
            raise ValueError('自分がしたこと、またはメメのその後を入力してください。')
        if self.acted_at and self.responded_at and self.responded_at < self.acted_at:
            raise ValueError('メメのその後を見た日時は、自分がした日時以降にしてください。')
        return self

class EvidenceInput(Input):
    dictionary_item_id: RecordId
    assessment: Literal['supports','contradicts','unknown']
    rationale: str = Field(default='', max_length=3000)

class ReserveInput(Input):
    parent_kind: Literal['observation','health']
    parent_id: RecordId
    original_name: str = Field(min_length=1, max_length=200, pattern=r'^[^\x00-\x1f/\\]+$')
    size: int = Field(gt=0)
    upload_key: UUID
    captured_at: datetime | None = None
    _time = field_validator('captured_at', mode='before')(parse_time)


class MediaTimeInput(Input):
    captured_at: datetime | None = None
    _time = field_validator('captured_at', mode='before')(parse_time)


class GuessesInput(Input):
    guesses: list[str] = Field(min_length=1, max_length=30)

    @field_validator('guesses')
    @classmethod
    def bounded_guesses(cls, values):
        if any(not value or len(value) > 40 for value in values):
            raise ValueError('予想の項目が不正です。')
        return values


class ObservationEditInput(Input):
    # HTMLフォームは文字列を送るため、ここでは整数への変換を許可する。
    expected_revision: int = Field(ge=1, le=9223372036854775807)


class AppendInput(Input):
    operation_key: UUID
    expected_revision: int = Field(strict=True, ge=1, le=9223372036854775807)
    text: str = Field(min_length=1, max_length=10000)


class MediaKeepInput(Input):
    keep: StrictBool


def blank_none(value):
    return None if value == '' else value


class SoundLabelsInput(Input):
    """声の区間に飼い主が付ける分類・行動・その後。どれも任意。"""
    call_type_id: RecordId | None = None
    call_count: int | None = Field(default=None, ge=1, le=100)
    behaviors: list[str] = Field(default_factory=list, max_length=20)
    meaning: str = Field(default='', max_length=40)
    assessment: Literal['supports', 'contradicts', 'unknown'] = 'unknown'
    owner_response: str = Field(default='', max_length=1000)
    outcome: Literal['settled', 'repeated', 'stronger', 'left', 'unknown'] | None = None
    observation: str = Field(default='', max_length=1000)
    hypothesis: str = Field(default='', max_length=1000)
    unknown: str = Field(default='', max_length=1000)
    _blank = field_validator('call_type_id', 'call_count', 'outcome', mode='before')(blank_none)

    @field_validator('behaviors')
    @classmethod
    def known_behaviors(cls, values):
        from .services import BEHAVIORS
        if any(value not in BEHAVIORS for value in values):
            raise ValueError('行動の選択肢を確認してください。')
        return list(dict.fromkeys(values))

    @field_validator('owner_response', 'observation', 'hypothesis', 'unknown')
    @classmethod
    def no_nul(cls, value):
        if '\x00' in value:
            raise ValueError('使用できない文字が含まれています。')
        return value


class SoundCreateInput(SoundLabelsInput):
    """ローカルCLIから区間を保存するときの入力。開始・終了は秒。"""
    media_id: RecordId
    start: str = Field(max_length=20)
    end: str = Field(max_length=20)


class CallTypeInput(Input):
    name: str = Field(min_length=1, max_length=40, pattern=r'^[^\x00-\x1f]+$')
    description: str = Field(default='', max_length=500)
