"""Carry terminal task records into a distinct run, with explicit retry selection."""
import json
from pathlib import Path


def continue_results(previous: Path | None, retry_tasks: str, expected: dict):
    retry = set(filter(None, retry_tasks.split(',')))
    if previous is None:
        if retry:
            raise ValueError('--retry-tasks requires --continue-from')
        return [], None
    previous = previous.resolve(strict=True)
    old = json.loads((previous / 'manifest.json').read_text())
    for key in ['benchmark', 'sourceRevision', 'modelBindings', 'suite', 'split', 'mcpExposure', 'taskProtocol']:
        if old.get(key) != expected.get(key):
            raise ValueError(f'Continuation changes {key}; use a separate experiment')
    selected = set(expected['tasks'])
    rows = {row['task']: row for row in old['results'] if row['task'] in selected}
    if len(rows) != len([row for row in old['results'] if row['task'] in selected]):
        raise ValueError('Previous run contains duplicate task results')
    if retry - rows.keys():
        raise ValueError(f'Retry tasks do not have prior terminal records: {sorted(retry - rows.keys())}')
    retained = [{**row, 'retainedFrom': row.get('retainedFrom', str(previous))}
                for task, row in rows.items() if task not in retry]
    provenance = {'from': str(previous), 'retryTasks': sorted(retry),
                  'retainedTasks': [row['task'] for row in retained],
                  'semantics': 'Terminal results retained including genuine task failures; only explicitly selected tasks retry; original attempts remain in source run'}
    return retained, provenance
