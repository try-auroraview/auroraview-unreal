"""Demo contract failures are visible; native readback owns the success state."""
import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import demo_tools


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
