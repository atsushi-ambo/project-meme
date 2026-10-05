import io
import os
import json
import re
import subprocess
import uuid
from datetime import timedelta
from pathlib import Path
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, delete, text
from sqlalchemy.exc import IntegrityError
from app.main import app
from app.config import settings
from app.db import transaction
from app.models import Base, Tag, Observation, Health, Media, DictionaryItem, Interaction, Evidence, now
from app.media import run_jobs, paths
from app.schemas import HealthInput

@pytest.fixture
def client():
    assert os.environ.get('MEME_TEST_MODE') == '1' and settings.media_root == Path('/tmp/meme-test-media'), 'Use python3 scripts/test.py; refusing to clear non-test data'
    settings.media_root.mkdir(parents=True, exist_ok=True)
    (settings.media_root/'staging').mkdir(exist_ok=True)
    (settings.media_root/'deleting-all').unlink(missing_ok=True)
    with transaction(exclusive=True) as db:
        for table in reversed(Base.metadata.sorted_tables): db.execute(delete(table))
    for path in settings.media_root.rglob('*'):
        if path.is_file(): path.unlink()
    with TestClient(app, base_url='http://localhost:8000') as c:
        c.get('/')
        yield c

def csrf(client):
    return re.search(r'name="csrf-token" content="([^"]+)"',client.get('/').text)[1]

def post(client,url,data):
    return client.post(url,data={'csrf':csrf(client),**data},headers={'Origin':'http://localhost:8000'},follow_redirects=False)

def observation(client,description='食事前に鳴いた',key=None):
    with transaction() as db: tag_id=db.scalar(select(Tag.id).limit(1))
    response=post(client,'/entries/observation/save',dict(occurred_at='2026-10-04T12:00',description=description,location='台所',submission_key=key or str(uuid.uuid4()),tags=[str(tag_id)]))
    assert response.status_code==303,response.text
    return int(response.headers['location'].split('/')[-1])

def upload(client,id,data,name='photo.png',captured_at=None):
    token=csrf(client)
    response=client.post('/api/media/reserve',json=dict(parent_kind='observation',parent_id=id,original_name=name,size=len(data),captured_at=captured_at,upload_key=str(uuid.uuid4())),headers={'Origin':'http://localhost:8000','X-CSRF-Token':token})
    assert response.status_code==200,response.text
    mid=response.json()['id']
    result=client.put(f'/api/media/{mid}',content=data,headers={'Origin':'http://localhost:8000','X-CSRF-Token':token,'Content-Type':'application/octet-stream'})
    return mid,result

def png():
    buffer=io.BytesIO(); Image.new('RGB',(20,20),(100,150,90)).save(buffer,format='PNG'); return buffer.getvalue()

