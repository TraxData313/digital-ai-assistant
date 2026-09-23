"""Explicit opt-in desktop test; creates ONE visible task and one follow-up.

Only run after the owner authorizes creation of the test task. Uses a
temporary supervision store and never starts or modifies the assistant's
running room.
"""
import argparse
import json
from pathlib import Path
import tempfile
import time
from . import home, overmind as om


def wait_for_reply(marker):
    until = time.monotonic() + 180
    while time.monotonic() < until:
        wake = om.due()
        if wake:
            messages = [item.get('text') for event in wake['events']
                        for turn in (event.get('detail') or {}).get('conversation', {}).get('turns', [])
                        for item in turn.get('items', [])
                        if item.get('type') == 'agentMessage' and item.get('phase') == 'final_answer']
            if marker not in messages:
                raise RuntimeError('Reply event arrived without the expected assistant reply text: ' + json.dumps(wake))
            om.acknowledge(wake, True)
            return
        time.sleep(1)
    raise RuntimeError('The test task has not completed within three minutes; inspect it in Codex.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--controller', required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='assistant-desktop-probe-') as folder:
        om.STATE = Path(folder) / 'overmind.json'
        om.configure({'action': 'connect', 'controller': args.controller})
        result = om.execute({'op': 'start', 'title': home.NAME + ' supervision test',
            'prompt': home.OWNER_NAME + ' authorized this desktop supervision test. Do not use tools or change files. '
                      'Reply exactly ASSISTANT_DESKTOP_STARTED.'})
        print(json.dumps({'created': result}), flush=True)
        task = next(iter(om.snapshot()['tasks'].values()))
        wait_for_reply('ASSISTANT_DESKTOP_STARTED')
        print('PASS: task completion woke the adapter with the reply text.', flush=True)
        om.execute({'op': 'send', 'thread': task['thread'], 'host': task['host'],
                    'prompt': 'Second part of the same authorized test. Do not use tools or change files. '
                              'Reply exactly ASSISTANT_DESKTOP_FOLLOWUP.'})
        print('Follow-up sent to the same visible task.', flush=True)
        wait_for_reply('ASSISTANT_DESKTOP_FOLLOWUP')
        print('PASS: follow-up completion returned its reply text.', flush=True)
        om.execute({'op': 'release', 'thread': task['thread'], 'host': task['host']})
        print('PASS: supervision released; the test task remains visible for ' + home.OWNER_NAME + '.', flush=True)


if __name__ == '__main__':
    main()
