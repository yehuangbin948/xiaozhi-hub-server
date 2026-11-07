"""NetEase-only music service. Credentials stay in a private device file, never in tool output."""
import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SESSION = Path('data/netease-music-session.json')
_pending = {}
_players = {}
COMMAND_TIMEOUT = 20

class MusicError(ValueError):
    pass

def control_action(text):
    """Only explicit transport commands; song searches and negations stay with the model."""
    text = str(text or '').lower()
    for prefix in ('请帮我', '麻烦你', '帮我', '请', '麻烦'):
        if text.startswith(prefix): text = text[len(prefix):]; break
    return {
        '停止播放': 'stop', '停止音乐': 'stop', '停止播放音乐': 'stop',
        '关闭音乐': 'stop', '关掉音乐': 'stop', '把音乐停了': 'stop', '别放了': 'stop',
        '暂停播放': 'pause', '暂停音乐': 'pause',
        '继续播放': 'resume', '继续音乐': 'resume', '恢复播放': 'resume',
        '下一首': 'next', '下一首歌': 'next', '上一首': 'previous', '上一首歌': 'previous',
        '换一首': 'next', '换首歌': 'next', '换一首歌': 'next',
        '播放其他的歌': 'next', '播放其他歌曲': 'next', '播放别的歌': 'next',
        'stopmusic': 'stop', 'stopplayback': 'stop', 'pausemusic': 'pause',
        'resumemusic': 'resume', 'nextsong': 'next', 'previoussong': 'previous',
    }.get(text)

def _wake_parts(prefixes):
    if not isinstance(prefixes,(tuple,list)): return []
    labels=[p.strip() for p in prefixes[:10] if isinstance(p,str) and 0<len(p)<=24]
    return sorted({p[i:] for p in labels for i in range(len(p))},key=len,reverse=True)

def is_local_wake(text,prefixes):
    return str(text or '') in _wake_parts(prefixes)

def music_request(text,wake_prefixes=()):
    raw = str(text or '').strip().rstrip('。！？!?，,；;.')
    for prefix in _wake_parts(wake_prefixes):
        # ASR may put spaces or commas inside a repeated wake label. Preserve
        # the remainder as written (including spaces in English song names).
        separator=r'[\s，,。.!！?？、]*'
        match=re.match(separator.join(re.escape(c) for c in prefix)+separator,raw)
        if match:
            request=music_request(raw[match.end():])
            if request: return request
    compact = re.sub(r'\s+', '', raw)
    action = control_action(compact)
    if action: return {'action': action}
    match = re.fullmatch(r'(?:(?:请帮我|麻烦你|帮我|给我|请)\s*)?'
        r'(?:播放|放一首|来一首|我想听|想听|听一下|改播|换一首|换首|换成|放)\s*(.+)',raw)
    if not match:
        fragment=re.fullmatch(r'([\w· \-]{1,32})的(?:歌曲|音乐|歌)',raw)
        if fragment and not any(w in raw for w in ('不','别','介绍','评价','喜欢','讨厌','知道','了解','为什么','怎么','停止','暂停')):
            return {'action':'play','query':fragment.group(1).strip()}
        return None
    query = match.group(1).strip()
    explicit_music = any(word in query for word in ('这首歌','歌曲','音乐'))
    if not explicit_music and any(word in query for word in
        ('新闻','天气','报告','视频','电影','电视剧','故事','播客','电台','录音','闹钟','提醒','音量','倒计时')):
        return None
    query = re.sub(r'^(?:一下|一首|一些)\s*','',query)
    query = re.sub(r'的(?:歌曲|音乐|歌)(?:吧)?$','',query).strip()
    query = query.replace(' 的 ', ' ')
    if not query or len(query)>150: return None
    return {'action': 'play', 'query': query}

def enabled(config):
    name = (config.get('selected_module') or {}).get('Intent', 'function_call')
    entry = (config.get('Intent') or {}).get(name) or {}
    return entry.get('type', name) == 'function_call' and 'netease_music' in (entry.get('functions') or [])

def settings(config):
    cfg = (config.get('plugins') or {}).get('netease_music') or {}
    base = str(cfg.get('base_url') or '').strip().rstrip('/')
    try: u = urlsplit(base)
    except ValueError: raise MusicError('接口地址格式不正确') from None
    if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise MusicError('请先在网易云音乐插件设置中保存正确的接口地址')
    quality = cfg.get('quality') or 'exhigh'
    if quality not in ('standard', 'exhigh'):
        raise MusicError('音质请填写 standard 或 exhigh')
    return base, quality

