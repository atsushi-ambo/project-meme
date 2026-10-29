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

def token_matches(supplied, expected):
    # compare_digestのstr比較はASCIIのみ。外部入力を例外にせず拒否する。
    return (isinstance(supplied, str) and isinstance(expected, str)
            and len(supplied) == len(expected) == 64
            and supplied.isascii() and expected.isascii()
            and secrets.compare_digest(supplied, expected))


def read_token(cookie):
    try:
        token = signer.loads(cookie, max_age=43200)
        return token if isinstance(token, str) and len(token) == 64 and token.isascii() else None
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
            if mutation and not token_matches(supplied, token):
                return await rejection('送信を確認できません。', 403)(scope, receive, send)
        # フォームの日本語・絵文字はURLエンコードで最大12倍になる。
        # 10000文字の正当なメモを受け付け、JSON/APIの上限は維持する。
        content_type = headers.get(b'content-type', b'').split(b';', 1)[0].strip().lower()
        form_limit = 131072 if (not scope['path'].startswith('/api/')
            and content_type == b'application/x-www-form-urlencoded') else 65536
        maximum = 10_000_000 if scope['path']=='/api/profile-photo' and scope['method']=='PUT' else (settings.max_file_bytes if upload else form_limit)
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
                    (b'referrer-policy', b'same-origin'), (b'cache-control', b'no-store'),
                    # 他のサイトが<img>や<video>でこのアプリの写真・動画・JSONを読み込むのを防ぐ。
                    (b'cross-origin-resource-policy', b'same-origin'),
                    (b'cross-origin-opener-policy', b'same-origin'),
                    (b'permissions-policy', b'camera=(), microphone=(), geolocation=(), payment=(), usb=()')]
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
