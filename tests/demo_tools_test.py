"""Demo contract failures are visible; native readback owns the success state."""
import math
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'python')]
import demo_tools
from auroraview_unreal import Client


def registration_client():
    """Only the real registration algorithm; no constructor/socket/workers."""
    client = Client.__new__(Client)
    client._lock, client._binding_lock = threading.Lock(), threading.RLock()
    client._closed = threading.Event()
    client._handlers, client._events = {}, {}
    client.timeout = 5.0
    client.call, client.emit = Mock(), Mock()
    return client


class ClientRegistrationTimeoutContract(unittest.TestCase):
    def setUp(self):
        self.client = registration_client()
        self.descriptor = {'name': 'demo.echo', 'description': 'Echo one value.',
                           'inputSchema': {'type': 'object'}}

    def test_omitted_timeout_preserves_existing_rpc_kwargs_and_handle_owner(self):
        self.client.bind_call('demo.call', lambda: False)
        binding = self.client.bind_tool(self.descriptor, lambda: 0)
        self.assertIs(binding._client, self.client)
        self.assertEqual([call.kwargs for call in self.client.call.call_args_list], [{}, {}])
        binding.close()
        self.assertEqual(self.client.call.call_args.kwargs, {})
        self.assertEqual(self.client.timeout, 5.0)

    def test_numeric_and_deferred_callback_timeouts_apply_only_to_registration(self):
        self.client.bind_tool(self.descriptor, lambda: 0, timeout=2.5)
        callback = Mock(return_value=0.25)
        decorate = self.client.bind_call('demo.call', timeout=callback)
        callback.assert_not_called()
        handler = lambda: False
        self.assertIs(decorate(handler), handler)
        callback.assert_called_once_with()
        self.assertEqual([call.kwargs for call in self.client.call.call_args_list],
                         [{'timeout': 2.5}, {'timeout': 0.25}])
        self.client.unbind_call('demo.call')
        self.assertEqual(self.client.call.call_args.kwargs, {})
        self.assertEqual(self.client.timeout, 5.0)

    def test_expired_callback_restores_previous_handler_or_removes_new_wrapper(self):
        old = lambda: 'previous'
        for explicit, existing in [(False, False), (False, True), (True, False), (True, True)]:
            with self.subTest(explicit=explicit, existing=existing):
                self.client._handlers.clear()
                if existing:
                    self.client._handlers['demo.echo'] = old
                def expired():
                    self.assertIn('demo.echo', self.client._handlers)
                    self.assertIsNot(self.client._handlers['demo.echo'], old)
                    raise TimeoutError('Startup expired before registration')
                with self.assertRaisesRegex(TimeoutError, 'Startup expired'):
                    if explicit:
                        self.client.bind_tool(self.descriptor, lambda: 0, allow_rebind=True, timeout=expired)
                    else:
                        self.client.bind_call('demo.echo', lambda: 0, timeout=expired)
                self.assertEqual(self.client._handlers, {'demo.echo': old} if existing else {})
        self.client.call.assert_not_called()

    def test_invalid_timeout_does_not_publish_a_handler(self):
        for timeout in (0, -1, math.inf, math.nan, lambda: None):
            with self.subTest(timeout=timeout), self.assertRaises((TypeError, ValueError)):
                self.client.bind_call('demo.echo', lambda: 0, timeout=timeout)
            self.assertFalse(self.client._handlers)
        self.client.call.assert_not_called()

    def test_invalid_descriptor_does_not_evaluate_deadline_callback(self):
        callback = Mock(return_value=1)
        with self.assertRaises(ValueError):
            self.client.bind_tool(dict(self.descriptor, inputSchema=None), lambda: 0, timeout=callback)
        callback.assert_not_called()
        self.client.call.assert_not_called()

    def test_registration_rpc_failure_still_restores_handler(self):
        old = lambda: 'previous'
        self.client._handlers['demo.echo'] = old
        self.client.call.side_effect = RuntimeError('Native registration refused')
        with self.assertRaisesRegex(RuntimeError, 'Native registration refused'):
            self.client.bind_call('demo.echo', lambda: 0, timeout=lambda: 0.5)
        self.assertIs(self.client._handlers['demo.echo'], old)


