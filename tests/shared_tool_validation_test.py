"""Public SDK contracts with simulated native/transport boundaries, never GUI proof.

The real ToolSet, DemoTools and owner dispatcher are exercised. Only this test
transport creates worker threads; the production runner uses Client's workers.
"""
import copy
from concurrent.futures import Future
import io
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'python'), str(ROOT / 'scripts')]
from auroraview_dcc_mcp import ClosedError
import demo_tools
from owner_dispatch import OwnerDispatcher
import run_demo
import tool_validation
from shared_demo_tools_test import Connection


class WireConnection(Connection):
    """Simulate broadcast receipt on workers rather than count emit as success."""

    def __init__(self):
        super().__init__()
        self.workers, self.worker_errors = [], []
        self.reply_transform = lambda data: data
        self.drop_reply = False
        self.async_calls = []
        self.rpc_transform = lambda method, data: data
        self.pending_rpc = None

    def call_async(self, method, params=None, *, timeout=None):
        self.async_calls.append((method, params, timeout))
        future = Future()
        if self.pending_rpc == method:
            return future
        if method == 'auroraview.tools.list':
            future.set_result(self.rpc_transform(method, copy.deepcopy(list(self.descriptors.values()))))
            return future

        def invoke():
            try:
                value = self.handlers[method](**(params or {}))
            except BaseException as error:
                if not future.done():
                    future.set_exception(error)
            else:
                if not future.done():
                    future.set_result(self.rpc_transform(method, value))

        worker = threading.Thread(target=invoke, name='contract-reverse-rpc')
        self.workers.append(worker)
        worker.start()
        return future

    def emit(self, name, data):
        super().emit(name, copy.deepcopy(data))
        if name == 'demo:event.reply':
            if self.drop_reply:
                return
            data = self.reply_transform(copy.deepcopy(data))
        callbacks = list(self.listeners.get(name, []))
        if not callbacks:
            return

        def deliver():
            try:
                for callback in callbacks:
                    callback(copy.deepcopy(data))
            except BaseException as error:
                self.worker_errors.append(error)

        worker = threading.Thread(target=deliver, name='contract-event-delivery')
        self.workers.append(worker)
        worker.start()

    def join_workers(self):
        for worker in self.workers:
            worker.join(2)
            if worker.is_alive():
                raise AssertionError('A contract transport worker did not finish')
        if self.worker_errors:
            raise AssertionError(self.worker_errors)


class ToolValidationContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.receipt_path = Path(temporary.name) / 'tool-validation.json'
        self.client = WireConnection()
        self.client.height = 73
        self.dispatcher = OwnerDispatcher()
        self.records = []
        self.tools = demo_tools.DemoTools(
            self.client, '/Game/Owned.Scene',
            lambda kind, data: self.records.append((kind, data)),
            shared_tools=True, dispatcher=self.dispatcher)
        self.tools.register()
        self.addCleanup(self.client.join_workers)
        self.addCleanup(self.dispatcher.close)
        self.addCleanup(self.tools.close)

    def validate(self, timeout=2):
        result = tool_validation.validate_tools(
            self.client, self.tools, self.dispatcher, self.receipt_path, timeout,
            source={'head': 'contract-fixture-only'})
        self.client.join_workers()
        self.assertEqual(json.loads(self.receipt_path.read_text(encoding='utf-8')), result)
        return result

    def assert_failed(self, result, message):
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(any(message in error['message'] for error in result['errors']), result)

    def test_public_tools_restore_native_state_receive_events_and_release_only_own_leases(self):
        outsider = self.tools.shared_owner.borrow()
        self.addCleanup(outsider.close)
        initial_handlers = dict(self.client.handlers)
        borrowed = []
        borrow = self.tools.shared_owner.borrow

        def capture_borrow():
            lease = borrow()
            borrowed.append(lease)
            return lease

        with patch.object(self.tools.shared_owner, 'borrow', side_effect=capture_borrow):
            result = self.validate()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['before'], {'height': 73, 'revision': 0})
        self.assertEqual(result['readback'], {'height': 150, 'revision': 1})
        self.assertEqual(result['after_restore'], {'height': 73, 'revision': 2})
        self.assertEqual(result['native_state'], 'restored')
        self.assertEqual(result['checks']['input_schema_refusal'], 'pass')
        self.assertEqual(result['checks']['closed_lease_refusal'], 'pass')
        self.assertEqual(len(result['descriptors']), 4)
        height = next(item for item in result['descriptors'] if item['name'] == 'demo.scene.set_height')
        self.assertEqual(height['inputSchema']['properties']['height']['maximum'], 300)
        self.assertEqual(result['event_reply']['message'], 'AuroraView 验收 ✓')
        self.assertIs(result['event_reply']['false_value'], False)
        self.assertIs(type(result['event_reply']['zero']), int)
        self.assertEqual(len(self.client.workers), 4)
        self.assertEqual([method for method, _params, _timeout in self.client.async_calls],
                         ['auroraview.tools.list', 'demo.python.multiply', 'demo.status'])
        self.assertEqual(result['reverse_socket_rpc'], 'pass')
        self.assertTrue(all(lease.closed for lease in borrowed))
        for lease in borrowed:
            with self.assertRaises(ClosedError):
                lease.call('demo.status')
        self.assertFalse(outsider.closed)
        self.assertEqual(outsider.call('demo.python.multiply', {'left': 2, 'right': 3})['value'], 6)
        self.assertEqual(self.client.handlers, initial_handlers)
        self.assertFalse(self.client.listeners['demo:event.reply'])
        self.assertFalse(self.tools.shared_owner.closed)
        for gate in ('gui', 'shipping_runtime', 'javascript_call_invoke',
                     'gateway_adapter_route'):
            self.assertEqual(result[gate], 'not_run')

    def test_native_mutation_chooses_a_different_height_when_initially_150(self):
        self.client.height = 150
        result = self.validate()
        self.assertEqual(result['status'], 'passed', result)
        self.assertEqual(result['readback']['height'], 75)
        self.assertEqual(self.client.height, 150)

    def test_independent_native_readback_cannot_be_replaced_by_the_setter_result(self):
        call = self.client.call
        reads = [0]

        def stale_readback(method, params=None):
            value = call(method, params)
            if method == 'unreal.object.call' and params['function'] == 'GetDemoState':
                reads[0] += 1
                if reads[0] == 3:  # Original, setter readback, then independent status.
                    value['return_value']['height'] = 1
            return value

        self.client.call = stale_readback
        result = self.validate()
        self.assert_failed(result, 'independent state/revision readback')
        self.assertEqual(result['changed']['height'], 150)
        self.assertEqual(result['readback']['height'], 1)
        self.assertEqual(result['cleanup']['restore'], 'pass')
        self.assertEqual(self.client.height, 73)

    def test_revision_must_advance_even_if_native_height_matches(self):
        call = self.client.call

        def unchanged_revision(method, params=None):
            value = call(method, params)
            if method == 'unreal.object.call' and params['function'] == 'SetCubeHeight':
                self.client.revision -= 1
            return value

        self.client.call = unchanged_revision
        result = self.validate()
        self.assert_failed(result, 'independent state/revision readback')
        self.assertEqual(result['native_state'], 'restoration_unverified')
        self.assertEqual(result['cleanup']['restore'], 'failed')
        self.assertEqual([error['phase'] for error in result['errors']], ['validation', 'restore'])

    def test_event_nonce_unicode_false_and_zero_mutations_are_failures(self):
        for field, value in [('nonce', 'different'), ('message', 'ASCII-only'),
                             ('false_value', 0), ('zero', False), ('zero', 0.0)]:
            with self.subTest(field=field, value=value):
                self.client.reply_transform = lambda data, field=field, value=value: dict(data, **{field: value})
                result = self.validate()
                self.assert_failed(result, 'Socket event reply changed')
                self.assertEqual(result['native_state'], 'restored')
                self.assertNotIn('socket_event_callback', result['checks'])
                self.assertFalse(self.client.listeners['demo:event.reply'])

    def test_emit_without_a_reply_times_out_and_does_not_claim_callback_success(self):
        self.client.drop_reply = True
        result = self.validate(timeout=1)
        self.assert_failed(result, 'deadline expired')
        self.assertEqual(result['errors'][0]['type'], 'TimeoutError')
        self.assertEqual(result['native_state'], 'restored')
        self.assertNotIn('socket_event_callback', result['checks'])
        self.assertTrue(any(event == 'demo:event.request' for event, _data in self.client.events))
        self.assertFalse(self.client.listeners['demo:event.reply'])

    def test_socket_catalog_schema_mutation_is_not_a_successful_registration(self):
        def changed_schema(method, data):
            if method == 'auroraview.tools.list':
                data[0]['inputSchema'] = {'type': 'string'}
            return data

        self.client.rpc_transform = changed_schema
        result = self.validate()
        self.assert_failed(result, 'catalog changed a shared tool descriptor/schema')
        self.assertEqual(result['native_state'], 'not_mutated')
        self.assertNotIn('socket_tool_catalog', result['checks'])

    def test_reverse_rpc_result_cannot_be_replaced_by_the_direct_session_result(self):
        self.client.rpc_transform = lambda method, data: dict(data, value=41) if method == 'demo.python.multiply' else data
        result = self.validate()
        self.assert_failed(result, 'Reverse socket RPC changed')
        self.assertEqual(result['checks']['python_multiply']['value'], 42)
        self.assertEqual(result['socket_python_multiply']['value'], 41)
        self.assertEqual(result['reverse_socket_rpc'], 'failed')
        self.assertEqual(result['native_state'], 'not_mutated')

    def test_pending_reverse_rpc_timeout_only_cancels_its_local_future(self):
        self.client.pending_rpc = 'demo.python.multiply'
        result = self.validate(timeout=1)
        self.assert_failed(result, 'deadline expired')
        self.assertIs(result['cleanup']['rpc_wait_cancelled'], True)
        self.assertIn('remote execution is not cancelled', result['cleanup']['rpc_cancellation_scope'])
        self.assertEqual(result['native_state'], 'not_mutated')
        self.assertNotIn('reverse_socket_rpc', result['checks'])

    def test_native_rpc_timeout_retains_uncertainty_until_cleanup_readback(self):
        call = self.client.call
        timed_out = [False]

        def timeout_after_mutating(method, params=None):
            value = call(method, params)
            if method == 'unreal.object.call' and params['function'] == 'SetCubeHeight' and not timed_out[0]:
                timed_out[0] = True
                raise TimeoutError('Native RPC deadline; mutation may already have completed')
            return value

        self.client.call = timeout_after_mutating
        result = self.validate()
        self.assert_failed(result, 'mutation may already have completed')
        self.assertNotIn('native_mutation_readback', result['checks'])
        self.assertEqual(result['before']['height'], 73)
        self.assertEqual(result['after_restore']['height'], 73)
        self.assertEqual(result['native_state'], 'restored')

    def test_restore_failure_preserves_initial_failure_and_unverified_native_state(self):
        call = self.client.call

        def failed_mutation_and_restore(method, params=None):
            if method == 'unreal.object.call' and params['function'] == 'SetCubeHeight':
                if params['args']['Height'] == 73:
                    raise RuntimeError('Fixture restoration refused')
                call(method, params)
                raise TimeoutError('Fixture mutation outcome uncertain')
            return call(method, params)

        self.client.call = failed_mutation_and_restore
        result = self.validate()
        self.assert_failed(result, 'Fixture mutation outcome uncertain')
        self.assert_failed(result, 'Fixture restoration refused')
        self.assertEqual([error['phase'] for error in result['errors']], ['validation', 'restore'])
        self.assertEqual(result['native_state'], 'restoration_unverified')
        self.assertEqual(result['cleanup']['restore'], 'failed')
        self.assertEqual(result['before']['height'], 73)
        self.assertEqual(self.client.height, 150)

    def test_wrong_host_identity_refuses_native_mutation(self):
        self.client.identity = dict(self.client.identity, pid=456)
        call = self.client.call

        def wrong_host(method, params=None):
            return dict(self.client.identity, pid=789) if method == 'unreal.engine.info' else call(method, params)

        self.client.call = wrong_host
        result = self.validate()
        self.assert_failed(result, 'different native host: pid')
        self.assertEqual(result['native_state'], 'not_mutated')
        self.assertFalse(any(params and params.get('function') == 'SetCubeHeight'
                             for _method, params in self.client.calls))

    def test_cleanup_failure_revokes_the_lease_and_keeps_other_borrowers_alive(self):
        outsider = self.tools.shared_owner.borrow()
        self.addCleanup(outsider.close)
        on = self.client.on
        attempts = [0]

        def fail_once(event, callback):
            unsubscribe = on(event, callback)
            if event != 'demo:event.reply':
                return unsubscribe

            def close():
                attempts[0] += 1
                if attempts[0] == 1:
                    raise RuntimeError('Fixture event unsubscription failed')
                unsubscribe()
            return close

        self.client.on = fail_once
        result = self.validate()
        self.assert_failed(result, 'Fixture event unsubscription failed')
        self.assertEqual(result['cleanup']['borrowed_session_close'], 'pass')
        self.assertFalse(self.client.listeners['demo:event.reply'])
        self.assertFalse(outsider.closed)
        self.assertEqual(outsider.call('demo.status')['scene']['height'], 73)


