"""AIVDM 多片消息的接收与组装。

分片身份由来源连接（岸站、连接、连接代次）、频道、formatter、顺序号和
总片数共同界定。顺序号不是全局唯一的，因此不同岸站上相同的顺序号
绝不能进入同一个组装组。

顺序号为空的片段没有可依赖的组标识。只有从第 1 片开始、连续按 1..N
到达的片段才会被组装；任何同时出现、乱序或重复的情况都会保留原文并
标记为未组装，而不是猜测配对。
"""

import hashlib
import json


def _identity_digest(payload):
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _diagnosis(received_parts, total, extra):
    """给未组装记录一个机器可读的分类，便于汇总和排查。"""
    received_parts = set(received_parts)
    if extra and 'conflicting_part' in extra:
        return 'conflicting_part'
    if len(received_parts) < total:
        if not received_parts:
            return 'missing_all_parts'
        if 1 not in received_parts:
            return 'missing_first_part'
        first_gap = min(number for number in range(1, total + 1) if number not in received_parts)
        if first_gap > max(received_parts):
            return 'missing_trailing_parts'
        return 'missing_middle_part'
    return 'duplicate_or_overflow'


def _group_key_text(scope, sentence, sequenced):
    station, connection, generation = scope
    if sequenced:
        return f'{station}/{connection}/{generation}/{sentence["channel"]}/{sentence["formatter"]}/{sentence["sequence"]}/{sentence["total"]}'
    return f'{station}/{connection}/{generation}/{sentence["channel"]}/{sentence["formatter"]}/-/{sentence["total"]}'


def _source_dict(scope):
    station, connection, generation = scope
    return {'station': station, 'connection': connection, 'generation': generation}


def _part_view(receipt):
    sentence = receipt['sentence']
    return {
        'part': sentence['part'],
        'raw': sentence['raw'],
        'line': sentence['line'],
        'arrival': receipt['arrival'],
        'payload': sentence['payload'],
        'fill': sentence['fill'],
    }