def valid_stream(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or '').lower()
        return (p.scheme in ('http', 'https') and not p.username and not p.password
                and p.port in (None, 80, 443)
                and any(host.endswith('.' + suffix) for suffix in ('music.126.net', 'music.163.com', 'music.127.net')))
    except (ValueError, TypeError):
        return False

class Service:
    def __init__(self, config, session_path=None):
        self.base, self.quality = settings(config)
        self.path = Path(session_path or SESSION)

    def _saved(self):
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) and data.get('base_url') == self.base else {}
        except (OSError, ValueError, TypeError):
            return {}

    @property
    def logged_in(self):
        return bool(self._saved().get('cookie'))

    def _save(self, cookie):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'base_url': self.base, 'cookie': cookie}, f)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, self.path)
        os.chmod(self.path, 0o600)

    async def request(self, path, data=None, auth=True):
        body = dict(data or {})
        body.update(timestamp=int(time.time()*1000), timeout=12000, ua='pc', unblock='false')
        if auth:
            cookie = self._saved().get('cookie')
            if not cookie:
                raise MusicError('请先在网易云音乐插件中扫码登录')
            body['cookie'] = cookie
        try:
            async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
                response = await client.post(self.base + path, json=body)
                value = response.json()
        except (httpx.HTTPError, ValueError):
            raise MusicError('网易云接口暂时无法连接，请检查插件接口地址和服务') from None
        if not isinstance(value, dict):
            raise MusicError('网易云接口返回格式异常')
        code = value.get('code', (value.get('data') or {}).get('code') if isinstance(value.get('data'), dict) else None)
        if code == 301:
            raise MusicError('网易云登录已失效，请重新扫码')
        if response.status_code >= 400 or code not in (None, 200, 800, 801, 802, 803):
            raise MusicError('网易云暂时无法处理请求，请稍后再试')
        return value

    async def login_start(self):
        key = (await self.request('/login/qr/key', auth=False)).get('data', {}).get('unikey')
        if not key: raise MusicError('获取登录二维码失败')
        qr = await self.request('/login/qr/create', {'key': key, 'qrimg': True, 'platform': 'web'}, auth=False)
        image = qr.get('data', {}).get('qrimg', '')
        if not image.startswith('data:image/png;base64,'): raise MusicError('二维码格式异常')
        now = time.monotonic()
        for token in list(_pending):
            if _pending[token]['expires'] < now: _pending.pop(token, None)
        ticket = secrets.token_urlsafe(24)
        _pending[ticket] = {'key': key, 'base': self.base, 'path': str(self.path.resolve()), 'expires': now + 180}
        return {'ticket': ticket, 'image': image, 'state': 'waiting'}

    async def login_check(self, ticket):
        pending = _pending.get(ticket)
        if not pending or pending['base'] != self.base or pending['path'] != str(self.path.resolve()) or pending['expires'] < time.monotonic():
            return {'state': 'expired', 'logged_in': self.logged_in}
        result = await self.request('/login/qr/check', {'key': pending['key']}, auth=False)
        code = result.get('code')
        if code == 803:
            cookie = result.get('cookie')
            if not cookie or not isinstance(cookie, str): raise MusicError('登录未返回有效会话，请重新扫码')
            self._save(cookie)
            _pending.pop(ticket, None)
            return {'state': 'logged_in', 'logged_in': True}
        return {'state': {800: 'expired', 801: 'waiting', 802: 'confirm'}.get(code, 'waiting'), 'logged_in': self.logged_in}

    def logout(self):
        self.path.unlink(missing_ok=True)
        _players.clear()
        for token in list(_pending):
            if _pending[token]['path'] == str(self.path.resolve()): _pending.pop(token, None)

    async def test(self):
        result = await self.request('/login/status')
        data = result.get('data') or {}
        if not data.get('account') or not data.get('profile'): raise MusicError('网易云登录已失效，请重新扫码')
        return {'logged_in': True, 'member': bool(data['profile'].get('vipType'))}

    async def search(self, query):
        query = str(query or '').strip()
        if not query or len(query) > 150: raise MusicError('请说出歌名或歌手，长度不超过 150 字')
        data = await self.request('/search', {'keywords': query, 'type': 1, 'limit': 20})
        songs, seen = [], set()
        for song in (data.get('result') or {}).get('songs') or []:
            sid = song.get('id')
            if not isinstance(sid, int) or sid <= 0 or sid in seen: continue
            seen.add(sid)
            songs.append({'id': sid, 'title': str(song.get('name') or '')[:150],
                          'artist': ' / '.join(str(a.get('name') or '') for a in song.get('artists') or [])[:150],
                          'duration': (song.get('duration') or 0)/1000})
        return songs

    async def stream(self, song):
        data = await self.request('/song/url/v1', {'id': song['id'], 'level': self.quality})
        stream = next((s for s in data.get('data') or [] if s.get('id') == song['id']), {})
        if not stream.get('url') or stream.get('freeTrialInfo') is not None:
            raise MusicError('你的账号当前不能完整播放这首歌，将跳过试听内容')
        if not valid_stream(stream['url']) or str(stream.get('type')).lower() != 'mp3':
            raise MusicError('网易云返回了不支持的音源，本插件只播放网易云 MP3 在线音源')
        return {**song, 'url': stream['url'], 'duration': (stream.get('time') or song['duration']*1000)/1000}