def test_pages_and_security(client):
    for path in ['/','/observations','/health','/dictionary','/storage','/entries/observation/new','/entries/health/new','/health/print']:
        response=client.get(path); assert response.status_code==200,(path,response.text)
        assert 'frame-ancestors' in response.headers['content-security-policy']
    assert client.get('/',headers={'Host':'evil.example'}).status_code==400
    assert client.post('/profile',data={'name':'bad'},headers={'Origin':'http://localhost:8000'}).status_code==403
    assert client.post('/profile',data={'csrf':csrf(client),'name':'bad'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/profile',data={'csrf':csrf(client),'name':'bad'}).status_code==403
    assert client.post('/api/media/reserve',json={},headers={'Origin':'http://localhost:8000'}).status_code==403

def test_observation_roundtrip_and_idempotency(client):
    key=str(uuid.uuid4()); id=observation(client,'<script>alert(1)</script>',key)
    assert observation(client,'<script>alert(1)</script>',key)==id
    text=client.get(f'/entries/observation/{id}').text
    assert '&lt;script&gt;' in text and '<script>alert' not in text
    assert post(client,f'/entries/observation/{id}/save',dict(occurred_at='2026-10-04T13:00',description='更新',location='',submission_key=str(uuid.uuid4()))).status_code==303
    assert '更新' in client.get(f'/entries/observation/{id}').text
    assert post(client,f'/observations/{id}/comment',{'body':'撮影前の様子'}).status_code==303
    assert '撮影前の様子' in client.get(f'/entries/observation/{id}').text
    assert post(client,'/entries/observation/save',dict(occurred_at='2026-10-04T12:00',description='違う入力',submission_key=key)).status_code==409

def test_dictionary_and_later_response(client):
    id=observation(client)
    result=post(client,'/dictionary/save',dict(kind='hypothesis',title='ごはん要求かも',content='食事を求めている',notes='仮説'))
    did=int(result.headers['location'].split('/')[-1])
    assert post(client,f'/observations/{id}/evidence',dict(dictionary_item_id=did,assessment='supports',rationale='食べた')).status_code==303
    cue=post(client,'/dictionary/save',dict(kind='cue',title='遊ぼう',content='遊ぼうと声をかける',notes=''))
    cid=int(cue.headers['location'].split('/')[-1])
    assert post(client,f'/observations/{id}/interaction',dict(owner_action='声をかけた',cue_item_id=cid)).status_code==303
    with transaction() as db: iid=db.scalar(select(Interaction.id))
    assert post(client,f'/observations/{id}/interaction',dict(id=iid,owner_action='声をかけた',cue_item_id=cid,cat_response='近づいた')).status_code==303
    assert post(client,f'/observations/{id}/interaction',dict(id=iid,owner_action='声をかけた',cat_response='近づいた')).status_code==303
    with transaction() as db: assert db.get(Interaction,iid).cue_item_id==cid
    assert '近づいた' in client.get(f'/dictionary/{cid}').text
    assert 'メメのその後' in client.get(f'/dictionary/{cid}').text
    assert post(client,f'/observations/{id}/evidence',dict(dictionary_item_id=cid,assessment='supports')).status_code==422
    assert post(client,f'/dictionary/{cid}/delete',dict(confirm='削除')).status_code==303
    with transaction() as db:
        assert db.get(Interaction,iid).cue_item_id is None
        assert db.get(Interaction,iid).cat_response=='近づいた'

def test_health_csv_and_invalid_input(client):
    values=dict(occurred_at='2026-10-04T12:00',event_type='weight',weight_value='4.250',weight_unit='kg',details='=HYPERLINK("bad")\n二行目',submission_key=str(uuid.uuid4()))
    assert post(client,'/entries/health/save',values).status_code==303
    response=client.get('/health.csv?start=2026-10-04&end=2026-10-04')
    assert response.status_code==200 and "'=HYPERLINK" in response.text
    assert '4.250' in response.text
    assert '=HYPERLINK' not in client.get('/health.csv?start=2026-10-05').text
    values['weight_value']='-1';values['submission_key']=str(uuid.uuid4())
    response=post(client,'/entries/health/save',values)
    assert response.status_code==422 and 'まだ保存されていません' in response.text
    with transaction() as db:
        row=db.scalar(select(Health)); assert row.occurred_at.hour==3
    with pytest.raises(IntegrityError):
        with transaction() as db:
            db.execute(text("UPDATE health_entries SET weight_value=-1"))

def test_file_validation_expiry_and_keep(client):
    id=observation(client)
    mid,response=upload(client,id,png())
    assert response.status_code==200,response.text
    assert client.get(f'/media/{mid}').status_code==200
    with transaction() as db:
        row=db.get(Media,mid); assert timedelta(days=59)<row.expires_at-row.retention_started_at<timedelta(days=61)
        original_expiry=row.expires_at
    assert post(client,f'/media/{mid}/keep',{}).status_code==303
    run_jobs(original_expiry+timedelta(days=1))
    assert client.get(f'/media/{mid}').status_code==200
    assert post(client,f'/media/{mid}/keep',{}).status_code==303
    run_jobs(original_expiry+timedelta(days=1))
    assert client.get(f'/media/{mid}').status_code==404
    with transaction() as db:
        row=db.get(Media,mid); assert row.status=='deleted' and row.reserved_bytes==0
        assert db.get(Observation,id)
    bad,result=upload(client,id,b'<html><script>bad</script></html>','bad.mp4')
    assert result.status_code==415
    run_jobs()
    with transaction() as db: assert db.get(Media,bad).status=='failed'

def test_quota_path_and_parent_deletion(client,monkeypatch):
    id=observation(client)
    token=csrf(client); headers={'Origin':'http://localhost:8000','X-CSRF-Token':token}
    payload=dict(parent_kind='observation',parent_id=id,original_name='../bad.mp4',size=10,upload_key=str(uuid.uuid4()))
    assert client.post('/api/media/reserve',json=payload,headers=headers).status_code==422
    payload['original_name']='test.mp4';payload['size']=settings.max_file_bytes+1
    assert client.post('/api/media/reserve',json=payload,headers=headers).status_code==413
    mid,result=upload(client,id,png());assert result.status_code==200
    assert post(client,f'/entries/observation/{id}/delete',{'confirm':'削除'}).status_code==303
    assert client.get(f'/entries/observation/{id}').status_code==404
    with transaction() as db: assert db.get(Media,mid) is None

def test_export_and_all_delete(client):
    import zipfile
    id=observation(client); mid,response=upload(client,id,png()); assert response.status_code==200
    response=post(client,'/export',{'confirm':'出力'}); assert response.status_code==200,response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        data=json.loads(archive.read('records.json')); assert data['format_version']==1
        assert len(data['tables']['observations'])==1
        assert len([n for n in archive.namelist() if n.startswith('media/')])==1
    assert post(client,'/delete-all',{'confirm':'メメの全記録を削除'}).status_code==303
    assert not (settings.media_root/'deleting-all').exists()
    with transaction() as db: assert db.scalar(select(Observation)) is None

def test_generated_hevc(client,tmp_path):
    file=tmp_path/'sample.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=green:s=128x96:r=120','-t','0.2','-c:v','libx265','-x265-params','pools=1:log-level=error','-tag:v','hvc1','-metadata','creation_time=2026-01-02T03:04:05Z',str(file)],check=True,capture_output=True)
    id=observation(client);mid,response=upload(client,id,file.read_bytes(),'sample.mp4')
    assert response.status_code==200,response.text
    with transaction() as db:
        row=db.get(Media,mid)
        assert row.captured_at.isoformat() == '2026-01-02T03:04:05+00:00'
        assert row.captured_at_source == 'metadata'
    manual,response=upload(client,id,file.read_bytes(),'manual.mp4','2026-01-03T12:34')
    assert response.status_code == 200
    with transaction() as db:
        row=db.get(Media,manual)
        assert row.captured_at.isoformat() == '2026-01-03T03:34:00+00:00'
        assert row.captured_at_source == 'user'
    result=client.get(f'/media/{mid}',headers={'Range':'bytes=0-99'})
    assert result.status_code==206 and len(result.content)==100

