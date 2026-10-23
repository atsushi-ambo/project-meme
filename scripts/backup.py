"""MacBookで実行。アプリ停止中にPostgreSQLダンプとメディアを一組で保存。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]

def run(args, **kw):
    return subprocess.run(['docker','compose',*args], cwd=ROOT, check=True, **kw)

def env_values():
    return dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if line and not line.startswith('#'))

def backup(destination, *, restart=True, runner=None):
    run = runner or globals()["run"]
    values=env_values(); media=Path(values['MEDIA_DIR']).resolve()
    destination = destination.resolve()
    if destination.is_relative_to(media) or destination.is_relative_to(ROOT.resolve()):
        raise ValueError('バックアップはメディア・リポジトリの外へ保存してください。')
    destination.mkdir(parents=True,exist_ok=False)
    destination.chmod(0o700)
    running = bool(run(['ps','--status','running','-q','web'],capture_output=True,text=True).stdout.strip())
    try:
        run(['stop','web'])
        # 回復処理後のDBとファイルを一貫した組として取得する。
        run(['run','--rm','--no-deps','web','python','-c','from app.media import run_jobs; run_jobs()'])
        with (destination/'database.dump').open('wb') as f:
            run(['exec','-T','db','pg_dump','-U','meme_owner','-d','meme','-Fc'],stdout=f)
        with tarfile.open(destination/'media.tar','w') as archive:
            archive.add(media,arcname='media')
        (destination/'manifest.json').write_text(json.dumps({'format_version':1,'created_at':datetime.now().isoformat(),'complete':True}))
        print(f'バックアップ完了: {destination}')
    finally:
        if running and restart: run(['start','web'])

def restore(source, project, destination):
    if not project.startswith('meme-restore-') or not all(c.isalnum() or c=='-' for c in project):
        raise SystemExit('復元先プロジェクト名は meme-restore- から始めてください。')
    if not json.loads((source/'manifest.json').read_text()).get('complete'):
        raise SystemExit('完了したバックアップではありません。')
    existing=subprocess.run(['docker','ps','-aq','--filter',f'label=com.docker.compose.project={project}'],check=True,capture_output=True,text=True).stdout.strip()
    volume=subprocess.run(['docker','volume','ls','-q','--filter',f'label=com.docker.compose.project={project}'],check=True,capture_output=True,text=True).stdout.strip()
    if existing or volume or destination.exists():
        raise SystemExit('復元先が既に存在します。新しい名前と保存先を指定してください。')
    destination.mkdir(parents=True); destination.chmod(0o700)
    with tarfile.open(source/'media.tar') as archive:
        for member in archive.getmembers():
            target=(destination/member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or not (member.isfile() or member.isdir()):
                raise SystemExit('安全でないアーカイブです。')
        for member in archive.getmembers():
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as src, target.open('xb') as dst:
                    shutil.copyfileobj(src, dst)
    # 再現用の環境ファイル。元の環境は変更しない。
    values=env_values(); values['MEDIA_DIR']=str((destination/'media').resolve());values['PORT']='8002'
    envfile=destination/'restore.env'
    with envfile.open('x') as f:
        envfile.chmod(0o600); f.write(''.join(f'{k}={v}\n' for k,v in values.items()))
    args=['--env-file',str(envfile),'-p',project]
    run([*args,'up','-d','--wait','db'])
    with (source/'database.dump').open('rb') as f:
        run([*args,'exec','-T','db','pg_restore','-U','meme_owner','-d','meme','--exit-on-error','--no-owner'],stdin=f)
    run([*args,'up','-d','web'])
    print('復元完了。http://localhost:8002 で確認できます。元の環境は変更していません。')

if __name__=='__main__':
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest='command',required=True)
    b=sub.add_parser('backup');b.add_argument('--destination',type=Path,default=Path.home()/'Library/Application Support/project-meme/backups'/datetime.now().strftime('%Y%m%d-%H%M%S'))
    r=sub.add_parser('restore');r.add_argument('source',type=Path);r.add_argument('--project',required=True);r.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='backup': backup(args.destination)
    else: restore(args.source,args.project,args.destination)
