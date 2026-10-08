"""Optional shared ToolSet consumer for an existing external Unreal Client.

The host owns the ToolSet, Client and dispatcher. This adapter owns one borrowed
session and its native registrations; it creates no thread, event loop, server
or tool registry. Import this module only when the dcc-mcp extra is installed.
"""

from concurrent.futures import Future, TimeoutError as FutureTimeoutError
import math
import threading

from auroraview_dcc_mcp import CleanupError, ClosedError, ThreadError, ToolSet


class NativeToolBinding:
    """Bind shared contracts to the existing native transport explicitly.

    Construct/register/subscribe/close on the ToolSet owner's thread. dispatch
    must accept a zero-argument callable and return a concurrent.futures.Future;
    the host pumps its existing owner loop and honors Future cancellation before
    executing queued work. Running handlers finish cooperatively. Host event
    sources must also deliver on that same owner thread.
    """

    def __init__(self, client, tools, *, dispatch, timeout=5.0):
        if not isinstance(tools, ToolSet):
            raise TypeError("tools must be a shared ToolSet")
        if not callable(dispatch):
            raise TypeError("dispatch must be callable")
        duration = float(timeout)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("timeout must be finite and positive")
        if not callable(getattr(client, "bind_tool", None)) or not callable(getattr(client, "emit", None)):
            raise TypeError("client must provide bind_tool and emit")
        self._client, self._dispatch, self._timeout = client, dispatch, duration
        self._owner = threading.get_ident()
        self._lock = threading.Lock()
        self._closed = False
        self._registered = False
        self._handles = []
        self._pending = set()
        self._session = tools.borrow()

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise ThreadError("Native binding lifecycle must run on its tool owner's thread")

    def _check_open(self):
        if self._closed:
            raise ClosedError("The native tool binding has been released")

    def register(self):
        """Publish the borrowed ToolSet descriptors once; return this binding."""
        self._check_owner()
        with self._lock:
            self._check_open()
        if self._registered:
            return self
        try:
            for descriptor in self._session.list_tools():
                self._handles.append(self._client.bind_tool(descriptor, self._handler(descriptor["name"])))
        except Exception:
            # Revoke the lease and undo any already published registrations.
            # Preserve the registration failure if teardown also fails.
            try:
                self.close()
            except CleanupError:
                pass
            raise
        self._registered = True
        return self

    def _handler(self, name):
        def invoke(**params):
            return self.call(name, params)
        return invoke

    def call(self, method, params=None):
        """Invoke on the owner, dispatching only calls from foreign workers."""
        with self._lock:
            self._check_open()
        if threading.get_ident() == self._owner:
            return self._session.call(method, params)

        def invoke():
            self._check_owner()
            with self._lock:
                self._check_open()
            return self._session.call(method, params)

        future = self._dispatch(invoke)
        if not isinstance(future, Future):
            raise TypeError("dispatch must return concurrent.futures.Future")
        with self._lock:
            if self._closed:
                future.cancel()
                raise ClosedError("The native tool binding has been released")
            self._pending.add(future)
        try:
            return future.result(timeout=self._timeout)
        except FutureTimeoutError:
            if future.done():
                # Future.result also raises a handler's own TimeoutError. It is
                # a completed business failure, not an owner-queue timeout.
                return future.result()
            future.cancel()
            raise TimeoutError("The tool owner's dispatch did not complete in time") from None
        finally:
            with self._lock:
                self._pending.discard(future)

    def subscribe(self, event, callback):
        """Borrow one owner-delivered host event subscription."""
        self._check_owner()
        with self._lock:
            self._check_open()
        return self._session.subscribe(event, callback)

    def emit(self, event, data=None):
        """Send exactly one event through the existing native Client."""
        with self._lock:
            self._check_open()
        self._client.emit(event, data)

    def close(self):
        """Revoke this consumer only; retry any failed native cleanup."""
        self._check_owner()
        with self._lock:
            self._closed = True
            pending = tuple(self._pending)
        for future in pending:
            future.cancel()
        errors, remaining = [], []
        for handle in self._handles:
            try:
                handle.close()
            except Exception as error:
                errors.append(error)
                remaining.append(handle)
        self._handles = remaining
        try:
            self._session.close()
        except CleanupError as error:
            errors.extend(error.errors)
        if errors:
            raise CleanupError(errors)

    def __enter__(self):
        with self._lock:
            self._check_open()
        return self

    def __exit__(self, *_):
        self.close()
