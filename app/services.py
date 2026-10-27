"""画面から独立した保存処理。まず検証し、一つのトランザクションで更新する。"""
import hashlib
from sqlalchemy import select, delete
from fastapi import HTTPException
from .models import Cat, Observation, ObservationAppend, Health, Tag, ObservationTag, DictionaryItem, Evidence, Interaction

KINDS = {'observation': Observation, 'health': Health}
LABELS = {'vomit':'吐いた','food':'食事','water':'水','toilet':'トイレ','weight':'体重','energy':'元気','other':'その他'}
ASSESSMENTS = {'unknown':'まだ分からない','supports':'合っていそう','contradicts':'合わなそう'}
CONDITIONS = {'usual':'いつも通り','less':'少なめ','none':'なし','very_low':'かなり低い','more':'多め'}

def parent(db, kind, id, lock=False):
    model = KINDS.get(kind)
    if model is None:
        raise HTTPException(404, '記録がありません。')
    query = select(model).where(model.id == id, model.deletion_requested_at.is_(None))
    row = db.scalar(query.with_for_update() if lock else query)
    if row is None:
        raise HTTPException(404, '記録がないか、削除処理中です。')
    return row

def fingerprint(data):
    return hashlib.sha256(data.model_dump_json().encode()).hexdigest()

def save_entry(db, kind, data, id=None, *, expected_revision=None):
    # 同じ猫への新規保存を直列化し、再送を既存の結果へ結びつける。
    cat = db.scalar(select(Cat).where(Cat.id == 1).with_for_update())
    model = KINDS[kind]
    values = data.model_dump(exclude={'tags','submission_key'})
    if id:
        row = parent(db, kind, id, True)
        if kind == 'observation':
            if row.revision != expected_revision:
                raise HTTPException(409, '別の画面やCodexから更新されています。入力したメモは下に残しています。保存済みの記録を確認し、編集画面を開き直して内容を合わせてください。')
            row.revision += 1
    else:
        row = db.scalar(select(model).where(model.submission_key == str(data.submission_key)))
        if row:
            if row.submission_hash != fingerprint(data) or row.deletion_requested_at:
                raise HTTPException(409, '同じ送信に異なる内容があります。新しい入力画面から保存してください。')
            return row
        row = model(cat_id=cat.id, submission_key=str(data.submission_key), submission_hash=fingerprint(data))
        db.add(row)
    for key,value in values.items():
        setattr(row,key,value)
    db.flush()
    if kind == 'observation':
        ids = set(data.tags)
        if set(db.scalars(select(Tag.id).where(Tag.id.in_(ids)))) != ids:
            raise HTTPException(422, '状況タグが不正です。')
        db.execute(delete(ObservationTag).where(ObservationTag.observation_id == row.id))
        db.add_all(ObservationTag(observation_id=row.id, tag_id=id) for id in ids)
    return row

def save_dictionary(db, data, id=None):
    row = db.get(DictionaryItem, id, with_for_update=True) if id else DictionaryItem(cat_id=1)
    if row is None:
        raise HTTPException(404)
    if id and row.kind != data.kind:
        raise HTTPException(422, '種類の変更は、新しい辞書項目として登録してください。')
    row.kind, row.title, row.notes = data.kind, data.title, data.notes
    row.hypothesis = data.content if data.kind == 'hypothesis' else None
    row.cue = data.content if data.kind == 'cue' else None
    db.add(row)
    db.flush()
    return row

def save_interaction(db, observation_id, data, id=None):
    observation = parent(db, 'observation', observation_id, True)
    row = db.get(Interaction, id) if id else Interaction(observation_id=observation_id)
    if not row or row.observation_id != observation_id:
        raise HTTPException(404)
    from .schemas import InteractionInput
    # 新規作成では省略された既定値も使う。更新時だけ既存の値を引き継ぐ。
    values = data.model_dump()
    if id:
        values = {name: getattr(row, name) for name in InteractionInput.model_fields}
        values.update(data.model_dump(exclude_unset=True))
        # 省略された日時も合わせて再検証し、DB制約違反の前に入力エラーを返す。
        values = InteractionInput.model_validate(values).model_dump()
    if values['cue_item_id']:
        cue = db.get(DictionaryItem, values['cue_item_id'], with_for_update=True)
        if not cue or cue.cat_id != observation.cat_id or cue.kind != 'cue':
            raise HTTPException(422, 'この合図は関連づけられません。')
    for key, value in values.items():
        setattr(row, key, value)
    db.add(row)

