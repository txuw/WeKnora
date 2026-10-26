"""Publish GitOps through a checked PR; never push the Argo-tracked branch."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml

REQUIRED_CHECK = 'WeKnora GitOps validation'


class GitHubError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(f'GitHub HTTP {status}: {message}')


class GitHub:
    def __init__(self, token):
        if not token:
            raise RuntimeError('The GitOps publishing token is missing')
        self.token = token

    def request(self, method, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request('https://api.github.com' + path, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28', 'Content-Type': 'application/json',
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            # Do not include headers, credentials or complete response bodies.
            raise GitHubError(error.code, 'GitOps request rejected') from None


def check_state(runs, head):
    matching = [r for r in runs if r.get('name') == REQUIRED_CHECK and r.get('head_sha') == head and r.get('app', {}).get('slug') == 'github-actions']
    if not matching:
        return 'pending'
    latest = max(matching, key=lambda r: r['id'])
    if latest['status'] != 'completed':
        return 'pending'
    return 'success' if latest.get('conclusion') == 'success' else 'failure'


def source_is_current(api, repository, environment, source_sha):
    ref = api.request('GET', f'/repos/{repository}/git/ref/heads/{environment}')
    return ref['object']['sha'] == source_sha


def wait_and_merge(api, repository, number, source_repository, environment, source_sha, timeout=480, pause=time.sleep, base_branch='weknora-release'):
    prefix = f'/repos/{repository}'
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pr = api.request('GET', prefix + f'/pulls/{number}')
        if pr.get('merged'):
            return pr['merge_commit_sha']
        if pr['state'] != 'open':
            raise RuntimeError('The GitOps release PR was closed without merging')
        if pr.get('base', {}).get('ref') != base_branch:
            raise RuntimeError('The release PR targets an unexpected branch')
        if not source_is_current(api, source_repository, environment, source_sha):
            api.request('PATCH', prefix + f'/pulls/{number}', {'state': 'closed'})
            print('A newer source revision exists; the stale GitOps PR was closed.', flush=True)
            return None
        head = pr['head']['sha']
        runs = api.request('GET', prefix + f'/commits/{head}/check-runs?per_page=100')['check_runs']
        state = check_state(runs, head)
        if state == 'failure':
            raise RuntimeError('GitOps validation failed; the PR remains unmerged')
        if state == 'success':
            if pr.get('mergeable_state') == 'behind':
                api.request('PUT', prefix + f'/pulls/{number}/update-branch', {'expected_head_sha': head})
            elif pr.get('mergeable_state') in ['dirty', 'blocked', 'draft']:
                raise RuntimeError('GitHub branch protection or a conflict blocks this GitOps PR')
            elif pr.get('mergeable') is not None:
                # The cluster independently waits for successful push CI before
                # advancing Argo. This lease rejects a changed PR candidate.
                try:
                    merged = api.request('PUT', prefix + f'/pulls/{number}/merge', {'sha': head, 'merge_method': 'squash'})
                except GitHubError as error:
                    if error.status in [405, 409]:
                        pause(10)
                        continue
                    raise
                if merged.get('merged'):
                    return merged['sha']
                raise RuntimeError('GitHub did not merge the validated release PR')
        pause(10)
    raise TimeoutError('GitOps checks did not complete; the PR remains unmerged')


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--environment', choices=['dev', 'self'], required=True)
    parser.add_argument('--sha', required=True)
    parser.add_argument('--deployment-repo', default='txuw/k3s-apps-config')
    parser.add_argument('--source-repo', default='txuw/WeKnora')
    parser.add_argument('--base-branch', default='weknora-release', choices=['weknora-release'])
    args = parser.parse_args()
    assert re.fullmatch(r'[a-f0-9]{40}', args.sha)
    for repository in [args.deployment_repo, args.source_repo]:
        assert re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
    api = GitHub(os.environ.get('GH_TOKEN'))
    if not source_is_current(api, args.source_repo, args.environment, args.sha):
        print('A newer source revision exists; skipping this promotion.')
        return
    relative = f'apps/weknora/environments/{args.environment}/values.yaml'
    values = yaml.safe_load((args.root / relative).read_text(encoding='utf-8'))
    assert all(values[c]['image']['tag'] == args.sha and re.fullmatch(r'sha256:[a-f0-9]{64}', values[c]['image']['digest']) for c in ['app', 'frontend', 'docreader']), 'All candidate images must match the validated source revision'
    changes = git(args.root, 'diff', '--name-only').splitlines()
    if not changes:
        print('The environment already has these image digests; no release PR is needed.')
        return
    assert changes == [relative] and not git(args.root, 'diff', '--cached', '--name-only'), 'Only this environment values file may be published'
    run_id = os.environ.get('GITHUB_RUN_ID', str(int(time.time())))
    attempt = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    assert run_id.isdigit() and attempt.isdigit()
    branch = f'codex/weknora-{args.environment}-{args.sha[:12]}-{run_id}-{attempt}'
    git(args.root, 'switch', '-c', branch)
    git(args.root, 'config', 'user.name', 'WeKnora Release Bot')
    git(args.root, 'config', 'user.email', 'weknora-release@users.noreply.github.com')
    git(args.root, 'add', relative)
    git(args.root, 'commit', '-m', f'Deploy WeKnora {args.environment} at {args.sha}')
    git(args.root, 'push', 'origin', f'HEAD:refs/heads/{branch}')
    pr = api.request('POST', f'/repos/{args.deployment_repo}/pulls', {
        'base': args.base_branch, 'head': branch,
        'title': f'Deploy WeKnora {args.environment} at {args.sha[:12]}',
        'body': f'Update App, frontend and DocReader together with verified ACR digests.\n\nEnvironment: `{args.environment}`\nSource: `{args.source_repo}@{args.sha}`\n\nThe release waits for `{REQUIRED_CHECK}` and merges only while the source branch is still current.',
    })
    print('GitOps candidate: ' + pr['html_url'], flush=True)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write(f"GitOps candidate: [{pr['number']}]({pr['html_url']})\n")
    merged = wait_and_merge(api, args.deployment_repo, pr['number'], args.source_repo, args.environment, args.sha)
    if merged:
        print('Validated GitOps PR merged at ' + merged, flush=True)
        if summary:
            with open(summary, 'a', encoding='utf-8') as stream:
                stream.write(f'Validated GitOps merge: `{merged}`\n')


if __name__ == '__main__':
    main()
