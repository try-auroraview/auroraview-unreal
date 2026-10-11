"""Stdlib-only external client; no dependency on Unreal's embedded Python.

Transport is upstream parent IPC v1. The negotiated auroraview.unreal/1 RPC
extension carries unchanged AuroraView Core call/result envelopes in events.
Event callbacks and reverse handlers run on bounded workers and may make
synchronous calls to the host. They must return cooperatively; Python cannot
forcibly stop a user thread.
"""

import inspect
import json
import logging
import math
import queue
import socket
import threading
import time
import types
import typing
import uuid
from concurrent.futures import Future, InvalidStateError, TimeoutError as FutureTimeoutError

MAX_FRAME_BYTES = 1024 * 1024
RPC_EVENT = "__auroraview_rpc"
RESULT_EVENT = "__auroraview_call_result"
RPC_PROTOCOL = "auroraview.unreal/1"
_ABSENT = object()
_LOG = logging.getLogger(__name__)


def _complete(future, *, result=None, error=None):
    try:
        if error is None:
            future.set_result(result)
        else:
            future.set_exception(error)
    except InvalidStateError:
        # Cancellation can race a reply, timeout or disconnect on another thread.
        pass


class AuroraViewError(Exception):
    """Base exception for client and remote failures."""


class ProtocolError(AuroraViewError):
    """The peer violated the negotiated wire contract."""


class ConnectionClosedError(AuroraViewError):
    """The connection closed before an operation completed."""


class RemoteError(AuroraViewError):
    def __init__(self, name, message, code=None, data=None):
        super().__init__(message)
        self.name, self.message, self.code, self.data = name, message, code, data


class _Workers:
    """Fixed worker count and bounded waiting queue, including event callbacks."""

    def __init__(self, count, capacity, name):
        self._queue = queue.Queue(maxsize=capacity)
        self._stopped = threading.Event()
        self.threads = [threading.Thread(target=self._run, name=name + "-worker-" + str(i), daemon=True)
                        for i in range(count)]
        for thread in self.threads:
            thread.start()

    def submit(self, function):
        if self._stopped.is_set():
            return False
        try:
            self._queue.put_nowait(function)
            return True
        except queue.Full:
            return False

    def _run(self):
        while not self._stopped.is_set():
            try:
                function = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                if not self._stopped.is_set():
                    function()
            except BaseException:
                # Script exits belong to that callback, not to the worker pool.
                _LOG.exception("AuroraView Python callback failed")
            finally:
                self._queue.task_done()

    def stop(self):
        self._stopped.set()
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def join(self, deadline):
        current = threading.current_thread()
        for thread in self.threads:
            if thread is not current:
                thread.join(max(0, deadline - time.monotonic()))
        return [thread.name for thread in self.threads if thread is not current and thread.is_alive()]


def _schema(annotation):
    if annotation in (inspect.Parameter.empty, typing.Any):
        return {}
    primitive = {str: "string", bool: "boolean", int: "integer", float: "number", type(None): "null"}
    if annotation in primitive:
        return {"type": primitive[annotation]}
    origin, arguments = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, getattr(types, "UnionType", typing.Union)):
        return {"anyOf": [_schema(value) for value in arguments]}
    if origin is typing.Literal:
        return {"enum": list(arguments)}
    if annotation is list or origin in (list, tuple, typing.Sequence):
        return {"type": "array", "items": _schema(arguments[0]) if arguments else {}}
    if annotation is dict or origin is dict:
        return {"type": "object"}
    return {}


def _tool(method, function):
    signature = inspect.signature(function)
    try:
        annotations = typing.get_type_hints(function)
    except (NameError, TypeError):
        annotations = getattr(function, "__annotations__", {})
    properties, required, additional = {}, [], False
    for name, parameter in signature.parameters.items():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            additional = True
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        properties[name] = _schema(annotations.get(name, parameter.annotation))
        if parameter.default is inspect.Parameter.empty:
            required.append(name)
    parameters = {"type": "object", "properties": properties, "additionalProperties": additional}
    if required:
        parameters["required"] = required
    return {"name": method, "description": inspect.getdoc(function) or "", "parameters": parameters}


