"""Isolated, stdlib-only checks: no model calls or real Hermes state."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

SOURCE = Path(__file__).resolve().parents[1] / 'api' / 'execution_policy.py'
if not SOURCE.exists():
    SOURCE = Path(__file__).with_name('execution_policy.py')
spec = importlib.util.spec_from_file_location('business_policy_under_test', SOURCE)
policy = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = policy
spec.loader.exec_module(policy)


class BusinessPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executed = 0

    def runtime(self, **config):
        return policy.BusinessExecution.from_config({'enabled': True, **config}, workspace=self.root)

    def run_call(self, runtime, call_id='one', args=None, result='unchanged', execute=None, name='generate', **kwargs):
        def actual():
            self.executed += 1
            return result
        return runtime.execute(name, {} if args is None else args, execute=execute or actual,
                               call_id=call_id, cwd=self.root, on_block=lambda body: json.dumps(body), **kwargs)

    def test_default_is_disabled(self):
        self.assertIsNone(policy.BusinessExecution.from_config(None, workspace=self.root))
        self.assertIsNone(policy.BusinessExecution.from_config({'enabled': False}, workspace=self.root))

    def test_mcp_error_envelope_is_preserved(self):
        result = {'isError': True, 'structuredContent': {'output_path': 'report.pdf'}}
        self.assertTrue(policy.result_is_error(result))
        self.assertTrue(policy.result_is_error(json.dumps(result)))
        self.assertFalse(policy.result_is_error({'structuredContent': {'output_path': 'report.pdf'}}))

    def test_unconfigured_tool_result_is_unchanged(self):
        runtime = self.runtime()
        self.assertEqual('unchanged', self.run_call(runtime))
        self.assertEqual(1, self.executed)
        self.assertEqual('not_configured', runtime.snapshot('one')['verification'])

    def test_bad_configuration_is_rejected(self):
        for config in ({'max_retries': 6}, {'max_retries': True}, {'allowed_tools': 'all'}, {'tools': {'generate': {'retry_safe': 'yes'}}}):
            with self.subTest(config=config), self.assertRaises((ValueError, TypeError)):
                self.runtime(**config)

    def test_managed_execution_is_not_reported_as_protected(self):
        with self.assertRaises(ValueError):
            policy.BusinessExecution.from_config({'enabled': True}, workspace=self.root, api_mode='codex_app_server')
        self.assertIsNone(policy.BusinessExecution.from_config(None, workspace=self.root, api_mode='codex_app_server'))

    def test_enabled_rules_require_an_authoritative_workspace(self):
        for workspace in ('', 'relative', self.root/'missing'):
            with self.subTest(workspace=workspace), self.assertRaises(ValueError):
                policy.BusinessExecution.from_config({'enabled': True}, workspace=workspace)

    def test_tool_and_profile_denial_never_dispatch(self):
        for config in ({'allowed_tools': []}, {'denied_tools': ['generate']}, {'allowed_profiles': ['another']}):
            runtime = self.runtime(**config)
            self.assertEqual('business_policy_blocked', json.loads(self.run_call(runtime))['error'])
        self.assertEqual(0, self.executed)

    def test_required_type_and_workspace_checks_never_dispatch(self):
        runtime = self.runtime(tools={'generate': {'required_args': ['path'], 'path_args': ['path'], 'argument_types': {'count': 'integer'}}})
        for index, args in enumerate(({}, {'path': '../outside'}, {'path': 'file', 'count': True})):
            self.run_call(runtime, str(index), args=args)
        self.assertEqual(0, self.executed)

    def test_symlink_outside_workspace_is_refused(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root/'link').symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Host does not permit symlinks')
            runtime = self.runtime(tools={'generate': {'path_args': ['path']}})
            self.run_call(runtime, args={'path': 'link/file'})
            self.assertEqual(0, self.executed)

    def test_retry_budget_allows_initial_plus_two_retries(self):
        runtime = self.runtime(max_retries=2, tools={'generate': {'required_result_fields': ['output_path'], 'retry_safe': True}})
        for i in range(4):
            self.run_call(runtime, str(i), result='{}')
        self.assertEqual(3, self.executed)
        self.assertEqual([0, 1, 2], [runtime.snapshot(str(i))['retry_count'] for i in range(3)])
        self.assertFalse(runtime.snapshot('2')['retryable'])
        self.assertEqual('blocked', runtime.snapshot('3')['permission'])

    def test_unsafe_operation_is_not_replayed(self):
        runtime = self.runtime(tools={'generate': {'required_result_fields': ['output_path']}})
        self.run_call(runtime, 'one', result='{}')
        self.run_call(runtime, 'two', result='{}')
        self.assertEqual(1, self.executed)
        self.assertFalse(runtime.snapshot('one')['retryable'])

    def test_office_artifact_is_checked_and_original_result_preserved(self):
        artifact = self.root/'report.docx'
        with zipfile.ZipFile(artifact, 'w') as archive:
            archive.writestr('word/document.xml', '<document/>')
        runtime = self.runtime(tools={'generate': {'artifact_fields': ['output_path']}})
        original = json.dumps({'output_path': str(artifact)})
        self.assertEqual(original, self.run_call(runtime, result=original))
        self.assertEqual('passed', runtime.snapshot('one')['verification'])

    def test_empty_or_invalid_artifact_fails(self):
        (self.root/'report.pdf').write_text('invalid PDF')
        runtime = self.runtime(tools={'generate': {'artifact_fields': ['output_path'], 'retry_safe': True}})
        result = self.run_call(runtime, result=json.dumps({'output_path': 'report.pdf'}))
        self.assertEqual('business_validation_failed', json.loads(result)['error'])
        self.assertIn('report.pdf', json.loads(result)['tool_result_preview'])
        self.assertEqual(['output_path'], json.loads(result)['expected_fields'])
        self.assertEqual('failed', runtime.snapshot('one')['verification'])

    def test_call_budget_and_deadline_prevent_more_dispatch(self):
        runtime = self.runtime(max_tool_calls=1)
        self.run_call(runtime, 'one')
        self.run_call(runtime, 'two')
        self.assertEqual(1, self.executed)
        runtime = self.runtime(time_budget_seconds=1)
        with patch.object(policy.time, 'monotonic', return_value=runtime._started+2):
            self.run_call(runtime, 'expired')
        self.assertEqual(1, self.executed)

    def test_parallel_limit_is_released_after_completion(self):
        runtime = self.runtime(max_concurrent_calls=1)
        entered, release = threading.Event(), threading.Event()
        def waiting():
            entered.set()
            release.wait(3)
            return 'ok'
        thread = threading.Thread(target=lambda: self.run_call(runtime, 'first', execute=waiting))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual('business_policy_blocked', json.loads(self.run_call(runtime, 'second'))['error'])
        finally:
            release.set()
            thread.join(3)
        self.run_call(runtime, 'third')
        self.assertEqual(1, self.executed)
        self.assertEqual(0, runtime._active)

    def test_exception_releases_active_slot(self):
        runtime = self.runtime(max_concurrent_calls=1)
        def fail():
            raise RuntimeError('synthetic failure')
        with self.assertRaises(RuntimeError):
            self.run_call(runtime, execute=fail)
        self.run_call(runtime, 'next')
        self.assertEqual(1, self.executed)

    def test_blocked_events_only_belong_to_owner(self):
        events = []
        runtime = policy.BusinessExecution.from_config({'enabled': True, 'allowed_tools': []}, workspace=self.root,
                    session_id='owner', on_blocked=lambda *event: events.append(event))
        self.run_call(runtime, 'child-call', caller_session_id='child')
        self.run_call(runtime, 'owner-call', caller_session_id='owner')
        self.assertEqual(['owner-call'], [event[0] for event in events])
        self.assertEqual(0, self.executed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
