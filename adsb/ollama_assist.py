"""Coding assistant on a local Ollama model (standard library only).

    export OLLAMA_HOST=http://localhost:11434        # default
    python -m adsb.ollama_assist "TC 5-8 surface position decode を追加して" \
        --file adsb/modes.py --model gpt-oss:20b > suggestion.md

The answer goes to stdout for a human to review; nothing is written to the
repository automatically. From a container use http://host.docker.internal:11434.
"""
import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

DEFAULT_MODEL = os.environ.get('OLLAMA_MODEL', 'gpt-oss:20b')
SYSTEM = ('You are a senior Python engineer working on eNMEA2000, an NMEA0183/AIS and '
          'ADS-B 1090ES (DO-260B) receiver. Follow the existing code style, use only the '
          'standard library and numpy, and return a unified diff or complete functions '
          'with a short Japanese explanation.')


def chat(prompt: str, model: str = DEFAULT_MODEL, files=(), host: str | None = None,
         temperature: float = 0.2, timeout: float = 600) -> str:
    host = (host or os.environ.get('OLLAMA_HOST', 'http://localhost:11434')).rstrip('/')
    if not host.startswith('http'):
        host = 'http://' + host
    context = ''.join(f'\n\n### {f}\n```python\n{Path(f).read_text(encoding="utf8")}\n```' for f in files)
    body = {'model': model, 'stream': False, 'options': {'temperature': temperature},
            'messages': [{'role': 'system', 'content': SYSTEM},
                         {'role': 'user', 'content': prompt + context}]}
    req = urllib.request.Request(host + '/api/chat', json.dumps(body).encode(),
                                 {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)['message']['content']


def main(argv=None):
    p = argparse.ArgumentParser(description='Ask a local Ollama model about this code base')
    p.add_argument('prompt')
    p.add_argument('--file', action='append', default=[], help='attach a source file (repeatable)')
    p.add_argument('--model', default=DEFAULT_MODEL)
    p.add_argument('--host')
    args = p.parse_args(argv)
    try:
        print(chat(args.prompt, args.model, args.file, args.host))
    except OSError as exc:
        sys.exit(f'Ollama not reachable: {exc}')


if __name__ == '__main__':
    main()
