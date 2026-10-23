"""リクエスト単位の観測: リクエストIDと構造化アクセスログ。

各リクエストに推測できないIDを付け、レスポンスヘッダ X-Request-ID で返す。
処理の開始・終了を1行JSONで記録し、障害時に画面のエラーとログを
同じIDで結びつけられるようにする。本文や個人データは記録しない。
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

log = logging.getLogger('meme.http')

class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault('state', {})['request_id'] = request_id
        started = time.monotonic()
        status = 500

        async def traced_send(msg):
            nonlocal status
            if msg['type'] == 'http.response.start':
                status = msg['status']
                msg['headers'] = list(msg.get('headers', [])) + [
                    (b'x-request-id', request_id.encode())]
            await send(msg)

        try:
            await self.app(scope, receive, traced_send)
        finally:
            log.info(json.dumps({
                'ts': datetime.now(timezone.utc).isoformat(),
                'event': 'http_request',
                'request_id': request_id,
                'method': scope.get('method', ''),
                'path': scope.get('path', ''),
                'status': status,
                'duration_ms': round((time.monotonic() - started) * 1000, 1),
            }, separators=(',', ':')))
