"""按来源接收 AIVDM 句子、组装分片并建立船舶观测索引。

所有输入都是本地资料文件；本模块不打开任何无线电或网络设备。
"""

import json
from pathlib import Path

from .ais import DecodeError, decode_bits, payload_bits
from .core import read_sentences
from .fragments import FragmentAssembler


# 查询视图要求的静态字段。缺失其中任一字段即视为静态资料不完整。
REQUIRED_STATIC_FIELDS = ('name', 'callsign', 'dimensions')

STATIC_FIELD_LABELS = {
    'name': '船名',
    'callsign': '呼号',
    'imo_number': 'IMO 号',
    'ship_type': '船舶类型',
    'ship_type_text': '船舶类型说明',
    'dimensions': '尺寸',
    'destination': '目的地',
    'eta': '预计到达',
    'draught': '吃水',
    'vendor_id': '厂商编号',
    'ais_version': 'AIS 版本',
    'epfd_type': '定位设备类型',
}

STATIC_FIELD_NAMES = (
    'name', 'callsign', 'imo_number', 'ship_type', 'ship_type_text',
    'dimensions', 'destination', 'eta', 'draught', 'vendor_id',
    'ais_version', 'epfd_type',
)

POSITION_TYPES = (1, 2, 3, 18)


def load_connections(path):
    """读取连接清单，校验身份字段并解析出同一目录下的资料文件。"""
    path = Path(path)
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, list) or not raw:
        raise ValueError('连接清单必须是非空列表')
    entries = []
    identities = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f'连接清单第 {index} 项不是对象')
        file_name = item.get('file')
        station = item.get('station')
        connection = item.get('connection')
        if not isinstance(file_name, str) or not file_name:
            raise ValueError(f'连接清单第 {index} 项的 file 不合法')
        if not isinstance(station, str) or not station:
            raise ValueError(f'连接清单第 {index} 项的 station 不合法')
        if isinstance(connection, bool) or not isinstance(connection, int):
            raise ValueError(f'连接清单第 {index} 项的 connection 必须是整数')
        identity = (station, connection)
        if identity in identities:
            raise ValueError(f'连接清单第 {index} 项重复声明了 {station}/{connection}')
        identities.add(identity)
        entries.append({'file': str(path.parent / file_name), 'station': station,
                        'connection': connection})
    return entries


def _load_sentences(entries):
    streams = []
    for entry in entries:
        sentences = list(read_sentences(entry['file']))
        if not sentences:
            raise ValueError(f'来源资料为空：{entry["file"]}')
        streams.append({'entry': entry, 'sentences': sentences, 'index': 0})
    return streams


def receive_entries(entries, interleave=True, stale_after=64, max_groups=512):
    """接收连接清单中的句子，返回（组装结果，组装器）。

    ``interleave=True`` 时按行轮转：先取每个来源的第 1 行，再取第 2 行，
    用于模拟两个岸站交错发送。分片组仍严格按来源隔开，交错只会暴露
    不同岸站上相同顺序号的碰撞，不会把它们拼在一起。
    """
    streams = _load_sentences(entries)
    assembler = FragmentAssembler(stale_after=stale_after, max_groups=max_groups)
    if interleave:
        while any(stream['index'] < len(stream['sentences']) for stream in streams):
            for stream in streams:
                if stream['index'] < len(stream['sentences']):
                    sentence = stream['sentences'][stream['index']]
                    stream['index'] += 1
                    entry = stream['entry']
                    assembler.receive(sentence, entry['station'], entry['connection'])
    else:
        for stream in streams:
            entry = stream['entry']
            for sentence in stream['sentences']:
                assembler.receive(sentence, entry['station'], entry['connection'])
    return assembler.finish(), assembler


def receive_files(path, interleave=True, stale_after=64, max_groups=512):
    """读取连接清单并返回可直接 JSON 序列化的接收结果。"""
    result, _ = receive_entries(load_connections(path), interleave=interleave,
                                stale_after=stale_after, max_groups=max_groups)
    index = ObservationIndex()
    index.ingest_assemblies(result['assembled'])
    return index.result(assembler_result=result)


# ---- 观测索引 ---------------------------------------------------------

def _observation_id(message_id, part_label):
    suffix = f':{part_label}' if part_label else ''
    return 'obs-' + message_id[:16] + suffix


def _decode_assembly(assembly):
    """解码组装后的 payload；失败或不受支持时保留原文而不建船。"""
    try:
        bits = payload_bits(assembly['payload'], assembly['fill'])
        decoded = decode_bits(bits)
    except DecodeError as error:
        return None, f'解码失败：{error}'
    if not decoded['supported']:
        return None, '不支持的消息类型，保留原文，不建立船舶资料'
    fields = decoded['fields']
    if not fields.get('mmsi'):
        return None, '报文缺少有效 MMSI，保留原文，不建立船舶资料'
    return decoded, None


