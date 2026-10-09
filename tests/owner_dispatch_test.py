"""Owner dispatch must preserve thread affinity and cancel stale mutations."""
from pathlib import Path
import sys
import threading
import unittest

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


if __name__ == '__main__':
    unittest.main()
