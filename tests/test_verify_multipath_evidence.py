"""The independent verifier rejects damaged evidence without printing content."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import verify_multipath_evidence as verifier


class EvidenceVerifierTests(unittest.TestCase):
    def test_missing_input_returns_fixed_error_without_private_path(self):
        result=subprocess.run([sys.executable,verifier.__file__,'--data-dir','missing-private-location'],
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,2)
        self.assertEqual(json.loads(result.stdout)['error'],'unreadable_evidence')
        self.assertNotIn('missing-private-location',result.stdout)
        self.assertEqual(result.stderr,'')

    def test_invalid_workload_rejected_before_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'report.json').write_text(json.dumps({'workload':{'chunks_per_direction':True,'bytes_per_chunk':4096}}))
            (root/'attempts.json').write_text('{}')
            with patch.object(verifier.sqlite3,'connect') as connect:
                with self.assertRaisesRegex(verifier.InvalidEvidence,'workload_bounds'):
                    verifier.verify(root)
                connect.assert_not_called()

    def test_database_opened_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'report.json').write_text(json.dumps({'workload':{'chunks_per_direction':1,'bytes_per_chunk':4096},
                'policy':'adaptive','scenario':'standard','phase_events':[]}))
            (root/'attempts.json').write_text('{}')
            with patch.object(verifier.sqlite3,'connect',side_effect=RuntimeError('stop')) as connect:
                with self.assertRaises(RuntimeError): verifier.verify(root)
                self.assertTrue(connect.call_args.args[0].endswith('?mode=ro'))
                self.assertEqual(connect.call_args.kwargs,{'uri':True})


if __name__=='__main__': unittest.main()
