from pathlib import Path
import tempfile
import unittest
from vessel_observations.core import read_sentences


class SentenceTests(unittest.TestCase):
    def test_multipart_envelope_is_preserved(self):
        rows=list(read_sentences('fixtures/station-a.nmea'))
        self.assertEqual(rows[1]['sequence'],'7')
        self.assertEqual(rows[1]['part'],1)
        self.assertEqual(rows[2]['part'],2)
        self.assertNotEqual(rows[1]['payload'],rows[2]['payload'])

    def test_corrupt_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bad.nmea'
            path.write_text('!AIVDM,1,1,,A,abc,0*00\n')
            with self.assertRaises(ValueError):
                list(read_sentences(path))