def test_delete_failure_retry_and_stale_upload(client, monkeypatch):
    id=observation(client);mid,result=upload(client,id,png());assert result.status_code==200
    assert post(client,f'/media/{mid}/delete',{}).status_code==303
    real_unlink=Path.unlink
    def fail(path,*args,**kwargs):
        if path.parent==settings.media_root: raise PermissionError('simulated')
        return real_unlink(path,*args,**kwargs)
    monkeypatch.setattr(Path,'unlink',fail)
    run_jobs()
    with transaction() as db:
        row=db.get(Media,mid);assert row.status=='delete_pending' and row.retry_count==1 and row.reserved_bytes>0
        retry=row.next_retry_at
    monkeypatch.setattr(Path,'unlink',real_unlink)
    run_jobs(retry+timedelta(seconds=1))
    with transaction() as db: assert db.get(Media,mid).status=='deleted'
    token=csrf(client)
    response=client.post('/api/media/reserve',json=dict(parent_kind='observation',parent_id=id,original_name='missing.mp4',size=100,upload_key=str(uuid.uuid4())),headers={'Origin':'http://localhost:8000','X-CSRF-Token':token})
    assert response.status_code==200
    missing=response.json()['id']
    run_jobs(now()+timedelta(minutes=16))
    with transaction() as db: assert db.get(Media,missing).reserved_bytes==0

