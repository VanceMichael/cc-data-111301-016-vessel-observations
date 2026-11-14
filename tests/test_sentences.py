from pathlib import Path
import tempfile
import unittest
from vessel_observations.core import read_sentences


class SentenceTests(unittest.TestCase):
    def test_multipart_envelope_is_preserved(self):
        rows = list(read_sentences('fixtures/station-a.nmea'))
        self.assertEqual(rows[0]['sequence'], '7')
        self.assertEqual(rows[0]['part'], 1)
        self.assertEqual(rows[1]['part'], 2)
        self.assertNotEqual(rows[0]['payload'], rows[1]['payload'])
        self.assertEqual(rows[0]['raw'], '!AIVDM,2,1,7,A,568rO0@00000<4hjl404i0P61HE=<Dh0000,0*2C')

    def test_corrupt_checksum_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.nmea'
            path.write_text('!AIVDM,1,1,,A,abc,0*00\n')
            with self.assertRaises(ValueError):
                list(read_sentences(path))
