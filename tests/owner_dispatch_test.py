"""Owner dispatch must preserve thread affinity and cancel stale mutations."""
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from owner_dispatch import OwnerDispatcher


class OwnerDispatchContract(unittest.TestCase):
    def submit_from_worker(self, dispatcher, handler):
        replies = []
        worker = threading.Thread(target=lambda: replies.append(dispatcher(handler)))
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        return replies[0]

    def test_worker_call_runs_only_when_owner_pumps(self):
        owner = threading.get_ident()
        calls = []
        dispatcher = OwnerDispatcher()
        pending = self.submit_from_worker(dispatcher, lambda: calls.append(threading.get_ident()) or 42)
        self.assertFalse(pending.done())
        self.assertFalse(calls)
        self.assertEqual(dispatcher.pump(), 1)
        self.assertEqual(pending.result(), 42)
        self.assertEqual(calls, [owner])

    def test_cancelled_or_closed_queue_never_mutates_host(self):
        calls = []
        dispatcher = OwnerDispatcher()
        first = self.submit_from_worker(dispatcher, lambda: calls.append('cancelled'))
        first.cancel()
        second = self.submit_from_worker(dispatcher, lambda: calls.append('closed'))
        dispatcher.close()
        dispatcher.close()
        self.assertTrue(first.cancelled())
        self.assertTrue(second.cancelled())
        self.assertEqual(dispatcher.pump(), 0)
        self.assertFalse(calls)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            dispatcher(lambda: calls.append('late')).result()
        self.assertFalse(calls)

    def test_capacity_refuses_extra_work_without_replacing_pending_call(self):
        dispatcher = OwnerDispatcher(capacity=1)
        first = self.submit_from_worker(dispatcher, lambda: 'original')
        second = self.submit_from_worker(dispatcher, lambda: 'overflow')
        with self.assertRaisesRegex(RuntimeError, 'full'):
            second.result()
        dispatcher.pump()
        self.assertEqual(first.result(), 'original')

    def test_only_owner_may_pump_or_close(self):
        dispatcher = OwnerDispatcher()
        failures = []
        def foreign():
            for operation in (dispatcher.pump, dispatcher.close):
                try:
                    operation()
                except RuntimeError as error:
                    failures.append(str(error))
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(2)
        self.assertEqual(len(failures), 2)
        self.assertEqual(dispatcher(lambda: 'still open').result(), 'still open')

    def test_owner_reentrant_call_and_handler_failure_preserve_future_results(self):
        dispatcher = OwnerDispatcher()
        self.assertEqual(dispatcher(lambda: dispatcher(lambda: 42).result()).result(), 42)
        def fail():
            raise ValueError('native rejection')
        future = self.submit_from_worker(dispatcher, fail)
        dispatcher.pump()
        with self.assertRaisesRegex(ValueError, 'native rejection'):
            future.result()
        self.assertEqual(dispatcher(lambda: 43).result(), 43)

    def test_wait_wakes_on_enqueue_but_callbacks_run_only_when_owner_pumps(self):
        dispatcher = OwnerDispatcher()
        waiting = threading.Event()
        calls, pending = [], []
        real_wait = dispatcher._wake.wait
        def wait(timeout):
            waiting.set()
            return real_wait(timeout)
        def incoming():
            if waiting.wait(2):
                pending.append(dispatcher(lambda: calls.append(threading.get_ident())))
        worker = threading.Thread(target=incoming)
        worker.start()
        try:
            with patch.object(dispatcher._wake, 'wait', side_effect=wait):
                self.assertTrue(dispatcher.wait_for_work(2))
            self.assertFalse(calls)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(dispatcher.pump(), 1)
            self.assertEqual(calls, [threading.get_ident()])
            self.assertIsNone(pending[0].result())
        finally:
            dispatcher.close()
            worker.join(2)

    def test_enqueue_between_empty_check_and_wait_cannot_lose_wakeup(self):
        dispatcher = OwnerDispatcher()
        pending = []
        real_wait = dispatcher._wake.wait
        def enqueue_before_wait(timeout):
            pending.append(self.submit_from_worker(dispatcher, lambda: 42))
            return real_wait(timeout)
        with patch.object(dispatcher._wake, 'wait', side_effect=enqueue_before_wait):
            self.assertTrue(dispatcher.wait_for_work(0))
        self.assertFalse(pending[0].done())
        dispatcher.pump()
        self.assertEqual(pending[0].result(), 42)
        dispatcher.close()

    def test_pending_flood_never_waits_or_exceeds_default_pump_limit_and_close_cancels_rest(self):
        dispatcher = OwnerDispatcher(capacity=40)
        calls = []
        pending = [self.submit_from_worker(dispatcher, lambda: calls.append('owned')) for _ in range(40)]
        overflow = self.submit_from_worker(dispatcher, lambda: calls.append('overflow'))
        with self.assertRaisesRegex(RuntimeError, 'full'):
            overflow.result()
        with patch.object(dispatcher._wake, 'wait', side_effect=AssertionError('Pending work must not block')):
            self.assertTrue(dispatcher.wait_for_work())
            self.assertEqual(dispatcher.pump(), 32)
            self.assertTrue(dispatcher.wait_for_work())
        dispatcher.close()
        self.assertEqual(len(calls), 32)
        self.assertTrue(all(future.done() and not future.cancelled() for future in pending[:32]))
        self.assertTrue(all(future.cancelled() for future in pending[32:]))
        self.assertFalse(dispatcher.wait_for_work())

    def test_empty_wait_is_bounded_and_invalid_timeout_never_reaches_native_wait(self):
        dispatcher = OwnerDispatcher()
        with patch.object(dispatcher._wake, 'wait', return_value=False) as wait:
            self.assertFalse(dispatcher.wait_for_work())
            wait.assert_called_once_with(0.2)
            for timeout in (True, None, -1, float('nan'), float('inf'), 10 ** 1000, threading.TIMEOUT_MAX * 2):
                with self.subTest(timeout_type=type(timeout).__name__):
                    with self.assertRaises(ValueError):
                        dispatcher.wait_for_work(timeout)
            self.assertEqual(wait.call_count, 1)
        self.assertFalse(dispatcher.wait_for_work(0))
        dispatcher.close()

    def test_foreign_wait_is_refused_without_executing_or_consuming_owned_work(self):
        dispatcher = OwnerDispatcher()
        failures = []
        def foreign():
            try:
                dispatcher.wait_for_work(0)
            except RuntimeError as error:
                failures.append(str(error))
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertEqual(dispatcher(lambda: 'still owned').result(), 'still owned')
        dispatcher.close()


if __name__ == '__main__':
    unittest.main()
