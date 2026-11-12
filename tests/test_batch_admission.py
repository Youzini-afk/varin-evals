import argparse
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from varin_evals import appworld


class BatchAdmissionTests(unittest.TestCase):
    def test_execution_failure_finishes_other_inflight_world_without_accepting_more(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prompt = root / 'source/experiments/prompts/function_calling_agent/instructions.txt'
            prompt.parent.mkdir(parents=True)
            prompt.write_text('# Key Instructions:\nOriginal task protocol\nNext, I will show you')
            runtime, data = root / 'runtime', root / 'data'
            runtime.mkdir()
            data.mkdir()
            output = root / 'output'
            arguments = argparse.Namespace(source=root / 'source', data_root=data, runtime=runtime,
                appworld_python=root / 'python', config=root / 'config.json', private_provider=None,
                private_aux_provider=None, output=output, split='dev', tasks='all', experiment='fixture',
                continue_from=None, retry_tasks='', workers=2, mcp_exposure='deferred')
            in_flight = threading.Barrier(2)
            failed_world_closed = threading.Event()
            calls, started = [], []

            def control(base, route, body):
                calls.append((route, body['task_id']))
                if route == '/initialize':
                    return {'output': {'instruction': 'Task', 'supervisor': {
                        'first_name': 'A', 'last_name': 'B', 'email': 'a@example.test', 'phone_number': '123'}}}
                if route == '/evaluate':
                    return {'output': {'success': False}}
                if route == '/close' and body['task_id'] == 'a':
                    failed_world_closed.set()
                return {}

            def execute(command, **kwargs):
                task = Path(command[command.index('--output') + 1]).parent.name
                started.append(task)
                in_flight.wait(timeout=5)
                if task == 'b':
                    self.assertTrue(failed_world_closed.wait(timeout=5))
                return argparse.Namespace(returncode=1 if task == 'a' else 0)

            server = MagicMock(pid=12345, returncode=0)
            server.poll.return_value = None
            with patch.object(appworld, 'load_configuration', return_value=({'model': {}}, {})), \
                 patch.object(appworld, 'source_revision', return_value='fixture'), \
                 patch.object(appworld, 'sdk_json', return_value=['a', 'b', 'c', 'd']) as sdk, \
                 patch.object(appworld, 'free_port', side_effect=range(12000, 12006)), \
                 patch.object(appworld, 'urlopen'), patch.object(appworld.socket, 'create_connection'), \
                 patch.object(appworld, 'control', side_effect=control), \
                 patch.object(appworld.subprocess, 'Popen', return_value=server), \
                 patch.object(appworld.subprocess, 'run', side_effect=execute), \
                 patch.object(appworld.os, 'killpg'):
                with self.assertRaisesRegex(RuntimeError, 'in-flight tasks finalized'):
                    appworld.main(arguments)
            self.assertCountEqual(started, ['a', 'b'])
            for task in ['a', 'b']:
                for route in ['/save', '/evaluate', '/close']:
                    self.assertIn((route, task), calls)
            rows = json.loads((output / 'manifest.json').read_text())['results']
            self.assertEqual({r['task']: r['agentCompleted'] for r in rows}, {'a': False, 'b': True})
            self.assertFalse(any(r['verifierPassed'] for r in rows))
            self.assertFalse((output / 'scores.json').exists())
            self.assertEqual(sdk.call_count, 1)