def _provenance(observation, value):
    return {
        'value': value,
        'observation_id': observation['observation_id'],
        'message_type': observation['message_type'],
        'message_type_name': observation['message_type_name'],
        'part_label': observation.get('part_label'),
        'source': observation['source'],
        'received_at': observation['received_at'],
        'sequence': observation['sequence'],
        'channel': observation['channel'],
        'parts': observation['parts'],
        'first_line': observation['first_line'],
        'reordered': observation['reordered'],
    }


class ObservationIndex:
    """保存结构化观测，按 MMSI 汇总字段及其来源观测。"""

    def __init__(self):
        self._observations = []
        self._seen = set()
        self._unprocessed = []
        self._duplicate_observations = []
        self._vessel_messages = {}
        self._source_observation_counts = {}

    def ingest_assemblies(self, assemblies):
        for assembly in assemblies:
            self._ingest(assembly)

    def _ingest(self, assembly):
        decoded, reason = _decode_assembly(assembly)
        if decoded is None:
            self._unprocessed.append({
                'id': assembly['id'],
                'source': assembly['source'],
                'channel': assembly['channel'],
                'sequence': assembly['sequence'],
                'received_at': assembly['completed_at'],
                'raw': assembly['raw_text'],
                'parts': assembly['received_parts'],
                'reason': reason,
                'supported': False,
            })
            return
        fields = decoded['fields']
        source = assembly['source']
        observation = {
            'observation_id': _observation_id(assembly['id'], fields.get('part_label')),
            'message_id': assembly['id'],
            'mmsi': fields['mmsi'],
            'message_type': decoded['message_type'],
            'message_type_name': decoded['message_type_name'],
            'part_label': fields.get('part_label'),
            'source': source,
            'channel': assembly['channel'],
            'sequence': assembly['sequence'],
            'received_at': assembly['completed_at'],
            'first_line': assembly['first_line'],
            'group_key': assembly['group_key'],
            'parts': assembly['received_parts'],
            'total_parts': assembly['total'],
            'reordered': assembly['reordered'],
            'fields': fields,
            'raw': assembly['raw_text'],
        }
        key = (source['station'], source['connection'], source['generation'],
               observation['observation_id'])
        if key in self._seen:
            self._duplicate_observations.append({
                'observation_id': observation['observation_id'],
                'source': source,
                'reason': '同一来源已记录过该观测，重复回放不增加计数',
            })
            return
        self._seen.add(key)
        self._observations.append(observation)
        self._vessel_messages.setdefault(str(fields['mmsi']), []).append(observation)
        source_key = (source['station'], source['connection'], source['generation'])
        self._source_observation_counts[source_key] = \
            self._source_observation_counts.get(source_key, 0) + 1

    def observations(self):
        return list(self._observations)

    def unprocessed(self):
        return list(self._unprocessed)

    def duplicate_observations(self):
        return list(self._duplicate_observations)

    def observation_counts(self):
        return _count_list(self._source_observation_counts)

    def vessel_ids(self):
        return sorted(self._vessel_messages, key=int)

    def result(self, assembler_result=None):
        assembler_result = assembler_result or {}
        source_receipts = {(item['station'], item['connection'], item['generation']): item['received']
                           for item in assembler_result.get('sources', [])}
        observed = {(item['station'], item['connection'], item['generation']): item['observations']
                    for item in self.observation_counts()}
        counts = []
        # 有接收但一条观测都没组装出来的来源也要出现，计数为 0。
        for key in sorted(set(source_receipts) | set(observed)):
            counts.append({'station': key[0], 'connection': key[1], 'generation': key[2],
                           'observations': observed.get(key, 0),
                           'received': source_receipts.get(key, 0)})
        return {
            'observations': self.observations(),
            'unprocessed': self.unprocessed(),
            'unresolved': list(assembler_result.get('unresolved', [])),
            'duplicates': list(assembler_result.get('duplicates', [])),
            'duplicate_observations': self.duplicate_observations(),
            'observation_counts': counts,
            'receipts': assembler_result.get('receipts', 0),
            'source_receipts': list(assembler_result.get('sources', [])),
            'pending_groups': assembler_result.get('pending_groups', 0),
            'vessels': self.vessel_ids(),
        }

    def query(self, mmsi):
        key = str(mmsi)
        observations = self._vessel_messages.get(key)
        if not observations:
            return {'mmsi': key, 'found': False, 'reason': '没有可用的船舶观测'}
        return _query_observations(key, observations)


