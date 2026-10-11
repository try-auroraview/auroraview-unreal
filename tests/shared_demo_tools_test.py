"""Exercise the public shared package through the demo's actual tool owner.

Requires the pinned optional dcc-mcp extra. This is a Python contract test;
the native scene, browser and host acceptance have separate receipts.
"""
from pathlib import Path
import sys
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'python'), str(ROOT / 'scripts')]
import demo_tools
from owner_dispatch import OwnerDispatcher
from demo_tools_test import NativeHost


class Connection(NativeHost):
    def __init__(self):
        super().__init__()
        self.handlers, self.descriptors, self.listeners = {}, {}, {}

    def bind_tool(self, descriptor, handler, *, allow_rebind=False):
        name = descriptor['name']
        if name in self.handlers and not allow_rebind:
            raise ValueError('Tool already registered')
        self.handlers[name], self.descriptors[name] = handler, descriptor
        client = self
        class Handle:
            def close(self):
                if client.handlers.get(name) is handler:
                    client.handlers.pop(name)
                    client.descriptors.pop(name)
        return Handle()

    def on(self, event, callback):
        listeners = self.listeners.setdefault(event, [])
        listeners.append(callback)
        def unsubscribe():
            if callback in listeners:
                listeners.remove(callback)
        return unsubscribe


class SharedDemoContract(unittest.TestCase):
    def setUp(self):
        self.client = Connection()
        self.dispatcher = OwnerDispatcher()
        self.records = []
        self.tools = demo_tools.DemoTools(self.client, '/Game/Owned.Scene',
                                         lambda kind, data: self.records.append((kind, data)),
                                         shared_tools=True, dispatcher=self.dispatcher)
        self.tools.register()
        self.addCleanup(self.dispatcher.close)
        self.addCleanup(self.tools.close)

    def worker_call(self, method, **params):
        result, errors = [], []
        def invoke():
            try:
                result.append(self.client.handlers[method](**params))
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=invoke)
        worker.start()
        deadline = time.monotonic() + 2
        while worker.is_alive() and time.monotonic() < deadline:
            self.dispatcher.pump()
            worker.join(0.01)
        self.assertFalse(worker.is_alive())
        if errors:
            raise errors[0]
        return result[0]

    def test_worker_calls_share_published_schema_and_native_readback(self):
        self.assertEqual(self.worker_call('demo.python.multiply', left=6, right=7)['value'], 42)
        lifted = self.worker_call('demo.scene.set_height', height=150)
        self.assertEqual(lifted, {'height': 150, 'revision': 1})
        self.assertEqual(self.client.calls[-1][1]['function'], 'GetDemoState')
        self.assertEqual(self.worker_call('demo.scene.reset')['height'], 0)
        descriptor = self.client.descriptors['demo.scene.set_height']
        self.assertEqual(descriptor['inputSchema']['properties']['height']['maximum'], 300)
        self.assertEqual(descriptor['outputSchema']['required'], ['height', 'revision'])
        self.assertFalse(descriptor['annotations']['destructiveHint'])

    def test_invalid_shared_input_cannot_reach_native(self):
        before = list(self.client.calls)
        for height in (True, -1, 301):
            with self.subTest(height=height), self.assertRaises(ValueError):
                self.worker_call('demo.scene.set_height', height=height)
        self.assertEqual(self.client.calls, before)

    def test_browser_events_deliver_on_owner_and_preserve_payload(self):
        data = {'nonce': 'shared-demo-1', 'message': '你好', 'false_value': False, 'zero': 0}
        incoming = list(self.client.listeners['demo:event.request'])
        worker = threading.Thread(target=lambda: [callback(data) for callback in incoming])
        worker.start()
        worker.join(2)
        self.assertFalse(self.client.events)
        self.dispatcher.pump()
        self.assertEqual(self.client.events, [('demo:event.reply', dict(data, source='python'))])

    def test_binding_close_revokes_queued_events_and_preserves_other_borrower(self):
        borrower = self.tools.shared_owner.borrow()
        self.addCleanup(borrower.close)
        incoming = list(self.client.listeners['demo:browser.ready'])
        worker = threading.Thread(target=lambda: [callback(self.client.identity) for callback in incoming])
        worker.start()
        worker.join(2)
        self.tools.shared_binding.close()
        self.dispatcher.pump()
        self.assertFalse(self.tools.browser_ready.is_set())
        self.assertFalse(self.client.handlers)
        self.assertFalse(self.tools.shared_owner.closed)
        self.assertEqual(borrower.call('demo.python.multiply', {'left': 0, 'right': 7})['value'], 0)
        self.assertEqual(self.client.call('unreal.engine.info'), self.client.identity)


if __name__ == '__main__':
    unittest.main()
