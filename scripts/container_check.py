"""使い捨てDocker環境でOS脆弱性を検査。修正版のあるHIGH/CRITICALで失敗する。"""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
TRIVY_IMAGE = 'ghcr.io/aquasecurity/trivy@sha256:af6acf9a6b85dfe389a1941505c0ce9efef52a4719635e1a962f022a3d855daa'
DB_REPOSITORY = 'ghcr.io/aquasecurity/trivy-db:2'
SEVERITIES = {'UNKNOWN', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}
MAX_REPORT_BYTES = 32 * 1024 * 1024
MAX_COMPOSE_BYTES = 64 * 1024


def run(command, *, timeout=300, **kwargs):
    return subprocess.run(command, cwd=ROOT, check=True, timeout=timeout, **kwargs)


def validate_image(image):
    # Dockerのオプションやシェル構文として解釈できない参照だけを受け付ける。
    if not isinstance(image, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}', image):
        raise ValueError('Dockerイメージの参照が不正です。')
    return image



def database_image(compose_path=None, *, with_kind=False):
    # .envを読んだりComposeを起動せず、公開設定の固定参照だけを取り出す。
    path = ROOT / 'compose.yaml' if compose_path is None else Path(compose_path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_COMPOSE_BYTES:
            raise ValueError('compose.yamlは容量上限内の通常ファイルである必要があります。')
        data = source.read(MAX_COMPOSE_BYTES + 1)
    if len(data) > MAX_COMPOSE_BYTES:
        raise ValueError('compose.yamlが容量上限を超えています。')
    text = data.decode('utf-8')
    # 採用している2スペースの構造だけを許可し、曖昧なYAMLは推測しない。
    services = re.findall(r'^services:\r?\n(.*?)(?=^\S|\Z)', text, re.MULTILINE | re.DOTALL)
    if len(services) != 1:
        raise ValueError('compose.yamlのservicesを確認できません。')
    databases = re.findall(r'^  db:\r?\n(.*?)(?=^  \S|\Z)', services[0], re.MULTILINE | re.DOTALL)
    if len(databases) != 1:
        raise ValueError('compose.yamlのdbを確認できません。')
    image_lines = re.findall(r'^    image:(.*)$', databases[0], re.MULTILINE)
    if len(image_lines) != 1:
        raise ValueError('compose.yamlのDBイメージが一意に指定されていません。')
    value = image_lines[0].rstrip('\r')
    if value == ' ${DB_IMAGE:-project-meme-db:local}':
        dockerfile = path.parent / 'Dockerfile.db'
        with os.fdopen(os.open(dockerfile, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_COMPOSE_BYTES:
                raise ValueError('Dockerfile.dbは通常ファイルである必要があります。')
            lines = source.read(MAX_COMPOSE_BYTES + 1).decode('utf-8').splitlines()
        bases = [line[5:] for line in lines if line.startswith('FROM ')]
        if len(bases) != 1 or not re.fullmatch(r'postgres:18-bookworm@sha256:[a-f0-9]{64}', bases[0]):
            raise ValueError('DBのベースイメージはPostgreSQL 18 bookwormの固定参照が必要です。')
        return (bases[0], True) if with_kind else bases[0]
    if not re.fullmatch(r' postgres:18-(?:bookworm|trixie)@sha256:[a-f0-9]{64}', value):
        raise ValueError('DBイメージは公式PostgreSQL18の固定ダイジェストで指定してください。')
    return (value[1:], False) if with_kind else value[1:]


def database_is_fresh(cache):
    metadata = json.loads((cache / 'db' / 'metadata.json').read_text())
    if metadata.get('Version') != 2 or not isinstance(metadata.get('UpdatedAt'), str):
        raise ValueError('脆弱性DBの形式を確認できません。')
    # TrivyのRFC3339はナノ秒を含む。MacBookのPython3.9は6桁までなので切りそろえる。
    value = metadata['UpdatedAt'].replace('Z', '+00:00')
    value = re.sub(r'(\.\d{6})\d+(?=[+-]\d{2}:\d{2}$)', r'\1', value)
    updated = datetime.fromisoformat(value)
    if updated.tzinfo is None:
        raise ValueError('脆弱性DBの更新日時が不正です。')
    age = datetime.now(timezone.utc) - updated
    if age > timedelta(hours=48) or age < timedelta(hours=-1):
        raise ValueError('脆弱性DBが古いか、更新日時が不正です。')
    return updated



def package_version(package):
    version = package.get('Version', '')
    epoch = package.get('Epoch')
    release = package.get('Release')
    if isinstance(epoch, int) and epoch:
        version = str(epoch) + ':' + version
    if isinstance(release, str) and release:
        version += '-' + release
    return version


def summarize(report):
    if (not isinstance(report, dict) or report.get('SchemaVersion') != 2
            or not isinstance(report.get('Results'), list)):
        raise ValueError('検査結果の形式を確認できません。')
    os_info = report.get('Metadata', {}).get('OS', {})
    if not all(isinstance(os_info.get(key), str) and os_info[key] for key in ('Family', 'Name')):
        raise ValueError('検査対象のOSを確認できません。')
    if os_info.get('EOSL') is True:
        raise ValueError('検査対象のOSはサポート期間を過ぎています。')
    results = [item for item in report['Results'] if isinstance(item, dict) and item.get('Class') == 'os-pkgs']
    if not results or any(not isinstance(item.get('Packages'), list) or not item['Packages'] for item in results):
        raise ValueError('OSパッケージを検査できませんでした。')
    findings = []
    for item in results:
        values = item.get('Vulnerabilities', [])
        if not isinstance(values, list):
            raise ValueError('脆弱性の検査結果が不正です。')
        for value in values:
            if not isinstance(value, dict) or value.get('Severity') not in SEVERITIES:
                raise ValueError('脆弱性の重大度を確認できません。')
            for key in ('PkgName', 'VulnerabilityID', 'InstalledVersion'):
                if not isinstance(value.get(key), str) or not value[key]:
                    raise ValueError('脆弱性のパッケージ情報を確認できません。')
            fixed = value.get('FixedVersion', '')
            if not isinstance(fixed, str):
                raise ValueError('修正版の情報を確認できません。')
            findings.append(value)
    counts = Counter(item['Severity'] for item in findings)
    actionable = [item for item in findings if item['Severity'] in {'HIGH', 'CRITICAL'} and item.get('FixedVersion')]
    unfixed = [item for item in findings if item['Severity'] in {'HIGH', 'CRITICAL'} and not item.get('FixedVersion')]
    versions = {package['Name']: package_version(package)
                for item in results for package in item['Packages']
                if isinstance(package, dict) and isinstance(package.get('Name'), str)
                and (package['Name'] in {'ffmpeg', 'libpcre2-8-0', 'perl-base'}
                     or re.fullmatch(r'postgresql-(?:client-)?\d+', package['Name']))}
    print('OS: ' + json.dumps(os_info, ensure_ascii=False))
    if versions:
        print('主要パッケージ: ' + json.dumps(versions, ensure_ascii=False, sort_keys=True))
    print('パッケージ別の検出数: ' + json.dumps(dict(counts), ensure_ascii=False, sort_keys=True))
    print(f'修正版のあるHIGH/CRITICAL: {len(actionable)}件')
    print(f'修正版のないHIGH/CRITICAL: {len(unfixed)}件（件数は重複を含み、攻撃可能性の確定ではありません）')
    for item in sorted(actionable, key=lambda value: (value['Severity'] != 'CRITICAL', value['PkgName'], value['VulnerabilityID'])):
        # 外部DB由来の文字列はJSON表記で出し、制御文字もそのまま表示しない。
        print(json.dumps({key: item[key] for key in ('PkgName', 'VulnerabilityID', 'Severity', 'InstalledVersion', 'FixedVersion')}, ensure_ascii=False))
    return 1 if actionable else 0


def container_args(name, cache, *, offline=False, archive_directory=None):
    args = ['docker', 'run', '--rm', '--name', name, '--user', f'{os.getuid()}:{os.getgid()}',
            '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '2g', '--cpus', '2', '--pids-limit', '128',
            '--tmpfs', '/tmp:rw,noexec,nosuid,size=1g',
            '--mount', f'type=bind,src={cache},dst=/cache']
    if offline:
        args.extend(['--network', 'none'])
    if archive_directory is not None:
        args.extend(['--mount', f'type=bind,src={archive_directory},dst=/input,readonly'])
    args.append(TRIVY_IMAGE)
    return args


def check(image=None, *, database=False):
    suffix = uuid.uuid4().hex
    built_image = None
    containers = []
    result = 2
    with tempfile.TemporaryDirectory(prefix='project-meme-container-check-') as temporary:
        work = Path(temporary)
        work.chmod(0o700)
        cache = work / 'cache'
        cache.mkdir(mode=0o700)
        source = work / 'input'
        source.mkdir(mode=0o700)
        archive = source / 'image.tar'
        try:
            if database:
                if image is not None:
                    raise ValueError('--imageと--databaseは同時に指定できません。')
                image, custom_database = database_image(with_kind=True)
                if custom_database:
                    image = 'project-meme-security-db-image-' + suffix
                    built_image = image
                    # 公式固定イメージを基にOS更新を含む候補を作る。実DBには触れない。
                    run(['docker', 'build', '--pull', '--no-cache', '-f', 'Dockerfile.db', '-t', image, '.'], timeout=900)
                else:
                    run(['docker', 'pull', image], timeout=300)
            if image is None:
                image = 'project-meme-security-' + suffix
                built_image = image
                # Dockerignoreで私用データ・.envを除外した通常のビルドだけを使う。
                run(['docker', 'build', '--pull', '--no-cache', '-t', image, '.'], timeout=900)
            else:
                validate_image(image)
            run(['docker', 'image', 'save', '--output', str(archive), image], timeout=180)
            run(['docker', 'pull', TRIVY_IMAGE], timeout=180)
            db_name = 'project-meme-security-db-' + suffix
            containers.append(db_name)
            run(container_args(db_name, cache) + ['image', '--download-db-only', '--no-progress',
                '--cache-dir', '/cache', '--db-repository', DB_REPOSITORY], timeout=300)
            updated = database_is_fresh(cache)
            print('脆弱性DB更新日時: ' + updated.isoformat(), flush=True)
            scan_name = 'project-meme-security-scan-' + suffix
            containers.append(scan_name)
            with (work / 'report.json').open('wb') as output:
                run(container_args(scan_name, cache, offline=True, archive_directory=source) + [
                    'image', '--input', '/input/image.tar', '--cache-dir', '/cache',
                    '--scanners', 'vuln', '--pkg-types', 'os', '--skip-db-update',
                    '--skip-java-db-update', '--offline-scan', '--no-progress', '--format', 'json'],
                    timeout=240, stdout=output)
            report_path = work / 'report.json'
            if report_path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError('OS検査結果が容量上限を超えています。')
            result = summarize(json.loads(report_path.read_text()))
        except KeyboardInterrupt:
            print('OS検査を中断しました。', file=sys.stderr)
            result = 130
        except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError, subprocess.SubprocessError) as error:
            print('OS検査を完了できませんでした: ' + str(error), file=sys.stderr)
            result = 2
        finally:
            # Dockerクライアントのtimeoutでも、専用コンテナを残さない。
            for name in containers:
                try:
                    subprocess.run(['docker', 'rm', '-f', name], cwd=ROOT, timeout=30,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    remaining = run(['docker', 'ps', '-aq', '--filter', 'name=^/' + name + '$'],
                                    timeout=30, capture_output=True, text=True)
                    if remaining.stdout.strip():
                        raise ValueError('検査コンテナを整理できません。')
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    print('OS検査の整理に失敗しました: ' + str(error), file=sys.stderr)
                    result = 2
            if built_image:
                try:
                    removed = subprocess.run(['docker', 'image', 'rm', built_image], cwd=ROOT, timeout=30,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if removed.returncode and result in (0, 1):
                        raise ValueError('検査用イメージを整理できません。')
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    print('検査用イメージの整理に失敗しました: ' + str(error), file=sys.stderr)
                    result = 2
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument('--image', help='既存のローカルイメージを検査する。未指定なら通常のDockerfileをビルドする。')
    target.add_argument('--database', action='store_true', help='compose.yamlの固定PostgreSQLイメージを取得して検査する。')
    args = parser.parse_args()
    return check(args.image, database=args.database)


if __name__ == '__main__':
    raise SystemExit(main())
