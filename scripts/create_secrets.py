#!/usr/bin/env python3
"""Generate local credentials once, never print or commit private material."""
from __future__ import annotations
import base64
import json
import os
import secrets
import subprocess
from pathlib import Path
from runtime import command_env

ROOT = Path(__file__).resolve().parents[1]

def get(name: str, ns: str) -> dict | None:
    p = subprocess.run(['kubectl', '-n', ns, 'get', 'secret', name, '--ignore-not-found', '-o', 'json'],
                       check=True, capture_output=True, text=True, cwd=ROOT, env=command_env())
    return json.loads(p.stdout) if p.stdout.strip() else None

def apply(obj: dict) -> None:
    subprocess.run(['kubectl', 'apply', '--server-side', '--field-manager=signal-secrets', '-f', '-'],
                   input=json.dumps(obj), check=True, text=True, stdout=subprocess.DEVNULL,
                   cwd=ROOT, env=command_env())

def encode(data: bytes) -> str:
    return base64.b64encode(data).decode()

def main() -> None:
    os.umask(0o077)
    tls = ROOT / '.state/tls'
    tls.mkdir(parents=True, exist_ok=True)
    existing = get('signal-tls', 'signal')
    if existing:
        if 'ca.crt' not in existing.get('data', {}):
            raise RuntimeError('Existing signal-tls is not a SIGNAL-generated certificate. Refusing replacement.')
        (tls / 'ca.crt').write_bytes(base64.b64decode(existing['data']['ca.crt']))
        (tls / 'server.crt').write_bytes(base64.b64decode(existing['data']['tls.crt']))
        subprocess.run(['openssl', 'x509', '-checkend', '604800', '-noout', '-in', str(tls / 'server.crt')],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        def openssl(*args: str) -> None:
            subprocess.run(['openssl', *args], cwd=tls, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        openssl('req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '365', '-sha256',
                '-subj', '/CN=SIGNAL Local Demo CA', '-keyout', 'ca.key', '-out', 'ca.crt',
                '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign')
        openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=signal.local',
                '-keyout', 'server.key', '-out', 'server.csr')
        (tls / 'server.ext').write_text('subjectAltName=DNS:signal.local,IP:127.0.0.1\n'
                                      'basicConstraints=critical,CA:FALSE\n'
                                      'keyUsage=critical,digitalSignature,keyEncipherment\n'
                                      'extendedKeyUsage=serverAuth\n')
        openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key', '-CAcreateserial',
                '-days', '90', '-sha256', '-extfile', 'server.ext', '-out', 'server.crt')
        apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'signal-tls', 'namespace': 'signal'},
               'type': 'kubernetes.io/tls', 'data': {
                   'tls.crt': encode((tls / 'server.crt').read_bytes()),
                   'tls.key': encode((tls / 'server.key').read_bytes()),
                   'ca.crt': encode((tls / 'ca.crt').read_bytes())}})
    if not get('grafana-admin', 'signal-observe'):
        apply({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 'grafana-admin', 'namespace': 'signal-observe'},
               'type': 'Opaque', 'data': {'password': encode(secrets.token_urlsafe(32).encode())}})
    print('TLS trust anchor and Grafana credential are ready; secrets were not printed.')

if __name__ == '__main__':
    main()
