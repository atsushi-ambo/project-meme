"""フォームを解析する前にHost/Originを検証。CSRFは署名Cookieと照合する。"""
import asyncio
import secrets
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.responses import PlainTextResponse, JSONResponse
from .config import settings

signer = URLSafeTimedSerializer(settings.secret_key, salt='meme-browser-csrf')
COOKIE = 'meme_browser'

def read_token(cookie):
    try:
        token = signer.loads(cookie, max_age=43200)
        return token if isinstance(token, str) and len(token) == 64 else None
    except (BadSignature, SignatureExpired, TypeError):
        return None

def new_cookie():
    token = secrets.token_hex(32)
    return token, signer.dumps(token)

class SecurityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        def rejection(message, status):
            if scope['path'].startswith('/api/'):
                return JSONResponse({'detail': message}, status_code=status)
            return PlainTextResponse(message, status)
        headers = dict(scope['headers'])
        host = headers.get(b'host', b'').decode('latin1')
        port = urlsplit(settings.base_url).port or 80
        allowed = {f'localhost:{port}', f'127.0.0.1:{port}'}
        if port == 80:
            allowed |= {'localhost', '127.0.0.1'}
        if host not in allowed:
            return await rejection('接続先が許可されていません。', 400)(scope, receive, send)
        mutation = scope['method'] not in ('GET','HEAD','OPTIONS')
        if mutation and (settings.media_root / 'deleting-all').exists():
            return await rejection('全削除の処理中です。完了までお待ちください。', 409)(scope, receive, send)
        if mutation:
            origin = headers.get(b'origin', b'').decode('latin1')
            if origin not in {f'http://{h}' for h in allowed}:
                return await rejection('送信元を確認できません。', 403)(scope, receive, send)
        cookies = SimpleCookie()
        try:
            cookies.load(headers.get(b'cookie', b'').decode('latin1'))
        except Exception:
            pass
        token = read_token(cookies[COOKIE].value) if COOKIE in cookies else None
        set_cookie = None
        if not token:
            if mutation:
                return await rejection('ページを開き直してください。', 403)(scope, receive, send)
            token, set_cookie = new_cookie()
        scope.setdefault('state', {})['csrf'] = token
        upload = scope['path'].startswith('/api/media/') and scope['method'] == 'PUT'
        if upload or scope['path'].startswith('/api/'):
            supplied = headers.get(b'x-csrf-token', b'').decode('latin1')
            if mutation and not secrets.compare_digest(supplied, token):
                return await rejection('送信を確認できません。', 403)(scope, receive, send)
        maximum = 10_000_000 if scope['path']=='/api/profile-photo' and scope['method']=='PUT' else (settings.max_file_bytes if upload else 65536)
        seen = 0
        started = False
        deadline = asyncio.get_running_loop().time() + (settings.upload_timeout if upload else 60)
        async def limited_receive():
            nonlocal seen
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0: raise TimeoutError()
            msg = await asyncio.wait_for(receive(), timeout=remaining)
            seen += len(msg.get('body', b''))
            if seen > maximum:
                raise ValueError('body_limit')
            return msg
        async def protected_send(msg):
            nonlocal started
            if msg['type'] == 'http.response.start':
                started = True
                extra = [(b'x-content-type-options', b'nosniff'),
                    (b'content-security-policy', b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                    (b'referrer-policy', b'same-origin'), (b'cache-control', b'no-store')]
                if set_cookie:
                    extra.append((b'set-cookie', f'{COOKIE}={set_cookie}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200'.encode()))
                msg['headers'] = list(msg.get('headers', [])) + extra
            await send(msg)
        try:
            await self.app(scope, limited_receive, protected_send)
        except (TimeoutError, ValueError) as error:
            if not started:
                status = 408 if isinstance(error, TimeoutError) else 413
                await rejection('送信時間または容量の上限を超えました。', status)(scope, receive, protected_send)
            else:
                raise
