"""Update only the selected personal deployment's three immutable image tags."""
import argparse
import re
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--environment', required=True, choices=('self', 'dev'))
    parser.add_argument('--sha', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.sha):
        parser.error('sha must be a full Git commit SHA')
    target = args.root / 'apps/weknora/environments' / args.environment / 'values.yaml'
    data = yaml.safe_load(target.read_text(encoding='utf-8'))
    for component, image in (('app', 'app'), ('frontend', 'ui'), ('docreader', 'docreader')):
        data[component]['image'] = {
            'repository': f'registry.cn-hangzhou.aliyuncs.com/txuw/weknora-{image}',
            'tag': args.sha,
            'pullPolicy': 'IfNotPresent',
        }
    target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')


if __name__ == '__main__':
    main()
