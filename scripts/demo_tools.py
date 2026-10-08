"""External Python tools for the public Editor/Game demo, with native readback."""
import math
import platform
import threading
import time


def require_shared_tools():
    """Load the explicitly installed public consumer package, without a server."""
    try:
        from importlib.metadata import version
        from auroraview_dcc_mcp import Tool, ToolSet
        from auroraview_unreal.native_tools import NativeToolBinding
        installed = version('auroraview-dcc-mcp')
    except ImportError as error:
        raise ValueError('Install the pinned auroraview-unreal[dcc-mcp] extra before using --shared-tools') from error
    if installed != '0.1.0':
        raise ValueError('The shared demo requires auroraview-dcc-mcp 0.1.0')
    return Tool, ToolSet, NativeToolBinding, installed

SCENE_CLASS = '/Script/AuroraViewGameFixture.AuroraViewDemoScene'


def scene_state(client, scene):
    reply = client.call('unreal.object.call', {
        'object': scene, 'function': 'GetDemoState', 'args': {}})
    value = reply.get('return_value')
    if not isinstance(value, dict):
        raise ValueError('The native demo scene returned no reflected state')
    state = {key[:1].lower() + key[1:]: item for key, item in value.items()}
    height, revision = state.get('height'), state.get('revision')
    if (isinstance(height, bool) or not isinstance(height, (int, float))
            or not math.isfinite(height) or not 0 <= height <= 300
            or isinstance(revision, bool) or not isinstance(revision, int) or revision < 0):
        raise ValueError('The native demo state has invalid height/revision')
    return state


def find_scene(client, context):
    matches = []
    expected_type = 2 if context == 'editor' else 1  # EWorldType::Editor/Game.
    for world in client.call('unreal.world.list'):
        if world.get('world_type') != expected_type:
            continue
        for actor in client.call('unreal.actor.list', {'world': world['object']}):
            if actor.get('class') == SCENE_CLASS:
                matches.append((world['object'], actor['object']))
    if len(matches) > 1:
        raise ValueError('Multiple demo scenes found; refusing an ambiguous target')
    return matches[0] if matches else None


