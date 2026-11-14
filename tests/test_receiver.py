import json
import tempfile
import unittest
from pathlib import Path

from tests.synthetic import (encode_type_1, encode_type_5, encode_type_18,
                             encode_type_24a, encode_type_24b, encode_unsupported,
                             fragments, sentence, single, write_sources)
from vessel_observations.fragments import FragmentAssembler
from vessel_observations.core import parse_sentence, read_sentences
from vessel_observations.receiver import receive_files, query_result


def _assembler():
    return FragmentAssembler(stale_after=4)


def _feed(assembler, lines, station='shore-a', connection=1):
    results = []
    for line in lines:
        results.append(assembler.receive(parse_sentence(line), station, connection))
    return results


class CrossStationTests(unittest.TestCase):
    def test_same_sequence_on_two_stations_never_joins(self):
        lines_a = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        lines_b = fragments(encode_type_5(412000002, 'BETA', 'CALL-B'), sequence='7', channel='B')
        assembler = _assembler()
        for line in (lines_a[0], lines_b[0], lines_a[1], lines_b[1]):
            station = 'shore-a' if line in lines_a else 'shore-b'
            assembler.receive(parse_sentence(line), station, 1)
        result = assembler.finish()
        self.assertEqual(len(result['assembled']), 2)
        self.assertEqual(sorted(item['source']['station'] for item in result['assembled']),
                         ['shore-a', 'shore-b'])
        self.assertEqual(result['unresolved'], [])
        self.assertEqual(result['receipts'], 4)

    def test_same_station_different_connection_is_separate(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        assembler = _assembler()
        _feed(assembler, lines[:1], connection=1)
        _feed(assembler, lines[1:], connection=2)
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        self.assertEqual(len(result['unresolved']), 2)
        self.assertEqual({item['source']['connection'] for item in result['unresolved']}, {1, 2})

    def test_fixture_collision_is_not_merged_and_counts_stay_per_source(self):
        with tempfile.TemporaryDirectory() as directory:
            result = receive_files('fixtures/connections.json')
        self.assertEqual(len(result['observations']), 7)
        counts = {(item['station'], item['observations']) for item in result['observation_counts']}
        self.assertEqual(counts, {('shore-a', 3), ('shore-a', 0), ('shore-b', 4)})
        self.assertEqual(len(result['unresolved']), 1)
        self.assertEqual(result['unresolved'][0]['missing_parts'], [2])
        self.assertEqual(result['vessels'], ['412000001', '412000002'])
        # 两个岸站各自的顺序号 7 是独立的组，不能被拼成一条。
        sequences = {(item['source']['station'], item['sequence'], item['total_parts'])
                     for item in result['observations'] if item['message_type'] == 5}
        self.assertEqual(sequences, {('shore-a', '7', 2), ('shore-b', '7', 2)})

    def test_replay_does_not_inflate_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            first = receive_files('fixtures/connections.json')
            root = Path(directory)
            connections = json.loads(Path('fixtures/connections.json').read_text(encoding='utf-8'))
            for entry in connections:
                text = (Path('fixtures') / entry['file']).read_text(encoding='ascii')
                (root / entry['file']).write_text(text + text, encoding='ascii')
            (root / 'connections.json').write_text(
                json.dumps(connections, ensure_ascii=False), encoding='utf-8')
            replayed = receive_files(root / 'connections.json')
        self.assertEqual(len(replayed['observations']), len(first['observations']))
        self.assertEqual(replayed['observation_counts'], first['observation_counts'])
        self.assertEqual(len(replayed['duplicates']), 12)
        self.assertEqual(replayed['duplicate_observations'], [])
        self.assertEqual(replayed['receipts'], 12)


class AssemblyTests(unittest.TestCase):
    def test_reordered_sequenced_parts_are_assembled_and_flagged(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A', bow=100, stern=20,
                                        port=8, starboard=8), sequence='7')
        assembler = _assembler()
        _feed(assembler, [lines[1], lines[0]])
        result = assembler.finish()
        self.assertEqual(len(result['assembled']), 1)
        assembly = result['assembled'][0]
        self.assertTrue(assembly['reordered'])
        self.assertEqual(assembly['received_parts'], [1, 2])
        self.assertEqual(assembly['payload'], ''.join(
            parse_sentence(line)['payload'] for line in lines))

    def test_missing_part_is_unresolved_with_reason(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7',
                          split=[20, 20, 31])
        assembler = _assembler()
        _feed(assembler, [lines[0], lines[2]])
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        self.assertEqual(len(result['unresolved']), 1)
        self.assertEqual(result['unresolved'][0]['missing_parts'], [2])
        self.assertEqual(result['unresolved'][0]['received_parts'], [1, 3])

    def test_far_apart_identical_sentences_are_new_receipts(self):
        lines = single(encode_type_1(412000001, 12.3, 34.5))
        assembler = FragmentAssembler(stale_after=4, replay_window=2)
        _feed(assembler, lines)
        for mmsi in range(412000010, 412000013):
            _feed(assembler, single(encode_type_1(mmsi)))
        _feed(assembler, lines)
        result = assembler.finish()
        self.assertEqual(len(result['duplicates']), 0)
        self.assertEqual(result['receipts'], 5)
        same = [item for item in result['assembled']
                if item['payload'] == parse_sentence(lines[0])['payload']]
        self.assertEqual(len(same), 2)

    def test_duplicate_part_does_not_count(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        assembler = _assembler()
        _feed(assembler, [lines[0], lines[0], lines[1]])
        result = assembler.finish()
        self.assertEqual(len(result['assembled']), 1)
        self.assertEqual(len(result['duplicates']), 1)
        self.assertEqual(result['receipts'], 2)

    def test_conflicting_part_is_unresolved(self):
        lines_a = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        lines_b = fragments(encode_type_5(412000002, 'BETA', 'CALL-B'), sequence='7')
        assembler = _assembler()
        _feed(assembler, [lines_a[0], lines_b[0]])
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        # 冲突组原样保留，冲突片段另起一个组等待后续片，两者都不与对方拼接。
        self.assertEqual(len(result['unresolved']), 2)
        conflict = result['unresolved'][0]
        self.assertEqual(conflict['conflicting_part'], 1)
        self.assertIn('同一分片位置出现不同内容', conflict['reason'])
        self.assertEqual(conflict['missing_parts'], [2])
        restarted = result['unresolved'][1]
        self.assertEqual(restarted['received_parts'], [1])
        self.assertEqual(restarted['missing_parts'], [2])

    def test_different_totals_do_not_share_a_group(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7',
                          split=[20, 20, 31])
        assembler = _assembler()
        _feed(assembler, [lines[0], fragments(encode_type_5(412000002, 'BETA', 'CALL-B'),
                                              sequence='7')[0]])
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        self.assertEqual(len(result['unresolved']), 2)

    def test_unsequenced_contiguous_parts_are_assembled(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='')
        assembler = _assembler()
        _feed(assembler, lines)
        result = assembler.finish()
        self.assertEqual(len(result['assembled']), 1)
        self.assertFalse(result['assembled'][0]['sequenced'])

    def test_unsequenced_out_of_order_second_part_is_not_guessed(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='')
        assembler = _assembler()
        _feed(assembler, [lines[1]])
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        self.assertEqual(len(result['unresolved']), 1)
        self.assertIn('缺少起始片', result['unresolved'][0]['reason'])

    def test_unsequenced_interleaved_messages_are_ambiguous(self):
        lines_a = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='')
        lines_b = fragments(encode_type_5(412000002, 'BETA', 'CALL-B'), sequence='')
        assembler = _assembler()
        _feed(assembler, [lines_a[0], lines_b[0], lines_a[1], lines_b[1]])
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        self.assertGreaterEqual(len(result['unresolved']), 2)
        reasons = [item['reason'] for item in result['unresolved']]
        self.assertTrue(any('不一致' in reason or '歧义' in reason or '不连续' in reason
                            for reason in reasons), reasons)

    def test_unsequenced_missing_part_is_unresolved(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='',
                          split=[20, 20, 31])
        assembler = _assembler()
        _feed(assembler, [lines[0], lines[2]])
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        self.assertGreaterEqual(len(result['unresolved']), 2)

    def test_unsequenced_repeated_part_is_duplicate(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='')
        assembler = _assembler()
        _feed(assembler, [lines[0], lines[0], lines[1]])
        result = assembler.finish()
        self.assertEqual(len(result['assembled']), 1)
        self.assertEqual(len(result['duplicates']), 1)

    def test_channels_do_not_share_a_group(self):
        lines_a = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7', channel='A')
        lines_b = fragments(encode_type_5(412000002, 'BETA', 'CALL-B'), sequence='7', channel='B')
        assembler = _assembler()
        _feed(assembler, [lines_a[0], lines_b[0], lines_a[1], lines_b[1]])
        result = assembler.finish()
        self.assertEqual(len(result['assembled']), 2)


