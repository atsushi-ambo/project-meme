"""フォーム送信の保存・削除ルート。"""
import anyio
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import delete, select
from ..db import transaction
from ..media import request_delete
from ..models import CallType, Cat, Comment, DictionaryItem, Evidence, Media, SoundAnalysis, Tag, now
from ..sound_analysis import create as create_sound_analysis, remeasure, update_labels
from ..voice_match import LABEL_LIMIT
from ..schemas import (CallTypeInput, DictionaryInput, EvidenceInput, GuessesInput, HealthInput,
                       InteractionInput, JST, ObservationEditInput, ObservationInput, RecordId)
from ..services import add_guesses, parent, save_dictionary, save_entry, save_evidence, save_interaction
from ..web import id_validator, read_form, render, safe_jobs

router = APIRouter()

def sound_form_labels(raw):
    # フォームに表示していた評価を、別の予想へ引き継がない。
    if 'assessment_for' in raw and raw.pop('assessment_for') != raw.get('meaning', ''):
        raw['assessment'] = 'unknown'
    return raw


def sound_draft_choices(raw):
    """競合画面で、文章以外の未保存の選択も読める形で示す。辞書へは何も追加しない。"""
    from ..services import ASSESSMENTS, BEHAVIORS, GUESS_PRESETS, OUTCOMES
    def pick(value):
        return value if isinstance(value, str) else ''
    call_type, meaning = pick(raw.get('call_type_id')), pick(raw.get('meaning'))
    prefix, _, value = meaning.partition(':')
    # 保守ロック中などで名前を引けなくても、送信した値のまま下書きを残す。
    choices = [('鳴き声の種類', f'ID {call_type}' if call_type else '選ばない'),
               ('意味の予想', GUESS_PRESETS.get(value, meaning) if prefix == 'preset' else meaning or 'まだ選ばない')]
    try:
        with transaction() as db:
            name = db.get(CallType, int(call_type)) if call_type.isdecimal() and len(call_type) <= 18 else None
            item = db.get(DictionaryItem, int(value)) if prefix == 'item' and value.isdecimal() and len(value) <= 18 else None
            if name:
                choices[0] = ('鳴き声の種類', name.name)
            if item:
                choices[1] = ('意味の予想', item.title)
    except Exception:
        pass
    behaviors = raw.get('behaviors') if isinstance(raw.get('behaviors'), list) else []
    choices += [('この区間で鳴いた回数', pick(raw.get('call_count')) or '未入力'),
                ('そのときの行動', '、'.join(BEHAVIORS.get(b, b) for b in behaviors) or '未入力'),
                ('その予想とこの区間の関係', ASSESSMENTS.get(pick(raw.get('assessment')), 'まだ分からない')),
                ('そのあとのメメ', OUTCOMES.get(pick(raw.get('outcome')), '未入力'))]
    return choices


