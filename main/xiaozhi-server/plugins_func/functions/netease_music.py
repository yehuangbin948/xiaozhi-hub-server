"""Managed NetEase music tool; never expose playback URLs or session cookies to the model."""
import json
from core.netease_music import MusicError, perform
from plugins_func.register import Action, ActionResponse, ToolType, register_function

DESCRIPTION = {'type': 'function', 'function': {
    'name': 'netease_music',
    'description': '网易云音乐插件。用户点歌、搜歌曲或歌手、暂停音乐、继续、下一首、上一首、停止时使用。'
                   'play 按 query 搜索并播放完整歌曲；next/previous 在本次搜索结果中切换。'
                   '只能使用网易云账号有权播放的音源。播放音乐后简短确认，不要继续追问。',
    'parameters': {'type': 'object', 'properties': {
        'action': {'type': 'string', 'enum': ['search','play','pause','resume','next','previous','stop','status']},
        'query': {'type': 'string', 'description': 'search/play 必填，尽量包含歌手和歌名'}}, 'required': ['action']}}}

@register_function('netease_music', DESCRIPTION, ToolType.SYSTEM_CTL)
async def netease_music(conn, action, query=''):
    try:
        result = await perform(conn, action, query)
        if action in ('search', 'status'):
            return ActionResponse(Action.REQLLM, result=json.dumps(result, ensure_ascii=False))
        track = result.get('track') or {}
        text = ('正在播放' + track.get('artist', '') + '的《' + track.get('title', '') + '》。') if track else {
            'pause': '音乐已暂停。', 'resume': '继续播放。', 'stop': '音乐已停止。'}.get(action, '好的。')
        return ActionResponse(Action.RESPONSE, result=text, response=text)
    except MusicError as error:
        return ActionResponse(Action.RESPONSE, result=str(error), response=str(error))
