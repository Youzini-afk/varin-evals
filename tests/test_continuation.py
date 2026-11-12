import json
from pathlib import Path
import tempfile
import unittest

from varin_evals.continuation import continue_results


class ContinuationTests(unittest.TestCase):
    def test_only_explicit_retries_are_removed_and_source_stays_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            expected = {'benchmark': 'fixture', 'sourceRevision': 'pinned', 'modelBindings': {'primary': 'same'},
                        'tasks': ['pass', 'genuine-fail', 'infra-fail', 'pending']}
            old = {**expected, 'results': [
                {'task': 'pass', 'agentCompleted': True, 'verifierPassed': True},
                {'task': 'genuine-fail', 'agentCompleted': True, 'verifierPassed': False},
                {'task': 'infra-fail', 'agentCompleted': False, 'verifierPassed': False}]}
            path = directory / 'manifest.json'
            path.write_text(json.dumps(old))
            before = path.read_bytes()
            rows, provenance = continue_results(directory, 'infra-fail', expected)
            self.assertEqual([row['task'] for row in rows], ['pass', 'genuine-fail'])
            self.assertFalse(rows[1]['verifierPassed'])
            self.assertEqual(provenance['retryTasks'], ['infra-fail'])
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaisesRegex(ValueError, 'sourceRevision'):
                continue_results(directory, '', {**expected, 'sourceRevision': 'different'})
            with self.assertRaisesRegex(ValueError, 'prior terminal'):
                continue_results(directory, 'pending', expected)
