#!/usr/bin/env python3
"""Resolve all public Docker Hub tags to verified content digests; atomic write or failure."""
from __future__ import annotations
import hashlib, json, os, re, sys, urllib.parse, urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ACCEPT=', '.join(['application/vnd.oci.image.index.v1+json','application/vnd.docker.distribution.manifest.list.v2+json',
                  'application/vnd.oci.image.manifest.v1+json','application/vnd.docker.distribution.manifest.v2+json'])

def resolve(image: str) -> str:
    if not image.startswith('docker.io/') or '@' in image:raise ValueError('Only original Docker Hub tag references supported')
    repository,tag=image[len('docker.io/'):].rsplit(':',1)
    url='https://auth.docker.io/token?'+urllib.parse.urlencode({'service':'registry.docker.io','scope':f'repository:{repository}:pull'})
    with urllib.request.urlopen(url,timeout=30) as response:token=json.load(response)['token']
    request=urllib.request.Request(f'https://registry-1.docker.io/v2/{repository}/manifests/{tag}',headers={
        'Authorization':f'Bearer {token}','Accept':ACCEPT})
    with urllib.request.urlopen(request,timeout=40) as response:
        raw=response.read();declared=response.headers.get('Docker-Content-Digest')
    calculated='sha256:'+hashlib.sha256(raw).hexdigest()
    if declared and declared!=calculated:raise ValueError(f'Digest mismatch for {image}')
    manifest=json.loads(raw)
    if 'manifests' in manifest and not any(x.get('platform',{}).get('os')=='linux' and
        x.get('platform',{}).get('architecture')=='amd64' for x in manifest['manifests']):
        raise ValueError(f'No linux/amd64 platform in {image}')
    return image+'@'+calculated

def main():
    originals=json.loads((ROOT/'config/images.json').read_text());locked={}
    for name,image in originals.items():
        locked[name]=resolve(image);print(name,locked[name],flush=True)
    target=ROOT/'config/images.lock.json';temp=target.with_suffix('.json.tmp')
    temp.write_text(json.dumps(locked,indent=2)+'\n');temp.replace(target)
    print('Lock saved. Run make render, make static, then redeploy and accept this exact lock.')
if __name__=='__main__':main()