def test_backup_lock_and_body_limit(client):
    from app.db import transaction
    with transaction(exclusive=True):
        response=post_without_get(client,'/profile',dict(name='変更',notes=''))
        assert response.status_code==409
    token=csrf(client)
    result=client.post('/profile',content=('csrf='+token+'&name='+('a'*70000)),headers={'Origin':'http://localhost:8000','Content-Type':'application/x-www-form-urlencoded'})
    assert result.status_code in {400,413}

def post_without_get(client,url,data):
    from app.security import read_token, COOKIE
    token=read_token(client.cookies.get(COOKIE))
    return client.post(url,data={'csrf':token,**data},headers={'Origin':'http://localhost:8000'},follow_redirects=False)


def test_application_role_cannot_create_tables(client):
    from sqlalchemy.exc import ProgrammingError
    with pytest.raises(ProgrammingError):
        with transaction() as db: db.execute(text('CREATE TABLE forbidden_table (id int)'))


@pytest.mark.parametrize('variant,accepted', [
    ('alternate', True), ('unknown', False), ('no_fallback', False),
    ('default_apac', False), ('audio_only', False),
])
def test_iphone_alternate_audio(tmp_path, monkeypatch, variant, accepted):
    from app.media import inspect_file
    streams = [
        {'codec_type': 'video', 'codec_name': 'hevc'},
        {'codec_type': 'audio', 'codec_name': 'aac', 'disposition': {'default': 1}},
        {'codec_type': 'audio', 'codec_tag_string': 'apac', 'disposition': {'default': 0}},
        {'codec_type': 'data', 'codec_tag_string': 'mebx'},
    ]
    if variant == 'unknown': streams[2]['codec_tag_string'] = 'xxxx'
    if variant == 'default_apac': streams[2]['disposition']['default'] = 1
    if variant == 'no_fallback': streams.pop(1)
    if variant == 'audio_only': streams.pop(0)
    result = subprocess.CompletedProcess([], 0, stdout=json.dumps({
        'streams': streams, 'format': {'format_name': 'mov,mp4,m4a,3gp,3g2,mj2'}
    }).encode())
    monkeypatch.setattr(subprocess, 'run', lambda *args, **kwargs: result)
    if accepted:
        assert inspect_file(tmp_path/'video', 'iphone.MOV') == 'video/quicktime'
    else:
        with pytest.raises(HTTPException) as error:
            inspect_file(tmp_path/'video', 'iphone.MOV')
        assert error.value.status_code == 415


@pytest.mark.parametrize('tags,expected', [
    ({'com.apple.quicktime.creationdate':'2026-01-02T12:34:56+0900', 'creation_time':'2026-01-02T03:35:00Z'}, '2026-01-02T03:34:56+00:00'),
    ({'creation_time':'2026-01-02T03:34:56Z'}, '2026-01-02T03:34:56+00:00'),
    ({'creation_time':'2026-01-02T12:34:56'}, None),
    ({'creation_time':'1904-01-01T00:00:00Z'}, None),
    ({'creation_time':'9999-01-01T00:00:00Z'}, None),
    ({'creation_time':'invalid'}, None), ({}, None),
])
def test_capture_metadata(tmp_path,monkeypatch,tags,expected):
    from app.media_metadata import video_capture_time
    result=subprocess.CompletedProcess([],0,stdout=json.dumps({'format':{'tags':tags}}))
    monkeypatch.setattr(subprocess,'run',lambda *a,**kw:result)
    value=video_capture_time(tmp_path/'video','sample.MOV')
    assert (value.isoformat() if value else None)==expected