def _query_observations(mmsi, observations):
    static_fields = {}
    static_parts = set()
    latest_static = None
    latest_position = None
    last_available_position = None

    for observation in observations:
        fields = observation['fields']
        message_type = observation['message_type']
        if message_type in POSITION_TYPES:
            if latest_position is None or \
                    observation['received_at'] > latest_position['received_at']:
                latest_position = observation
            if fields.get('position_available') and \
                    (last_available_position is None
                     or observation['received_at'] > last_available_position['received_at']):
                last_available_position = observation
            continue
        if latest_static is None or observation['received_at'] > latest_static['received_at']:
            latest_static = observation
        if message_type == 5:
            static_parts.add('5')
        elif message_type == 24:
            static_parts.add('24' + fields['part_label'])
        for field_name in STATIC_FIELD_NAMES:
            value = fields.get(field_name)
            if value is None:
                continue
            current = static_fields.get(field_name)
            # 每个字段独立按到达顺序取最新值；较新的静态资料若未携带
            # 某字段，不会覆盖该字段上一次明确给出的值和依据。
            if current is None or observation['received_at'] > current['received_at']:
                static_fields[field_name] = _provenance(observation, value)

    static_missing = [name for name in REQUIRED_STATIC_FIELDS if name not in static_fields]
    return {
        'mmsi': mmsi,
        'found': True,
        'static': {
            'complete': not static_missing,
            'missing_fields': static_missing,
            'parts_received': sorted(static_parts),
            'fields': static_fields,
            'latest_static_observation_id':
                latest_static['observation_id'] if latest_static else None,
        },
        'position': _position_view(latest_position),
        'last_available_position': _position_view(last_available_position),
        'motion': _motion_view(latest_position),
        'observations': [_observation_summary(observation) for observation in
                         sorted(observations, key=lambda item: item['received_at'])],
        'source_observations': _count_list(_count_sources(observations)),
    }


def _position_view(observation):
    if observation is None:
        return {'available': False, 'observation_id': None, 'reason': '尚未收到位置报告'}
    fields = observation['fields']
    available = bool(fields.get('position_available'))
    view = {
        'available': available,
        'latitude': fields.get('latitude') if available else None,
        'longitude': fields.get('longitude') if available else None,
        'speed_over_ground': fields.get('speed_over_ground'),
        'course_over_ground': fields.get('course_over_ground'),
        'true_heading': fields.get('true_heading'),
        'timestamp': fields.get('timestamp'),
        'observation_id': observation['observation_id'],
        'message_type': observation['message_type'],
        'message_type_name': observation['message_type_name'],
        'source': observation['source'],
        'received_at': observation['received_at'],
    }
    if not available:
        view['reason'] = '本次位置报告的经纬度为不可用值，不能沿用上次位置作为本次测量'
    return view


def _motion_view(observation):
    if observation is None:
        return {}
    fields = observation['fields']
    return {
        'observation_id': observation['observation_id'],
        'received_at': observation['received_at'],
        'speed_over_ground': fields.get('speed_over_ground'),
        'course_over_ground': fields.get('course_over_ground'),
        'true_heading': fields.get('true_heading'),
        'navigation_status': fields.get('navigation_status'),
        'navigation_status_text': fields.get('navigation_status_text'),
    }


def _observation_summary(observation):
    summary = {
        'observation_id': observation['observation_id'],
        'mmsi': observation['mmsi'],
        'message_type': observation['message_type'],
        'message_type_name': observation['message_type_name'],
        'part_label': observation.get('part_label'),
        'source': observation['source'],
        'channel': observation['channel'],
        'sequence': observation['sequence'],
        'received_at': observation['received_at'],
        'parts': observation['parts'],
        'reordered': observation['reordered'],
    }
    if observation['message_type'] in POSITION_TYPES:
        summary['position_available'] = observation['fields'].get('position_available')
    return summary


def _count_sources(observations):
    counts = {}
    for observation in observations:
        source = observation['source']
        key = (source['station'], source['connection'], source['generation'])
        counts[key] = counts.get(key, 0) + 1
    return counts


def _count_list(counts):
    return [{'station': key[0], 'connection': key[1], 'generation': key[2], 'observations': count}
            for key, count in sorted(counts.items())]


def query_result(result, mmsi):
    """在 ``receive_files`` 的结果上按 MMSI 查询。"""
    index = ObservationIndex()
    index._vessel_messages = {}
    for observation in result['observations']:
        index._vessel_messages.setdefault(str(observation['mmsi']), []).append(observation)
    return index.query(mmsi)