class ToolValidationOptions(unittest.TestCase):
    def test_timeout_rejects_nonfinite_boolean_and_unbounded_values(self):
        for value in (True, False, None, '1', 0, -1, 301, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                tool_validation.validate_timeout(value)
        self.assertEqual(tool_validation.validate_timeout(1), 1.0)
        self.assertEqual(tool_validation.validate_timeout(300), 300.0)

    def test_invalid_cli_combinations_cannot_prepare_build_or_launch(self):
        for arguments in (['--validate-tools'], ['--validate-tools', '--shared-tools', '--prepare-only'],
                          ['--validation-timeout', 'nan'], ['--validation-timeout', '0'],
                          ['--validation-timeout', '301']):
            with self.subTest(arguments=arguments), \
                    patch.object(sys, 'argv', ['run_demo.py', '--output', 'unused-contract-output', *arguments]), \
                    patch.object(run_demo, 'prepare') as prepare, \
                    patch.object(run_demo, 'launch') as launch, redirect_stderr(io.StringIO()):
                self.assertEqual(run_demo.main(), 1)
                prepare.assert_not_called()
                launch.assert_not_called()

    def test_direct_launch_refuses_validation_without_shared_tools_before_loading_a_host(self):
        with patch.object(run_demo.demo_tools, 'require_shared_tools') as require, \
                self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'requires --shared-tools'):
            run_demo.launch({}, None, None, 0, validate_tools=True)
        require.assert_not_called()

    def test_validation_receipt_is_bound_after_readiness_and_failure_still_cleans_up(self):
        for status in ('passed', 'failed'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary)
                html = output / 'demo.html'
                html.write_text('<h1>Contract fixture</h1>', encoding='utf-8')
                process = Mock(pid=123, returncode=0)
                process.poll.return_value = None
                process.wait.return_value = 0
                client = Mock(identity={'pid': 123, 'context': 'game', 'engine_version': '5.7.4'})
                client.call.side_effect = lambda method, *_args, **_kwargs: (
                    {'engine_ready': True} if method == 'unreal.engine.info' else
                    {'ready': True, 'presentation': 'floating'} if method == 'auroraview.view.describe' else True)
                tools = Mock(browser_ready=threading.Event())
                tools.browser_ready.set()
                observed = []

                def validate(connection, owner, dispatcher, receipt, timeout, *, source=None):
                    self.assertIs(connection, client)
                    self.assertIs(owner, tools)
                    self.assertEqual(timeout, 7)
                    self.assertEqual(source, {'head': 'contract-only'})
                    methods = [call.args[0] for call in client.call.call_args_list]
                    self.assertIn('auroraview.view.describe', methods)
                    data = {'status': status}
                    receipt.write_text(json.dumps(data), encoding='utf-8')
                    observed.append((dispatcher, receipt))
                    return data

                with patch.object(run_demo, 'Client', return_value=client), \
                        patch.object(run_demo.subprocess, 'Popen', return_value=process), \
                        patch.object(run_demo.demo_tools, 'DemoTools', return_value=tools), \
                        patch.object(run_demo.demo_tools, 'find_scene', return_value=('world', 'scene')), \
                        patch.object(run_demo, 'run_ready_loop') as ready_loop, \
                        patch.object(tool_validation, 'validate_tools', side_effect=validate), \
                        redirect_stdout(io.StringIO()):
                    prepared = dict(mode='game', executable=str(output / 'Game.exe'),
                                    engine_version='5.7', source={'head': 'contract-only'})
                    if status == 'passed':
                        result = run_demo.launch(prepared, html, output, 1, True,
                                                 validate_tools=True, validation_timeout=7)
                        self.assertEqual(result['status'], 'closed')
                        ready_loop.assert_called_once()
                    else:
                        with self.assertRaisesRegex(run_demo.build_plugin.BuildError, 'Tool validation failed'):
                            run_demo.launch(prepared, html, output, 1, True,
                                            validate_tools=True, validation_timeout=7)
                        ready_loop.assert_not_called()
                dispatcher, validation_path = observed[0]
                saved = json.loads((validation_path.parent / 'session.json').read_text())
                self.assertEqual(saved['tool_validation']['status'], status)
                self.assertEqual(saved['tool_validation']['sha256'], run_demo.build_plugin.sha256(validation_path))
                self.assertEqual(saved['exit_code'], 0)
                self.assertFalse(saved['forced_cleanup'])
                tools.close.assert_called_once()
                client.close.assert_called_once()
                process.wait.assert_called_once_with(timeout=60)
                self.assertIsInstance(dispatcher(lambda: None).exception(), RuntimeError)


if __name__ == '__main__':
    unittest.main()
