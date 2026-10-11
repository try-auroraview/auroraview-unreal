"""Bounded public ToolSession checks in the demo's existing owner loop.

This borrows the already registered tools and never creates a service, scheduler,
thread or native host. Real transport/GUI acceptance is distinct from unit tests.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import secrets
import threading
import time


def validate_timeout(seconds):
    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
            or not 1 <= seconds <= 300):
        raise ValueError('Validation timeout must be finite and between 1 and 300 seconds')
    return float(seconds)


def validate_tools(client, tools, dispatcher, receipt_path, timeout=30, *, source=None):
    """Return a receipt; failures retain their original error and cleanup outcome.

    The deadline is cooperative: existing synchronous native RPCs retain their
    own finite Client timeout. Cleanup still attempts restoration after expiry.
    A timed-out native mutation is uncertain until a subsequent state readback.
    """
    from auroraview_dcc_mcp import ClosedError, ContractError

    duration = validate_timeout(timeout)
    if tools.shared_owner is None or dispatcher is None:
        raise ValueError('Tool validation requires the shared tool owner and its dispatcher')
    path = Path(receipt_path)
    started, owner_thread = time.monotonic(), threading.get_ident()
    deadline = started + duration
    result = dict(schema_version=1, status='failed', started_utc=datetime.now(timezone.utc).isoformat(),
                  timeout_seconds=duration, identity=dict(client.identity), source=source,
                  implementation_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  checks={}, cleanup={}, errors=[], native_state='not_mutated',
                  gui='not_run', shipping_runtime='not_run', reverse_socket_rpc='not_run',
                  javascript_call_invoke='not_run', gateway_adapter_route='not_run',
                  scope='Public borrowed ToolSession, reverse socket RPC, native scene readback and socket events')
    session = other = fresh = None
    original = None
    mutation_attempted = restored = False

    def save():
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('Tool validation deadline expired')
        return seconds

    def call(method, params=None):
        remaining()
        value = session.call(method, params)
        remaining()
        return value

    def remote_call(method, params=None):
        # Only read-only tools use this path. Cancelling a local Future cannot
        # revoke a request already accepted by the native host or its handler.
        future = client.call_async(method, params, timeout=remaining())
        try:
            while not future.done():
                remaining()
                dispatcher.pump()
                if not future.done():
                    dispatcher.wait_for_work(min(0.05, remaining()))
            value = future.result()
            remaining()
            return value
        finally:
            if not future.done():
                result['cleanup']['rpc_wait_cancelled'] = future.cancel()
                result['cleanup']['rpc_cancellation_scope'] = 'local_future_only; remote execution is not cancelled'

    def state(lease):
        status = lease.call('demo.status')
        for key in ('pid', 'context', 'engine_version'):
            if status.get(key) != client.identity.get(key):
                raise ValueError('Tool status identifies a different native host: ' + key)
        value = status.get('scene', {})
        height, revision = value.get('height'), value.get('revision')
        if (type(height) not in (int, float) or not math.isfinite(height) or not 0 <= height <= 300
                or type(revision) is not int or revision < 0):
            raise ValueError('Tool status has invalid native scene height/revision')
        return value

    def restore():
        nonlocal restored
        result['cleanup']['restore_attempted'] = True
        restored_reply = session.call('demo.scene.set_height', {'height': original['height']})
        result['restore_reply'] = restored_reply
        observed = state(session)
        result['after_restore'] = observed
        minimum_revision = result.get('readback', original)['revision']
        if (abs(observed['height'] - original['height']) > 0.01
                or observed['revision'] <= minimum_revision
                or restored_reply != observed):
            raise ValueError('Native scene restoration did not survive independent readback')
        restored = True
        result['native_state'] = 'restored'
        result['cleanup']['restore'] = 'pass'

    save()
    try:
        session = tools.shared_owner.borrow()
        other = tools.shared_owner.borrow()
        descriptors = session.list_tools()
        names = {item['name'] for item in descriptors}
        expected = {'demo.status', 'demo.python.multiply', 'demo.scene.set_height', 'demo.scene.reset'}
        if names != expected:
            raise ValueError('Shared tool descriptors differ from the demo contract')
        result['descriptors'] = descriptors
        result['checks']['tool_descriptors'] = 'pass'
        catalog = remote_call('auroraview.tools.list')
        published = {item['name']: item for item in catalog if item.get('name') in expected}
        result['socket_tool_descriptors'] = list(published.values())
        for descriptor in descriptors:
            native = published.get(descriptor['name'], {})
            if any(native.get(key) != value for key, value in descriptor.items()):
                raise ValueError('Native socket catalog changed a shared tool descriptor/schema')
        result['checks']['socket_tool_catalog'] = 'pass'
        try:
            session.call('demo.python.multiply', {'left': True, 'right': 7})
        except ContractError:
            result['checks']['input_schema_refusal'] = 'pass'
        else:
            raise ValueError('The shared input schema accepted a boolean number')
        multiplied = call('demo.python.multiply', {'left': 6, 'right': 7})
        if type(multiplied.get('value')) not in (int, float) or multiplied['value'] != 42:
            raise ValueError('Public Python multiply returned a different result')
        result['checks']['python_multiply'] = multiplied
        result['reverse_socket_rpc'] = 'failed'
        remote_multiplied = remote_call('demo.python.multiply', {'left': 6, 'right': 7})
        result['socket_python_multiply'] = remote_multiplied
        if remote_multiplied != multiplied:
            raise ValueError('Reverse socket RPC changed the public Python tool result')
        result['reverse_socket_rpc'] = 'pass'
        result['checks']['reverse_socket_rpc'] = 'pass'
        remaining()
        original = state(session)
        result['before'] = original
        target = 150 if abs(original['height'] - 150) > 0.01 else 75
        mutation_attempted = True
        result['native_state'] = 'mutation_outcome_uncertain'
        save()
        changed = call('demo.scene.set_height', {'height': target})
        result['changed'] = changed
        observed = state(session)
        result['readback'] = observed
        if (abs(observed['height'] - target) > 0.01 or observed['revision'] <= original['revision']
                or abs(changed['height'] - observed['height']) > 0.01
                or changed['revision'] != observed['revision']):
            raise ValueError('Native mutation differs from its independent state/revision readback')
        result['checks']['native_mutation_readback'] = 'pass'
        restore()
        remote_status = remote_call('demo.status')
        result['socket_native_status'] = remote_status
        if (any(remote_status.get(key) != client.identity.get(key)
                for key in ('pid', 'context', 'engine_version'))
                or remote_status.get('scene') != result['after_restore']):
            raise ValueError('Reverse socket RPC status differs from the restored native host/scene')
        result['checks']['reverse_socket_native_readback'] = 'pass'
        remaining()
        nonce = secrets.token_hex(16)
        payload = dict(nonce=nonce, message='AuroraView 验收 ✓', false_value=False, zero=0)
        expected_reply = dict(payload, source='python')
        received = []

        def on_reply(data):
            if threading.get_ident() != owner_thread:
                raise ValueError('Shared event callback did not run on the owner thread')
            received.append(data)

        session.subscribe('demo:event.reply', on_reply)
        client.emit('demo:event.request', payload)
        while not received:
            dispatcher.pump()
            if not received:
                dispatcher.wait_for_work(min(0.05, remaining()))
        remaining()
        result['event_reply'] = received[0]
        reply = received[0]
        if (received != [expected_reply] or not isinstance(reply, dict)
                or reply.get('false_value') is not False or type(reply.get('zero')) is not int):
            raise ValueError('Socket event reply changed its nonce, Unicode, false or zero payload')
        result['checks']['socket_event_callback'] = 'pass'
        session.close()
        result['cleanup']['borrowed_session_close'] = 'pass'
        try:
            session.call('demo.status')
        except ClosedError:
            result['checks']['closed_lease_refusal'] = 'pass'
        else:
            raise ValueError('The closed borrowed session accepted another call')
        if other.call('demo.python.multiply', {'left': 0, 'right': 7})['value'] != 0:
            raise ValueError('Closing one consumer changed another borrower')
        fresh = tools.shared_owner.borrow()
        result['fresh_borrower_state'] = state(fresh)
        result['checks']['other_and_fresh_borrowers'] = 'pass'
        remaining()
        result['status'] = 'passed'
    except Exception as error:
        result['errors'].append({'phase': 'validation', 'type': type(error).__name__, 'message': str(error)})
    finally:
        if mutation_attempted and not restored:
            try:
                restore()
            except Exception as error:
                result['native_state'] = 'restoration_unverified'
                result['cleanup']['restore'] = 'failed'
                result['errors'].append({'phase': 'restore', 'type': type(error).__name__, 'message': str(error)})
        for name, lease in [('borrowed', session), ('other', other), ('fresh', fresh)]:
            if lease is not None:
                try:
                    lease.close()
                    result['cleanup'][name + '_session_close'] = 'pass'
                except Exception as error:
                    result['cleanup'][name + '_session_close'] = 'failed'
                    result['errors'].append({'phase': name + '_session_close', 'type': type(error).__name__, 'message': str(error)})
        if result['errors']:
            result['status'] = 'failed'
        result['completed_utc'] = datetime.now(timezone.utc).isoformat()
        result['elapsed_seconds'] = time.monotonic() - started
        save()
    return result
