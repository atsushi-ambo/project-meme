"""python -m app.demo: 空のDBへ、明示したサンプルを3件だけ追加する。"""
from datetime import timedelta
import uuid
from sqlalchemy import select
from .db import transaction
from .models import Observation, Health, now
from .schemas import ObservationInput, HealthInput, DictionaryInput, EvidenceInput, InteractionInput
from .services import save_entry, save_dictionary, save_evidence, save_interaction

with transaction(exclusive=True) as db:
    if db.scalar(select(Observation.id).limit(1)) or db.scalar(select(Health.id).limit(1)):
        raise SystemExit('既存の記録があるためサンプルを追加しません。')
    observation=save_entry(db,'observation',ObservationInput(occurred_at=now()-timedelta(hours=2),description='【サンプル】食事前に台所で鳴いた。皿の周りを歩いていた。',location='台所',submission_key=uuid.uuid4()))
    hypothesis=save_dictionary(db,DictionaryInput(kind='hypothesis',title='【サンプル】ごはんの要求かも',content='食事を求めている可能性。まだ予想です。'))
    save_evidence(db,observation.id,EvidenceInput(dictionary_item_id=hypothesis.id,assessment='supports',rationale='【サンプル】ごはんを出すと食べ始めた。'))
    save_interaction(db,observation.id,InteractionInput(owner_action='【サンプル】ごはんを出した',cat_response='【サンプル】食べ始めて鳴きやんだ'))
    save_entry(db,'health',HealthInput(occurred_at=now(),event_type='weight',weight_value='4.250',weight_unit='kg',details='【サンプル】架空の体重です。メメの実測値ではありません。',submission_key=uuid.uuid4()))
print('明示したサンプルを追加しました。実データ利用前に画面から削除してください。')