def test_capture_metadata_failure(tmp_path,monkeypatch):
    from app.media_metadata import video_capture_time
    def timeout(*args,**kwargs): raise subprocess.TimeoutExpired('ffprobe',15)
    monkeypatch.setattr(subprocess,'run',timeout)
    assert video_capture_time(tmp_path/'video','sample.mov') is None


def test_capture_time_edit_security(client):
    id=observation(client);mid,result=upload(client,id,png())
    assert result.status_code==200
    url=f'/media/{mid}/captured-at'
    assert client.post(url,data={'captured_at':'2026-01-01T12:00'},headers={'Origin':'http://localhost:8000'}).status_code==403
    assert post(client,url,{'captured_at':'invalid'}).status_code==422
    assert post(client,url,{'captured_at':'2026-01-01T12:00','status':'ready'}).status_code==422
    assert post(client,url,{'captured_at':'2026-01-01T12:00'}).status_code==303
    with transaction() as db:
        row=db.get(Media,mid)
        assert row.captured_at.hour==3 and row.captured_at_source=='user'
        assert db.get(Observation,id).occurred_at.hour==3
    assert post(client,url,{'captured_at':''}).status_code==303
    with transaction() as db:
        row=db.get(Media,mid);assert row.captured_at is None and row.captured_at_source=='unknown'
    assert post(client,f'/media/{mid}/delete',{}).status_code==303
    assert post(client,url,{'captured_at':'2026-01-01T12:00'}).status_code==409


def test_simple_observation_followup_and_guesses(client):
    id=observation(client)
    response=client.get(f'/entries/observation/{id}')
    assert response.status_code==200
    assert '何を伝えたかったと思う？（任意）' in response.text
    assert '日時や使った合図' not in response.text
    assert 'あとから思い出したことを足す' not in response.text
    assert '家のどこ？（任意）' in client.get('/entries/observation/new').text
    assert post(client,f'/observations/{id}/interaction',{'owner_action':'','cat_response':'廊下へ走っていった'}).status_code==303
    assert post(client,f'/observations/{id}/interaction',{'owner_action':'','cat_response':''}).status_code==422
    url=f'/observations/{id}/guesses'
    assert client.post(url,data={'guesses':['preset:play']},headers={'Origin':'http://localhost:8000'}).status_code==403
    assert post(client,url,{'guesses':['preset:play','preset:attention']}).status_code==303
    assert post(client,url,{'guesses':['preset:play','preset:play']}).status_code==303
    with transaction() as db:
        items=list(db.scalars(select(DictionaryItem)))
        assert len(items)==2
        links=list(db.scalars(select(Evidence)))
        assert len(links)==2 and all(e.assessment=='unknown' for e in links)
        links[0].rationale='以前の根拠';links[0].assessment='supports'
        existing_id=links[0].dictionary_item_id
    assert post(client,url,{'guesses':[f'item:{existing_id}']}).status_code==303
    with transaction() as db:
        e=db.get(Evidence,(existing_id,id));assert e.rationale=='以前の根拠' and e.assessment=='supports'
    assert post(client,url,{'guesses':['preset:food','preset:invalid']}).status_code==422
    assert post(client,url,{'guesses':['item:99999999999999999999999']}).status_code==422
    with transaction() as db: assert len(list(db.scalars(select(DictionaryItem))))==2
    response=client.get(f'/entries/observation/{id}')
    assert response.status_code==200 and '以前の根拠' in response.text and '合っていそう' in response.text
    assert '以前の評価' not in response.text
    assert 'value="preset:play"' not in response.text
    bad,result=upload(client,id,b'not a video','bad.mov');assert result.status_code==415
    run_jobs()
    assert 'bad.mov' not in client.get(f'/entries/observation/{id}').text
    assert 'bad.mov' in client.get('/storage').text
    assert post(client,f'/entries/observation/{id}/delete',{'confirm':'削除'}).status_code==303
    assert post(client,url,{'guesses':['preset:food']}).status_code==404