async def transport(conn, payload):
    client = getattr(conn, 'mcp_client', None)
    if client is None: raise MusicError('设备没有连接，请先唤醒 ThinkSmart')
    name = next((key for key, raw in client.name_mapping.items() if raw == 'self.music.transport'), None)
    if not name: raise MusicError('设备客户端尚未安装网易云播放组件')
    from core.providers.tools.device_mcp.mcp_handler import call_mcp_tool
    try:
        raw = await call_mcp_tool(conn, client, name, payload, timeout=15)
        result = json.loads(raw)
    except (RuntimeError, ValueError, TimeoutError):
        raise MusicError('设备播放组件暂时无法响应，请检查客户端') from None
    if not result.get('ok'): raise MusicError(result.get('error') or '设备播放失败')
    return result

async def perform(conn, action, query='', session_path=None):
    try:
        return await asyncio.wait_for(_perform(conn, action, query, session_path),COMMAND_TIMEOUT)
    except asyncio.TimeoutError:
        raise MusicError('音乐请求超时，请稍后重试') from None

async def _perform(conn, action, query='', session_path=None):
    if action not in ('search', 'play', 'pause', 'resume', 'next', 'previous', 'stop', 'status'):
        raise MusicError('不支持的音乐操作')
    if action != 'stop' and not enabled(conn.config): raise MusicError('网易云音乐插件尚未启用')
    # A voice connection expires while a song can still be playing. Keep only
    # public queue metadata across reconnects, scoped to device and API/vault.
    now = time.monotonic()
    for key in list(_players):
        if _players[key]['used'] < now - 43200: _players.pop(key, None)
    device = getattr(conn, 'device_id', None)
    scope = (str(device), str(Path(session_path or SESSION).resolve()),
             str(((conn.config.get('plugins') or {}).get('netease_music') or {}).get('base_url', '')))
    state = _players.get(scope) if device else getattr(conn, '_netease_player', None)
    if state is None:
        state = {'lock': asyncio.Lock(), 'queue': [], 'index': -1, 'used': now}
        if device:
            if len(_players) >= 64: _players.pop(min(_players, key=lambda k: _players[k]['used']))
            _players[scope] = state
        else: conn._netease_player = state
    state['used'] = now
    async with state['lock']:
        if action in ('pause', 'resume', 'stop', 'status'):
            return await transport(conn, {'action': action})
        service = Service(conn.config, session_path)
        if action in ('search', 'play'):
            songs = await service.search(query)
            if not songs: raise MusicError('网易云没有搜到匹配的歌曲，请补充歌手或歌名')
            if action == 'search': return {'ok': True, 'songs': songs[:5]}
            queue, start, step = songs, 0, 1
        else:
            queue = state['queue']
            step = 1 if action == 'next' else -1
            start = state['index'] + step
        if not queue or not 0 <= start < len(queue): raise MusicError('当前播放列表没有更多歌曲，请重新点歌')
        last_error = None
        for index in range(start, len(queue) if step > 0 else -1, step):
            try: track = await service.stream(queue[index])
            except MusicError as error:
                last_error = error
                continue
            result = await transport(conn, {'action': 'play', 'track': track})
            state['queue'], state['index'] = queue, index
            return {**result, 'track': {k: v for k, v in track.items() if k != 'url'}}
        raise MusicError(str(last_error or '没有可完整播放的歌曲'))
