"""Dispatch SDK callbacks through the demo's existing owner thread.

The caller pumps the queue in its existing main loop. This module creates no
thread, event loop or host service.
"""
from concurrent.futures import Future
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

    def close(self):
        self._check_owner()
        with self._lock:
            self._closed = True
            while True:
                try:
                    future, _function = self._queue.get_nowait()
                except queue.Empty:
                    break
                future.cancel()
                self._queue.task_done()
