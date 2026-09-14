"""Opt-in provider evaluation; never collected by pytest. Uses synthetic data only.

python tests/live_eval.py --base-url URL --model MODEL --output result.json
Each run has its own temporary project and KERN_HOME. No baseline comparison.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import tempfile
import sys
import time


async def evaluate(args, root):
    from kern.client import Client
    from kern.context import ContextManager, receipts
    from kern.engine import Engine
    from kern.journal import Session, create_session
    from kern.memory import MemoryTree

    auxiliary = []

    class ObservedClient(Client):
        async def stream_chat(self, model, messages, **kwargs):
            record = None
            if kwargs.get('system', '').startswith(('Review task completion', 'You create attributed')):
                record = {'system': kwargs['system'], 'text': '', 'errors': []}
                auxiliary.append(record)
            async for event in super().stream_chat(model, messages, **kwargs):
                if record is not None:
                    if event.kind == 'text':
                        record['text'] += event.text
                    elif event.kind == 'error':
                        record['errors'].append(event.error)
                yield event

    client = ObservedClient(timeout=90)
    project = root / 'project'
    project.mkdir()
    os.chdir(project)
    result = {'model': args.model, 'root': str(root), 'turns': [], 'checks': {}, 'auxiliary': auxiliary}
    started = time.monotonic()

    def progress(kind, text):
        if kind in ('tool', 'note'):
            print(kind, str(text)[:300], flush=True)

    def approve(description, *unused, **kwargs):
        operation, _, target = description.partition(' ')
        if operation not in ('write', 'edit'):
            return False
        path = Path(target.splitlines()[0])
        path = (project / path).resolve()
        return path == project / 'receipt.txt'

    async def turn(session, prompt):
        engine = Engine(client, args.model, session, str(project),
                        approve=approve, stream_cb=progress)
        answer = await asyncio.wait_for(engine.chat(prompt, max_steps=12), 240)
        result['turns'].append({'session': session.id, 'answer': answer,
                                'stop': engine.stop_reason, 'requests': engine.requests,
                                'receipts': receipts(session.events)})
        print('TURN', engine.stop_reason, answer[:600], flush=True)
        return engine, answer

    try:
        result['probe'] = await asyncio.wait_for(client.probe(args.model), 180)
        print('PROBE', json.dumps(result['probe']), flush=True)
        if not result['probe']['ok']:
            raise RuntimeError('Provider capability probe failed')
        session = create_session(str(project))
        engine, _ = await turn(session,
            'Synthetic reliability evaluation. Work only in this empty project. '
            'Use todo to track this task. Create receipt.txt exactly once with content '
            '"KERN-CHECK-731\\n" (one trailing newline). Read it back to verify. '
            'Remember this durable project fact using memory: the synthetic delivery '
            'code is ORCHID-731. Mark verified work done and report the result. '
            'Use read/write/todo/memory only; no shell, network, Python or subagents.')
        expected = b'KERN-CHECK-731\n'
        file = project / 'receipt.txt'
        result['checks']['file_exact'] = file.exists() and file.read_bytes() == expected
        original_time = file.stat().st_mtime_ns if file.exists() else None
        end = len(session.events)
        await asyncio.wait_for(ContextManager(engine).fold(session.events[:], 0, end), 120)
        episode = session.events[-1]
        result['checks']['episode_valid_json_contract'] = (
            episode['kind'] == 'episode' and 'summary_error' not in episode['text'])
        result['checks']['episode_source_exists'] = Path(episode['source']).exists()
        resumed = Session(session.id)
        _, answer = await turn(resumed,
            'Continue. Consult the recorded progress and report the delivery code and '
            'the status of receipt.txt. Completed work must not be repeated. '
            'Use read/memory/todo only if needed; do not modify files.')
        writes = [r for r in receipts(resumed.events) if r['name'] in ('write', 'edit')
                  and r['status'] == 'succeeded']
        result['checks']['single_write_after_reopen'] = len(writes) == 1
        result['checks']['file_unchanged_after_reopen'] = (
            file.exists() and file.read_bytes() == expected and file.stat().st_mtime_ns == original_time)
        result['checks']['recall_after_episode'] = 'ORCHID-731' in answer
        result['checks']['plan_complete'] = all(
            item['status'] == 'done' for item in engine.todo) and bool(engine.todo)
        result['checks']['durable_note'] = 'ORCHID-731' in MemoryTree(str(project)).search('ORCHID')
        fresh = create_session(str(project))
        _, answer = await turn(fresh,
            'Using this project\'s persistent memory, retrieve the synthetic delivery '
            'code saved in an earlier session. Do not guess it. Do not change files. '
            'Use memory to find the evidence and report the exact code.')
        result['checks']['fresh_session_recall'] = 'ORCHID-731' in answer
        result['checks']['fresh_session_no_mounts'] = not any(
            event['kind'] == 'mount' for event in fresh.events)
        result['checks']['all_turns_done'] = all(t['stop'] == 'done' for t in result['turns'])
        result['checks']['no_failed_tool_receipts'] = all(
            r['status'] == 'succeeded' for t in result['turns'] for r in t['receipts'])
    except Exception as error:
        result['error'] = f'{type(error).__name__}: {error}'
    finally:
        result['requests'] = client.requests
        result['elapsed_seconds'] = round(time.monotonic() - started, 2)
        result['passed'] = len(result['checks']) == 12 and all(result['checks'].values()) and 'error' not in result
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k not in ('turns', 'auxiliary')}, ensure_ascii=False, indent=2), flush=True)
    return result['passed']


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='kern-live-eval-'))
    os.environ.update(KERN_HOME=str(root / 'state'), KERN_BASE_URL=args.base_url,
                      KERN_LOCAL='1', KERN_SANDBOX='0', KERN_MAX_OUTPUT_TOKENS='4096',
                      KERN_STALL_FIRST='60', KERN_STALL_NEXT='30')
    raise SystemExit(0 if asyncio.run(evaluate(args, root)) else 1)


if __name__ == '__main__':
    main()
