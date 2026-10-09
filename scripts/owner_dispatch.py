"""Dispatch SDK callbacks through the demo's existing owner thread.

The caller pumps the queue in its existing main loop. This module creates no
thread, event loop or host service.
"""
from concurrent.futures import Future
import math
import queue
import threading


class OwnerDispatcher:
    def __init__(self, capacity=32):
        if type(capacity) is not int or capacity <= 0:
            raise ValueError('Dispatcher capacity must be a positive integer')
        self._owner = threading.get_ident()
        self._queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._closed = False
        self._wake = threading.Event()

    def _check_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError('Only the registered demo owner may pump or close dispatch')

    def __call__(self, function):
        if not callable(function):
            raise TypeError('Dispatch requires a callable')
        future = Future()
        with self._lock:
            if self._closed:
                future.set_exception(RuntimeError('The demo dispatcher is closed'))
            elif threading.get_ident() == self._owner:
                # Run outside the lock: a host handler can dispatch another call.
                pass
            else:
                try:
                    self._queue.put_nowait((future, function))
                    self._wake.set()
                except queue.Full:
                    future.set_exception(RuntimeError('The demo dispatch queue is full'))
                return future
        if not future.done():
            self._run(future, function)
        return future

    @staticmethod
    def _run(future, function):
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function())
            except BaseException as error:
                future.set_exception(error)

    def pump(self, limit=32):
        self._check_owner()
        if type(limit) is not int or limit <= 0:
            raise ValueError('Pump limit must be a positive integer')
        count = 0
        while count < limit:
            try:
                future, function = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._run(future, function)
            finally:
                self._queue.task_done()
            count += 1
        return count

    def wait_for_work(self, timeout=0.2):
        """Wait on the owner without executing callbacks; zero polls once.

        A finite platform-supported timeout bounds the wait. The caller retains
        its stop deadline, then pumps the existing bounded queue on this thread.
        """
        self._check_owner()
        if (type(timeout) not in (int, float) or not 0 <= timeout <= threading.TIMEOUT_MAX
                or not math.isfinite(timeout)):
            raise ValueError('Wait timeout must be finite, nonnegative and platform-supported')
        with self._lock:
            if self._closed:
                return False
            # Clear before testing the queue under the enqueue lock. A worker
            # arriving after this check sets the event, even before wait starts.
            self._wake.clear()
            if not self._queue.empty():
                return True
        return self._wake.wait(timeout)

    def close(self):
        self._check_owner()
        with self._lock:
            self._closed = True
            self._wake.set()
            while True:
                try:
                    future, _function = self._queue.get_nowait()
                except queue.Empty:
                    break
                future.cancel()
                self._queue.task_done()
