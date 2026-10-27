"""フォーム送信の保存・削除ルート。"""
import anyio
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import delete, select
from ..db import transaction
from ..media import request_delete
from ..models import Cat, Comment, DictionaryItem, Evidence, Tag, now
from ..schemas import (DictionaryInput, EvidenceInput, GuessesInput, HealthInput,
                       InteractionInput, JST, ObservationEditInput, ObservationInput, RecordId)
from ..services import add_guesses, parent, save_dictionary, save_entry, save_evidence, save_interaction
from ..web import id_validator, read_form, render, safe_jobs

router = APIRouter()

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
