"""Publish one environment with the three authenticated registry manifest digests."""
import argparse
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml


REGISTRY = 'registry.cn-hangzhou.aliyuncs.com'


def resolve_digest(repository, sha):
    url = f'https://{REGISTRY}/v2/{repository}/manifests/{sha}'
    headers = {'Accept': 'application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json'}
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise RuntimeError(f'Manifest lookup failed for {repository}: HTTP {error.code}') from None
        challenge = dict(re.findall(r'(\w+)="([^"]+)"', error.headers.get('WWW-Authenticate', '')))
        if challenge.get('realm') != 'https://dockerauth.cn-hangzhou.aliyuncs.com/auth':
            raise RuntimeError('Unexpected registry authentication endpoint')
        credentials = base64.b64encode((os.environ['ACR_USERNAME'] + ':' + os.environ['ACR_PASSWORD']).encode()).decode()
        query = urllib.parse.urlencode({'service': challenge['service'], 'scope': f'repository:{repository}:pull'})
        request = urllib.request.Request(challenge['realm'] + '?' + query, headers={'Authorization': 'Basic ' + credentials})
        with urllib.request.urlopen(request, timeout=30) as token_response:
            token_data = json.load(token_response)
        headers['Authorization'] = 'Bearer ' + (token_data.get('token') or token_data['access_token'])
        response = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
    with response:
        payload = response.read()
        digest = response.headers.get('Docker-Content-Digest') or 'sha256:' + hashlib.sha256(payload).hexdigest()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
        raise RuntimeError('Invalid manifest digest')
    return digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--environment', required=True, choices=('self', 'dev'))
    parser.add_argument('--sha', required=True)
    parser.add_argument('--resolve-digests', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.sha):
        parser.error('sha must be a full Git commit SHA')
    target = args.root / 'apps/weknora/environments' / args.environment / 'values.yaml'
    data = yaml.safe_load(target.read_text(encoding='utf-8'))
    for component, image in (('app', 'app'), ('frontend', 'ui'), ('docreader', 'docreader')):
        previous = data[component].get('image', {})
        digest = resolve_digest(f'txuw/weknora-{image}', args.sha) if args.resolve_digests else previous.get('digest') if previous.get('tag') == args.sha else None
        if not digest or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest):
            raise RuntimeError('A verified digest is required for ' + component)
        data[component]['image'] = {
            'repository': f'registry.cn-hangzhou.aliyuncs.com/txuw/weknora-{image}',
            'tag': args.sha,
            'digest': digest,
            'pullPolicy': 'IfNotPresent',
        }
    target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')


if __name__ == '__main__':
    main()
