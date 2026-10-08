"""External SDK acceptance against a real loopback parent-IPC peer."""
import copy
import json
from pathlib import Path
import queue
import socket
import sys
import threading
import time
import unittest
import uuid
from concurrent.futures import Future
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from auroraview_unreal import Client, ConnectionClosedError, ProtocolError, RemoteError, AuroraViewError
from auroraview_unreal.client import MAX_FRAME_BYTES

ABSENT = object()
RPC, RESULT = "__auroraview_rpc", "__auroraview_call_result"
TOKEN = "test-native-authentication-token-123456789"
ACK = {"type": "hello_ack", "protocol": 1, "accepted": True, "parent_id": "unreal-test",
       "data": {"capabilities": ["event", "rpc", "tools"], "rpc_protocol": "auroraview.unreal/1",
                "pid": 1234, "engine_version": "5.7.4-12345+++UE5", "context": "game"}}


class Peer:
    """Separate TCP reader acts as native host; never calls client internals."""
    def __init__(self, ack=ACK, bom=False, reject_registration=False):
        self.ack = copy.deepcopy(ack)
        self.bom = bom
        self.reject_registration = reject_registration
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.connection = None
        self.connected, self.stopped = threading.Event(), threading.Event()
        self.send_lock, self.pending_lock = threading.Lock(), threading.Lock()
        self.frames, self.calls = queue.Queue(), queue.Queue()
        self.registrations, self.tools, self.pending = [], set(), {}
        self.thread = threading.Thread(target=self._run, daemon=True, name="mock-unreal-peer")
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send_raw(self, data):
        if not self.connected.wait(2):
            raise AssertionError("Client never connected")
        with self.send_lock:
            self.connection.sendall(data)

    def send(self, frame, bom=False):
        raw = json.dumps(frame, separators=(",", ":")).encode()
        self.send_raw((b"\xef\xbb\xbf" if bom else b"") + raw + (b"\r\n" if bom else b"\n"))

    def event(self, name, data):
        self.send({"type": "event", "event": name, "data": data})

    def reply(self, request_id, result=None, error=None):
        envelope = {"id": request_id, "ok": error is None}
        envelope["result" if error is None else "error"] = result if error is None else error
        self.event(RESULT, envelope)

    def reverse(self, method, params=ABSENT):
        request_id, future = uuid.uuid4().hex, Future()
        with self.pending_lock:
            self.pending[request_id] = future
        envelope = {"type": "call", "id": request_id, "method": method}
        if params is not ABSENT:
            envelope["params"] = params
        self.event(RPC, envelope)
        return future

    def wait_frame(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self.frames.get(timeout=max(0.01, deadline - time.monotonic()))
            if predicate(frame):
                return frame
        raise AssertionError("Expected client frame was not received")

    def _run(self):
        try:
            while not self.stopped.is_set():
                try:
                    self.connection, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if self.connection is None:
                return
            self.connected.set()
            with self.connection.makefile("rb") as stream:
                while not self.stopped.is_set():
                    line = stream.readline()
                    if not line:
                        break
                    frame = json.loads(line)
                    self.frames.put(frame)
                    if frame["type"] == "hello":
                        if self.ack is not None:
                            self.send(self.ack, self.bom)
                    elif frame["type"] == "event":
                        if frame["event"] == RPC:
                            self._call(frame["data"])
                        elif frame["event"] == RESULT:
                            with self.pending_lock:
                                future = self.pending.pop(frame["data"]["id"], None)
                            if future:
                                future.set_result(frame["data"])
                        elif not frame["event"].startswith("child:"):
                            self.send(frame)
        except (OSError, ValueError):
            pass

    def _call(self, request):
        self.calls.put(request)
        name, params = request["method"], request.get("params", ABSENT)
        if name == "auroraview.tools.register":
            if self.reject_registration:
                self.reply(request["id"], error={"name": "RegistrationError", "message": "denied", "code": "DENIED"})
                return
            self.registrations.extend(params["tools"])
            self.tools.update(tool["name"] for tool in params["tools"])
            self.reply(request["id"], True)
        elif name == "auroraview.tools.unregister":
            self.tools.difference_update(params["names"])
            self.reply(request["id"], True)
        elif name in self.tools:
            future = self.reverse(name, params)
            def relay(done):
                value = done.result()
                self.reply(request["id"], value.get("result"), value.get("error") if not value["ok"] else None)
            future.add_done_callback(relay)
        elif name == "test.error":
            self.reply(request["id"], error={"name": "NativeError", "message": "actual failure", "code": "BAD_VALUE", "data": {"field": "x"}})
        elif name != "test.wait":
            self.reply(request["id"], {"absent": True} if params is ABSENT else params)

    def disconnect(self):
        if self.connection:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()

    def close(self):
        self.stopped.set()
        self.disconnect()
        self.listener.close()
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError("Mock peer reader leaked")


class PythonClientTests(unittest.TestCase):
    def tearDown(self):
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            live = [t.name for t in threading.enumerate() if t.name.startswith("auroraview-python-")]
            if not live:
                return
            time.sleep(0.02)
        self.fail("Client threads leaked: " + repr(live))

    def test_handshake_identity_bom_crlf_and_ping(self):
        with Peer(bom=True) as host, Client(host.port, TOKEN, expected_pid=1234, expected_engine="5.7.", expected_context="game") as client:
            hello = host.wait_frame(lambda f: f["type"] == "hello")
            self.assertEqual(hello["protocol"], 1)
            self.assertEqual(hello["data"]["token"], TOKEN)
            self.assertEqual(set(hello["data"]["capabilities"]), {"event", "rpc", "tools"})
            self.assertEqual(host.wait_frame(lambda f: f.get("event") == "child:ready")["data"]["child_id"], client.child_id)
            self.assertEqual(client.identity["pid"], 1234)
            host.send({"type": "ping", "protocol": 1})
            self.assertEqual(host.wait_frame(lambda f: f["type"] == "pong")["protocol"], 1)
            host.send({"type": "future-upstream-extension"})
            self.assertEqual(client.call("test.echo", "still open"), "still open")

    def test_reject_identity_and_unnegotiated_protocol(self):
        for changes, options in [({"accepted": False}, {}), ({"protocol": 2}, {}),
                                 ({}, {"expected_pid": 999}), ({}, {"expected_engine": "5.6"}),
                                 ({}, {"expected_context": "editor"})]:
            with self.subTest(changes=changes, options=options):
                ack = copy.deepcopy(ACK)
                ack.update(changes)
                with Peer(ack) as host, self.assertRaises(ProtocolError):
                    Client(host.port, TOKEN, timeout=0.3, **options)
        for field in ("rpc_protocol", "capabilities", "pid"):
            ack = copy.deepcopy(ACK)
            del ack["data"][field]
            with self.subTest(field=field), Peer(ack) as host, self.assertRaises(ProtocolError):
                Client(host.port, TOKEN, timeout=0.3)

    def test_missing_ack_never_degrades_to_legacy(self):
        with Peer(None) as host, self.assertRaises(TimeoutError):
            Client(host.port, TOKEN, timeout=0.15)
        with self.assertRaises(ValueError):
            Client(1234, TOKEN, host="localhost")

    def test_core_calls_preserve_parameter_shape_and_errors(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            self.assertEqual(client.call("test.echo"), {"absent": True})
            for value in (None, 13, "unicode 源", [1, 2], {"a": 3}):
                self.assertEqual(client.call("test.echo", value), value)
            with self.assertRaises(RemoteError) as failure:
                client.call("test.error")
            self.assertEqual((failure.exception.name, failure.exception.code, failure.exception.data),
                             ("NativeError", "BAD_VALUE", {"field": "x"}))

    def test_reverse_parameter_mapping_and_registration_schema(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            @client.bind_call("python.zero")
            def zero():
                return "zero"
            @client.bind_call("python.add")
            def add(x: int, y: int = 1):
                """Add two values."""
                return x + y
            @client.bind_call("python.scalar")
            def scalar(value):
                return {"value": value}
            self.assertEqual(host.reverse("python.zero").result(2)["result"], "zero")
            self.assertEqual(host.reverse("python.add", {"x": 4, "y": 8}).result(2)["result"], 12)
            self.assertEqual(host.reverse("python.add", [2, 3]).result(2)["result"], 5)
            self.assertEqual(host.reverse("python.scalar", False).result(2)["result"], {"value": False})
            schema = next(t for t in host.registrations if t["name"] == "python.add")
            self.assertEqual(schema["description"], "Add two values.")
            self.assertEqual(schema["parameters"]["properties"]["x"], {"type": "integer"})
            self.assertEqual(schema["parameters"]["required"], ["x"])
            client.unbind_call("python.zero")
            self.assertEqual(host.reverse("python.zero").result(2)["error"]["code"], "METHOD_NOT_FOUND")

    def test_api_binding_collisions_and_reserved_names(self):
        class API:
            def echo(self, value: str):
                return value
            def _private(self):
                return "hidden"
        with Peer() as host, Client(host.port, TOKEN) as client:
            client.bind_api(API())
            self.assertEqual(client.call("api.echo", {"value": "native reverse route"}), "native reverse route")
            self.assertEqual(host.tools, {"api.echo"})
            with self.assertRaises(ValueError):
                client.bind_api(API())
            client.bind_api(API(), allow_rebind=True)
            for reserved in ("auroraview.host.shutdown", "unreal.objects.get"):
                with self.assertRaises(ValueError):
                    client.bind_call(reserved, lambda: None)

    def test_registration_failure_rolls_back_local_handler(self):
        with Peer(reject_registration=True) as host, Client(host.port, TOKEN) as client:
            with self.assertRaises(RemoteError):
                client.bind_call("python.denied", lambda: "must not run")
            self.assertEqual(host.reverse("python.denied").result(2)["error"]["code"], "METHOD_NOT_FOUND")

    def test_explicit_contract_preserves_schemas_annotations_and_ownership(self):
        descriptor = {"name": "python.explicit", "description": "An explicit shared contract.",
                      "inputSchema": {"type": "object", "properties": {"value": {"type": "boolean"}},
                                      "required": ["value"], "additionalProperties": False},
                      "outputSchema": {"type": "boolean"},
                      "annotations": {"readOnlyHint": True, "destructiveHint": False,
                                      "idempotentHint": True}, "extension": {"version": 1}}
        original = copy.deepcopy(descriptor)
        with Peer() as host, Client(host.port, TOKEN) as client:
            handler = lambda value: value
            first = client.bind_tool(descriptor, handler)
            self.assertEqual(host.registrations[-1], dict(original, parameters=original["inputSchema"]))
            self.assertEqual(descriptor, original)
            descriptor["inputSchema"]["required"].clear()
            self.assertEqual(host.registrations[-1]["inputSchema"]["required"], ["value"])
            self.assertIs(host.reverse("python.explicit", {"value": False}).result(2)["result"], False)
            second = client.bind_tool(original, handler, allow_rebind=True)
            first.close()
            first.close()
            self.assertIn("python.explicit", host.tools)
            self.assertIs(host.reverse("python.explicit", {"value": True}).result(2)["result"], True)
            second.close()
            self.assertNotIn("python.explicit", host.tools)
            self.assertEqual(host.reverse("python.explicit", {}).result(2)["error"]["code"], "METHOD_NOT_FOUND")

    def test_explicit_handle_cannot_remove_legacy_replacement_and_survives_disconnect(self):
        descriptor = {"name": "python.explicit", "description": "Read a value.", "inputSchema": {"type": "object"}}
        with Peer() as host, Client(host.port, TOKEN) as client:
            handle = client.bind_tool(descriptor, lambda: "old")
            client.bind_call("python.explicit", lambda: "new")
            handle.close()
            self.assertEqual(host.reverse("python.explicit", {}).result(2)["result"], "new")
            owned = client.bind_tool(dict(descriptor, name="python.other"), lambda: None)
            client.close()
            owned.close()
            owned.close()

    def test_explicit_contract_validation_and_registration_failure(self):
        descriptor = {"name": "python.explicit", "description": "Read a value.", "inputSchema": {"type": "object"}}
        with Peer(reject_registration=True) as host, Client(host.port, TOKEN) as client:
            for change in ({"inputSchema": []}, {"description": ""}, {"outputSchema": []},
                           {"annotations": {"readOnlyHint": "yes"}}, {"parameters": {}},
                           {"inputSchema": {"enum": [float("nan")]}}, {"extension": {1: "non-string key"}}):
                with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                    client.bind_tool(dict(descriptor, **change), lambda: None)
            with self.assertRaises(RemoteError):
                client.bind_tool(descriptor, lambda: "must not run")
            self.assertEqual(host.reverse("python.explicit", {}).result(2)["error"]["code"], "METHOD_NOT_FOUND")

    def test_explicit_native_unregistration_failure_can_be_retried(self):
        descriptor = {"name": "python.explicit", "description": "Read a value.", "inputSchema": {"type": "object"}}
        with Peer() as host, Client(host.port, TOKEN) as client:
            handle = client.bind_tool(descriptor, lambda: "still owned")
            with mock.patch.object(client, "call", side_effect=TimeoutError("Native unregister unavailable")):
                with self.assertRaisesRegex(TimeoutError, "Native unregister unavailable"):
                    handle.close()
            self.assertEqual(host.reverse("python.explicit", {}).result(2)["result"], "still owned")
            handle.close()
            handle.close()
            self.assertNotIn("python.explicit", host.tools)

    def test_events_are_bidirectional_and_callbacks_can_call(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            received, complete = [], threading.Event()
            def callback(value):
                received.append(client.call("test.echo", value))
                complete.set()
            unsubscribe = client.on("selection.changed", callback)
            client.emit("selection.changed", {"actors": ["A"]})
            self.assertTrue(complete.wait(2))
            self.assertEqual(received, [{"actors": ["A"]}])
            unsubscribe()
            client.emit("selection.changed", {"actors": ["B"]})
            time.sleep(0.1)
            self.assertEqual(len(received), 1)

    def test_reverse_handler_can_make_synchronous_native_call(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            client.bind_call("python.nested", lambda value: client.call("test.echo", value))
            self.assertEqual(client.call("python.nested", {"value": 41}), 41)
            client.bind_call("python.failure", lambda: 1 / 0)
            self.assertEqual(host.reverse("python.failure").result(2)["error"]["name"], "ZeroDivisionError")

    def test_timeout_cancellation_pending_bound_and_late_reply(self):
        with Peer() as host, Client(host.port, TOKEN, timeout=0.2, max_pending=1) as client:
            pending = client.call_async("test.wait")
            with self.assertRaises(AuroraViewError):
                client.call_async("test.echo")
            request = host.calls.get(timeout=1)
            with self.assertRaises(TimeoutError):
                pending.result(1)
            host.reply(request["id"], "late reply must be ignored")
            cancelled = client.call_async("test.wait")
            self.assertTrue(cancelled.cancel())
            self.assertEqual(client.call("test.echo", 9), 9)

    def test_disconnect_fails_pending_and_closes_reader(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            pending = client.call_async("test.wait")
            host.disconnect()
            with self.assertRaises(ConnectionClosedError):
                pending.result(2)

    def test_malformed_and_oversized_frames_fail_pending(self):
        for raw in (b"{broken\n", b"[]\n", b"\xff\n", b"x" * MAX_FRAME_BYTES,
                    json.dumps({"type": "event", "event": RESULT, "data": {"id": "bad", "ok": "true"}}).encode() + b"\n"):
            with self.subTest(prefix=raw[:30]), Peer() as host, Client(host.port, TOKEN) as client:
                pending = client.call_async("test.wait")
                host.send_raw(raw)
                with self.assertRaises(ProtocolError):
                    pending.result(2)

    def test_deeply_nested_frame_fails_pending_and_closes_reader(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            pending = client.call_async("test.wait")
            # Valid JSON below the byte limit can still exceed json.loads' stack.
            raw = b'{"type":"event","event":"nested","data":' + b'[' * 50000 + b'0' + b']' * 50000 + b'}\n'
            self.assertLess(len(raw), MAX_FRAME_BYTES)
            host.send_raw(raw)
            with self.assertRaises(ProtocolError):
                pending.result(2)
            client._reader.join(1)
            self.assertFalse(client._reader.is_alive())

    def test_large_valid_frame_and_outgoing_limit(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            host.event("large", "x" * (MAX_FRAME_BYTES - 100))
            self.assertEqual(client.call("test.echo", "after large event"), "after large event")
            with self.assertRaises(ProtocolError):
                client.call("test.echo", "x" * MAX_FRAME_BYTES)
            self.assertEqual(client.call("test.echo", "still usable"), "still usable")

    def test_frame_limit_counts_bom_crlf_and_handles_fragmented_utf8(self):
        with Peer() as host, Client(host.port, TOKEN) as client:
            frame = {"type": "event", "event": "boundary", "data": ""}
            empty = len(json.dumps(frame, separators=(",", ":")).encode())
            frame["data"] = "x" * (MAX_FRAME_BYTES - empty - 5)
            # Three BOM bytes and CRLF are included in the 1 MiB frame limit.
            host.send(frame, bom=True)
            self.assertEqual(client.call("test.echo", "exact boundary accepted"), "exact boundary accepted")
            delivered, complete = [], threading.Event()
            client.on("unicode", lambda data: (delivered.append(data), complete.set()))
            raw = json.dumps({"type": "event", "event": "unicode", "data": "界"}, ensure_ascii=False).encode() + b"\n"
            split = raw.index("界".encode()) + 1
            host.send_raw(raw[:split])
            host.send_raw(raw[split:])
            self.assertTrue(complete.wait(1))
            self.assertEqual(delivered, ["界"])
            pending = client.call_async("test.wait")
            frame["data"] += "x"
            host.send(frame, bom=True)
            with self.assertRaises(ProtocolError):
                pending.result(2)

    def test_bounded_reverse_workers_report_busy(self):
        release, entered = threading.Event(), threading.Event()
        with Peer() as host, Client(host.port, TOKEN, max_workers=1, max_queued=1) as client:
            def blocking():
                entered.set()
                release.wait(2)
                return True
            client.bind_call("python.blocking", blocking)
            first = host.reverse("python.blocking")
            self.assertTrue(entered.wait(1))
            second = host.reverse("python.blocking")
            # Reader is independent of the blocked worker and its full queue.
            third = host.reverse("python.blocking")
            try:
                self.assertEqual(third.result(2)["error"]["code"], "HANDLER_BUSY")
            finally:
                release.set()
            self.assertTrue(first.result(2)["result"])
            self.assertTrue(second.result(2)["result"])

    def test_close_from_callback_has_no_self_join_or_thread_leak(self):
        complete = threading.Event()
        with Peer() as host, Client(host.port, TOKEN) as client:
            def closing(_):
                client.close()
                complete.set()
            client.on("close", closing)
            host.event("close", None)
            self.assertTrue(complete.wait(2))

    def test_reverse_base_exceptions_reply_and_preserve_worker(self):
        with Peer() as host, Client(host.port, TOKEN, max_workers=1) as client:
            client.bind_call("python.echo", lambda value: value)
            for exception in (SystemExit, KeyboardInterrupt, GeneratorExit):
                with self.subTest(exception=exception.__name__):
                    def abort(exception=exception):
                        raise exception("script aborted")
                    client.bind_call("python.abort", abort)
                    reply = host.reverse("python.abort").result(1)
                    self.assertFalse(reply["ok"])
                    self.assertEqual(reply["error"]["name"], exception.__name__)
                    self.assertEqual(reply["error"]["code"], "PYTHON_ERROR")
                    self.assertEqual(client.call("python.echo", {"value": 42}), 42)

    def test_event_base_exception_does_not_kill_worker(self):
        complete = threading.Event()
        with Peer() as host, Client(host.port, TOKEN, max_workers=1) as client:
            def abort(_):
                raise SystemExit("event aborted")
            client.on("abort", abort)
            client.on("after.abort", lambda _: complete.set())
            with self.assertLogs("auroraview_unreal.client", level="ERROR"):
                host.event("abort", None)
                host.event("after.abort", None)
                self.assertTrue(complete.wait(1))
            self.assertEqual(client.call("test.echo", 42), 42)

    def test_concurrent_worker_close_never_joins_another_callback(self):
        barrier, outcomes = threading.Barrier(2), queue.Queue()
        with Peer() as host, Client(host.port, TOKEN, timeout=0.2, max_workers=2) as client:
            def closing(_):
                try:
                    barrier.wait(1)
                    client.close()
                    outcomes.put(None)
                except BaseException as error:
                    outcomes.put(error)
            client.on("close.a", closing)
            client.on("close.b", closing)
            host.event("close.a", None)
            host.event("close.b", None)
            self.assertIsNone(outcomes.get(timeout=1))
            self.assertIsNone(outcomes.get(timeout=1))

    def test_future_callback_rejects_reader_wait_but_can_schedule_call(self):
        outcome = queue.Queue()
        with Peer() as host, Client(host.port, TOKEN, timeout=0.2) as client:
            first = client.call_async("test.wait")
            request = host.calls.get(timeout=1)
            def completed(_):
                try:
                    client.call("test.echo", "must not send")
                except Exception as error:
                    outcome.put(error)
                outcome.put(client.call_async("test.echo", 42))
            first.add_done_callback(completed)
            host.reply(request["id"], True)
            error = outcome.get(timeout=1)
            self.assertIsInstance(error, AuroraViewError)
            self.assertIn("nonblocking", str(error))
            self.assertEqual(outcome.get(timeout=1).result(1), 42)
            self.assertEqual(host.calls.get(timeout=1)["params"], 42)

    def test_reader_and_worker_close_do_not_join_each_other(self):
        barrier, outcomes = threading.Barrier(2), queue.Queue()
        with Peer() as host, Client(host.port, TOKEN, timeout=0.2, max_workers=1) as client:
            def closing(_):
                try:
                    barrier.wait(1)
                    client.close()
                    outcomes.put(None)
                except BaseException as error:
                    outcomes.put(error)
            client.on("close.worker", closing)
            first = client.call_async("test.wait")
            request = host.calls.get(timeout=1)
            first.add_done_callback(closing)
            host.event("close.worker", None)
            host.reply(request["id"], True)
            self.assertIsNone(outcomes.get(timeout=1))
            self.assertIsNone(outcomes.get(timeout=1))

    def test_external_close_reports_reader_callback_that_did_not_return(self):
        entered, release = threading.Event(), threading.Event()
        with Peer() as host, Client(host.port, TOKEN, timeout=0.15) as client:
            first = client.call_async("test.wait")
            request = host.calls.get(timeout=1)
            def blocked(_):
                entered.set()
                release.wait(2)
            first.add_done_callback(blocked)
            host.reply(request["id"], True)
            self.assertTrue(entered.wait(1))
            try:
                with self.assertRaisesRegex(TimeoutError, "reader"):
                    client.close()
            finally:
                release.set()


if __name__ == "__main__":
    unittest.main()