def test_profile_photo_lifecycle(client):
    import zipfile
    token=csrf(client)
    headers={'Origin':'http://localhost:8000','X-CSRF-Token':token}
    assert client.put('/api/profile-photo',content=png(),headers={'Origin':'http://localhost:8000'}).status_code==403
    assert client.put('/api/profile-photo',content=png(),headers=headers).status_code==200
    assert 'src="/profile-photo"' in client.get('/').text
    assert client.get('/profile-icon').headers['content-type']=='image/png'
    with Image.open(io.BytesIO(client.get('/profile-photo').content)) as photo:
        assert photo.format=='JPEG' and not photo.getexif()
    saved=client.get('/profile-photo').content
    assert client.put('/api/profile-photo',content=b'<svg></svg>',headers=headers).status_code==415
    assert client.get('/profile-photo').content==saved
    result=post(client,'/export',{'confirm':'出力'})
    with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
        assert archive.read('media/profile.jpg')==saved
    assert post(client,'/profile-photo/delete',{}).status_code==303
    assert client.get('/profile-photo').status_code==404
    assert client.put('/api/profile-photo',content=png(),headers=headers).status_code==200
    assert post(client,'/delete-all',{'confirm':'メメの全記録を削除'}).status_code==303
    assert client.get('/profile-icon').status_code==404



def test_one_memo_ui_preserves_legacy_notes(client):
    id=observation(client,description='おもちゃを持ってきた。立つと走っていった。')
    assert post(client,f'/observations/{id}/comment',{'body':'保存済みの補足'}).status_code==303
    assert post(client,f'/observations/{id}/interaction',{'owner_action':'椅子から立った','cat_response':'廊下へ走った'}).status_code==303
    detail=client.get(f'/entries/observation/{id}').text
    assert '保存済みの補足' in detail and '廊下へ走った' in detail
    assert f'action="/observations/{id}/comment"' not in detail
    assert f'action="/observations/{id}/interaction"' not in detail
    assert 'name="owner_action"' not in detail and 'name="cat_response"' not in detail
    assert '使った合図' not in detail and 'その後を残す' not in detail
    form=client.get(f'/entries/observation/{id}/edit').text
    assert form.count('<textarea')==1
    assert '記入例（保存されません）' in form
    assert 'おもちゃを持ってきた。立つと走っていった。</textarea>' in form
    assert post(client,f'/entries/observation/{id}/save',dict(occurred_at='2026-10-04T12:00',description='まとめたメモ',location='',submission_key=str(uuid.uuid4()))).status_code==303
    detail=client.get(f'/entries/observation/{id}').text
    assert 'まとめたメモ' in detail and '保存済みの補足' in detail and '廊下へ走った' in detail



def test_response_only_creation_and_partial_update(client):
    from datetime import datetime, timezone
    id=observation(client)
    url=f'/observations/{id}/interaction'
    assert post(client,url,{'cat_response':'廊下へ走った'}).status_code==303
    with transaction() as db:
        row=db.scalar(select(Interaction))
        assert row.owner_action=='' and row.cat_response=='廊下へ走った'
        iid=row.id
    assert post(client,url,dict(id=iid,owner_action='立ち上がった',acted_at='2026-01-02T12:00',responded_at='2026-01-02T12:05')).status_code==303
    assert post(client,url,dict(id=iid,cat_response='こちらを見た')).status_code==303
    with transaction() as db:
        row=db.get(Interaction,iid)
        assert row.owner_action=='立ち上がった'
        assert row.acted_at==datetime(2026,1,2,3,tzinfo=timezone.utc)
        assert row.responded_at==datetime(2026,1,2,3,5,tzinfo=timezone.utc)
    assert post(client,url,dict(id=iid,cat_response='不正な更新',responded_at='2026-01-02T11:59')).status_code==422
    with transaction() as db:
        row=db.get(Interaction,iid)
        assert row.cat_response=='こちらを見た' and row.responded_at.minute==5



