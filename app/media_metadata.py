"""動画に埋め込まれた日時だけを読む。コピー日時・ファイル更新日時は使わない。"""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def video_capture_time(path: Path, name: str):
    if Path(name).suffix.lower() not in {'.mov', '.mp4'}:
        return None
    command = ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
               '-format_whitelist', 'mov', '-show_entries',
               'format_tags=creation_time,com.apple.quicktime.creationdate',
               '-of', 'json', str(path)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=15, check=True)
        tags = json.loads(result.stdout).get('format', {}).get('tags', {})
        # 撮影地のUTCオフセットを持つAppleの撮影日時を優先する。
        for key in ('com.apple.quicktime.creationdate', 'creation_time'):
            value = tags.get(key)
            if not isinstance(value, str) or len(value) > 80:
                continue
            try:
                value = datetime.fromisoformat(value)
                if value.tzinfo is None:
                    continue  # タイムゾーンが不明な値を日本時間と決めつけない。
                value = value.astimezone(timezone.utc)
                if datetime(1970, 1, 1, tzinfo=timezone.utc) <= value <= datetime.now(timezone.utc) + timedelta(days=1):
                    return value
            except (ValueError, OverflowError):
                continue
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError):
        pass  # メタデータの欠落や破損だけでは、検証済み動画の保存を失敗させない。
    return None
