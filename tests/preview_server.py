"""Deterministic, isolated browser QA server. No provider calls or real project edits.

Run from the repository: python tests/preview_server.py
"""
import asyncio
import os
from pathlib import Path
import tempfile

root = Path(tempfile.mkdtemp(prefix='kern-web-qa-'))
os.environ['KERN_HOME'] = str(root / 'state')
os.environ['KERN_MODEL'] = 'qa-scripted'
os.environ['KERN_SERVE_PORT'] = '8976'
os.chdir(root)

from kern import daemon
from kern.client import StreamEvent
from kern.web import run_server


class ScriptedModel:
    requests = 0
    async def probe(self, model):
        pass
    async def list_models(self):
        return [{'id':'qa-scripted'}]
    async def stream_chat(self, model, messages, **kwargs):
        self.requests += 1
        phase = (self.requests - 1) % 4
        if phase == 0:
            yield StreamEvent('tool_call', tool_call={'id':'qa','name':'todo','arguments':{'items':[
                {'text':'Écrire le fichier de contrôle','status':'active'},
                {'text':'Présenter le résultat','status':'pending'}]}})
        elif phase == 1:
            yield StreamEvent('tool_call', tool_call={'id':'qa','name':'write','arguments':{'path':'qa.txt','content':'Contrôle réussi 🐾\n'}})
        elif phase == 2:
            yield StreamEvent('tool_call', tool_call={'id':'qa','name':'todo','arguments':{'items':[
                {'text':'Écrire le fichier de contrôle','status':'done'},
                {'text':'Présenter le résultat','status':'done'}]}})
        else:
            for part in ['## Résultat de contrôle\n\n', '**Fichier créé** dans le dossier temporaire.\n\n',
                         '- Unicode préservé\n- Reçu disponible\n\n', '```python\nprint("Bonjour")\n```\n\n',
                         'Le travail est terminé. <script>alert("texte inerte")</script>']:
                yield StreamEvent('text', text=part)
                await asyncio.sleep(.1)


if __name__ == '__main__':
    daemon.Client = ScriptedModel
    print(f'QA workspace: {root}', flush=True)
    asyncio.run(run_server())