class FragmentAssembler:
    """按来源连接隔离的分片组装器。

    ``stale_after`` 以同一来源连接已接收（非重复）片段数为期限：超过
    该期限仍未齐的组会被移出并保留为未组装记录，避免半包无限留存。
    ``max_groups`` 对同时挂起的组数给出硬上限。
    """

    def __init__(self, stale_after=64, max_groups=512, replay_window=1024):
        if stale_after < 1:
            raise ValueError('过期期限必须为正数')
        if max_groups < 1:
            raise ValueError('挂起组上限必须为正数')
        if replay_window < 1:
            raise ValueError('重复回放窗口必须为正数')
        self.stale_after = stale_after
        self.max_groups = max_groups
        self.replay_window = replay_window
        self._generations = {}
        self._closed = set()
        self._pending_sequenced = {}
        self._pending_unsequenced = {}
        self._assembled = []
        self._unresolved = []
        self._duplicates = []
        self._seen = {}
        self._receipts = 0
        self._clock = 0
        self._source_receipts = {}
        self._content_counts = {}

    # ---- 连接生命周期 -------------------------------------------------

    def generation(self, station, connection):
        return self._generations.get((station, connection), 0)

    def _scope(self, station, connection):
        key = (station, connection)
        if key in self._closed:
            # 已关闭的连接再次收到资料，必须作为新连接处理，旧半包不串入。
            self._flush_scope((station, connection, self._generation_for(key)),
                              '连接已关闭，旧半包不参与新连接')
            self._generations[key] = self._generation_for(key) + 1
            self._closed.discard(key)
        return (station, connection, self._generation_for(key))

    def _generation_for(self, key):
        return self._generations.get(key, 0)

    def reconnect(self, station, connection):
        """重新界定连接代次；旧代次挂起的分片全部转为未组装记录。"""
        key = (station, connection)
        old = self._generation_for(key)
        self._flush_scope((station, connection, old), '岸站重连，旧连接的分片不再组装')
        self._generations[key] = old + 1
        self._closed.discard(key)
        return self._generations[key]

    def close_connection(self, station, connection):
        key = (station, connection)
        scope = (station, connection, self._generation_for(key))
        self._flush_scope(scope, '连接关闭前仍未组装')
        self._closed.add(key)

    def _flush_scope(self, scope, reason):
        for store in (self._pending_sequenced, self._pending_unsequenced):
            for key in list(store):
                if key[:3] == scope:
                    self._unresolved.append(self._unresolved_view(store.pop(key), reason))

    # ---- 接收 ---------------------------------------------------------

    def receive(self, sentence, station, connection):
        """接收一条已校验的句子。返回 ``duplicate``/``pending``/``assembled``/``unresolved``。"""
        self._clock += 1
        receipt = {'sentence': sentence, 'station': station, 'connection': connection,
                   'arrival': self._clock}
        scope = self._scope(station, connection)

        duplicate_key = scope + (sentence['raw'],)
        previous = self._seen.get(duplicate_key)
        # 只有同一来源连接在近期窗口内再次出现完全相同的句子才视为回放；
        # 时间上相隔很远的相同报文仍是一次新的接收。
        if previous is not None and self._receipts - previous <= self.replay_window:
            self._duplicates.append({
                'source': _source_dict(scope),
                'line': sentence['line'],
                'raw': sentence['raw'],
                'reason': '重复回放，已忽略',
            })
            return 'duplicate'
        self._seen[duplicate_key] = self._receipts
        self._receipts += 1
        self._source_receipts[scope] = self._source_receipts.get(scope, 0) + 1

        self._expire(scope, station, connection)
        if sentence['total'] == 1:
            self._assembled.append(self._assembled_view(scope, receipt, [receipt], False))
            return 'assembled'
        if sentence['sequence']:
            return self._receive_sequenced(scope, receipt)
        return self._receive_unsequenced(scope, receipt)

    def _receive_sequenced(self, scope, receipt):
        sentence = receipt['sentence']
        key = scope + (sentence['channel'], sentence['formatter'], sentence['sequence'], sentence['total'])
        group = self._pending_sequenced.get(key)
        if group is None:
            self._evict_to_limit()
            group = {'scope': scope, 'key': key, 'parts': {}, 'arrivals': [],
                     'first_receipt': self._source_receipts[scope],
                     'last_receipt': self._source_receipts[scope]}
            self._pending_sequenced[key] = group

        part = sentence['part']
        if part in group['parts']:
            previous = group['parts'][part]['sentence']
            if previous['payload'] == sentence['payload'] and previous['fill'] == sentence['fill']:
                # 同一片重复出现（原文完全相同时已在上层去重）；内容相同仍按重复片处理。
                self._duplicates.append({
                    'source': _source_dict(scope),
                    'line': sentence['line'],
                    'raw': sentence['raw'],
                    'reason': '重复分片，已忽略',
                })
                return 'duplicate'
            self._pending_sequenced.pop(key, None)
            self._unresolved.append(self._unresolved_view(
                group, '同一分片位置出现不同内容，原组未组装',
                extra={'conflicting_part': part,
                       'existing_raw': previous['raw'],
                       'conflicting_raw': sentence['raw']}))
            # 冲突片段本身以新的组继续等待；旧组的内容不会被静默丢弃，
            # 也不会和新消息拼在一起。
            self._evict_to_limit()
            self._pending_sequenced[key] = {
                'scope': scope, 'key': key, 'parts': {part: receipt},
                'arrivals': [part],
                'first_receipt': self._source_receipts[scope],
                'last_receipt': self._source_receipts[scope],
            }
            return 'unresolved'

        group['parts'][part] = receipt
        group['arrivals'].append(part)
        group['last_receipt'] = self._source_receipts[scope]

        if len(group['parts']) == sentence['total']:
            self._pending_sequenced.pop(key, None)
            ordered = [group['parts'][number] for number in range(1, sentence['total'] + 1)]
            reordered = group['arrivals'] != list(range(1, sentence['total'] + 1))
            self._assembled.append(self._assembled_view(scope, receipt, ordered, reordered))
            return 'assembled'
        return 'pending'

    def _receive_unsequenced(self, scope, receipt):
        sentence = receipt['sentence']
        key = scope + (sentence['channel'], sentence['formatter'], sentence['total'])
        group = self._pending_unsequenced.get(key)
        part = sentence['part']

        if group is None:
            if part != 1:
                self._unresolved.append(self._single_unresolved(
                    scope, receipt, '空顺序号且缺少起始片，无法安全配对'))
                return 'unresolved'
            self._evict_to_limit()
            self._pending_unsequenced[key] = {
                'scope': scope, 'key': key, 'parts': {1: receipt}, 'arrivals': [1],
                'expected': 2,
                'first_receipt': self._source_receipts[scope],
                'last_receipt': self._source_receipts[scope],
            }
            return 'pending'

        expected = group['expected']
        if part == expected:
            group['parts'][part] = receipt
            group['arrivals'].append(part)
            group['last_receipt'] = self._source_receipts[scope]
            group['expected'] += 1
            if part == sentence['total']:
                self._pending_unsequenced.pop(key, None)
                ordered = [group['parts'][number] for number in range(1, sentence['total'] + 1)]
                self._assembled.append(self._assembled_view(scope, receipt, ordered, False,
                                                            sequenced=False))
                return 'assembled'
            return 'pending'

        if part < expected:
            previous = group['parts'].get(part)
            if previous is not None and previous['sentence']['payload'] == sentence['payload'] \
                    and previous['sentence']['fill'] == sentence['fill']:
                self._duplicates.append({
                    'source': _source_dict(scope),
                    'line': sentence['line'],
                    'raw': sentence['raw'],
                    'reason': '空顺序号下重复分片，已忽略',
                })
                return 'duplicate'
            self._pending_unsequenced.pop(key, None)
            self._unresolved.append(self._unresolved_view(
                group, '空顺序号下出现同位置不同内容，无法区分同时出现的消息'))
            self._unresolved.append(self._single_unresolved(
                scope, receipt, '空顺序号内容歧义，未参与拼接'))
            return 'unresolved'

        # part > expected：缺片。空顺序号下无法确认后续片段属于同一消息。
        self._pending_unsequenced.pop(key, None)
        self._unresolved.append(self._unresolved_view(
            group, '空顺序号下片段不连续，存在缺片，未组装',
            extra={'missing_parts': list(range(expected, part))}))
        if part == 1:
            self._evict_to_limit()
            self._pending_unsequenced[key] = {
                'scope': scope, 'key': key, 'parts': {1: receipt}, 'arrivals': [1],
                'expected': 2,
                'first_receipt': self._source_receipts[scope],
                'last_receipt': self._source_receipts[scope],
            }
            return 'pending'
        self._unresolved.append(self._single_unresolved(
            scope, receipt, '空顺序号下片段不连续，无法归属已有组'))
        return 'unresolved'

    def _evict_to_limit(self):
        """在放入新组前腾出空间，保证新组本身不会被立刻移出。"""
        while len(self._pending_sequenced) + len(self._pending_unsequenced) >= self.max_groups:
            if not self._evict_oldest():
                break

    def _evict_oldest(self):
        candidates = [group for store in (self._pending_sequenced, self._pending_unsequenced)
                      for group in store.values()]
        if not candidates:
            return False
        oldest = min(candidates, key=lambda group: group['first_receipt'])
        store = (self._pending_sequenced if oldest['key'] in self._pending_sequenced
                 else self._pending_unsequenced)
        self._unresolved.append(self._unresolved_view(
            store.pop(oldest['key']), '挂起组数量超过上限，移出未组装'))
        return True

    def _prune_replay_window(self):
        cutoff = self._receipts - self.replay_window
        for key in [key for key, seen in self._seen.items() if seen < cutoff]:
            del self._seen[key]

    def _expire(self, scope, station, connection):
        self._prune_replay_window()
        # 期限按同一来源连接后续到达的片段数计算；其他岸站的流量不会
        # 让安静来源的半包提前过期。重连（代次变化）也是明确的过期点：
        # 即使没有新片段进来，旧代次的半包也不会跨连接留存。
        current = (station, connection, self._generation_for((station, connection)))
        received = self._source_receipts.get(scope, 0)
        for store in (self._pending_sequenced, self._pending_unsequenced):
            for key in list(store):
                group = store[key]
                if group['scope'] != scope:
                    continue
                if group['scope'] != current or received - group['last_receipt'] > self.stale_after:
                    self._unresolved.append(self._unresolved_view(
                        store.pop(key), '超过等待期限仍未组装，半包已移出'))

    # ---- 组装结果 -----------------------------------------------------

    def _assembled_view(self, scope, last_receipt, ordered, reordered, sequenced=True):
        sentence = last_receipt['sentence']
        parts = [_part_view(receipt) for receipt in ordered]
        payloads = [part['payload'] for part in parts]
        # 身份按来源内的内容出现次数区分：同一来源的不同岸站读取顺序不
        # 影响编号；内容相同、但发生在不同时刻的两次接收是两条观测，
        # 真正的回放已在上层按窗口去重。
        content_key = (scope, sentence['channel'], sentence['formatter'],
                       sentence['sequence'], sentence['total'],
                       tuple((part['part'], part['payload'], part['fill']) for part in parts))
        occurrence = self._content_counts.get(content_key, 0)
        self._content_counts[content_key] = occurrence + 1
        identity = _identity_digest({
            'source': _source_dict(scope), 'channel': sentence['channel'],
            'formatter': sentence['formatter'], 'sequence': sentence['sequence'],
            'total': sentence['total'], 'occurrence': occurrence,
            'parts': [(part['part'], part['payload'], part['fill']) for part in parts],
        })
        return {
            'id': identity,
            'source': _source_dict(scope),
            'channel': sentence['channel'],
            'formatter': sentence['formatter'],
            'sequence': sentence['sequence'],
            'sequenced': sequenced,
            'total': sentence['total'],
            'group_key': _group_key_text(scope, sentence, sequenced),
            'parts': parts,
            'received_parts': [part['part'] for part in parts],
            'reordered': reordered,
            'fill': parts[-1]['fill'],
            'payload': ''.join(payloads),
            'completed_at': last_receipt['arrival'],
            'raw_text': '\n'.join(part['raw'] for part in parts),
            'first_line': parts[0]['line'],
        }

    def _unresolved_view(self, group, reason, extra=None):
        parts = [group['parts'][number] for number in sorted(group['parts'])]
        view = {
            'source': _source_dict(group['scope']),
            'channel': group['key'][3],
            'formatter': group['key'][4],
            'sequence': group['key'][5] if len(group['key']) > 6 else '',
            'total': group['key'][-1],
            'received_parts': [receipt['sentence']['part'] for receipt in parts],
            'received': [_part_view(receipt) for receipt in parts],
            'arrivals': list(group['arrivals']),
            'missing_parts': [number for number in range(1, group['key'][-1] + 1)
                              if number not in group['parts']],
            'reason': reason,
            'detail': _diagnosis(group['parts'].keys(), group['key'][-1], extra),
            'group_key': _group_key_text(group['scope'], {
                'channel': group['key'][3], 'formatter': group['key'][4],
                'sequence': group['key'][5] if len(group['key']) > 6 else '',
                'total': group['key'][-1]}, len(group['key']) > 6),
        }
        if extra:
            view.update(extra)
        return view

    def _single_unresolved(self, scope, receipt, reason):
        sentence = receipt['sentence']
        return {
            'source': _source_dict(scope),
            'channel': sentence['channel'],
            'formatter': sentence['formatter'],
            'sequence': sentence['sequence'],
            'total': sentence['total'],
            'received_parts': [sentence['part']],
            'received': [_part_view(receipt)],
            'arrivals': [sentence['part']],
            'missing_parts': [number for number in range(1, sentence['total'] + 1)
                              if number != sentence['part']],
            'reason': reason,
            'detail': _diagnosis([sentence['part']], sentence['total'], None),
            'group_key': _group_key_text(scope, sentence, bool(sentence['sequence'])),
        }

    # ---- 结束 ---------------------------------------------------------

    def finish(self):
        """结束接收，把仍挂起的半包全部转为显式的未组装记录。"""
        for key in list(self._pending_sequenced):
            self._unresolved.append(self._unresolved_view(
                self._pending_sequenced.pop(key), '输入结束时仍未组装'))
        for key in list(self._pending_unsequenced):
            self._unresolved.append(self._unresolved_view(
                self._pending_unsequenced.pop(key), '输入结束时仍未组装'))
        return {
            'assembled': self._assembled,
            'unresolved': self._unresolved,
            'duplicates': self._duplicates,
            'receipts': self._receipts,
            'pending_groups': 0,
            'sources': [dict(_source_dict(scope), received=count)
                        for scope, count in sorted(self._source_receipts.items())],
        }

    def pending_count(self):
        return len(self._pending_sequenced) + len(self._pending_unsequenced)
