"""保存済みの声の区間から、声のかたちが近いものを探し、地図とかたちの図を作る。

近さは声の高さの動き・高さ・長さという音の特徴だけで決まる。近い声に付いた
種類や意味の予想は「参考」として件数で示し、自動で付けたり確定したりしない。
"""
from collections import Counter
import math

from sqlalchemy import select

from .acoustics import SHAPE_POINTS, closeness, voice_distance
from .analysis import local_time
from .models import CallType, DictionaryItem, Media, Observation, SoundAnalysis
from .services import ASSESSMENTS, OUTCOMES

POOL_LIMIT = 2000
NEIGHBOR_LIMIT = 5
CLOSE = 3.0           # 「近い」以内だけを参考の件数に数える。
MAP_COLORS = ['#2a78d6', '#eb6834', '#1baf7a']   # 散布図で見分けられるのは3色まで。
OTHER_COLOR = '#8a8a85'
MARKERS = ['circle', 'square', 'triangle', 'diamond']
MATCH_NOTICE = ('声の高さの動き・高さ・長さが近い順です。同じ声でも状況で意味が変わることがあり、'
                '近い声に付いた種類や予想は参考です。録音の雑音や人の声で測定がずれることもあります。')


def has_shape(features):
    return (isinstance(features, dict) and isinstance(features.get('shape_st'), list)
            and len(features['shape_st']) == SHAPE_POINTS)


def pool(db, limit=POOL_LIMIT):
    """比較の相手。実記録の、声のかたちを測った新しい区間から。"""
    rows = db.execute(
        select(SoundAnalysis, Observation, CallType.name, DictionaryItem.title)
        .join(Media, Media.id == SoundAnalysis.media_id)
        .join(Observation, Observation.id == Media.observation_id)
        .outerjoin(CallType, CallType.id == SoundAnalysis.call_type_id)
        .outerjoin(DictionaryItem, DictionaryItem.id == SoundAnalysis.dictionary_item_id)
        .where(Observation.cat_id == 1, Observation.is_sample.is_(False),
               Observation.deletion_requested_at.is_(None), SoundAnalysis.features.is_not(None))
        .order_by(SoundAnalysis.created_at.desc(), SoundAnalysis.id.desc()).limit(limit))
    return [(result, observation, type_name, title) for result, observation, type_name, title in rows
            if has_shape(result.features)]


def neighbor_entry(result, observation, type_name, title, measured):
    return {
        'id': result.id, 'observation_id': observation.id,
        'url': f'/entries/observation/{observation.id}#sound-{result.id}',
        'occurred_at': local_time(observation.occurred_at),
        'start_seconds': result.start_ms / 1000, 'end_seconds': result.end_ms / 1000,
        'distance': measured['distance'], 'closeness': closeness(measured['distance']),
        'detail': measured,
        'call_type': {'id': result.call_type_id, 'name': type_name} if result.call_type_id else None,
        'meaning': {'id': result.dictionary_item_id, 'title': title, 'url': f'/dictionary/{result.dictionary_item_id}',
                    'assessment': result.assessment, 'assessment_label': ASSESSMENTS.get(result.assessment)}
        if result.dictionary_item_id else None,
        'outcome': {'key': result.outcome, 'label': OUTCOMES[result.outcome]} if result.outcome else None,
        'shape': centered(result.features['shape_st']),
    }


def neighbors(target, candidates, limit=NEIGHBOR_LIMIT):
    """targetと同じ記録の同じ区間は除き、近い順に返す。"""
    found = []
    for result, observation, type_name, title in candidates:
        if result.id == target.id:
            continue
        measured = voice_distance(target.features, result.features)
        if measured is not None:
            found.append((measured['distance'], result.id, result, observation, type_name, title, measured))
    found.sort(key=lambda row: (row[0], row[1]))
    return [neighbor_entry(result, observation, type_name, title, measured)
            for _, _, result, observation, type_name, title, measured in found[:limit]]


def hints(close):
    """近い声に飼い主が付けた種類と予想の件数。確率や正解ではない。"""
    close = [row for row in close if row['distance'] < CLOSE]
    types = Counter(row['call_type']['name'] for row in close if row['call_type'])
    meanings = {}
    for row in close:
        if row['meaning']:
            entry = meanings.setdefault(row['meaning']['id'], {
                'id': row['meaning']['id'], 'title': row['meaning']['title'], 'url': row['meaning']['url'],
                'counts': {'supports': 0, 'contradicts': 0, 'unknown': 0}})
            entry['counts'][row['meaning']['assessment']] += 1
    return {'close_count': len(close),
            'call_types': [{'name': name, 'count': count} for name, count in types.most_common()],
            'meanings': sorted(meanings.values(), key=lambda m: (-sum(m['counts'].values()), m['id']))}