class DemoTools:
    """Own the external tools; native objects remain owned by the sample project."""

    def __init__(self, client, scene, record=lambda _kind, _data: None, *,
                 shared_tools=False, dispatcher=None):
        self.client, self.scene, self.record = client, scene, record
        self.lock = threading.Lock()
        self.browser_ready = threading.Event()
        self.unsubscribers = []
        self.dispatcher = dispatcher
        self.shared_binding = self.shared_owner = None
        self.shared_api = require_shared_tools() if shared_tools else None
        if shared_tools and not callable(dispatcher):
            raise ValueError('Shared tools require the caller-owned thread dispatcher')

    def status(self):
        info = self.client.call('unreal.engine.info')
        return dict(info, python_version=platform.python_version(),
                    scene=scene_state(self.client, self.scene))

    def multiply(self, left, right):
        for value in [left, right]:
            if (isinstance(value, bool) or not isinstance(value, (float, int))
                    or not math.isfinite(value) or abs(value) > 1000000):
                raise ValueError('Use finite numbers between -1000000 and 1000000')
        result = {'value': left * right, 'python_version': platform.python_version()}
        self.record('python.multiply', dict(left=left, right=right, **result))
        return result

    def set_height(self, height):
        if (isinstance(height, bool) or not isinstance(height, (float, int))
                or not math.isfinite(height) or not 0 <= height <= 300):
            raise ValueError('Cube lift must be a finite number from 0 to 300 cm')
        with self.lock:
            changed = self.client.call('unreal.object.call', {
                'object': self.scene, 'function': 'SetCubeHeight', 'args': {'Height': height}})
            state = scene_state(self.client, self.scene)
            if changed.get('return_value') is not True or abs(state['height'] - height) > 0.01:
                raise ValueError('Native scene did not confirm the requested cube height')
            self.client.emit('demo:scene', state)
            self.record('native.set_height', state)
            return state

    def reset(self):
        with self.lock:
            self.client.call('unreal.object.call', {
                'object': self.scene, 'function': 'ResetScene', 'args': {}})
            state = scene_state(self.client, self.scene)
            if abs(state['height']) > 0.01:
                raise ValueError('Native scene did not reset the cube')
            self.client.emit('demo:scene', state)
            self.record('native.reset', state)
            return state

    def event_request(self, data):
        if (not isinstance(data, dict) or not isinstance(data.get('nonce'), str)
                or not 1 <= len(data['nonce']) <= 128
                or not isinstance(data.get('message'), str) or len(data['message']) > 512
                or data.get('false_value') is not False
                or isinstance(data.get('zero'), bool) or data.get('zero') != 0):
            self.record('event.rejected', {'reason': 'Invalid demo event payload'})
            return
        reply = dict(data, source='python')
        self.client.emit('demo:event.reply', reply)
        self.record('python.event_roundtrip', reply)

    def ready(self, data):
        identity = self.client.identity
        if (not isinstance(data, dict) or data.get('pid') != identity['pid']
                or data.get('context') != identity['context']
                or data.get('engine_version') != identity['engine_version']):
            self.record('browser.rejected', {'reason': 'Browser reported a different host'})
            return
        self.record('browser.ready', data)
        self.browser_ready.set()

    def _subscribe_shared(self, event, callback):
        def deliver(data):
            # Native SDK callbacks arrive on its existing workers. ToolSession
            # delivery and the host operation belong to the demo owner thread.
            pending = self.dispatcher(lambda: callback(data))
            def completed(future):
                if not future.cancelled() and future.exception() is not None:
                    self.record('shared.event.failed', {'event': event,
                                                       'error_type': type(future.exception()).__name__})
            pending.add_done_callback(completed)
        return self.client.on(event, deliver)

    def _register_shared(self):
        Tool, ToolSet, NativeToolBinding, installed = self.shared_api
        empty = {'type': 'object', 'properties': {}, 'additionalProperties': False}
        number = {'type': 'number', 'minimum': -1000000, 'maximum': 1000000}
        state = {'type': 'object', 'properties': {
            'height': {'type': 'number', 'minimum': 0, 'maximum': 300},
            'revision': {'type': 'integer', 'minimum': 0}}, 'required': ['height', 'revision']}
        declarations = [
            Tool('demo.status', 'Read the bound Unreal host and actual demo scene.', empty,
                 self.status, output_schema={'type': 'object'},
                 read_only=True, destructive=False, idempotent=True),
            Tool('demo.python.multiply', 'Multiply two finite numbers in external Python.',
                 {'type': 'object', 'properties': {'left': number, 'right': number},
                  'required': ['left', 'right'], 'additionalProperties': False},
                 self.multiply, output_schema={'type': 'object', 'properties': {
                     'value': {'type': 'number'}, 'python_version': {'type': 'string'}},
                     'required': ['value', 'python_version']},
                 read_only=True, destructive=False, idempotent=True),
            Tool('demo.scene.set_height', 'Lift the cube and read back its native height and revision.',
                 {'type': 'object', 'properties': {'height': {
                     'type': 'number', 'minimum': 0, 'maximum': 300}},
                  'required': ['height'], 'additionalProperties': False},
                 self.set_height, output_schema=state, destructive=False),
            Tool('demo.scene.reset', 'Reset the cube and read back the native scene.', empty,
                 self.reset, output_schema=state, destructive=False),
        ]
        self.shared_owner = ToolSet('unreal_demo', declarations, dcc='unreal',
                                    subscribe=self._subscribe_shared)
        self.shared_binding = NativeToolBinding(self.client, self.shared_owner,
                                               dispatch=self.dispatcher)
        self.shared_binding.register()
        self.record('shared.tools.registered', {'package': 'auroraview-dcc-mcp',
                                               'version': installed,
                                               'service_created': False,
                                               'tools': [tool.name for tool in declarations]})

    def register(self):
        if self.shared_api:
            self._register_shared()
        else:
            for name, handler in [('demo.status', self.status),
                                  ('demo.python.multiply', self.multiply),
                                  ('demo.scene.set_height', self.set_height),
                                  ('demo.scene.reset', self.reset)]:
                self.client.bind_call(name, handler)
        for event, handler in [('demo:event.request', self.event_request),
                               ('demo:browser.ready', self.ready),
                               ('demo:browser.result', lambda data: self.record('browser.result', data))]:
            subscribe = self.shared_binding.subscribe if self.shared_binding else self.client.on
            self.unsubscribers.append(subscribe(event, handler))

    def close(self):
        errors = []
        for unsubscribe in self.unsubscribers:
            try:
                unsubscribe()
            except Exception as error:
                errors.append(error)
        self.unsubscribers.clear()
        for resource in (self.shared_binding, self.shared_owner):
            if resource:
                try:
                    resource.close()
                except Exception as error:
                    errors.append(error)
        if errors:
            raise RuntimeError('Demo tool cleanup: ' + '; '.join(str(error) for error in errors))