class ToolBinding:
    """Own one explicit native tool registration, without owning the client.

    Closing an older handle never removes a handler that replaced it. Failed
    native unregistration can be retried by calling close again.
    """

    def __init__(self, client, method, handler):
        self._client, self._method, self._handler = client, method, handler
        self._closed = False

    def close(self):
        with self._client._binding_lock:
            if not self._closed:
                self._client._unbind_owned(self._method, self._handler)
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Client:
    """Connect to an explicitly authorized loopback Unreal host.

    Native launch flags: -AuroraViewHostPort=<port>
    -AuroraViewHostToken=<32+ characters> -AuroraViewAllowControl.
    Construction requires an explicit successful versioned hello acknowledgement.
    """

    def __init__(self, port, token, *, host="127.0.0.1", expected_pid=None,
                 expected_engine=None, expected_context=None, timeout=5.0,
                 max_pending=128, max_workers=4, max_queued=32):
        if host != "127.0.0.1":
            raise ValueError("Only literal 127.0.0.1 is allowed")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be an integer from 1 to 65535")
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("token must contain at least 32 characters")
        if not math.isfinite(timeout) or timeout <= 0 or any(type(value) is not int or value <= 0 for value in (max_pending, max_workers, max_queued)):
            raise ValueError("timeouts must be finite and positive; worker/pending limits must be positive")
        self.timeout = float(timeout)
        self.child_id = "python-" + uuid.uuid4().hex
        self._expected = expected_pid, expected_engine, expected_context
        self._identity = None
        self._lock, self._send_lock = threading.Lock(), threading.Lock()
        self._binding_lock = threading.RLock()
        self._closed, self._ready = threading.Event(), threading.Event()
        self._failure = None
        self._pending, self._handlers, self._events = {}, {}, {}
        self._reverse_ids = set()
        self._max_pending = max_pending
        self._socket = socket.create_connection((host, port), timeout=self.timeout)
        self._socket.settimeout(0.1)
        self._workers = _Workers(max_workers, max_queued, "auroraview-" + self.child_id)
        self._reader = threading.Thread(target=self._read_loop, name="auroraview-" + self.child_id + "-reader", daemon=True)
        try:
            self._send({"type": "hello", "protocol": 1, "child_id": self.child_id,
                        "data": {"capabilities": ["event", "rpc", "tools"], "token": token}})
            self._reader.start()
            if not self._ready.wait(self.timeout):
                raise TimeoutError("Unreal host did not acknowledge the negotiated RPC protocol")
            self._ensure_open()
        except BaseException:
            self.close()
            raise

    @property
    def identity(self):
        return dict(self._identity) if self._identity else None

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *_):
        self.close()

    def _ensure_open(self):
        if self._closed.is_set():
            raise self._failure or ConnectionClosedError("Client is closed")

    def _send(self, frame):
        self._ensure_open()
        encoded = (json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_FRAME_BYTES:
            raise ProtocolError("Outgoing parent IPC frame exceeds 1 MiB")
        try:
            with self._send_lock:
                self._ensure_open()
                self._socket.sendall(encoded)
        except OSError as error:
            closed = ConnectionClosedError("Unreal host transport failed: " + str(error))
            self._abort(closed)
            raise closed from error

    def call_async(self, method, params=_ABSENT, *, timeout=None):
        """Return a standard Future whose done callbacks must be nonblocking.

        Reply and timeout callbacks normally execute on the socket reader.
        Schedule another call with call_async; do not wait for it in a callback.
        """
        self._ensure_open()
        if not isinstance(method, str) or not method or len(method) > 256:
            raise ValueError("method must be a nonempty string of at most 256 characters")
        duration = self.timeout if timeout is None else float(timeout)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("timeout must be finite and positive")
        request_id, future = uuid.uuid4().hex, Future()
        with self._lock:
            self._ensure_open()
            if len(self._pending) >= self._max_pending:
                raise AuroraViewError("Too many pending Unreal calls")
            self._pending[request_id] = (future, time.monotonic() + duration)
        future.add_done_callback(lambda _: self._forget(request_id))
        envelope = {"type": "call", "id": request_id, "method": method}
        if params is not _ABSENT:
            envelope["params"] = params
        try:
            self._send({"type": "event", "event": RPC_EVENT, "data": envelope})
        except Exception:
            self._forget(request_id)
            future.cancel()
            raise
        return future

    def _forget(self, request_id):
        with self._lock:
            self._pending.pop(request_id, None)

    def call(self, method, params=_ABSENT, *, timeout=None):
        if threading.current_thread() is self._reader:
            raise AuroraViewError("Future done callbacks on the socket reader must be nonblocking; use call_async")
        future = self.call_async(method, params, timeout=timeout)
        duration = self.timeout if timeout is None else float(timeout)
        try:
            return future.result(timeout=duration + 0.2)
        except FutureTimeoutError:
            future.cancel()
            raise TimeoutError("Unreal call timed out: " + method) from None

    def emit(self, event, data=None):
        if not isinstance(event, str) or not event or event in (RPC_EVENT, RESULT_EVENT):
            raise ValueError("event must be an ordinary nonempty event name")
        self._send({"type": "event", "event": event, "data": data})

    send_event = emit

    def on(self, event, callback):
        if not isinstance(event, str) or not event or event in (RPC_EVENT, RESULT_EVENT) or not callable(callback):
            raise ValueError("on requires an ordinary event name and callable")
        with self._lock:
            self._ensure_open()
            self._events.setdefault(event, []).append(callback)

        def unsubscribe():
            with self._lock:
                callbacks = self._events.get(event, [])
                if callback in callbacks:
                    callbacks.remove(callback)
        return unsubscribe

    @staticmethod
    def _validate_tool_name(method):
        if not isinstance(method, str) or not method or len(method) > 256 or method.startswith(("auroraview.", "unreal.")):
            raise ValueError("Tool names must be nonempty and cannot use reserved auroraview. or unreal. namespaces")

    def _bind(self, functions, allow_rebind, descriptors=None, timeout=None):
        for name, function in functions.items():
            self._validate_tool_name(name)
            if not callable(function):
                raise TypeError("Tool handler must be callable")
        tools = descriptors if descriptors is not None else [_tool(name, function) for name, function in functions.items()]
        with self._binding_lock:
            with self._lock:
                self._ensure_open()
                if not allow_rebind and any(name in self._handlers for name in functions):
                    raise ValueError("A tool with this name is already bound")
                previous = {name: self._handlers.get(name, _ABSENT) for name in functions}
                self._handlers.update(functions)
            try:
                if timeout is None:
                    self.call("auroraview.tools.register", {"tools": tools})
                else:
                    duration = float(timeout() if callable(timeout) else timeout)
                    if not math.isfinite(duration) or duration <= 0:
                        raise ValueError("Registration timeout must be finite and positive")
                    self.call("auroraview.tools.register", {"tools": tools}, timeout=duration)
            except Exception:
                with self._lock:
                    for name, function in functions.items():
                        if self._handlers.get(name) is function:
                            if previous[name] is _ABSENT:
                                self._handlers.pop(name, None)
                            else:
                                self._handlers[name] = previous[name]
                raise

    def bind_tool(self, descriptor, handler, *, allow_rebind=False, timeout=None):
        """Publish an explicit JSON tool contract and return its ownership handle.

        inputSchema/outputSchema/annotations are retained unchanged. parameters
        mirrors inputSchema for consumers of the original native SDK catalog.
        Schema validation remains the declared tool handler's responsibility.
        timeout is a per-registration RPC duration or zero-argument callback
        returning that duration immediately before dispatch. Callback failures
        roll back the local handler; omission retains the client default.
        """
        if not isinstance(descriptor, dict):
            raise TypeError("Tool descriptor must be a JSON object")
        if not callable(handler):
            raise TypeError("Tool handler must be callable")
        self._validate_tool_name(descriptor.get("name"))
        if not isinstance(descriptor.get("description"), str) or not descriptor["description"]:
            raise ValueError("Tool descriptor requires a nonempty description")
        if not isinstance(descriptor.get("inputSchema"), dict):
            raise ValueError("Tool descriptor requires an inputSchema object")
        if "outputSchema" in descriptor and not isinstance(descriptor["outputSchema"], dict):
            raise ValueError("Tool outputSchema must be an object")
        if "annotations" in descriptor:
            annotations = descriptor["annotations"]
            if not isinstance(annotations, dict) or any(
                    key in annotations and type(annotations[key]) is not bool
                    for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")):
                raise ValueError("Tool annotation hints must be booleans")
        if "parameters" in descriptor and descriptor["parameters"] != descriptor["inputSchema"]:
            raise ValueError("Tool parameters must match inputSchema")

        def check_keys(value):
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("Tool descriptor object keys must be strings")
                for child in value.values():
                    check_keys(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    check_keys(child)

        check_keys(descriptor)
        contract = json.loads(json.dumps(descriptor, allow_nan=False))
        contract["parameters"] = contract["inputSchema"]

        # A fresh wrapper gives each handle a distinct ownership identity even
        # when callers intentionally reuse the same business function.
        def invoke(*args, **kwargs):
            return handler(*args, **kwargs)

        self._bind({contract["name"]: invoke}, allow_rebind, [contract], timeout=timeout)
        return ToolBinding(self, contract["name"], invoke)

    def bind_call(self, method, func=None, *, allow_rebind=True, timeout=None):
        """Publish a handler directly or as a decorator.

        timeout is a per-registration RPC duration or zero-argument callback
        evaluated immediately before dispatch, including decorator application.
        Callback failures roll back the local handler. Omission retains the
        client default; subsequent calls and cleanup keep their normal timeout.
        """
        def bind(function):
            self._bind({method: function}, allow_rebind, timeout=timeout)
            return function
        return bind if func is None else bind(func)

    def bind_api(self, api, namespace="api", *, allow_rebind=False):
        if not isinstance(namespace, str) or not namespace:
            raise ValueError("namespace must be a nonempty string")
        functions = {namespace + "." + name: function for name, function in inspect.getmembers(api, callable)
                     if not name.startswith("_")}
        if not functions:
            raise ValueError("API exposes no public callable methods")
        self._bind(functions, allow_rebind)
        return api

    def unbind_call(self, method):
        self._validate_tool_name(method)
        with self._binding_lock:
            self.call("auroraview.tools.unregister", {"names": [method]})
            with self._lock:
                self._handlers.pop(method, None)

    def _unbind_owned(self, method, handler):
        with self._lock:
            if self._handlers.get(method) is not handler:
                return
        self.call("auroraview.tools.unregister", {"names": [method]})
        with self._lock:
            if self._handlers.get(method) is handler:
                self._handlers.pop(method, None)

    def _acknowledge(self, frame):
        data = frame.get("data")
        if (self._identity is not None or type(frame.get("protocol")) is not int or frame["protocol"] != 1
                or frame.get("accepted") is not True or not isinstance(data, dict)
                or data.get("rpc_protocol") != RPC_PROTOCOL
                or not isinstance(data.get("capabilities"), list)
                or not all(isinstance(value, str) for value in data["capabilities"])
                or not {"event", "rpc", "tools"}.issubset(data["capabilities"])
                or type(data.get("pid")) is not int or data["pid"] <= 0
                or not isinstance(data.get("engine_version"), str) or not data["engine_version"]
                or not isinstance(data.get("context"), str) or not data["context"]
                or not isinstance(frame.get("parent_id"), str) or not frame["parent_id"]):
            raise ProtocolError("Unreal host did not explicitly accept parent IPC v1 and " + RPC_PROTOCOL)
        expected_pid, engine_prefix, context = self._expected
        if expected_pid is not None and data["pid"] != expected_pid:
            raise ProtocolError("Unreal host PID does not match the expected process")
        version = data["engine_version"]
        if engine_prefix is not None and not (version == engine_prefix or version.startswith(engine_prefix + ".")
                                               or version.startswith(engine_prefix + "-")
                                               or (engine_prefix.endswith((".", "-")) and version.startswith(engine_prefix))):
            raise ProtocolError("Unreal host engine version does not match")
        if context is not None and data["context"] != context:
            raise ProtocolError("Unreal host context does not match")
        self._identity = dict(data, parent_id=frame["parent_id"])
        self._send({"type": "event", "event": "child:ready", "data": {"child_id": self.child_id}})
        self._ready.set()

    def _result(self, envelope):
        if (not isinstance(envelope, dict) or not isinstance(envelope.get("id"), str)
                or not envelope["id"] or type(envelope.get("ok")) is not bool):
            raise ProtocolError("Malformed Core call result")
        error = None
        if not envelope["ok"]:
            value = envelope.get("error")
            if (not isinstance(value, dict) or not isinstance(value.get("name"), str)
                    or not isinstance(value.get("message"), str)
                    or ("code" in value and not isinstance(value["code"], str))):
                raise ProtocolError("Malformed Core error result")
            error = RemoteError(value["name"], value["message"], value.get("code"), value.get("data"))
        with self._lock:
            pending = self._pending.pop(envelope["id"], None)
        if pending:
            _complete(pending[0], error=error, result=envelope.get("result"))

    def _reverse(self, envelope):
        if (not isinstance(envelope, dict) or envelope.get("type") != "call"
                or not isinstance(envelope.get("id"), str) or not 1 <= len(envelope["id"]) <= 256
                or not isinstance(envelope.get("method"), str) or not 1 <= len(envelope["method"]) <= 256):
            raise ProtocolError("Malformed Core reverse call")
        request_id = envelope["id"]
        with self._lock:
            if request_id in self._reverse_ids:
                raise ProtocolError("Duplicate active reverse call ID")
            function = self._handlers.get(envelope["method"])
            if len(self._reverse_ids) >= self._max_pending:
                function = None
                error = {"name": "BusyError", "message": "Python reverse-call limit reached", "code": "HANDLER_BUSY"}
            else:
                error = {"name": "MethodNotFoundError", "message": "No registered Python tool", "code": "METHOD_NOT_FOUND"}
            if function is not None:
                self._reverse_ids.add(request_id)
        if function is None:
            self._reply(request_id, error=error)
            return

        def invoke():
            try:
                params = envelope.get("params", _ABSENT)
                if params is _ABSENT:
                    result = function()
                elif isinstance(params, dict):
                    result = function(**params)
                elif isinstance(params, list):
                    result = function(*params)
                else:
                    result = function(params)
                self._reply(request_id, result=result)
            except BaseException as exception:
                try:
                    self._reply(request_id, error={"name": type(exception).__name__, "message": str(exception), "code": "PYTHON_ERROR"})
                except (AuroraViewError, OSError):
                    pass
            finally:
                with self._lock:
                    self._reverse_ids.discard(request_id)
        if not self._workers.submit(invoke):
            with self._lock:
                self._reverse_ids.discard(request_id)
            self._reply(request_id, error={"name": "BusyError", "message": "Python worker queue is full", "code": "HANDLER_BUSY"})

    def _reply(self, request_id, result=None, error=None):
        value = {"id": request_id, "ok": error is None}
        value["result" if error is None else "error"] = result if error is None else error
        self._send({"type": "event", "event": RESULT_EVENT, "data": value})

    def _dispatch(self, frame):
        if not isinstance(frame, dict) or not isinstance(frame.get("type", "event"), str):
            raise ProtocolError("Parent IPC frame must be a JSON object with a string type")
        kind = frame.get("type", "event")
        if kind == "hello_ack":
            self._acknowledge(frame)
        elif kind == "ping":
            self._send({"type": "pong", "protocol": 1, "child_id": self.child_id})
        elif kind == "event":
            if self._identity is None:
                raise ProtocolError("Host sent an event before the authenticated acknowledgement")
            event = frame.get("event")
            if not isinstance(event, str) or not event:
                raise ProtocolError("Parent IPC event has no event name")
            data = frame.get("data")
            if event == RESULT_EVENT:
                self._result(data)
            elif event == RPC_EVENT:
                self._reverse(data)
            else:
                with self._lock:
                    callbacks = tuple(self._events.get(event, ()))
                for callback in callbacks:
                    if not self._workers.submit(lambda callback=callback: callback(data)):
                        raise ProtocolError("Python event callback queue is full")
        elif kind == "error":
            raise ProtocolError("Parent IPC transport error: " + str(frame.get("message", frame.get("data", "unspecified"))))
        # Upstream parent IPC accepts pong/hello and ignores unknown frame kinds.

    def _expire(self):
        now = time.monotonic()
        with self._lock:
            expired = [(request_id, value[0]) for request_id, value in self._pending.items() if value[1] <= now]
            for request_id, _ in expired:
                self._pending.pop(request_id, None)
        for _, future in expired:
            _complete(future, error=TimeoutError("Unreal call deadline expired"))

    def _read_loop(self):
        buffer = bytearray()
        try:
            while not self._closed.is_set():
                self._expire()
                try:
                    received = self._socket.recv(65536)
                except socket.timeout:
                    continue
                if not received:
                    if buffer:
                        raise ProtocolError("Unreal host disconnected with a truncated frame")
                    raise ConnectionClosedError("Unreal host disconnected")
                buffer.extend(received)
                while b"\n" in buffer:
                    position = buffer.index(b"\n")
                    if position + 1 > MAX_FRAME_BYTES:
                        raise ProtocolError("Incoming parent IPC frame exceeds 1 MiB")
                    raw = bytes(buffer[:position])
                    del buffer[:position + 1]
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    text = raw.decode("utf-8-sig")
                    if not text.strip():
                        continue
                    def reject_constant(value):
                        raise ProtocolError("Non-JSON number in parent IPC frame: " + value)
                    self._dispatch(json.loads(text, parse_constant=reject_constant))
                if len(buffer) >= MAX_FRAME_BYTES:
                    raise ProtocolError("Incoming parent IPC frame exceeds 1 MiB")
        except (OSError, UnicodeError, ValueError, TypeError, RecursionError, AuroraViewError) as error:
            self._abort(error if isinstance(error, AuroraViewError) else ProtocolError("Invalid parent IPC frame: " + str(error)))

    def _abort(self, error):
        with self._lock:
            if self._closed.is_set():
                return
            self._failure = error
            self._closed.set()
            pending = list(self._pending.values())
            self._pending.clear()
            self._events.clear()
            self._handlers.clear()
            self._reverse_ids.clear()
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._socket.close()
        self._workers.stop()
        self._ready.set()
        for future, _ in pending:
            _complete(future, error=error)

    def close(self):
        """Stop the connection; external callers also join the SDK threads.

        A reader/worker callback requests shutdown without waiting for peer
        callbacks, which may themselves be closing or waiting for this thread.
        """
        if not self._closed.is_set():
            try:
                if self._identity is not None:
                    self._send({"type": "event", "event": "child:closing", "data": {"child_id": self.child_id}})
            except (AuroraViewError, OSError):
                pass
            self._abort(ConnectionClosedError("Client closed"))
        current = threading.current_thread()
        if current is self._reader or current in self._workers.threads:
            return
        deadline = time.monotonic() + self.timeout
        if self._reader.ident is not None:
            self._reader.join(max(0, deadline - time.monotonic()))
        remaining = self._workers.join(deadline)
        if self._reader.is_alive():
            remaining.append(self._reader.name)
        if remaining:
            raise TimeoutError("Python callbacks did not return during close: " + ", ".join(remaining))

    shutdown = close