@router.post('/observations/{id}/sound/{media_id}')
async def sound_analyse(request: Request, id: RecordId, media_id: RecordId):
    raw = sound_form_labels(await read_form(request))
    def process():
        with transaction() as db:
            create_sound_analysis(db, id, media_id, raw)
        return RedirectResponse(f'/entries/observation/{id}#sound-analysis', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/observations/{id}/sound-result/{result_id}/labels')
async def sound_labels(request: Request, id: RecordId, result_id: RecordId):
    raw = sound_form_labels(await read_form(request))
    def process():
        try:
            with transaction() as db:
                update_labels(db, id, result_id, raw)
        except HTTPException as error:
            if error.status_code != 409:
                raise
            # 入力した文章と選択をコピーできるよう残す。最新の版番号で自動再送しない。
            response = render(request, 'error.html', message=str(error.detail),
                sound_draft=raw, sound_choices=sound_draft_choices(raw), sound_url=f'/entries/observation/{id}#sound-{result_id}')
            response.status_code = 409
            return response
        return RedirectResponse(f'/entries/observation/{id}#sound-{result_id}', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/observations/{id}/sound-result/{result_id}/measure')
async def sound_remeasure(request: Request, id: RecordId, result_id: RecordId):
    await read_form(request)
    def process():
        with transaction() as db:
            remeasure(db, id, result_id)
        return RedirectResponse(f'/entries/observation/{id}#sound-{result_id}', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/call-types/save')
@router.post('/call-types/{id}/save')
async def call_type_save(request: Request, id: RecordId | None = None):
    raw = await read_form(request)
    try:
        data = CallTypeInput.model_validate(raw)
    except ValidationError:
        raise HTTPException(422, '鳴き声の種類の名前（40文字以内）と説明（500文字以内）を確認してください。')
    def process():
        with transaction() as db:
            row = db.scalar(select(CallType).where(CallType.id == id).with_for_update()) if id else CallType()
            if row is None:
                raise HTTPException(404, '鳴き声の種類が見つかりません。')
            duplicate = db.scalar(select(CallType.id).where(CallType.name == data.name, CallType.id != row.id)) \
                if row.id else db.scalar(select(CallType.id).where(CallType.name == data.name))
            if duplicate:
                raise HTTPException(409, '同じ名前の鳴き声の種類があります。')
            row.name, row.description = data.name, data.description
            db.add(row)
        return RedirectResponse('/voices#call-types', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/voices/groups/label')
async def voice_group_label(request: Request):
    """飼い主が確認したグループの、種類未選択の区間にだけ種類を付ける。上書きはしない。"""
    raw = await read_form(request)
    try:
        targets = []
        for value in raw.get('sound_ids', []):
            sound_id, _, revision = str(value).partition(':')
            targets.append((id_validator.validate_python(sound_id), id_validator.validate_python(revision)))
        ids = [sound_id for sound_id, _ in targets]
        new_name = str(raw.get('new_call_type', '')).strip()
        chosen = id_validator.validate_python(raw['call_type_id']) if raw.get('call_type_id') else None
        if new_name:
            CallTypeInput.model_validate({'name': new_name})
    except (ValidationError, KeyError):
        raise HTTPException(422, '区間と鳴き声の種類を確認してください。')
    if not 1 <= len(ids) <= LABEL_LIMIT or len(set(ids)) != len(ids) or (chosen is None) == (not new_name):
        raise HTTPException(422, '種類を一つ選ぶか、新しい名前を入力してください。')
    def process():
        with transaction() as db:
            # 種類の新規作成と区間の更新を、ほかの分類の保存と同じ順で直列化する。
            db.scalar(select(Cat).where(Cat.id == 1).with_for_update())
            type_id = chosen
            if type_id is not None and db.get(CallType, type_id) is None:
                raise HTTPException(422, '鳴き声の種類を選び直してください。')
            if new_name:
                existing = db.scalar(select(CallType.id).where(CallType.name == new_name))
                if existing is None:
                    created = CallType(name=new_name, description='')
                    db.add(created)
                    db.flush()
                    existing = created.id
                type_id = existing
            labelled = 0
            for sound_id, revision in targets:
                found = db.execute(select(SoundAnalysis.call_type_id, Media.observation_id).join(Media)
                                   .where(SoundAnalysis.id == sound_id)).first()
                if found is None or found.observation_id is None:
                    raise HTTPException(404, '声の区間が見つかりません。')
                if found.call_type_id is None:
                    update_labels(db, found.observation_id, sound_id,
                                  {'call_type_id': type_id, 'expected_revision': revision}, merge=True)
                    labelled += 1
        return RedirectResponse(f'/voices/groups?labelled={labelled}', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/observations/{id}/sound-result/{result_id}/delete')
async def sound_delete(request: Request, id: RecordId, result_id: RecordId):
    await read_form(request)
    def process():
        with transaction() as db:
            parent(db, 'observation', id, True)
            result = db.scalar(select(SoundAnalysis).join(Media).where(
                SoundAnalysis.id == result_id, Media.observation_id == id).with_for_update())
            if not result: raise HTTPException(404)
            db.delete(result)
        return RedirectResponse(f'/entries/observation/{id}#sound-analysis', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/entries/{kind}/save')
@router.post('/entries/{kind}/{id}/save')
async def entry_save(request: Request, kind:str,id:RecordId|None=None):
    if kind not in {'observation','health'}: raise HTTPException(404)
    raw=await read_form(request)
    def invalid_response(errors, status):
        with transaction() as db:
            response = render(request, 'form.html', kind=kind, id=id, values=raw,
                tags=list(db.scalars(select(Tag))), errors=errors)
            response.status_code = status
            return response
    def process():
        payload = dict(raw)
        expected_revision = None
        try:
            if id and kind == 'observation':
                expected_revision = ObservationEditInput.model_validate({
                    'expected_revision': payload.pop('expected_revision', None)}).expected_revision
            data = (ObservationInput if kind == 'observation' else HealthInput).model_validate(payload)
        except ValidationError as error:
            return invalid_response([f"{'.'.join(map(str,e['loc']))}: {e['msg']}" for e in error.errors()], 422)
        try:
            with transaction() as db:
                row = save_entry(db, kind, data, id, expected_revision=expected_revision)
                return RedirectResponse(f'/entries/{kind}/{row.id}', 303)
        except HTTPException as error:
            if id and kind == 'observation' and error.status_code == 409:
                return invalid_response([str(error.detail)], 409)
            raise
    return await anyio.to_thread.run_sync(process)

@router.post('/entries/{kind}/{id}/delete')
async def entry_delete(request:Request,kind:str,id:RecordId):
    raw=await read_form(request)
    if raw.get('confirm')!='削除': raise HTTPException(422,'「削除」と入力してください。')
    def process():
        with transaction() as db: request_delete(db,kind,id)
        safe_jobs()
        return RedirectResponse('/storage',303)
    return await anyio.to_thread.run_sync(process)

@router.post('/observations/{id}/guesses')
async def observation_guesses(request: Request, id: RecordId):
    raw = await read_form(request)
    try:
        data = GuessesInput.model_validate(raw)
    except ValidationError:
        raise HTTPException(422, '追加する予想を選んでください。')
    def process():
        with transaction() as db:
            add_guesses(db, id, data.guesses)
        return RedirectResponse(f'/entries/observation/{id}', 303)
    return await anyio.to_thread.run_sync(process)

@router.post('/observations/{id}/{action}')
async def observation_action(request:Request,id:RecordId,action:str):
    raw=await read_form(request)
    def process():
        with transaction() as db:
            parent(db,'observation',id,True)
            try:
                if action=='comment':
                    body=str(raw.get('body','')).strip()
                    if not 1<=len(body)<=5000: raise HTTPException(422,'追記は1〜5000文字で入力してください。')
                    db.add(Comment(observation_id=id,body=body))
                elif action=='interaction':
                    edit_id=raw.pop('id','')
                    save_interaction(db,id,InteractionInput.model_validate(raw),id_validator.validate_python(edit_id) if edit_id else None)
                elif action=='evidence': save_evidence(db,id,EvidenceInput.model_validate(raw))
                elif action=='unlink':
                    db.execute(delete(Evidence).where(Evidence.observation_id==id,Evidence.dictionary_item_id==id_validator.validate_python(raw['dictionary_item_id'])))
                else: raise HTTPException(404)
            except (ValidationError,ValueError,KeyError): raise HTTPException(422,'入力内容と日時を確認してください。戻ると入力を修正できます。')
        return RedirectResponse(f'/entries/observation/{id}',303)
    return await anyio.to_thread.run_sync(process)

@router.post('/dictionary/save')
@router.post('/dictionary/{id}/save')
async def dictionary_save(request:Request,id:RecordId|None=None):
    raw=await read_form(request)
    try: data=DictionaryInput.model_validate(raw)
    except ValidationError: raise HTTPException(422,'予想か合図の種類、タイトル、内容を確認してください。')
    def process():
        with transaction() as db: item=save_dictionary(db,data,id)
        return RedirectResponse(f'/dictionary/{item.id}',303)
    return await anyio.to_thread.run_sync(process)

@router.post('/dictionary/{id}/delete')
async def dictionary_delete(request:Request,id:RecordId):
    raw=await read_form(request)
    if raw.get('confirm')!='削除': raise HTTPException(422,'「削除」と入力してください。')
    def process():
        with transaction() as db:
            row=db.get(DictionaryItem,id,with_for_update=True)
            if row: db.delete(row)
        return RedirectResponse('/dictionary',303)
    return await anyio.to_thread.run_sync(process)

@router.post('/profile')
async def profile(request:Request):
    raw=await read_form(request)
    name=str(raw.get('name','')).strip(); notes=str(raw.get('notes','')).strip()
    if not 1<=len(name)<=80 or len(notes)>3000: raise HTTPException(422,'名前またはメモの長さを確認してください。')
    try:
        birthday = datetime.fromisoformat(raw['birthday']).date() if raw.get('birthday') else None
        if birthday and birthday > now().astimezone(JST).date(): raise ValueError()
    except ValueError: raise HTTPException(422,'誕生日を確認してください。')
    def process():
        with transaction() as db:
            cat=db.get(Cat,1,with_for_update=True); cat.name=name; cat.notes=notes; cat.birthday=birthday
        return RedirectResponse('/storage',303)
    return await anyio.to_thread.run_sync(process)