def test_search_in_legacy_comment_escapes_wildcards(client):
    first=observation(client,description='本文には探す語がない')
    second=observation(client,description='別の記録')
    assert post(client,f'/observations/{first}/comment',{'body':'補足の literal%_needle'}).status_code==303
    assert post(client,f'/observations/{second}/comment',{'body':'補足の literalXXneedle'}).status_code==303
    response=client.get('/observations',params={'q':'literal%_needle'})
    assert response.status_code==200
    assert f'href="/entries/observation/{first}"' in response.text
    assert f'href="/entries/observation/{second}"' not in response.text



@pytest.mark.parametrize('state', ['failed', 'deleted'])
def test_retry_reservation_after_cleanup(client, state):
    id=observation(client)
    headers={'Origin':'http://localhost:8000','X-CSRF-Token':csrf(client)}
    data=dict(parent_kind='observation',parent_id=id,original_name='retry.png',size=len(png()),upload_key=str(uuid.uuid4()))
    first=client.post('/api/media/reserve',json=data,headers=headers)
    mid=first.json()['id']
    if state=='deleted':
        assert client.put(f'/api/media/{mid}',content=png(),headers=headers).status_code==200
        assert post(client,f'/media/{mid}/delete',{}).status_code==303
        run_jobs()
    else:
        run_jobs(now()+timedelta(minutes=16))
    retry=client.post('/api/media/reserve',json=data,headers=headers)
    assert retry.status_code==200 and retry.json()['status']==state
    assert client.post('/api/media/reserve',json={**data,'original_name':'other.png'},headers=headers).status_code==409
    fresh=client.post('/api/media/reserve',json={**data,'upload_key':str(uuid.uuid4())},headers=headers)
    assert fresh.status_code==200 and fresh.json()['status']=='pending'
    assert client.put('/api/media/'+str(fresh.json()['id']),content=png(),headers=headers).status_code==200


def test_resume_interrupted_upload_and_reject_concurrent_upload(client):
    id=observation(client)
    headers={'Origin':'http://localhost:8000','X-CSRF-Token':csrf(client)}
    data=dict(parent_kind='observation',parent_id=id,original_name='retry.png',size=len(png()),upload_key=str(uuid.uuid4()))
    response=client.post('/api/media/reserve',json=data,headers=headers)
    mid=response.json()['id']
    assert client.post('/api/media/reserve',json={**data,'size':len(png())+1},headers=headers).status_code==409
    with transaction() as db:
        row=db.get(Media,mid,with_for_update=True)
        final,temp=paths(row.storage_key)
        temp.parent.mkdir(exist_ok=True)
        temp.write_bytes(b'partial')
        assert client.put(f'/api/media/{mid}',content=png(),headers=headers).status_code==409
        assert temp.read_bytes()==b'partial'
    assert client.put(f'/api/media/{mid}',content=png(),headers=headers).status_code==200
    assert final.read_bytes()==png() and not temp.exists()
    repeated=client.post('/api/media/reserve',json=data,headers=headers)
    assert repeated.json()=={'id':mid,'status':'ready'}


def test_api_rejections_are_readable_json(client):
    response=client.put('/api/profile-photo',content=png(),headers={'Origin':'http://localhost:8000'})
    assert response.status_code==403
    assert response.json()['detail']=='送信を確認できません。'
    marker=settings.media_root/'deleting-all'
    marker.write_text('pending')
    try:
        response=client.post('/api/media/reserve',json={},headers={'Origin':'http://localhost:8000','X-CSRF-Token':csrf(client)})
        assert response.status_code==409 and '全削除の処理中' in response.json()['detail']
    finally:
        marker.unlink(missing_ok=True)