class LifecycleTests(unittest.TestCase):
    def test_reconnect_separates_fragment_identity(self):
        first = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        second = fragments(encode_type_5(412000002, 'BETA', 'CALL-B'), sequence='7')
        assembler = _assembler()
        _feed(assembler, [first[0]], connection=1)
        generation = assembler.reconnect('shore-a', 1)
        self.assertEqual(generation, 1)
        _feed(assembler, [first[1]], connection=1)
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        self.assertEqual(len(result['unresolved']), 2)
        self.assertEqual({item['source']['generation'] for item in result['unresolved']}, {0, 1})
        self.assertNotEqual(result['unresolved'][0]['source']['generation'],
                            result['unresolved'][1]['source']['generation'])

    def test_complete_pair_after_reconnect_assembles_in_new_generation(self):
        pair = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        assembler = _assembler()
        _feed(assembler, [pair[0]], connection=1)
        assembler.reconnect('shore-a', 1)
        _feed(assembler, pair, connection=1)
        result = assembler.finish()
        assembled = [item for item in result['assembled'] if item['sequence'] == '7']
        self.assertEqual(len(assembled), 1)
        self.assertEqual(assembled[0]['source']['generation'], 1)
        self.assertEqual(len(result['unresolved']), 1)
        self.assertEqual(result['unresolved'][0]['source']['generation'], 0)

    def test_stale_half_packet_is_expired(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        assembler = _assembler()
        _feed(assembler, [lines[0]])
        for mmsi in range(412000010, 412000016):
            _feed(assembler, single(encode_type_1(mmsi)))
        _feed(assembler, [lines[1]])
        result = assembler.finish()
        multipart = [item for item in result['assembled'] if item['total'] > 1]
        self.assertEqual(multipart, [])
        self.assertEqual(len(result['unresolved']), 2)
        self.assertTrue(any('超过等待期限' in item['reason'] for item in result['unresolved']))

    def test_stale_after_is_scoped_to_the_same_connection(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        assembler = _assembler()
        _feed(assembler, [lines[0]], station='shore-a', connection=1)
        for mmsi in range(412000010, 412000016):
            _feed(assembler, single(encode_type_1(mmsi)), station='shore-b', connection=1)
        _feed(assembler, [lines[1]], station='shore-a', connection=1)
        result = assembler.finish()
        multipart = [item for item in result['assembled'] if item['total'] > 1]
        self.assertEqual(len(multipart), 1)
        self.assertEqual(len(result['unresolved']), 0)

    def test_close_then_reopen_starts_new_generation_and_flushes(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence='7')
        assembler = _assembler()
        _feed(assembler, [lines[0]], connection=1)
        assembler.close_connection('shore-a', 1)
        _feed(assembler, [lines[1]], connection=1)
        result = assembler.finish()
        self.assertEqual(result['assembled'], [])
        unresolved = result['unresolved']
        self.assertEqual(len(unresolved), 2)
        self.assertEqual({item['source']['generation'] for item in unresolved}, {0, 1})
        self.assertTrue(any('关闭' in item['reason'] for item in unresolved))

    def test_group_limit_bounds_pending_half_packets(self):
        assembler = FragmentAssembler(stale_after=1000, max_groups=2)
        for sequence in ('1', '2', '3', '4'):
            lines = fragments(encode_type_5(412000001, 'ALPHA', 'CALL-A'), sequence=sequence)
            _feed(assembler, [lines[0]])
        self.assertEqual(assembler.pending_count(), 2)
        result = assembler.finish()
        self.assertEqual(len(result['unresolved']), 4)
        self.assertEqual({item['sequence'] for item in result['unresolved']}, {'1', '2', '3', '4'})
        evicted = [item for item in result['unresolved'] if '上限' in item['reason']]
        self.assertEqual({item['sequence'] for item in evicted}, {'1', '2'})
        # 最早的组先被移出，最新的两组仍在等待后续片。
        self.assertEqual({item['sequence'] for item in result['unresolved'] if '结束' in item['reason']},
                         {'3', '4'})


class QueryTests(unittest.TestCase):
    def _receive(self, sources):
        with tempfile.TemporaryDirectory() as directory:
            path = write_sources(directory, sources)
            return receive_files(path)

    def test_field_provenance_and_split_24_static(self):
        lines = (single(encode_type_24a(412000001, 'ALPHA VESSEL'), sequence='1')
                 + single(encode_type_24b(412000001, 'CALL-A', bow=100, stern=20,
                                          port=8, starboard=8), sequence='2'))
        result = self._receive({'shore-a': {'connection': 1, 'lines': lines}})
        view = query_result(result, 412000001)
        self.assertTrue(view['static']['complete'])
        self.assertEqual(view['static']['fields']['name']['value'], 'ALPHA VESSEL')
        self.assertEqual(view['static']['fields']['callsign']['value'], 'CALL-A')
        self.assertEqual(view['static']['fields']['dimensions']['value']['length'], 120)
        self.assertNotEqual(view['static']['fields']['name']['observation_id'],
                            view['static']['fields']['callsign']['observation_id'])
        self.assertEqual(sorted(view['static']['parts_received']), ['24A', '24B'])

    def test_incomplete_static_is_reported(self):
        lines = single(encode_type_24a(412000001, 'ALPHA VESSEL'), sequence='1')
        result = self._receive({'shore-a': {'connection': 1, 'lines': lines}})
        view = query_result(result, 412000001)
        self.assertFalse(view['static']['complete'])
        self.assertEqual(view['static']['missing_fields'], ['callsign', 'dimensions'])
        self.assertEqual(view['static']['fields']['name']['value'], 'ALPHA VESSEL')

    def test_newer_name_does_not_rewrite_older_callsign_provenance(self):
        lines = (single(encode_type_24a(412000001, 'OLD NAME'), sequence='1')
                 + single(encode_type_24b(412000001, 'CALL-A', bow=100, stern=20,
                                          port=8, starboard=8), sequence='2')
                 + single(encode_type_24a(412000001, 'NEW NAME'), sequence='3'))
        result = self._receive({'shore-a': {'connection': 1, 'lines': lines}})
        view = query_result(result, 412000001)
        self.assertEqual(view['static']['fields']['name']['value'], 'NEW NAME')
        self.assertEqual(view['static']['fields']['name']['observation_id'],
                         view['observations'][-1]['observation_id'])
        self.assertEqual(view['static']['fields']['callsign']['observation_id'],
                         view['observations'][1]['observation_id'])

    def test_type_5_static_is_complete_and_cites_assembly(self):
        lines = fragments(encode_type_5(412000001, 'ALPHA VESSEL', 'CALL-A', bow=100,
                                        stern=20, port=8, starboard=8), sequence='7')
        result = self._receive({'shore-a': {'connection': 1, 'lines': lines}})
        view = query_result(result, 412000001)
        self.assertTrue(view['static']['complete'])
        name = view['static']['fields']['name']
        self.assertEqual(name['message_type'], 5)
        self.assertEqual(name['observation_id'], view['static']['latest_static_observation_id'])
        self.assertEqual(name['parts'], [1, 2])

    def test_unavailable_position_is_not_backfilled_as_current(self):
        lines = (single(encode_type_1(412000001, 12.3, 34.5, sog=5.0, cog=45.0))
                 + single(encode_type_1(412000001, 91.0, 181.0, sog=0.0, cog=0.0)))
        result = self._receive({'shore-a': {'connection': 1, 'lines': lines}})
        view = query_result(result, 412000001)
        self.assertFalse(view['position']['available'])
        self.assertIsNone(view['position']['latitude'])
        self.assertIsNone(view['position']['longitude'])
        self.assertEqual(view['position']['observation_id'], view['observations'][-1]['observation_id'])
        self.assertIn('不可用', view['position']['reason'])
        self.assertTrue(view['last_available_position']['available'])
        self.assertEqual(view['last_available_position']['latitude'], 12.3)
        self.assertNotEqual(view['last_available_position']['observation_id'],
                            view['position']['observation_id'])

    def test_class_b_position_report_is_supported(self):
        lines = single(encode_type_18(412000001, 12.3, 34.5, sog=3.0, cog=90.0))
        result = self._receive({'shore-a': {'connection': 1, 'lines': lines}})
        view = query_result(result, 412000001)
        self.assertEqual(view['position']['message_type'], 18)
        self.assertEqual(view['position']['latitude'], 12.3)

    def test_unsupported_type_is_preserved_without_creating_a_vessel(self):
        lines = fragments(encode_unsupported(), sequence='7')
        result = self._receive({'shore-a': {'connection': 1, 'lines': lines}})
        self.assertEqual(result['vessels'], [])
        self.assertEqual(len(result['unprocessed']), 1)
        record = result['unprocessed'][0]
        self.assertFalse(record['supported'])
        self.assertIn('不支持', record['reason'])
        self.assertEqual(record['raw'], '\n'.join(lines))
        self.assertFalse(query_result(result, 412000099)['found'])

    def test_query_unknown_mmsi_is_not_empty_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_sources(directory, {'shore-a': {
                'connection': 1,
                'lines': single(encode_type_1(412000001))}})
            result = receive_files(path)
        view = query_result(result, 499999999)
        self.assertFalse(view['found'])
        self.assertIn('reason', view)

    def test_same_ship_on_two_stations_reports_both_sources(self):
        lines_a = single(encode_type_1(412000001, 12.3, 34.5))
        lines_b = single(encode_type_1(412000001, 12.4, 34.6))
        result = self._receive({
            'shore-a': {'connection': 1, 'lines': lines_a},
            'shore-b': {'connection': 1, 'lines': lines_b},
        })
        view = query_result(result, 412000001)
        stations = {item['station'] for item in view['source_observations']}
        self.assertEqual(stations, {'shore-a', 'shore-b'})


class EnvelopeValidationTests(unittest.TestCase):
    def test_non_last_part_with_fill_is_rejected(self):
        line = '!AIVDM,2,1,7,A,abc,2*00'
        with self.assertRaises(ValueError):
            parse_sentence(line)

    def test_payload_outside_six_bit_alphabet_is_rejected(self):
        body = 'AIVDM,1,1,,A,ab\x88,0'
        checksum = 0
        for character in body:
            checksum ^= ord(character)
        with self.assertRaises(ValueError):
            parse_sentence(f'!{body}*{checksum:02X}')

    def test_non_numeric_sequence_is_rejected(self):
        body = 'AIVDM,1,1,x,A,abc,0'
        checksum = 0
        for character in body:
            checksum ^= ord(character)
        with self.assertRaises(ValueError):
            parse_sentence(f'!{body}*{checksum:02X}')

    def test_out_of_range_part_is_rejected(self):
        body = 'AIVDM,1,2,,A,abc,0'
        checksum = 0
        for character in body:
            checksum ^= ord(character)
        with self.assertRaises(ValueError):
            parse_sentence(f'!{body}*{checksum:02X}')

    def test_fill_on_final_part_is_applied(self):
        payload = '0' * 36
        line = sentence(payload, 2, total=2, part=2, sequence='7')
        first = parse_sentence(line)
        self.assertEqual(first['fill'], 2)
        self.assertEqual(first['payload'], payload)


class SyntheticInterleavingTests(unittest.TestCase):
    """两个岸站逐行交错，验证不会跨站拼接且结果稳定可重复。"""

    def _run(self, interleave=True):
        lines_a = (fragments(encode_type_5(412000001, 'ALPHA VESSEL', 'CALL-A',
                                            bow=100, stern=20, port=8, starboard=8),
                             sequence='7')
                   + single(encode_type_1(412000001, 12.3, 34.5, sog=5.0, cog=45.0))
                   + single(encode_type_24a(412000001, 'ALPHA VESSEL'), sequence='8')
                   + single(encode_type_24b(412000001, 'CALL-A', bow=100, stern=20,
                                            port=8, starboard=8), sequence='9'))
        lines_b = (fragments(encode_type_5(412000002, 'BETA VESSEL', 'CALL-B'),
                             sequence='7')
                   + single(encode_type_1(412000002, -12.3, -34.5, sog=1.0, cog=270.0))
                   + fragments(encode_unsupported(21, 412000099), sequence='7')
                   + single(encode_type_18(412000002, -12.3, -34.5)))
        with tempfile.TemporaryDirectory() as directory:
            path = write_sources(directory, {
                'shore-a': {'connection': 1, 'lines': lines_a},
                'shore-b': {'connection': 1, 'lines': lines_b},
            })
            return receive_files(path, interleave=interleave)

    def test_interleaved_sources_stay_separate(self):
        result = self._run()
        self.assertEqual(result['vessels'], ['412000001', '412000002'])
        self.assertEqual(len(result['unresolved']), 0)
        self.assertEqual(len(result['unprocessed']), 1)
        alpha = query_result(result, 412000001)
        beta = query_result(result, 412000002)
        self.assertEqual(alpha['static']['fields']['name']['value'], 'ALPHA VESSEL')
        self.assertEqual(beta['static']['fields']['name']['value'], 'BETA VESSEL')
        self.assertEqual(alpha['position']['latitude'], 12.3)
        self.assertEqual(beta['position']['latitude'], -12.3)
        self.assertEqual({item['station'] for item in alpha['source_observations']}, {'shore-a'})
        self.assertEqual({item['station'] for item in beta['source_observations']}, {'shore-b'})

    def test_order_of_files_does_not_change_observations(self):
        interleaved = self._run(interleave=True)
        sequential = self._run(interleave=False)
        strip = lambda result: sorted(
            (_normalize(item) for item in result['observations']), key=lambda item: item['observation_id'])
        self.assertEqual(strip(interleaved), strip(sequential))
        self.assertEqual(interleaved['observation_counts'], sequential['observation_counts'])

    def test_24a_and_24b_across_separate_messages_builds_complete_static(self):
        result = self._run()
        alpha = query_result(result, 412000001)
        self.assertTrue(alpha['static']['complete'])
        self.assertEqual(sorted(alpha['static']['parts_received']), ['24A', '24B', '5'])
        self.assertNotEqual(alpha['static']['fields']['name']['observation_id'],
                            alpha['static']['fields']['dimensions']['observation_id'])


def _normalize(observation):
    item = dict(observation)
    item.pop('received_at')
    item['source'] = dict(item['source'])
    return item


class MessageTypeCoverageTests(unittest.TestCase):
    def _receive(self, lines, station='shore-a'):
        with tempfile.TemporaryDirectory() as directory:
            path = write_sources(directory, {station: {'connection': 1, 'lines': lines}})
            return receive_files(path)

    def test_type_2_and_3_position_reports_are_supported(self):
        lines = []
        for message_type, lat, lon in ((2, 11.0, 22.0), (3, 12.0, 23.0)):
            bits = encode_type_1(412000001, lat, lon)
            bits = f'{message_type:06b}' + bits[6:]
            lines.extend(single(bits))
        result = self._receive(lines)
        view = query_result(result, 412000001)
        self.assertEqual(len(view['observations']), 2)
        self.assertEqual(view['position']['latitude'], 12.0)
        self.assertEqual([item['message_type'] for item in view['observations']], [2, 3])

    def test_24b_alone_does_not_claim_name(self):
        lines = single(encode_type_24b(412000001, 'CALL-A', bow=100, stern=20,
                                       port=8, starboard=8), sequence='9')
        result = self._receive(lines)
        view = query_result(result, 412000001)
        self.assertFalse(view['static']['complete'])
        self.assertIn('name', view['static']['missing_fields'])
        self.assertEqual(view['static']['fields']['callsign']['value'], 'CALL-A')

    def test_24a_then_24b_on_different_channels_and_sequences(self):
        lines = (single(encode_type_24a(412000001, 'ALPHA VESSEL'), sequence='1', channel='A')
                 + single(encode_type_24b(412000001, 'CALL-A', bow=100, stern=20,
                                          port=8, starboard=8), sequence='5', channel='B'))
        result = self._receive(lines)
        view = query_result(result, 412000001)
        self.assertTrue(view['static']['complete'])
        self.assertEqual(view['static']['fields']['name']['value'], 'ALPHA VESSEL')
        self.assertEqual(view['static']['fields']['callsign']['value'], 'CALL-A')

    def test_truncated_static_payload_is_preserved_unprocessed(self):
        # 声明总片数为 1，但 24B 的 payload 只有 24 个字符（144 位），
        # 解码时会发现静态资料位长不足。
        payload, fill = 'H68rO0E6CI>000', 0
        line = sentence(payload, fill, total=1, part=1, sequence='')
        result = self._receive([line])
        self.assertEqual(result['vessels'], [])
        self.assertEqual(len(result['unprocessed']), 1)
        self.assertIn('解码失败', result['unprocessed'][0]['reason'])
        self.assertEqual(result['unprocessed'][0]['raw'], line)

    def test_zero_mmsi_is_preserved_unprocessed(self):
        lines = single(encode_type_1(0, 12.3, 34.5))
        result = self._receive(lines)
        self.assertEqual(result['vessels'], [])
        self.assertEqual(len(result['unprocessed']), 1)
        self.assertIn('MMSI', result['unprocessed'][0]['reason'])
