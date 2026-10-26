"""Guard the release boundary against stale sources and failed/untrusted checks."""
import unittest
from promote_gitops import REQUIRED_CHECK, check_state, wait_and_merge

HEAD = 'a' * 40
SOURCE = 'b' * 40


def check(conclusion='success', **changes):
    result = {'id': 1, 'name': REQUIRED_CHECK, 'head_sha': HEAD, 'app': {'slug': 'github-actions'}, 'status': 'completed', 'conclusion': conclusion}
    result.update(changes)
    return result


class FakeGitHub:
    def __init__(self, checks=None, current=True, behind=False):
        self.checks = checks if checks is not None else [check()]
        self.current = current
        self.behind = behind
        self.mutations = []

    def request(self, method, path, data=None):
        if method == 'GET' and path.endswith('/pulls/1'):
            return {'state': 'open', 'base': {'ref': 'weknora-release'}, 'head': {'sha': HEAD}, 'mergeable_state': 'behind' if self.behind else 'clean', 'mergeable': True}
        if method == 'GET' and '/git/ref/' in path:
            return {'object': {'sha': SOURCE if self.current else 'c' * 40}}
        if method == 'GET' and '/check-runs' in path:
            return {'check_runs': self.checks}
        self.mutations.append((method, path, data))
        if path.endswith('/update-branch'):
            self.behind = False
            self.checks = [check('failure')]
            return {}
        return {'merged': True, 'sha': 'merged-source'}


class PromotionTests(unittest.TestCase):
    def run_candidate(self, api):
        return wait_and_merge(api, 'owner/config', 1, 'owner/source', 'dev', SOURCE, timeout=2, pause=lambda _: None)

    def test_only_the_exact_current_github_actions_success_is_accepted(self):
        for invalid in [check(head_sha='old'), check(app={'slug': 'untrusted'}), check(name='another job'), check(status='in_progress')]:
            self.assertEqual(check_state([invalid], HEAD), 'pending')
        self.assertEqual(check_state([check('skipped')], HEAD), 'failure')
        self.assertEqual(check_state([check('failure'), check(id=2)], HEAD), 'success')

    def test_failed_checks_do_not_merge(self):
        api = FakeGitHub(checks=[check('failure')])
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            self.run_candidate(api)
        self.assertFalse(api.mutations)

    def test_newer_source_closes_the_stale_candidate(self):
        api = FakeGitHub(current=False)
        self.assertIsNone(self.run_candidate(api))
        self.assertEqual(api.mutations, [('PATCH', '/repos/owner/config/pulls/1', {'state': 'closed'})])

    def test_merge_uses_a_head_sha_lease_after_validation(self):
        api = FakeGitHub()
        self.assertEqual(self.run_candidate(api), 'merged-source')
        self.assertEqual(api.mutations, [('PUT', '/repos/owner/config/pulls/1/merge', {'sha': HEAD, 'merge_method': 'squash'})])

    def test_updating_base_requires_new_successful_checks(self):
        api = FakeGitHub(behind=True)
        with self.assertRaisesRegex(RuntimeError, 'validation failed'):
            self.run_candidate(api)
        self.assertEqual(len(api.mutations), 1)
        self.assertTrue(api.mutations[0][1].endswith('/update-branch'))


if __name__ == '__main__':
    unittest.main()