def save_evidence(db, observation_id, data):
    observation = parent(db, 'observation', observation_id, True)
    item = db.get(DictionaryItem, data.dictionary_item_id, with_for_update=True)
    if not item or item.kind != 'hypothesis' or item.cat_id != observation.cat_id:
        raise HTTPException(422, 'この予想は関連づけられません。')
    row = db.get(Evidence, (item.id, observation.id)) or Evidence(dictionary_item_id=item.id, observation_id=observation.id)
    row.assessment, row.rationale = data.assessment, data.rationale
    db.add(row)


LOCATIONS = ['リビング', 'キッチン・台所', '寝室', '仕事部屋', '階段', '廊下', '玄関', '2階の部屋', '物置', 'トイレ', 'その他']
GUESS_PRESETS = {
    'play': '遊びたい', 'food': 'ごはんがほしい', 'attention': 'かまってほしい',
    'door': 'ドアを開けてほしい', 'follow': 'ついてきてほしい',
    'concern': '何かが気になる・嫌そう', 'unknown': 'まだ分からない',
}


def add_guesses(db, observation_id, choices):
    # 他の観察から同じ候補を追加しても辞書が重複しないよう猫単位で直列化。
    db.scalar(select(Cat).where(Cat.id == 1).with_for_update())
    observation = parent(db, 'observation', observation_id, True)
    for choice in set(choices):
        prefix, _, value = choice.partition(':')
        if prefix == 'preset' and value in GUESS_PRESETS:
            title = GUESS_PRESETS[value]
            item = db.scalar(select(DictionaryItem).where(DictionaryItem.cat_id == observation.cat_id,
                DictionaryItem.kind == 'hypothesis', DictionaryItem.title == title).order_by(DictionaryItem.id))
            if item is None:
                item = DictionaryItem(cat_id=observation.cat_id, kind='hypothesis', title=title,
                    hypothesis=title, notes='入力候補から追加した飼い主の予想。意味が確定したものではありません。')
                db.add(item)
                db.flush()
        elif prefix == 'item' and value.isdecimal() and len(value) <= 19 and int(value) <= 9223372036854775807:
            item = db.get(DictionaryItem, int(value))
            if not item or item.kind != 'hypothesis' or item.cat_id != observation.cat_id:
                raise HTTPException(422, 'この予想は選択できません。')
        else:
            raise HTTPException(422, '予想の選択内容を確認してください。')
        if db.get(Evidence, (item.id, observation.id)) is None:
            db.add(Evidence(dictionary_item_id=item.id, observation_id=observation.id,
                            assessment='unknown', rationale=''))
            db.flush()


def append_observation(db, observation_id, data):
    # 新規保存・編集と同じ順でロックし、本文と再送管理行を一緒に確定する。
    db.scalar(select(Cat).where(Cat.id == 1).with_for_update())
    row = parent(db, 'observation', observation_id, True)
    request_hash = hashlib.sha256(f'{observation_id}:{fingerprint(data)}'.encode()).hexdigest()
    previous = db.get(ObservationAppend, str(data.operation_key))
    if previous:
        if previous.observation_id != row.id or previous.request_hash != request_hash:
            raise HTTPException(409, '同じ追記の送信キーに異なる内容があります。保存状態を確認してください。')
        return row, True
    if row.revision != data.expected_revision:
        raise HTTPException(409, '確認後にメモが更新されています。最新の内容を確認してから追記してください。自動では上書きしません。')
    combined = row.description + '\n\n' + data.text
    if len(combined) > 10000:
        raise HTTPException(422, '追記後のメモは10000文字以内にしてください。')
    row.description = combined
    row.revision += 1
    db.add(ObservationAppend(operation_key=str(data.operation_key), observation_id=row.id,
                             request_hash=request_hash))
    db.flush()
    return row, False