class DemoRegistrationTimeoutContract(unittest.TestCase):
    def test_default_register_uses_no_new_kwargs(self):
        client = Mock()
        tools = demo_tools.DemoTools(client, 'scene')
        tools.register()
        self.assertEqual(client.bind_call.call_count, 4)
        self.assertTrue(all(call.kwargs == {} for call in client.bind_call.call_args_list))
        self.assertEqual(client.on.call_count, 3)

    def test_deadline_is_checked_before_each_actual_register_rpc(self):
        client = registration_client()
        tools = demo_tools.DemoTools(client, 'scene')
        remaining = [0.5]
        def timeout():
            if remaining[0] <= 0:
                raise TimeoutError('Startup expired')
            return remaining[0]
        client.call.side_effect = lambda *_args, **_kwargs: remaining.__setitem__(0, 0)
        with self.assertRaisesRegex(TimeoutError, 'Startup expired'):
            tools.register(registration_timeout=timeout)
        self.assertEqual(client.call.call_count, 1)
        self.assertEqual(client.call.call_args.kwargs, {'timeout': 0.5})
        self.assertEqual(set(client._handlers), {'demo.status'})
        self.assertFalse(client._events)
        self.assertEqual(client.timeout, 5.0)

    def test_shared_registration_adapter_preserves_real_handle_and_event_transport(self):
        client = registration_client()
        callback = Mock(return_value=0.5)
        adapter = demo_tools.RegistrationCalls(client, callback)
        descriptor = {'name': 'demo.echo', 'description': 'Echo one value.', 'inputSchema': {}}
        handle = adapter.bind_tool(descriptor, lambda: False)
        self.assertIs(handle._client, client)
        callback.assert_called_once_with()
        self.assertEqual(client.call.call_args.kwargs, {'timeout': 0.5})
        adapter.emit('demo:scene', {'height': 0})
        client.emit.assert_called_once_with('demo:scene', {'height': 0})
        handle.close()
        self.assertFalse(client._handlers)
        self.assertEqual(client.call.call_args.kwargs, {})

    def test_shared_registration_adapter_rejects_expiry_without_a_handle_or_rpc(self):
        client = registration_client()
        callback = Mock(side_effect=TimeoutError('Startup expired'))
        adapter = demo_tools.RegistrationCalls(client, callback)
        with self.assertRaisesRegex(TimeoutError, 'Startup expired'):
            adapter.bind_tool({'name': 'demo.echo', 'description': 'Echo one value.', 'inputSchema': {}}, lambda: 0)
        self.assertFalse(client._handlers)
        client.call.assert_not_called()


class NativeHost:
    def __init__(self):
        self.height, self.revision = 0, 0
        self.calls, self.events = [], []
        self.identity = {'pid': 123, 'context': 'game', 'engine_version': '5.7.4'}
        self.refuse = False

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'unreal.engine.info':
            return self.identity.copy()
        if method == 'unreal.object.call':
            if params['function'] == 'GetDemoState':
                return {'return_value': {'height': self.height, 'revision': self.revision}}
            if params['function'] == 'SetCubeHeight':
                if not self.refuse:
                    self.height = params['args']['Height']
                    self.revision += 1
                return {'return_value': not self.refuse}
            if params['function'] == 'ResetScene':
                self.height = 0
                self.revision += 1
                return {'return_value': {'height': self.height, 'revision': self.revision}}
        raise AssertionError(method)

    def emit(self, name, data):
        self.events.append((name, data))


class DemoToolsContract(unittest.TestCase):
    def setUp(self):
        self.host = NativeHost()
        self.records = []
        self.tools = demo_tools.DemoTools(self.host, '/Game/Owned.Scene',
                                         lambda kind, data: self.records.append((kind, data)))

    def test_move_reads_native_state_and_broadcasts_readback(self):
        result = self.tools.set_height(150)
        self.assertEqual(result, {'height': 150, 'revision': 1})
        self.assertEqual(self.host.calls[-1][1]['function'], 'GetDemoState')
        self.assertEqual(self.host.events, [('demo:scene', result)])

    def test_native_rejection_cannot_report_success_or_emit_state(self):
        self.host.refuse = True
        with self.assertRaisesRegex(ValueError, 'did not confirm'):
            self.tools.set_height(150)
        self.assertFalse(self.host.events)

    def test_invalid_height_never_reaches_native(self):
        for value in [True, -1, 301, math.inf, math.nan, '10']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.tools.set_height(value)
        self.assertFalse(self.host.calls)

    def test_reset_reads_the_actual_zero(self):
        self.tools.set_height(300)
        result = self.tools.reset()
        self.assertEqual(result['height'], 0)
        self.assertEqual(result['revision'], 2)

    def test_multiply_preserves_zero_and_rejects_booleans_nonfinite(self):
        self.assertEqual(self.tools.multiply(0, 7)['value'], 0)
        for value in [True, math.inf, math.nan, 1000001, '6']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.tools.multiply(value, 7)

    def test_event_preserves_nonce_false_and_zero(self):
        self.tools.event_request({'nonce': 'opaque-1', 'message': '你好', 'false_value': False, 'zero': 0})
        self.assertEqual(self.host.events, [('demo:event.reply', {
            'nonce': 'opaque-1', 'message': '你好', 'false_value': False, 'zero': 0, 'source': 'python'})])

    def test_invalid_event_does_not_acknowledge(self):
        for value in [None, {}, {'nonce': 'n', 'message': 'm', 'false_value': False, 'zero': False}]:
            self.tools.event_request(value)
        self.assertFalse(self.host.events)

    def test_browser_readiness_requires_the_actual_identity(self):
        self.tools.ready(dict(self.host.identity, pid=999))
        self.assertFalse(self.tools.browser_ready.is_set())
        self.tools.ready(self.host.identity.copy())
        self.assertTrue(self.tools.browser_ready.is_set())

    def test_invalid_native_readback_is_not_a_demo_state(self):
        for height, revision in [(math.nan, 1), (0, -1), (False, 1), (0, True)]:
            self.host.height, self.host.revision = height, revision
            with self.assertRaises(ValueError):
                demo_tools.scene_state(self.host, 'scene')


if __name__ == '__main__':
    unittest.main()