def matches_for(db, results):
    """詳細画面の区間ごとの近い声。相手の一覧は一度だけ読み込む。"""
    if not any(has_shape(result.features) for result in results):
        return {}
    candidates = pool(db)
    out = {}
    for result in results:
        if has_shape(result.features):
            close = neighbors(result, candidates)
            out[result.id] = {'neighbors': close, 'hints': hints(close),
                              'shape': centered(result.features['shape_st'])}
    return out


def similar_report(db, sound_id):
    """APIとCLI用。区間がない・測っていない場合はNoneを返す。"""
    target = db.scalar(select(SoundAnalysis).join(Media, Media.id == SoundAnalysis.media_id)
                       .join(Observation, Observation.id == Media.observation_id)
                       .where(SoundAnalysis.id == sound_id, Observation.cat_id == 1,
                              Observation.deletion_requested_at.is_(None)))
    if target is None:
        return None
    if not has_shape(target.features):
        return {'id': sound_id, 'measured': False, 'neighbors': [], 'hints': None, 'notice': MATCH_NOTICE}
    close = neighbors(target, pool(db))
    return {'id': sound_id, 'measured': True, 'features': target.features, 'neighbors': close,
            'hints': hints(close), 'notice': MATCH_NOTICE}


def centered(shape):
    middle = sorted(shape)[len(shape) // 2]
    return [round(value - middle, 2) for value in shape]


def sparkline(shape, width=120, height=36, span=8.0):
    """中心をそろえた半音の並びをSVGのpointsにする。上下は±span/2半音。"""
    step = width / (len(shape) - 1)
    clamp = lambda v: max(-span / 2, min(span / 2, v))
    return ' '.join(f'{i * step:.1f},{height / 2 - clamp(v) / span * height:.1f}' for i, v in enumerate(shape))


def voice_map(rows, width=640, height=320, pad=44):
    """声の長さ（横）と高さ（縦、対数）の散布図。多い3種類だけ色分けし、残りは「その他」。"""
    points = [row for row in rows if isinstance(row['features'], dict) and row['features'].get('pitch_hz')
              and row['features'].get('voiced_ms')]
    if not points:
        return None
    counts = Counter(row['call_type']['name'] for row in points if row['call_type'])
    top = [name for name, _ in counts.most_common(len(MAP_COLORS))]
    styles = {name: (MAP_COLORS[i], MARKERS[i]) for i, name in enumerate(top)}
    pitches = [row['features']['pitch_hz']['median'] for row in points]
    lengths = [row['features']['voiced_ms'] / 1000 for row in points]
    low, high = math.log2(max(min(pitches) * 0.9, 100)), math.log2(max(pitches) * 1.1)
    if high - low < 0.5:
        low, high = low - 0.25, high + 0.25
    longest = max(max(lengths) * 1.1, 1.0)
    x = lambda seconds: pad + seconds / longest * (width - pad * 1.5)
    y = lambda hz: height - pad - (math.log2(hz) - low) / (high - low) * (height - pad * 1.6)
    dots = []
    for row, hz, seconds in zip(points, pitches, lengths):
        name = row['call_type']['name'] if row['call_type'] else None
        color, marker = styles.get(name, (OTHER_COLOR, MARKERS[-1]))
        dots.append({'x': round(x(seconds), 1), 'y': round(y(hz), 1), 'color': color, 'marker': marker,
                     'url': row['observation_url'], 'label': f"{name or '種類未選択'} · 約{hz}Hz · {seconds:.1f}秒 · "
                     f"{(row['occurred_at'] or '')[:10]}"})
    ticks_hz = [hz for hz in (125, 250, 500, 1000, 2000) if low <= math.log2(hz) <= high]
    ticks_s = [s / 2 for s in range(0, int(longest * 2) + 1)] if longest <= 4 else list(range(0, int(longest) + 1))
    legend = [{'name': name, 'color': styles[name][0], 'marker': styles[name][1], 'count': counts[name]} for name in top]
    other = len(points) - sum(counts[name] for name in top)
    if other:
        legend.append({'name': 'その他・未選択', 'color': OTHER_COLOR, 'marker': MARKERS[-1], 'count': other})
    return {'width': width, 'height': height, 'pad': pad, 'dots': dots, 'legend': legend,
            'y_ticks': [{'y': round(y(hz), 1), 'label': f'{hz}Hz'} for hz in ticks_hz],
            'x_ticks': [{'x': round(x(s), 1), 'label': f'{s:g}秒'} for s in ticks_s],
            'count': len(points)}


def type_shapes(rows, limit=12):
    """種類ごとの声のかたちを重ねて描くための線。"""
    shapes = {}
    for row in rows:
        if row['call_type'] and has_shape(row['features']):
            lines = shapes.setdefault(row['call_type']['id'], [])
            if len(lines) < limit:
                lines.append(sparkline(centered(row['features']['shape_st'])))
    return shapes
