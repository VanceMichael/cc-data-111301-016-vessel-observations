import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def run(*arguments):
    return subprocess.run([sys.executable, '-m', 'vessel_observations', *arguments],
                          capture_output=True, text=True, timeout=15)


class CommandTests(unittest.TestCase):
    def test_preview_and_raw_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            process = run('demo')
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertIn('资料预览', result)
            self.assertGreater(result['原始资料']['bytes'], 0)
            self.assertEqual(len(result['原始资料']['sha256']), 64)

    def test_receive_and_query(self):
        received = run('receive', 'fixtures/connections.json')
        self.assertEqual(received.returncode, 0, received.stderr)
        result = json.loads(received.stdout)
        self.assertEqual(result['vessels'], ['412000001', '412000002'])
        self.assertEqual(len(result['unprocessed']), 1)
        self.assertEqual(len(result['unresolved']), 1)
        self.assertEqual(result['unresolved'][0]['missing_parts'], [2])
        self.assertEqual(sum(item['observations'] for item in result['observation_counts']), 7)
        self.assertIn({'station': 'shore-a', 'connection': 2, 'generation': 0,
                       'observations': 0, 'received': 1}, result['observation_counts'])

        queried = run('query', 'fixtures/connections.json', '412000001')
        self.assertEqual(queried.returncode, 0, queried.stderr)
        view = json.loads(queried.stdout)
        self.assertTrue(view['found'])
        self.assertEqual(view['static']['fields']['name']['value'], 'ALPHA VESSEL')
        self.assertTrue(view['static']['complete'])
        self.assertEqual(sorted(view['static']['parts_received']), ['24A', '5'])
        self.assertEqual(view['position']['latitude'], 22.5)
        self.assertNotEqual(view['static']['fields']['name']['observation_id'],
                            view['static']['fields']['callsign']['observation_id'])

        beta = json.loads(run('query', 'fixtures/connections.json', '412000002').stdout)
        self.assertEqual(beta['static']['fields']['name']['value'], 'BOA VESSEL')
        self.assertEqual(sorted(beta['static']['parts_received']), ['24A', '24B', '5'])
        self.assertEqual(beta['position']['message_type'], 18)

    def test_query_requires_mmsi(self):
        process = run('query', 'fixtures/connections.json')
        self.assertNotEqual(process.returncode, 0)

    def test_unknown_mmsi_is_not_empty_success(self):
        process = run('query', 'fixtures/connections.json', '499999999')
        self.assertEqual(process.returncode, 0, process.stderr)
        view = json.loads(process.stdout)
        self.assertFalse(view['found'])
        self.assertIn('reason', view)
